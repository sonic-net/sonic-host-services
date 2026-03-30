"""
Lightweight Python gRPC client for gNOI services.

Manages the gRPC channel and provides access to gNOI service stubs.
All RPCs are accessed through service properties, e.g.:

    with GnoiClient("10.0.0.1:8080") as client:
        client.system.Reboot(request, timeout=60)
        client.system.RebootStatus(request, timeout=10)
"""

import grpc
from host_modules.gnoi import system_pb2_grpc


class GnoiClient:
    """gNOI client managing a gRPC channel with access to service stubs."""

    def __init__(self, target):
        """
        Args:
            target: gRPC target address, e.g. "10.0.0.1:8080"
        """
        self._target = target
        self._channel = None

    def __enter__(self):
        self._channel = grpc.insecure_channel(self._target)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        if self._channel:
            self._channel.close()
            self._channel = None

    @property
    def channel(self):
        """Access the underlying gRPC channel for custom stubs."""
        return self._channel

    @property
    def system(self):
        """gNOI System service stub (gnoi.system.System)."""
        return system_pb2_grpc.SystemStub(self._channel)
