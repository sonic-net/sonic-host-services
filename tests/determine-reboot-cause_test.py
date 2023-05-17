import sys
import os
import shutil
import pytest

from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source

# TODO: Remove this if/else block once we no longer support Python 2
if sys.version_info.major == 3:
    from unittest import mock
else:
    # Expect the 'mock' package for python 2
    # https://pypi.python.org/pypi/mock
    import mock

# TODO: Remove this if/else block once we no longer support Python 2
if sys.version_info.major == 3:
    BUILTINS = "builtins"
else:
    BUILTINS = "__builtin__"

from .mock_connector import MockConnector

swsscommon.SonicV2Connector = MockConnector

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the file under test
determine_reboot_cause_path = os.path.join(scripts_path, 'determine-reboot-cause')
determine_reboot_cause = load_module_from_source('determine_reboot_cause', determine_reboot_cause_path)


PROC_CMDLINE_CONTENTS = """\
BOOT_IMAGE=/image-20191130.52/boot/vmlinuz-4.9.0-11-2-amd64 root=/dev/sda4 rw console=tty0 console=ttyS1,9600n8 quiet net.ifnames=0 biosdevname=0 loop=image-20191130.52/fs.squashfs loopfstype=squashfs apparmor=1 security=apparmor varlog_size=4096 usbcore.autosuspend=-1 module_blacklist=gpio_ich SONIC_BOOT_TYPE=warm"""

EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE = "warm"

PROC_CMDLINE_CONTENTS = """\
BOOT_IMAGE=/image-20191130.52/boot/vmlinuz-4.9.0-11-2-amd64 root=/dev/sda4 rw console=tty0 console=ttyS1,9600n8 quiet net.ifnames=0 biosdevname=0 loop=image-20191130.52/fs.squashfs loopfstype=squashfs apparmor=1 security=apparmor varlog_size=4096 usbcore.autosuspend=-1 module_blacklist=gpio_ich SONIC_BOOT_TYPE=warm"""

REBOOT_CAUSE_CONTENTS = """\
User issued 'warm-reboot' command [User: admin, Time: Mon Nov  2 22:37:45 UTC 2020]"""

GET_SONIC_VERSION_INFO = {'commit_id': 'e59ec8291', 'build_date': 'Mon Nov  2 06:00:14 UTC 2020', 'build_number': 75, 'kernel_version': '4.9.0-11-2-amd64', 'debian_version': '9.13', 'built_by': 'sonicbld@jenkins-slave-phx-2', 'asic_type': 'mellanox', 'build_version': '20191130.52'}

REBOOT_CAUSE_WATCHDOG = "Watchdog"
GEN_TIME_WATCHDOG = "2020_10_22_03_15_08"
REBOOT_CAUSE_USER = "User issued 'reboot' command [User: admin, Time: Thu Oct 22 03:11:08 UTC 2020]"
GEN_TIME_USER = "2020_10_22_03_14_07"
REBOOT_CAUSE_KERNEL_PANIC = "Kernel Panic [Time: Sun Mar 28 13:45:12 UTC 2021]"
GEN_TIME_KERNEL_PANIC = "2021_3_28_13_48_49"


REBOOT_CAUSE_UNKNOWN = "Unknown"
REBOOT_CAUSE_NON_HARDWARE = "Non-Hardware"
EXPECTED_NON_HARDWARE_REBOOT_CAUSE = {REBOOT_CAUSE_NON_HARDWARE, "N/A"}
REBOOT_CAUSE_HARDWARE_OTHER = "Hardware - Other"
EXPECTED_HARDWARE_REBOOT_CAUSE = {REBOOT_CAUSE_HARDWARE_OTHER, ""}

EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE = "warm-reboot"
EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER = "User issued 'warm-reboot' command [User: admin, Time: Mon Nov  2 22:37:45 UTC 2020]"
EXPECTED_FIND_FIRSTBOOT_VERSION = " (First boot of SONiC version 20191130.52)"
EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_FIRSTBOOT = "Unknown (First boot of SONiC version 20191130.52)"

EXPECTED_WATCHDOG_REBOOT_CAUSE_DICT = {'comment': '', 'gen_time': '2020_10_22_03_15_08', 'cause': 'Watchdog', 'user': 'N/A', 'time': 'N/A'}
EXPECTED_USER_REBOOT_CAUSE_DICT = {'comment': '', 'gen_time': '2020_10_22_03_14_07', 'cause': 'reboot', 'user': 'admin', 'time': 'Thu Oct 22 03:11:08 UTC 2020'}
EXPECTED_KERNEL_PANIC_REBOOT_CAUSE_DICT = {'comment': '', 'gen_time': '2021_3_28_13_48_49', 'cause': 'Kernel Panic', 'user': 'N/A', 'time': 'Sun Mar 28 13:45:12 UTC 2021'}

REBOOT_CAUSE_DIR="host/reboot-cause/"

