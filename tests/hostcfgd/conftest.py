import pytest
from sonic_py_common import device_info
from unittest import mock

@pytest.fixture(autouse=True, scope='session')
def mock_get_device_runtime_metadata():
    device_info.get_device_runtime_metadata = mock.MagicMock(return_value={})

@pytest.fixture(autouse=True, scope='session')
def mock_open_proc_cmdline():
    """Automatically mock opening /proc/cmdline for all tests."""
    EMPTY_LINE = "\n"
    original_open = open

    def mock_open_side_effect(filename, *args, **kwargs):
        if filename == "/proc/cmdline":
            return mock.mock_open(read_data=EMPTY_LINE)()
        else:
            return original_open(filename, *args, **kwargs)

    with mock.patch("builtins.open", side_effect=mock_open_side_effect):
        yield
