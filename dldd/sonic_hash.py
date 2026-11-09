"""Read-only, lazily connected SONiC hash access."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis


# Bound inactive socket waits; a complete operation can involve several reads.
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0


class SonicHashReaderError(RuntimeError):
    """Raised when the SONiC database read boundary is unavailable."""


def create_redis_client(database: str) -> Redis:
    """Create a private client with finite transport waits for one SONiC DB."""

    try:
        from swsscommon import swsscommon
    except ImportError as error:
        raise SonicHashReaderError(
            "swsscommon is unavailable: {}".format(error)
        )
    try:
        import redis
    except ImportError as error:
        raise SonicHashReaderError("redis is unavailable: {}".format(error))

    database_key = swsscommon.SonicDBKey()
    connection = {
        "db": swsscommon.SonicDBConfig.getDbId(database, database_key),
        "socket_connect_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
        "socket_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
    }
    socket_path = swsscommon.SonicDBConfig.getDbSock(database, database_key)
    if socket_path:
        connection["unix_socket_path"] = socket_path
    else:
        connection.update(
            host=swsscommon.SonicDBConfig.getDbHostname(database, database_key),
            port=swsscommon.SonicDBConfig.getDbPort(database, database_key),
        )
    try:
        from redis.retry import Retry
        from redis.backoff import NoBackoff
    except ImportError:
        # redis-py 3.5 has no Retry API and does not retry timeouts by default.
        pass
    else:
        # Newer clients otherwise retry transport failures with backoff before
        # the daemon can count the failed attempt or refresh its watchdog.
        connection["retry"] = Retry(NoBackoff(), 0)
    return redis.Redis(**connection)


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
    """Read through private Redis clients or an injected SONiC connector."""

    def __init__(self, connector=None, connector_factory=None) -> None:
        if connector is not None and connector_factory is not None:
            raise ValueError("provide either connector or connector_factory")
        self._connector = connector
        self._connector_factory = connector_factory
        self._redis_clients: dict[str, Redis] = {}
        self._connected_databases: set[str] = set()
        self._lock = RLock()

    def _connected_connector(self, database: str):
        if not isinstance(database, str) or not database:
            raise ValueError("database must be a non-empty string")
        if self._connector is None and self._connector_factory is None:
            if database not in self._redis_clients:
                self._redis_clients[database] = create_redis_client(database)
            return self._redis_clients[database]
        if self._connector is None:
            assert self._connector_factory is not None
            self._connector = self._connector_factory()
        connector = self._connector
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
            if self._connector is None:
                from redis.exceptions import ConnectionError, TimeoutError

                # Preserve SonicV2Connector's legacy null-string normalization.
                try:
                    row = decode_db_hash(connector.hgetall(key))
                except (ConnectionError, TimeoutError) as error:
                    raise SonicHashReaderError(str(error)) from error
                return {
                    name: "" if value == "None" else value
                    for name, value in row.items()
                }
            return decode_db_hash(self._connector.get_all(database, key))

    def keys(self, database: str, pattern: str):
        """Return stable text keys matching ``pattern`` from one database."""

        if not isinstance(pattern, str) or not pattern:
            raise ValueError("key pattern must be a non-empty string")
        with self._lock:
            connector = self._connected_connector(database)
            if self._connector is None:
                from redis.exceptions import ConnectionError, TimeoutError

                try:
                    return tuple(sorted({
                        decode_db_text(item)
                        for item in connector.scan_iter(match=pattern)
                    }))
                except (ConnectionError, TimeoutError) as error:
                    raise SonicHashReaderError(str(error)) from error
            return tuple(
                sorted(
                    decode_db_text(item)
                    for item in (self._connector.keys(database, pattern) or ())
                )
            )