class TestDetermineRebootCause(object):
    def test_parse_warmfast_reboot_from_proc_cmdline(self):
        with mock.patch("os.path.isfile") as mock_isfile:
            mock_isfile.return_value = True
            open_mocked = mock.mock_open(read_data=PROC_CMDLINE_CONTENTS)
            with mock.patch("{}.open".format(BUILTINS), open_mocked):
                result = determine_reboot_cause.parse_warmfast_reboot_from_proc_cmdline()
                assert result == EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE
                open_mocked.assert_called_once_with("/proc/cmdline")

    def test_find_software_reboot_cause_user(self):
        with mock.patch("os.path.isfile") as mock_isfile:
            mock_isfile.return_value = True
            open_mocked = mock.mock_open(read_data=REBOOT_CAUSE_CONTENTS)
            with mock.patch("{}.open".format(BUILTINS), open_mocked):
                 result = determine_reboot_cause.find_software_reboot_cause_from_reboot_cause_file()
                 assert result == EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER
                 open_mocked.assert_called_once_with("/host/reboot-cause/reboot-cause.txt")

    def test_find_software_reboot_cause_first_boot(self):
        with mock.patch("sonic_py_common.device_info.get_sonic_version_info", return_value=GET_SONIC_VERSION_INFO):
            result = determine_reboot_cause.find_first_boot_version()
            assert result == EXPECTED_FIND_FIRSTBOOT_VERSION

    def test_find_software_reboot_cause(self):
        with mock.patch("determine_reboot_cause.find_software_reboot_cause_from_reboot_cause_file", return_value="Unknown"):
            with mock.patch("os.path.isfile") as mock_isfile:
                mock_isfile.return_value = False
                result = determine_reboot_cause.find_software_reboot_cause()
                assert result == "Unknown"

    def test_find_proc_cmdline_reboot_cause(self):
        with mock.patch("determine_reboot_cause.parse_warmfast_reboot_from_proc_cmdline", return_value="fast-reboot"):
            result = determine_reboot_cause.find_proc_cmdline_reboot_cause()
            assert result == "fast-reboot"

    def test_find_hardware_reboot_cause(self):
        with mock.patch("determine_reboot_cause.get_reboot_cause_from_platform", return_value=("Powerloss", None)):
            result = determine_reboot_cause.find_hardware_reboot_cause()
            assert result == "Powerloss"

    def test_find_hardware_reboot_cause_with_minor(self):
        with mock.patch("determine_reboot_cause.get_reboot_cause_from_platform", return_value=("Powerloss", "under-voltage")):
            result = determine_reboot_cause.find_hardware_reboot_cause()
            assert result == "Powerloss (under-voltage)"

    def test_get_reboot_cause_dict_watchdog(self):
        reboot_cause_dict = determine_reboot_cause.get_reboot_cause_dict(REBOOT_CAUSE_WATCHDOG, "", GEN_TIME_WATCHDOG)
        assert reboot_cause_dict == EXPECTED_WATCHDOG_REBOOT_CAUSE_DICT

    def test_get_reboot_cause_dict_user(self):
        reboot_cause_dict = determine_reboot_cause.get_reboot_cause_dict(REBOOT_CAUSE_USER, "", GEN_TIME_USER)
        assert reboot_cause_dict == EXPECTED_USER_REBOOT_CAUSE_DICT

    def test_get_reboot_cause_dict_kernel_panic(self):
        reboot_cause_dict = determine_reboot_cause.get_reboot_cause_dict(REBOOT_CAUSE_KERNEL_PANIC, "", GEN_TIME_KERNEL_PANIC)
        assert reboot_cause_dict == EXPECTED_KERNEL_PANIC_REBOOT_CAUSE_DICT

    def test_determine_reboot_cause_hardware(self):
        with mock.patch("determine_reboot_cause.find_proc_cmdline_reboot_cause", return_value=None):
            with mock.patch("determine_reboot_cause.find_software_reboot_cause", return_value=REBOOT_CAUSE_UNKNOWN):
                with mock.patch("determine_reboot_cause.find_hardware_reboot_cause", return_value=EXPECTED_HARDWARE_REBOOT_CAUSE):
                    previous_reboot_cause, additional_reboot_info = determine_reboot_cause.determine_reboot_cause()
                    assert previous_reboot_cause == EXPECTED_HARDWARE_REBOOT_CAUSE
                    assert additional_reboot_info == "N/A"

    def test_determine_reboot_cause_software(self):
        with mock.patch("determine_reboot_cause.find_proc_cmdline_reboot_cause", return_value=None):
            with mock.patch("determine_reboot_cause.find_software_reboot_cause", return_value=EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER):
                with mock.patch("determine_reboot_cause.find_hardware_reboot_cause", return_value=EXPECTED_NON_HARDWARE_REBOOT_CAUSE):
                    previous_reboot_cause, additional_info = determine_reboot_cause.determine_reboot_cause()
                    assert previous_reboot_cause == EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER
                    assert additional_info == "N/A"

    def test_determine_reboot_cause_cmdline_software(self):
        with mock.patch("determine_reboot_cause.find_proc_cmdline_reboot_cause", return_value=EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE):
            with mock.patch("determine_reboot_cause.find_software_reboot_cause", return_value=EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER):
                with mock.patch("determine_reboot_cause.find_hardware_reboot_cause", return_value=EXPECTED_NON_HARDWARE_REBOOT_CAUSE):
                    previous_reboot_cause, additional_info = determine_reboot_cause.determine_reboot_cause()
                    assert previous_reboot_cause == EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER
                    assert additional_info == "N/A"

    def test_determine_reboot_cause_cmdline_no_software(self):
        with mock.patch("determine_reboot_cause.find_proc_cmdline_reboot_cause", return_value=EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE):
            with mock.patch("determine_reboot_cause.find_software_reboot_cause", return_value=REBOOT_CAUSE_UNKNOWN):
                with mock.patch("determine_reboot_cause.find_hardware_reboot_cause", return_value=EXPECTED_NON_HARDWARE_REBOOT_CAUSE):
                    previous_reboot_cause, additional_info = determine_reboot_cause.determine_reboot_cause()
                    assert previous_reboot_cause == EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE
                    assert additional_info == "N/A"

    def test_determine_reboot_cause_cmdline_hardware(self):
        with mock.patch("determine_reboot_cause.find_proc_cmdline_reboot_cause", return_value=EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE):
            with mock.patch("determine_reboot_cause.find_software_reboot_cause", return_value=REBOOT_CAUSE_UNKNOWN):
                with mock.patch("determine_reboot_cause.find_hardware_reboot_cause", return_value=EXPECTED_HARDWARE_REBOOT_CAUSE):
                    previous_reboot_cause, additional_info = determine_reboot_cause.determine_reboot_cause()
                    assert previous_reboot_cause == EXPECTED_HARDWARE_REBOOT_CAUSE
                    assert additional_info == EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE

    def test_determine_reboot_cause_software_hardware(self):
        with mock.patch("determine_reboot_cause.find_proc_cmdline_reboot_cause", return_value=EXPECTED_PARSE_WARMFAST_REBOOT_FROM_PROC_CMDLINE):
            with mock.patch("determine_reboot_cause.find_software_reboot_cause", return_value=EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER):
                with mock.patch("determine_reboot_cause.find_hardware_reboot_cause", return_value=EXPECTED_HARDWARE_REBOOT_CAUSE):
                    previous_reboot_cause, additional_info = determine_reboot_cause.determine_reboot_cause()
                    assert previous_reboot_cause == EXPECTED_HARDWARE_REBOOT_CAUSE
                    assert additional_info == EXPECTED_FIND_SOFTWARE_REBOOT_CAUSE_USER

    @mock.patch('determine_reboot_cause.REBOOT_CAUSE_DIR', os.path.join(os.getcwd(), REBOOT_CAUSE_DIR))
    @mock.patch('determine_reboot_cause.REBOOT_CAUSE_HISTORY_DIR', os.path.join(os.getcwd(), 'host/reboot-cause/history/'))
    @mock.patch('determine_reboot_cause.PREVIOUS_REBOOT_CAUSE_FILE', os.path.join(os.getcwd(), 'host/reboot-cause/previous-reboot-cause.json'))
    @mock.patch('determine_reboot_cause.REBOOT_CAUSE_FILE', os.path.join(os.getcwd(),'host/reboot-cause/reboot-cause.txt'))
    def test_determine_reboot_cause_main_without_reboot_cause_dir(self):
        if os.path.exists(REBOOT_CAUSE_DIR):
            shutil.rmtree(REBOOT_CAUSE_DIR)
        with mock.patch("os.geteuid", return_value=0):
            determine_reboot_cause.main()
            assert os.path.exists("host/reboot-cause/reboot-cause.txt") == True
            assert os.path.exists("host/reboot-cause/previous-reboot-cause.json") == True

    @mock.patch('determine_reboot_cause.REBOOT_CAUSE_DIR', os.path.join(os.getcwd(), REBOOT_CAUSE_DIR))
    @mock.patch('determine_reboot_cause.REBOOT_CAUSE_HISTORY_DIR', os.path.join(os.getcwd(), 'host/reboot-cause/history/'))
    @mock.patch('determine_reboot_cause.PREVIOUS_REBOOT_CAUSE_FILE', os.path.join(os.getcwd(), 'host/reboot-cause/previous-reboot-cause.json'))
    @mock.patch('determine_reboot_cause.REBOOT_CAUSE_FILE', os.path.join(os.getcwd(),'host/reboot-cause/reboot-cause.txt'))
    def test_determine_reboot_cause_main_with_reboot_cause_dir(self):
        with mock.patch("os.geteuid", return_value=0):
            determine_reboot_cause.main()
            assert os.path.exists("host/reboot-cause/reboot-cause.txt") == True
            assert os.path.exists("host/reboot-cause/previous-reboot-cause.json") == True   
