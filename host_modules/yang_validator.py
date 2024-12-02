"""Yang validation handler"""

from host_modules import host_service
import json
import sonic_yang

YANG_MODELS_DIR = "/usr/local/yang-models"
MOD_NAME = 'yang'

class Yang(host_service.HostModule):
    """
    DBus endpoint that runs yang validation
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def validate(self, config_db_json):
        config = json.loads(config_db_json)
        # Run yang validation
        yang_parser = sonic_yang.SonicYang(YANG_MODELS_DIR)
        yang_parser.loadYangModel()
        try:
            yang_parser.loadData(configdbJson=config)
            yang_parser.validate_data_tree()
        except sonic_yang.SonicYangException as e:
            return -1, str(e)
        if len(yang_parser.tablesWithOutYang):
            return -1, "Tables without yang models: " + str(yang_parser.tablesWithOutYang)
        return 0, ""
