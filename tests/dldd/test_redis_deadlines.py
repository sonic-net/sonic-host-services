"""Finite private Redis transport waits, including a peer that never replies."""

from __future__ import annotations

import socket
import socketserver
import sys
import tempfile
import threading
import time
from functools import partial
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest
import redis
from redis.exceptions import ResponseError, TimeoutError

from dldd.config import DLDDConfig
from dldd import sonic_hash
from dldd.telemetry import SonicStateDB, TelemetryPublisher


def _database_config(monkeypatch, socket_path):
    package = ModuleType("swsscommon")
    setattr(package, "swsscommon", SimpleNamespace(
        SonicDBKey=lambda: "default",
        SonicDBConfig=SimpleNamespace(
            getDbId=lambda database, key: 6,
            getDbSock=lambda database, key: socket_path,
            getDbHostname=lambda database, key: "127.0.0.1",
            getDbPort=lambda database, key: 6379,
        ),
    ))
    monkeypatch.setitem(sys.modules, "swsscommon", package)


@pytest.mark.parametrize("socket_path", ["/run/redis/redis.sock", ""])
def test_default_state_and_hash_clients_set_finite_private_transport_options(
    monkeypatch, socket_path
):
    _database_config(monkeypatch, socket_path)
    creations = []
    client = SimpleNamespace(hgetall=lambda key: {b"value": b"42"})

    def create(**options):
        creations.append(options)
        return client

    monkeypatch.setattr(redis, "Redis", create)
    assert SonicStateDB()._db() is client
    reader = sonic_hash.SonicHashReader()
    assert reader.read("STATE_DB", "TABLE|A") == {"value": "42"}
    assert reader.read("STATE_DB", "TABLE|A") == {"value": "42"}
    assert len(creations) == 2
    for options in creations:
        assert options["db"] == 6
        assert options["socket_connect_timeout"] == 5
        assert options["socket_timeout"] == 5
        if "retry" in options:
            assert options["retry"].get_retries() == 0
        if socket_path:
            assert options["unix_socket_path"] == socket_path
            assert "host" not in options
        else:
            assert options["host"] == "127.0.0.1"
            assert options["port"] == 6379


def test_redis35_client_without_retry_api_keeps_supported_finite_options(monkeypatch):
    _database_config(monkeypatch, "/run/redis/redis.sock")
    monkeypatch.setitem(sys.modules, "redis.retry", None)

    # redis-py 3.5 rejects the newer retry kwarg; its default timeout is None.
    def create(db, unix_socket_path, socket_timeout=None, socket_connect_timeout=None):
        assert db == 6
        assert unix_socket_path == "/run/redis/redis.sock"
        assert socket_timeout == socket_connect_timeout == 5
        return "finite-client"

    monkeypatch.setattr(redis, "Redis", create)
    assert sonic_hash.create_redis_client("STATE_DB") == "finite-client"


def test_hash_reader_does_not_classify_bad_redis_data_as_transport_unavailability(
    monkeypatch,
):
    error = ResponseError("WRONGTYPE invalid hash")

    def read(key):
        raise error

    monkeypatch.setattr(
        sonic_hash, "create_redis_client",
        lambda database: SimpleNamespace(hgetall=read),
    )
    with pytest.raises(ResponseError) as caught:
        sonic_hash.SonicHashReader().read("STATE_DB", "TABLE|A")
    assert caught.value is error


def _response(value: object) -> bytes:
    if isinstance(value, int):
        return b":" + str(value).encode() + b"\r\n"
    if isinstance(value, list):
        return (b"*" + str(len(value)).encode() + b"\r\n"
                + b"".join(_response(item) for item in value))
    if isinstance(value, dict):
        return (b"%" + str(len(value)).encode() + b"\r\n"
                + b"".join(_response(key) + _response(item)
                           for key, item in value.items()))
    assert isinstance(value, bytes)
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


