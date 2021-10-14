import socket
import sys
import yaml

if __name__ == '__main__':
    msg = "/Users/rmcmahon/dev/awips-ml/server/dev_test/OR_ABI-L2-CMIPPR-M6C09_G16_s20212861650200_e20212861650200_c20212861650200.nc4"
    handler_type = sys.argv[1]

    # load in configs
    with open("/server/config_dev.yaml") as file:  # BONE change this
        config_dict = yaml.load(file, Loader=yaml.FullLoader)
    try:
        assert handler_type in ["process_container", "edex_container"]
    except AssertionError:
        raise SyntaxError("incorrect input argument; options are \"process_container\" or \"edex_container\"")
    host = config_dict[handler_type]["pygcdm_server"]
    port = config_dict[handler_type]["tx_port_trigger"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(bytes(msg, 'utf-8'))

    
