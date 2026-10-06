import io
import pytest
from sonic_py_common import device_info
from unittest import mock


@pytest.fixture(autouse=True)
def mock_proc_cmdline(monkeypatch):
    """Keep the runner's crashkernel setting out of hostcfgd unit tests."""
    original_open = open

    def open_with_test_cmdline(file, *args, **kwargs):
        if file == '/proc/cmdline':
            return io.StringIO('console=ttyS0\n')
        return original_open(file, *args, **kwargs)

    # The KDUMP cmdline test still reads its explicit tests/proc/cmdline file.
    monkeypatch.setattr('builtins.open', open_with_test_cmdline)


@pytest.fixture(autouse=True, scope='session')
def mock_get_device_runtime_metadata():
    device_info.get_device_runtime_metadata = mock.MagicMock(return_value={})