class _RedisPeer(socketserver.ThreadingUnixStreamServer):
    """A private RESP peer that accepts requests and can withhold all replies."""

    def __init__(self, path):
        self.reply = threading.Event()
        self.received = threading.Event()
        self.lock = threading.Lock()
        self.clients: set[socket.socket] = set()
        self.connection_count = 0
        self.commands: list[list[bytes]] = []
        super().__init__(path, _RedisRequest)

    def close_clients(self):
        with self.lock:
            for client in self.clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _RedisRequest(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        assert isinstance(server, _RedisPeer)
        with server.lock:
            server.clients.add(self.request)
            server.connection_count += 1
        queued: list[object] | None = None
        protocol = 2
        try:
            while True:
                header = self.rfile.readline()
                if not header:
                    return
                command = []
                for _ in range(int(header[1:])):
                    length = int(self.rfile.readline()[1:])
                    command.append(self.rfile.read(length))
                    self.rfile.read(2)
                with server.lock:
                    server.commands.append(command)
                server.received.set()
                if not server.reply.is_set():
                    continue
                name = command[0].upper()
                if name == b"HELLO":
                    protocol = 3
                    self.wfile.write(b"%1\r\n$5\r\nproto\r\n:3\r\n")
                    continue
                if name == b"MULTI":
                    queued = []
                    self.wfile.write(b"+OK\r\n")
                    continue
                if name == b"EXEC":
                    self.wfile.write(_response(queued))
                    queued = None
                    continue
                result: object
                if name == b"HGETALL":
                    result = ({b"value": b"42", b"null": b"None"}
                              if protocol == 3
                              else [b"value", b"42", b"null", b"None"])
                elif name == b"HKEYS":
                    result = []
                elif name == b"SCAN":
                    result = [b"0", [b"TABLE|B", b"TABLE|A", b"TABLE|A"]]
                elif name in (b"HSET", b"HDEL", b"EXPIRE"):
                    result = 1
                else:
                    self.wfile.write(b"+OK\r\n")
                    continue
                if queued is None:
                    self.wfile.write(_response(result))
                else:
                    queued.append(result)
                    self.wfile.write(b"+QUEUED\r\n")
        except (ConnectionError, OSError):
            pass
        finally:
            with server.lock:
                server.clients.discard(self.request)


@pytest.fixture
def redis_peer():
    # Keep the private socket path below the Unix-domain path length limit.
    with tempfile.TemporaryDirectory(prefix="dldd-redis-", dir="/tmp") as directory:
        server = _RedisPeer(directory + "/redis.sock")
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.close_clients()
            server.server_close()
            thread.join(timeout=1)
            assert not thread.is_alive()


@pytest.mark.parametrize("operation", ["heartbeat", "read", "keys"])
def test_silent_redis_peer_times_out_and_same_boundary_recovers(
    monkeypatch, redis_peer, operation
):
    _database_config(monkeypatch, redis_peer.server_address)
    monkeypatch.setattr(
        sonic_hash, "REDIS_SOCKET_TIMEOUT_SECONDS", 0.04, raising=False
    )
    database = SonicStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    invoke: Callable[[], object]
    recovered: object
    if operation == "heartbeat":
        invoke = partial(publisher.publish_status, "OK", "0.0.1", "", "")
        recovered = True
    elif operation == "read":
        invoke = partial(database.hgetall, "TABLE|A")
        recovered = {"value": "42", "null": ""}
    else:
        invoke = partial(database.keys, "TABLE|*")
        recovered = ("TABLE|A", "TABLE|B")
    result: list[object] = []

    def call():
        try:
            result.append(invoke())
        except sonic_hash.SonicHashReaderError as error:
            result.append(error)

    started = time.monotonic()
    worker = threading.Thread(target=call)
    worker.start()
    try:
        worker.join(timeout=0.6)
        assert not worker.is_alive(), "Redis peer held the caller past its deadline"
        assert redis_peer.received.is_set(), "peer must accept a real Redis request"
        assert 0.015 <= time.monotonic() - started < 0.6
        assert redis_peer.connection_count == 1, "transport must not retry implicitly"
        if operation == "heartbeat":
            assert result == [False]
        else:
            assert isinstance(result[0], sonic_hash.SonicHashReaderError)
            assert isinstance(result[0].__cause__, TimeoutError)

        redis_peer.reply.set()
        assert invoke() == recovered
        assert redis_peer.connection_count == 2
        if operation == "heartbeat":
            assert any(command[0] == b"EXEC" for command in redis_peer.commands)
    finally:
        redis_peer.close_clients()
        worker.join(timeout=1)
        assert not worker.is_alive()
