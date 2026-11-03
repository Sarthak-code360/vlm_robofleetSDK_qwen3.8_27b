"""Talks to a Bambu Lab printer on the local network.

Two channels, both need LAN Only + Developer Mode on the printer:
  - FTPS (port 990): upload the sliced .gcode.3mf file
  - MQTT (port 8883): read status and send the "start print" command
Login is always user "bblp" with the printer's LAN access code as password.
The printer uses a self-signed certificate, so certificate checks are off;
only use this on a trusted local network.
"""

from __future__ import annotations

import ftplib
import json
import ssl
import threading
from pathlib import Path

USER = "bblp"
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}


def _insecure_tls() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class _ImplicitFTPS(ftplib.FTP_TLS):
    """FTPS where TLS starts immediately (port 990), reusing the TLS session
    for data transfers, which the printer requires."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """Same as ftplib's, minus conn.unwrap(): the printer never answers the
        TLS close, so unwrap() hangs until timeout even though the file arrived."""
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
        return self.voidresp()


def start_print_payload(file_url: str, plate: int = 1, use_ams: bool = False) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "url": file_url,
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": Path(file_url).name,
            "md5": "",
            "timelapse": False,
            "bed_type": "auto",
            "bed_levelling": True,
            "flow_cali": False,
            "vibration_cali": False,
            "layer_inspect": False,
            "use_ams": use_ams,
            "ams_mapping": [],
        }
    }


class BambuPrinter:
    def __init__(self, ip: str, serial: str, access_code: str, file_url_prefix: str = "file:///sdcard/"):
        self.ip, self.serial, self.access_code = ip, serial, access_code
        self.file_url_prefix = file_url_prefix

    @classmethod
    def from_config(cls, config: dict) -> "BambuPrinter":
        """Builds a printer from the ``printer`` section of a printer config file."""
        return cls(config["ip"], config["serial"], config["access_code"],
                   config.get("file_url_prefix", "file:///sdcard/"))

    def _mqtt(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:
            raise ImportError('Printer support needs paho-mqtt: pip install "robofleet[printer]"') from e
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set(USER, self.access_code)
        client.tls_set_context(_insecure_tls())
        return client

    def status(self, timeout: float = 15) -> dict:
        """Asks the printer for a full status report and returns its "print" section."""
        result: dict = {}
        got = threading.Event()
        report_topic = f"device/{self.serial}/report"

        def on_connect(client, userdata, flags, reason_code, properties):
            client.subscribe(report_topic)
            client.publish(f"device/{self.serial}/request",
                           json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

        def on_message(client, userdata, msg):
            data = json.loads(msg.payload)
            if "gcode_state" in data.get("print", {}):
                result.update(data["print"])
                got.set()

        client = self._mqtt()
        client.on_connect, client.on_message = on_connect, on_message
        client.connect(self.ip, 8883)
        client.loop_start()
        try:
            if not got.wait(timeout):
                raise TimeoutError("Printer did not answer. Check IP, access code and Developer Mode.")
        finally:
            client.loop_stop()
            client.disconnect()
        return result

    def upload(self, path: Path) -> str:
        """Uploads a file to the printer's storage and returns the URL to print it."""
        ftp = _ImplicitFTPS(context=_insecure_tls())
        ftp.connect(self.ip, 990, timeout=30)
        try:
            ftp.login(USER, self.access_code)
            ftp.prot_p()
            with path.open("rb") as f:
                ftp.storbinary(f"STOR {path.name}", f)
        finally:
            ftp.close()
        return self.file_url_prefix + path.name

    def start_print(self, file_url: str, plate: int = 1, use_ams: bool = False) -> None:
        client = self._mqtt()
        client.connect(self.ip, 8883)
        client.loop_start()
        try:
            info = client.publish(f"device/{self.serial}/request",
                                  json.dumps(start_print_payload(file_url, plate, use_ams)), qos=1)
            info.wait_for_publish(timeout=10)
        finally:
            client.loop_stop()
            client.disconnect()
