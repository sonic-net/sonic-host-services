from swsscommon import swsscommon
import logging

logger = logging.getLogger(__name__)

class SonicDbUtils:
  """Utility class for fetching information from SONiC Redis DBs using swsscommon."""

  @staticmethod
  def get_portchannels() -> list[str]:
    """Fetch PortChannel names from APPL_DB.
    Returns:
        List[str]: List of Portchannel names
    """
    db = swsscommon.SonicV2Connector()
    try:
      db.connect(db.APPL_DB)
      keys = db.keys(db.APPL_DB, "PORTCHANNEL|*")
      if not keys:
        return []
      portchannels = []
      for key in keys or []:
        try:
          _, name = key.split("|", 1)
          portchannels.append(name)
        except ValueError:
          logger.warning(f"Malformed PORTCHANNEL key: {key}")
      return portchannels
    except Exception as e:
      logger.warning(f"Failed to retrieve the port channels from APPL_DB: {e}")
      return []
    finally:
      try:
        db.close()
      except Exception:
        pass
