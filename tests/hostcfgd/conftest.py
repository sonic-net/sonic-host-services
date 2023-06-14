import pytest
from sonic_py_common import device_info
from unittest import mock

@pytest.fixture(autouse=True, scope='session')
def mock_get_device_runtime_metadata():
    device_info.get_device_runtime_metadata = mock.MagicMock(return_value={})
