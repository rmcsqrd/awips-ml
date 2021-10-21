import grpc
from pygcdm.netcdf_encode import netCDF_Encode
from pygcdm.netcdf_decode import netCDF_Decode
from pygcdm.protogen import gcdm_server_pb2_grpc as grpc_server
from pygcdm.protogen import gcdm_netcdf_pb2 as grpc_msg
import socket
import queue
import pathlib
from concurrent import futures
import asyncio
import aiohttp
import sys
import yaml
import numpy as np
import xarray as xr
import netCDF4 as nc4
import subprocess

MAX_MESSAGE_LENGTH = 1000*1024*1024
EDEX_PYTHON_LOCATION = "/awips2/python/bin/python"
EDEX_QPID_NOTIFICATION = "/awips2/ldm/dev/notifyAWIPS2-unidata.py"

# define class stuff
class BaseServer():
    def __init__(self, fp_queue,
            process_type,
            rx_port=None,
            tx_port=None,
            host=None,
            rx_port_trigger=None,
            tx_port_trigger=None,
            pygcdm_client=None,
            pygcdm_server=None,
            variable_spec=None,
            ):

        # define queue and socket stuff
        self.fp_queue = fp_queue
        self.process_type = process_type
        self.rx_port = rx_port
        self.tx_port = tx_port
        self.host = host
        self.rx_port_trigger = rx_port_trigger
        self.tx_port_trigger = tx_port_trigger
        self.pygcdm_client = pygcdm_client
        self.pygcdm_server = pygcdm_server
        self.variable_spec = variable_spec 


        # setup pygcdm stuff
        # define responder server
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        grpc_server.add_GcdmServicer_to_server(Responder(), self.server)
        self.server.add_insecure_port(f'{self.pygcdm_client}:{self.tx_port}')
        print(f"responding to grpc requests on {self.pygcdm_client}:{self.tx_port}")
        self.pygcdm_responder()

        # define requester server
        self.request_handler = Requester(self.pygcdm_server, self.rx_port, self.variable_spec)
        print(f"making grpc requests on {self.pygcdm_server}:{self.rx_port}")

    async def trigger_listener(self):
        trigger_server = await asyncio.start_server(
                self.handle_trigger, self.host, self.rx_port_trigger)
        print(f"listening for file paths to request on {self.host}:{self.rx_port_trigger}...")
        async with trigger_server:
            await trigger_server.serve_forever()

    async def handle_trigger(self, reader, writer):
        data = await reader.read()
        message = data.decode()
        await self.fp_queue.put(message)
    
    def pygcdm_responder(self):
        self.server.start()
    
    async def pygcdm_requester(self):
        while True:
            file_loc = await self.fp_queue.get()
            print(f"trigger file to request: {file_loc}")
            print(f"current queue size = {self.fp_queue.qsize()}")
            nc_file = await self.request_handler.request_data(file_loc)
            print(f"netcdf file recieved")

            # BONE eventually break thins into distinct sub classes to get rid of "process_container" arg, inherit from base class
            # if process container then send to tf
            if self.process_type == 'process_container':

                # first send netcdf file data to tf container
                url = 'http://tfc:8501/v1/models/model:predict'  # bone expose this in API for namespace
                request = self.netcdf_to_request(nc_file, self.variable_spec)
                response = await self.make_request(url, request)

                
                # then update netcdf (in place) with values from tensorflow, and save to a path,
                # reuse path structure from edex container
                self.response_to_netcdf(nc_file, response, self.variable_spec)
                fp = pathlib.Path(file_loc)
                fp.mkdir(parents=True, exist_ok=True)
                fp_ml = fp.with_stem(fp.stem + '_ml')
                nc_file.to_netcdf(fp_ml)  # this saves to path

                # finally send back to edex
                reader, writer = await asyncio.open_connection(
                        self.pygcdm_server, self.tx_port_trigger)
                writer.write(str(fp_ml).encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            # else it is edex container so need to save stuff
            # we call because qpid notifier is written for python 2 not 3
            else:

                # start by copying old file to new on edex container
                fp_ml = pathlib.Path(file_loc)  # the recieved path will have _ml appended
                fp = fp_ml.with_stem(fp_ml.stem.replace('_ml', ''))
                og_nc_file = xr.open_dataset(fp, mask_and_scale=False)
                og_nc_file[self.variable_spec].data = nc_file[self.variable_spec].data
                nc_file = og_nc_file.rename_vars({self.variable_spec:self.variable_spec+'_ml'})
                nc_file.to_netcdf(fp_ml)
                nc = nc4.Dataset(fp_ml)
                print(nc.variables['fixedgrid_projection'].ncattrs())
                sys.stdout.flush()
                sys.stderr.flush()

                # view output via sudo journalctl -fu listener_start.service
                # BONE, add error handling
                proc_qpid = subprocess.run([EDEX_PYTHON_LOCATION,
                    EDEX_QPID_NOTIFICATION,
                    str(fp_ml)])
#                proc_qpid = await asyncio.create_subprocess_shell(
#                        f'{EDEX_PYTHON_LOCATION} \
#                        {EDEX_QPID_NOTIFICATION} \
#                        {str(fp)}')
#                stdout, stderr = await proc_qpid.communicate()

    async def make_request(self, url, data):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as response:
                response_json = await response.json()
                return np.array(response_json['predictions'])

    def netcdf_to_request(self, nc, variable_spec):
        # takes netcdf file data for variable spec, converts to numpy array, and sends via https to tensorflow
        data = nc.variables[variable_spec][:].data
        data = data.reshape((1, *data.shape))
        return f'{{"instances" : {data.tolist()}}}'

    def response_to_netcdf(self, nc, response, variable_spec):
        # takes returned numpy array and overwrites origin netcdf file
        nc.variables[variable_spec][:] = response.squeeze()


class Responder(grpc_server.GcdmServicer):

    def __init__(self):
        self.encoder = netCDF_Encode()

    def GetNetcdfHeader(self, request, context):
        print('Header Requested')
        return self.encoder.generate_header_from_request(request)

    def GetNetcdfData(self, request, context):
        print('Data Requested')

        # stream the data response
        data_response = [self.encoder.generate_data_from_request(request)]
        for data in data_response:
            yield(data)

class Requester():
    def __init__(self, host, port, var_spec):
        self.host = host
        self.port = port
        self.variable_spec = var_spec

    async def request_data(self, loc):
        options = [('grpc.max_receive_message_length', MAX_MESSAGE_LENGTH),
                   ('grpc.max_send_message_length', MAX_MESSAGE_LENGTH)]
        async with grpc.aio.insecure_channel(f'{self.host}:{self.port}', options=options) as channel:
            stub = grpc_server.GcdmStub(channel)
            print(f"requesting data from {self.host}:{self.port}")
            request_msg = grpc_msg.HeaderRequest(location=loc)
            data_msg = grpc_msg.DataRequest(location=loc, variable_spec=self.variable_spec)

            # async unpack the streaming response - data_response is only one item
            header_response = await stub.GetNetcdfHeader(request_msg)
            data_response = [data async for data in stub.GetNetcdfData(data_msg)][0]

            return self.decode_response(header_response, data_response)

    def decode_response(self, header, data):
        decoder = netCDF_Decode()
        return decoder.generate_file_from_response(header, data)

# define functions that run server
async def run_server(configs, process_type):
    server = BaseServer(asyncio.Queue(), process_type, **configs)
    g = await asyncio.gather(server.trigger_listener(), server.pygcdm_requester())

if __name__=="__main__":
    process_type = sys.argv[1]
    with open("server/config.yaml") as file:  # BONE change this
        config_dict = yaml.load(file, Loader=yaml.FullLoader)
    try:
        assert process_type in ["process_container", "edex_container"]
    except AssertionError:
        raise SyntaxError("incorrect input argument; options are \"process_container\" or \"edex_container\"")
    asyncio.run(run_server(config_dict[process_type], process_type))


