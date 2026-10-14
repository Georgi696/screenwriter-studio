"""Offline regression tests for Screenwriter Studio."""
import os
import socket
import tempfile

# Tests must never read the developer's saved model choices.
os.environ["STUDIO_SETTINGS_PATH"] = os.path.join(tempfile.mkdtemp(prefix="studio-test-"), "studio_settings.json")

# Tests must never reach a real provider: a missed mock would spend credits with a real key.
_connect = socket.socket.connect


def _local_only(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if self.family == socket.AF_UNIX or host in {"127.0.0.1", "localhost", "::1"}:
        return _connect(self, address)
    raise ConnectionRefusedError(f"Tests may not open network connections (tried {host}).")


socket.socket.connect = _local_only
