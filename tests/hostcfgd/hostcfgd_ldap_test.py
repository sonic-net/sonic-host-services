import importlib.machinery
import importlib.util
import filecmp
import shutil
import os
import sys
from swsscommon import swsscommon

from parameterized import parameterized
from unittest import TestCase, mock
from tests.hostcfgd.test_ldap_vectors import HOSTCFGD_TEST_LDAP_VECTOR
from tests.common.mock_configdb import MockConfigDb, MockDBConnector
from sonic_py_common.general import getstatusoutput_noshell


test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
templates_path = os.path.join(modules_path, "data/templates")
output_path = os.path.join(test_path, "hostcfgd/output")
sample_output_path = os.path.join(test_path, "hostcfgd/sample_output")
sys.path.insert(0, modules_path)

# Load the file under test
hostcfgd_path = os.path.join(scripts_path, 'hostcfgd')
loader = importlib.machinery.SourceFileLoader('hostcfgd', hostcfgd_path)
spec = importlib.util.spec_from_loader(loader.name, loader)
hostcfgd = importlib.util.module_from_spec(spec)
loader.exec_module(hostcfgd)
sys.modules['hostcfgd'] = hostcfgd

# Mock swsscommon classes
hostcfgd.ConfigDBConnector = MockConfigDb
hostcfgd.DBConnector = MockDBConnector
hostcfgd.Table = mock.Mock()

class TestHostcfgdLDAP(TestCase):
    """
        Test hostcfd daemon - LDAP
    """
    def run_diff(self, file1, file2):
        _, output = getstatusoutput_noshell(['diff', '-uR', file1, file2])
        return output


    @parameterized.expand(HOSTCFGD_TEST_LDAP_VECTOR)
    def test_hostcfgd_ldap(self, test_name, test_data):
        """
            Test LDAP hostcfd daemon initialization
            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results
            Returns:
                None
        """

        t_path = templates_path
        op_path = output_path + "/" + test_name
        sop_path = sample_output_path + "/" + test_name

        hostcfgd.PAM_AUTH_CONF_TEMPLATE = t_path + "/common-auth-sonic.j2"
        hostcfgd.NSS_TACPLUS_CONF_TEMPLATE = t_path + "/tacplus_nss.conf.j2"
        hostcfgd.NSS_RADIUS_CONF_TEMPLATE = t_path + "/radius_nss.conf.j2"
        hostcfgd.PAM_RADIUS_AUTH_CONF_TEMPLATE = t_path + "/pam_radius_auth.conf.j2"
        hostcfgd.PAM_AUTH_CONF = op_path + "/common-auth-sonic"
        hostcfgd.NSS_TACPLUS_CONF = op_path + "/tacplus_nss.conf"
        hostcfgd.NSS_RADIUS_CONF = op_path + "/radius_nss.conf"
        hostcfgd.NSS_CONF = op_path + "/nsswitch.conf"
        hostcfgd.NSLCD_CONF = op_path + "/nslcd.conf"
        hostcfgd.NSLCD_CONF_TEMPLATE = t_path + "/nslcd.conf.j2"
        hostcfgd.ETC_PAMD_SSHD = op_path + "/sshd"
        hostcfgd.ETC_PAMD_LOGIN = op_path + "/login"
        hostcfgd.RADIUS_PAM_AUTH_CONF_DIR = op_path + "/"

        shutil.rmtree( op_path, ignore_errors=True)
        os.mkdir( op_path)

        shutil.copyfile( sop_path + "/sshd.old", op_path + "/sshd")
        shutil.copyfile( sop_path + "/login.old", op_path + "/login")

        MockConfigDb.set_config_db(test_data["config_db"])
        host_config_daemon = hostcfgd.HostConfigDaemon()

        aaa = host_config_daemon.config_db.get_table('AAA')

        try:
            ldap_global = host_config_daemon.config_db.get_table('LDAP')
        except:
            ldap_global = []
        try:
            ldap_server = \
                host_config_daemon.config_db.get_table('LDAP_SERVER')
        except:
            ldap_server = []

        host_config_daemon.aaacfg.load(aaa,[],[],[] ,[] , ldap_global, ldap_server)

        diff_output = ""
        files_to_compare = ['common-auth-sonic', 'nslcd.conf']

        # check output files exists
        for name in files_to_compare:
            if not os.path.isfile(sop_path + "/" + name):
                raise ValueError('filename: %s not exit' % (sop_path + "/" + name))
            if not os.path.isfile(op_path + "/" + name):
                raise ValueError('filename: %s not exit' % (op_path + "/" + name))

        # deep comparison
        match, mismatch, errors = filecmp.cmpfiles(sop_path, op_path, files_to_compare, shallow=False)

        if not match:
            for name in files_to_compare:
                diff_output += self.run_diff( sop_path + "/" + name,\
                    op_path + "/" + name).decode('utf-8')

        self.assertTrue(len(diff_output) == 0, diff_output)
