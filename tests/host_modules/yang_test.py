import sys
import os
import pytest
from unittest import mock
from host_modules import yang_validator

class TestConfigEngine(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_reload(self, MockInit, MockBusName, MockSystemBus):
        config_db_json = "{}"
        yang_stub = yang_validator.Yang(yang_validator.MOD_NAME)
        ret, _ = yang_stub.validate(config_db_json)
        assert ret == 0, "Yang validation failed"
