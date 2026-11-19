"""Live camera of a Bambu Lab A1 / A1 mini / P1 printer on the local network.

The printer streams JPEG pictures over TLS on port 6000 (LAN Only mode). The client sends one 80-byte login
packet (user "bblp" and the LAN access code), then the printer sends frames: a 16-byte header whose first
4 bytes are the picture size (little endian), followed by the JPEG. About 1 picture per second.

``BambuCamera`` reads frames in a background thread only while someone is looking (``frame()`` was called in
the last ``idle_stop_s`` seconds), and keeps the newest picture.
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time

CAMERA_PORT = 6000
MAX_FRAME_BYTES = 8 * 1024 * 1024


def auth_packet(access_code: str, user: str = "bblp") -> bytes:
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + user.encode().ljust(32, b"\0") + access_code.encode().ljust(32, b"\0")


def _recv_exact(sock, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("camera stream closed")
        data += chunk
    return bytes(data)


def read_frames(sock):
    """Yields JPEG pictures from a connected, logged-in camera socket."""
    while True:
        header = _recv_exact(sock, 16)
        size = struct.unpack("<I", header[:4])[0]
        if not 0 < size <= MAX_FRAME_BYTES:
            raise ConnectionError(f"bad camera frame size {size}")
        payload = _recv_exact(sock, size)
        if payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9":
            yield payload


class BambuCamera:
    def __init__(self, ip: str, access_code: str, port: int = CAMERA_PORT, use_tls: bool = True,
                 idle_stop_s: float = 30.0):
        self.ip, self.access_code, self.port, self.use_tls = ip, access_code, port, use_tls
        self.idle_stop_s = idle_stop_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._wanted_until = 0.0
        self._jpeg: bytes | None = None
        self._at = 0.0
        self.error: str | None = None

    def frame(self) -> tuple[bytes | None, float, str | None]:
        """The newest picture, its age in seconds, and the last error. Starts the stream if needed."""
        with self._lock:
            self._wanted_until = time.monotonic() + self.idle_stop_s
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name=f"camera-{self.ip}")
                self._thread.start()
            age = time.monotonic() - self._at if self._jpeg else None
            return self._jpeg, age, self.error

    def _connect(self):
        raw = socket.create_connection((self.ip, self.port), timeout=10)
        if not self.use_tls:
            return raw
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the printer uses a self-signed certificate
        return context.wrap_socket(raw, server_hostname=self.ip)

    def _run(self) -> None:
        delay = 2.0
        while time.monotonic() < self._wanted_until:
            try:
                with self._connect() as sock:
                    sock.sendall(auth_packet(self.access_code))
                    for jpeg in read_frames(sock):
                        with self._lock:
                            self._jpeg, self._at, self.error = jpeg, time.monotonic(), None
                        delay = 2.0
                        if time.monotonic() >= self._wanted_until:
                            return  # nobody is watching any more
            except (OSError, ConnectionError, ssl.SSLError) as e:
                with self._lock:
                    self.error = f"camera not reachable ({e})"
                time.sleep(delay)
                delay = min(delay * 2, 15.0)
