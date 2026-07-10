"""Read-only, lazily connected SONiC hash access."""

from __future__ import annotations

from threading import RLock


class SonicHashReaderError(RuntimeError):
    pass


class SonicHashReader:
    """Reuse one injected or lazily created SonicV2Connector for hash reads."""

    def __init__(self, connector=None, connector_factory=None) -> None:
        if connector is not None and connector_factory is not None:
            raise ValueError("provide either connector or connector_factory")
        self._connector = connector
        self._connector_factory = connector_factory
        self._connected_databases = set()
        self._lock = RLock()

    def _get_connector(self):
        if self._connector is not None:
            return self._connector
        if self._connector_factory is not None:
            self._connector = self._connector_factory()
            return self._connector
        try:
            from swsscommon import swsscommon
        except ImportError as error:
            raise SonicHashReaderError(
                "swsscommon is unavailable: {}".format(error)
            )
        self._connector = swsscommon.SonicV2Connector(host="127.0.0.1")
        return self._connector

    def read(self, database: str, key: str):
        if not isinstance(database, str) or not database:
            raise ValueError("database must be a non-empty string")
        if not isinstance(key, str) or not key:
            raise ValueError("hash key must be a non-empty string")
        with self._lock:
            connector = self._get_connector()
            if database not in self._connected_databases:
                connector.connect(database, False)
                self._connected_databases.add(database)
            return connector.get_all(database, key) or {}
