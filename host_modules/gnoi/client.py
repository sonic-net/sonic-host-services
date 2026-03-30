"""
Lightweight Python gRPC client for gNOI System service.

Wraps the vendored proto stubs to provide reboot() and reboot_status()
with proper error handling and structured responses.
"""

import grpc
from host_modules.gnoi import system_pb2
from host_modules.gnoi import system_pb2_grpc


class GnoiClient:
    """gNOI System service client using direct gRPC (no Docker/subprocess)."""

    def __init__(self, target, timeout=60):
        """
        Args:
            target: gRPC target address, e.g. "10.0.0.1:8080"
            timeout: Default RPC timeout in seconds.
        """
        self._target = target
        self._timeout = timeout
        self._channel = None
        self._stub = None

    def __enter__(self):
        self._channel = grpc.insecure_channel(self._target)
        self._stub = system_pb2_grpc.SystemStub(self._channel)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None

    def reboot(self, method, message="", timeout=None):
        """
        Send a gNOI System.Reboot RPC.

        Args:
            method: RebootMethod enum value (e.g. system_pb2.HALT = 3).
            message: Human-readable reason string.
            timeout: RPC timeout in seconds (overrides default).

        Returns:
            RebootResponse protobuf message.

        Raises:
            grpc.RpcError: On any gRPC failure (with code() and details()).
        """
        request = system_pb2.RebootRequest(
            method=method,
            message=message,
        )
        return self._stub.Reboot(request, timeout=timeout or self._timeout)

    def reboot_status(self, timeout=None):
        """
        Poll gNOI System.RebootStatus RPC.

        Args:
            timeout: RPC timeout in seconds (overrides default).

        Returns:
            RebootStatusResponse protobuf message with fields:
              - active (bool): True if reboot is still in progress
              - wait (uint64): nanoseconds before next poll
              - when (uint64): reboot scheduled time
              - reason (str): reason for reboot
              - count (uint32): number of reboots since active

        Raises:
            grpc.RpcError: On any gRPC failure.
        """
        request = system_pb2.RebootStatusRequest()
        return self._stub.RebootStatus(request, timeout=timeout or self._timeout)

    def cancel_reboot(self, message="", timeout=None):
        """
        Cancel a pending reboot via gNOI System.CancelReboot RPC.

        Args:
            message: Human-readable reason for cancellation.
            timeout: RPC timeout in seconds (overrides default).

        Returns:
            CancelRebootResponse protobuf message.

        Raises:
            grpc.RpcError: On any gRPC failure.
        """
        request = system_pb2.CancelRebootRequest(message=message)
        return self._stub.CancelReboot(request, timeout=timeout or self._timeout)
