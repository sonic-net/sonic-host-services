"""Mock class for HostModule to be used in unit tests."""

BUS_NAME_BASE = 'org.SONiC.HostService'


def bus_name(mod_name):
  """Return the bus name for the service."""
  return BUS_NAME_BASE + '.' + mod_name


# method = dbus.service.method
def method(dbus_interface, in_signature=None, out_signature=None):
  del dbus_interface, in_signature, out_signature  # Unused in unit tests
  def decorator(fun):
    def wrapper(*args, **kwargs):
      return fun(*args, **kwargs)
    return wrapper
  return decorator


class HostModule():

  def __init__(self, mod_name):
    pass


class Logger():

  def __init__(self, mod_name):
    pass

  def log_error(self, msg):
    pass

  def log_notice(self, msg):
    pass

  def log_info(self, msg):
    pass

logger = Logger("sonic_host_service")
