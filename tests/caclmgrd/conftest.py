import pytest
from tests.common.mock_configdb import MockConfigDb


@pytest.fixture(autouse=True)
def reset_config_db():
    """Ensure MockConfigDb.CONFIG_DB has minimal required data for caclmgrd."""
    if not MockConfigDb.CONFIG_DB.get('DEVICE_METADATA'):
        MockConfigDb.CONFIG_DB['DEVICE_METADATA'] = {
            'localhost': {}
        }
    if not MockConfigDb.CONFIG_DB.get('FEATURE'):
        MockConfigDb.CONFIG_DB['FEATURE'] = {}
    yield
    MockConfigDb.CONFIG_DB = {}
