"""Read-only, lazily connected SONiC hash access."""

from __future__ import annotations

from threading import RLock


class SonicHashReaderError(RuntimeError):
    """Raised when the SONiC database read boundary is unavailable."""


def decode_db_text(value) -> str:
    """Normalize a SONiC database key or field to replacement-safe text."""

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def decode_db_hash(row):
    """Normalize one database hash without leaking backend byte values."""

    return {
        decode_db_text(name): decode_db_text(value)
        for name, value in (row or {}).items()
    }


class SonicHashReader:
    """Reuse one injected or lazily created connector for read-only DB access."""

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

    def _connected_connector(self, database: str):
        if not isinstance(database, str) or not database:
            raise ValueError("database must be a non-empty string")
        connector = self._get_connector()
        if database not in self._connected_databases:
            connector.connect(database, False)
            self._connected_databases.add(database)
        return connector

    def read(self, database: str, key: str):
        """Return one normalized hash, connecting to its database lazily."""

        if not isinstance(key, str) or not key:
            raise ValueError("hash key must be a non-empty string")
        with self._lock:
            connector = self._connected_connector(database)
            return decode_db_hash(connector.get_all(database, key))

    def keys(self, database: str, pattern: str):
        """Return stable text keys matching ``pattern`` from one database."""

        if not isinstance(pattern, str) or not pattern:
            raise ValueError("key pattern must be a non-empty string")
        with self._lock:
            connector = self._connected_connector(database)
            return tuple(
                sorted(
                    decode_db_text(item)
                    for item in (connector.keys(database, pattern) or ())
                )
            )
