"""SDRConnect WebSocket backend: tune over JSON, ingest IQ into GNU Radio.

This is the network path, not the native SoapySDR/gr-osmosdr USB path. It
assumes SDRConnect (GUI or headless) is already running with its WebSocket
server enabled, usually on port 5454. OmaSDR owns demodulation: the client
asks for signed 16-bit IQ (binary type 2 / 5) and feeds that into the same
flowgraph the RTL-SDR uses.

No third-party WebSocket package. The handshake and framing are small enough
to keep in this file, and the daemon already refuses pip-installed extras
because GNU Radio's bindings live in the system interpreter.

The official PDF and examples zip were 404 at implementation time
(2026-09-11). The contract here follows the public API summary and the
working messages used by third-party clients: JSON text frames with
event_type set_property / get_property / iq_stream_enable /
device_stream_enable, and binary frames that start with a 2-byte
little-endian type then the payload.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

try:
    import numpy as np
except ImportError:                                     # noqa: BLE001
    np = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5454
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROBE_TIMEOUT_S = 0.35
CONNECT_TIMEOUT_S = 2.0
START_TIMEOUT_S = 5.0
RECONNECT_S = 1.5
IQ_WAIT_S = 2.0
# About a quarter-second at 2.4 MS/s. Overflow drops the oldest samples so
# a slow consumer stays realtime rather than growing without bound.
RING_SECONDS = 0.25

# Binary payload types (little-endian uint16 prefix).
TYPE_AUDIO_PRIMARY = 1
TYPE_IQ_PRIMARY = 2
TYPE_SPECTRUM_PRIMARY = 3
TYPE_AUDIO_SECONDARY = 4
TYPE_IQ_SECONDARY = 5
TYPE_SPECTRUM_SECONDARY = 6

GAIN_PROPERTIES = ("rf_gain", "device_rf_gain", "if_gain", "lna_state")
PPM_PROPERTIES = ("device_ppm", "ppm", "freq_correction")


@dataclass(frozen=True)
class Endpoint:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    device: str = "primary"     # SDRConnect "primary" or "secondary"

    @property
    def args(self) -> str:
        extra = "" if self.device == "primary" else f",device={self.device}"
        return f"sdrconnect={self.host}:{self.port}{extra}"

    @property
    def label(self) -> str:
        name = f"SDRConnect ({self.host}:{self.port})"
        if self.device != "primary":
            name += f" {self.device}"
        return name

    @property
    def iq_type(self) -> int:
        return TYPE_IQ_SECONDARY if self.device == "secondary" else TYPE_IQ_PRIMARY


def is_sdrconnect_args(value: str) -> bool:
    text = (value or "").strip()
    return text == "sdrconnect" or text.startswith("sdrconnect=")


def parse_device_args(value: str,
                      default_host: str = DEFAULT_HOST,
                      default_port: int = DEFAULT_PORT) -> Endpoint:
    """Accept sdrconnect, sdrconnect=HOST, sdrconnect=HOST:PORT, plus ,device=."""
    text = (value or "").strip()
    host, port, device = default_host or DEFAULT_HOST, int(default_port or DEFAULT_PORT), "primary"
    if not text or text == "sdrconnect":
        return Endpoint(host, port, device)
    if not text.startswith("sdrconnect="):
        raise ValueError("not an SDRConnect device string: %r" % value)
    body = text[len("sdrconnect="):]
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("device="):
            device = part.split("=", 1)[1].strip() or "primary"
            continue
        if part.startswith("host="):
            host = part.split("=", 1)[1].strip() or host
            continue
        if part.startswith("port="):
            port = int(part.split("=", 1)[1])
            continue
        if part.startswith(":"):
            port = int(part[1:])
            continue
        if ":" in part:
            h, p = part.rsplit(":", 1)
            if h:
                host = h
            if p:
                port = int(p)
        else:
            host = part
    if port <= 0 or port > 65535:
        raise ValueError("invalid SDRConnect port: %s" % port)
    if device not in ("primary", "secondary"):
        raise ValueError("SDRConnect device must be primary or secondary")
    return Endpoint(host, port, device)


def probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True when something accepts a TCP connection on the WebSocket port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


_probe_cache = {"key": "", "ok": False, "t": 0.0}


def probe_cached(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 ttl: float = 1.0) -> bool:
    """probe(), remembering the last answer for `ttl` seconds.

    Device listing and get_state call this often; a 350 ms TCP timeout on
    every command would make the popover feel stuck when SDRConnect is down.
    """
    key = f"{host}:{int(port)}"
    now = time.monotonic()
    if _probe_cache["key"] == key and now - _probe_cache["t"] < ttl:
        return bool(_probe_cache["ok"])
    ok = probe(host, port)
    _probe_cache.update(key=key, ok=ok, t=now)
    return ok


def unpack_iq(payload: bytes):
    """Signed 16-bit little-endian IQIQ → complex64 samples, scaled to ±1."""
    n = len(payload) - (len(payload) % 4)
    if n <= 0:
        return [] if np is None else np.zeros(0, dtype=np.complex64)
    payload = payload[:n]
    if np is not None:
        raw = np.frombuffer(payload, dtype="<i2")
        f = raw.astype(np.float32)
        f *= np.float32(1.0 / 32768.0)
        return f.view(np.complex64).copy()
    out = []
    for i in range(0, n, 4):
        ii, qq = struct.unpack_from("<hh", payload, i)
        out.append(complex(ii / 32768.0, qq / 32768.0))
    return out


def split_binary_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < 2:
        raise ValueError("binary frame shorter than the 2-byte type prefix")
    kind = struct.unpack_from("<H", data, 0)[0]
    return kind, data[2:]


class SampleRing:
    """Thread-safe queue of complex samples. Overflow drops the oldest."""

    def __init__(self, capacity: int):
        self.capacity = max(1024, int(capacity))
        self._chunks: collections.deque = collections.deque()
        self._n = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def write(self, samples) -> None:
        if samples is None:
            return
        n = len(samples)
        if n == 0:
            return
        with self._cond:
            self._chunks.append(samples)
            self._n += n
            while self._n > self.capacity and self._chunks:
                old = self._chunks.popleft()
                self._n -= len(old)
            self._cond.notify_all()

    def readinto(self, out) -> int:
        """Copy up to len(out) samples. Non-blocking. Returns how many."""
        need = len(out)
        got = 0
        with self._lock:
            while got < need and self._chunks:
                chunk = self._chunks[0]
                take = min(len(chunk), need - got)
                out[got:got + take] = chunk[:take]
                if take == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
                self._n -= take
                got += take
        return got

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._n = 0

    def __len__(self) -> int:
        with self._lock:
            return self._n


class SdrconnectClient:
    """One WebSocket connection to SDRConnect, plus a ring of IQ samples.

    A background thread owns the socket: it reconnects while start() has been
    called, and it is the only writer. Control methods (tune, gain, rate)
    queue a JSON text frame; they do not touch GNU Radio.
    """

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self.ring = SampleRing(int(2_400_000 * RING_SECONDS))
        self.error = ""
        self.fatal_error = ""
        self.connected = False
        self.iq_seen = False
        self.audio_seen = False
        self.spectrum_seen = False
        self.properties: dict[str, str] = {}
        self.actual_rate = 0
        self._wanted_freq = 0
        self._wanted_center = 0
        self._wanted_rate = 0
        self._wanted_gain = None
        self._wanted_ppm = 0
        self._mute_was = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._get_lock = threading.Lock()
        self._pending_gets: dict[str, threading.Event] = {}
        self._pending_values: dict[str, str] = {}
        self._recv_buf = b""

    def start(self, frequency: int, sample_rate: int, gain, offset: int = 0,
              ppm: int = 0) -> None:
        self.stop()
        self.error = ""
        self.fatal_error = ""
        self.iq_seen = False
        self.audio_seen = False
        self.spectrum_seen = False
        self.actual_rate = int(sample_rate)
        self._wanted_freq = int(frequency)
        self._wanted_center = int(frequency) + int(offset)
        self._wanted_rate = int(sample_rate)
        self._wanted_gain = gain
        self._wanted_ppm = int(ppm)
        self.ring = SampleRing(max(int(sample_rate * RING_SECONDS), 16_384))
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="sdrconnect", daemon=True)
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            self.stop()
            raise ConnectionError(self._offline_message("did not complete the WebSocket handshake in time"))
        if self.error:
            msg = self.error
            self.stop()
            raise ConnectionError(msg)
        # IQ should start flowing once iq_stream_enable is acked. Wait a
        # moment so play() can refuse with a useful reason rather than
        # starting a silent flowgraph.
        deadline = time.monotonic() + IQ_WAIT_S
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.iq_seen:
                break
            time.sleep(0.05)
        if not self.iq_seen:
            extra = ""
            if self.audio_seen:
                extra = " SDRConnect sent demodulated audio (type 1) but no IQ."
            elif self.spectrum_seen:
                extra = " SDRConnect sent its own spectrum (type 3) but no IQ."
            msg = ("SDRConnect connected but sent no IQ." + extra
                   + " Enable the WebSocket IQ stream (iq_stream_enable / Full IQ) and start the radio.")
            self.stop()
            raise ConnectionError(msg)

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                self._send_event("iq_stream_enable", "false")
                if self._mute_was is not None:
                    self._send_property("audio_mute", self._mute_was)
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None
        self.connected = False
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self.ring.clear()

    def set_frequency(self, hz: int, offset: int = 0) -> None:
        self._wanted_freq = int(hz)
        self._wanted_center = int(hz) + int(offset)
        if not self.connected:
            return
        try:
            self._send_property("device_center_frequency", self._wanted_center)
            self._send_property("device_vfo_frequency", self._wanted_freq)
        except OSError as exc:
            self.error = short_ws_error(exc)

    def set_sample_rate(self, rate: int) -> None:
        self._wanted_rate = int(rate)
        if self.connected:
            try:
                self._send_property("device_sample_rate", int(rate))
            except OSError as exc:
                self.error = short_ws_error(exc)

    def set_gain(self, gain) -> None:
        self._wanted_gain = gain
        if self.connected:
            try:
                self._apply_gain(gain)
            except OSError as exc:
                self.error = short_ws_error(exc)

    def set_ppm(self, ppm: int) -> None:
        self._wanted_ppm = int(ppm)
        if self.connected:
            try:
                self._apply_ppm(int(ppm))
            except OSError as exc:
                self.error = short_ws_error(exc)

    def make_source(self):
        """GNU Radio sync block that reads the ring. Import is deferred."""
        if np is None:
            raise RuntimeError("numpy is required to feed SDRConnect IQ into GNU Radio")
        from gnuradio import gr as _gr
        ring = self.ring

        class _Block(_gr.sync_block):
            def __init__(self):
                _gr.sync_block.__init__(self, "omasdr_sdrconnect", None, [np.complex64])

            def work(self, input_items, output_items):
                out = output_items[0]
                got = ring.readinto(out)
                if got < len(out):
                    out[got:] = 0
                return len(out)

        return _Block()

    def _offline_message(self, detail: str) -> str:
        ep = self.endpoint
        return (f"SDRConnect is not running on {ep.host}:{ep.port} ({detail}). "
                "Start SDRConnect, enable the WebSocket server in Preferences, "
                "and leave it holding the radio.")

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self._connect()
                self._configure()
                self.connected = True
                self.error = ""
                self.fatal_error = ""
                self._ready.set()
                first = False
                self._loop()
                if self._stop.is_set():
                    return
                self.connected = False
                self.error = self._offline_message("connection closed")
            except Exception as exc:                    # noqa: BLE001
                self.connected = False
                self.error = self._offline_message(short_ws_error(exc)) if first else (
                    "SDRConnect disconnected: " + short_ws_error(exc))
                self._ready.set()
                if first:
                    return
                if self._stop.wait(RECONNECT_S):
                    return
            else:
                if self._stop.wait(RECONNECT_S):
                    return
            finally:
                self._close_socket()

    def _connect(self) -> None:
        ep = self.endpoint
        sock = socket.create_connection((ep.host, ep.port), timeout=CONNECT_TIMEOUT_S)
        sock.settimeout(1.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {ep.host}:{ep.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(req.encode("ascii"))
        status, headers, leftover = _read_http_headers(sock)
        if " 101 " not in status:
            sock.close()
            raise ConnectionError("WebSocket upgrade failed: " + status.strip())
        expected = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise ConnectionError("WebSocket accept key mismatch")
        sock.settimeout(1.0)
        self._sock = sock
        self._recv_buf = leftover

    def _configure(self) -> None:
        ep = self.endpoint
        # Best-effort reads: an older SDRConnect just ignores unknown fields.
        for name in ("api_version", "started", "audio_mute", "device_sample_rate"):
            try:
                self.request_property(name)
            except Exception:                           # noqa: BLE001
                pass
        self._mute_was = self.properties.get("audio_mute")
        if self._wanted_rate:
            self._send_property("device_sample_rate", self._wanted_rate)
        if self._wanted_center:
            self._send_property("device_center_frequency", self._wanted_center)
        if self._wanted_freq:
            self._send_property("device_vfo_frequency", self._wanted_freq)
        self._apply_gain(self._wanted_gain)
        self._apply_ppm(self._wanted_ppm)
        try:
            self._send_property("audio_mute", "true")
        except OSError:
            pass
        enable = "set_secondary_device_enable" if ep.device == "secondary" else "set_primary_device_enable"
        self._send_event(enable, "true")
        self._send_event("device_stream_enable", "true")
        self._send_event("iq_stream_enable", "true")
        try:
            rate = self.request_property("device_sample_rate")
            self.actual_rate = int(float(rate)) or self._wanted_rate
        except Exception:                               # noqa: BLE001
            self.actual_rate = self._wanted_rate

    def _apply_gain(self, gain) -> None:
        if gain is None:
            return
        if gain == "auto":
            for name in ("agc", "if_agc", "device_agc"):
                try:
                    self._send_property(name, "true")
                    return
                except OSError:
                    return
            return
        for name in GAIN_PROPERTIES:
            try:
                self._send_property(name, gain)
                return
            except OSError:
                return

    def _apply_ppm(self, ppm: int) -> None:
        if not ppm:
            return
        for name in PPM_PROPERTIES:
            try:
                self._send_property(name, ppm)
                return
            except OSError:
                return

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                opcode, payload = self._read_frame()
            except socket.timeout:
                continue
            if not self._dispatch(opcode, payload):
                break

    def _dispatch(self, opcode: int, payload: bytes) -> bool:
        """Handle one frame. False means the peer closed."""
        if opcode == 0x8:
            return False
        if opcode == 0x9:
            self._write_frame(0xA, payload)
            return True
        if opcode == 0x1:
            self._on_text(payload.decode("utf-8", errors="replace"))
        elif opcode == 0x2:
            self._on_binary(payload)
        return True

    def _pump(self, timeout: float) -> None:
        """Read frames on the I/O thread until `timeout` seconds have passed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock = self._sock
            if sock is not None:
                sock.settimeout(max(0.05, min(0.2, remaining)))
            try:
                opcode, payload = self._read_frame()
            except socket.timeout:
                continue
            if not self._dispatch(opcode, payload):
                raise ConnectionError("SDRConnect closed the socket")

    def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        event = msg.get("event_type")
        name = str(msg.get("property") or "")
        value = str(msg.get("value", ""))
        if event in ("property_changed", "get_property_response") and name:
            self.properties[name] = value
            if name == "device_sample_rate":
                try:
                    self.actual_rate = int(float(value)) or self.actual_rate
                except ValueError:
                    pass
        if event == "get_property_response" and name:
            ev = self._pending_gets.get(name)
            if ev is not None:
                self._pending_values[name] = value
                ev.set()

    def _on_binary(self, data: bytes) -> None:
        try:
            kind, payload = split_binary_frame(data)
        except ValueError:
            return
        if kind in (TYPE_IQ_PRIMARY, TYPE_IQ_SECONDARY):
            if kind != self.endpoint.iq_type:
                return
            samples = unpack_iq(payload)
            if len(samples):
                self.iq_seen = True
                self.ring.write(samples)
        elif kind in (TYPE_AUDIO_PRIMARY, TYPE_AUDIO_SECONDARY):
            self.audio_seen = True
        elif kind in (TYPE_SPECTRUM_PRIMARY, TYPE_SPECTRUM_SECONDARY):
            self.spectrum_seen = True

    def request_property(self, name: str, timeout: float = 1.0) -> str:
        ev = threading.Event()
        with self._get_lock:
            self._pending_gets[name] = ev
            self._pending_values.pop(name, None)
        try:
            self._send({
                "event_type": "get_property",
                "property": name,
                "device": self.endpoint.device,
                "value": "",
            })
            if threading.current_thread() is self._thread:
                # _configure runs on the I/O thread, before _loop. Pump so a
                # get_property_response is not sitting unread on the socket.
                deadline = time.monotonic() + timeout
                while not ev.is_set() and time.monotonic() < deadline:
                    self._pump(min(0.2, deadline - time.monotonic()))
            elif not ev.wait(timeout):
                raise TimeoutError("get_property %s timed out" % name)
            if not ev.is_set():
                raise TimeoutError("get_property %s timed out" % name)
            return self._pending_values.get(name, "")
        finally:
            self._pending_gets.pop(name, None)

    def _send_property(self, name: str, value) -> None:
        self._send({
            "event_type": "set_property",
            "property": name,
            "device": self.endpoint.device,
            "value": str(value),
        })

    def _send_event(self, event_type: str, value: str = "", property_name: str = "") -> None:
        self._send({
            "event_type": event_type,
            "property": property_name,
            "device": self.endpoint.device,
            "value": str(value),
        })

    def _send(self, message: dict) -> None:
        raw = json.dumps(message, separators=(",", ":"))
        self._write_frame(0x1, raw.encode("utf-8"))

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("SDRConnect is not connected")
        mask = os.urandom(4)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            sock.sendall(header + mask + masked)

    def _read_frame(self) -> tuple[int, bytes]:
        sock = self._sock
        if sock is None:
            raise ConnectionError("SDRConnect is not connected")
        header = self._recv_exact(2)
        first, second = header
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if not fin:
            # SDRConnect does not fragment in the documented examples; still
            # reassemble so a large IQ frame is not dropped.
            while True:
                more_op, more = self._read_frame()
                if more_op not in (0x0, opcode):
                    break
                payload += more
                # _read_frame already consumed one full frame; we cannot see
                # its FIN from here. Stop after one continuation.
                break
        return opcode, payload

    def _recv_exact(self, n: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise ConnectionError("SDRConnect is not connected")
        buf = self._recv_buf
        while len(buf) < n:
            try:
                chunk = sock.recv(max(4096, n - len(buf)))
            except socket.timeout:
                if self._stop.is_set():
                    raise ConnectionError("stopped")
                raise
            if not chunk:
                raise ConnectionError("SDRConnect closed the socket")
            buf += chunk
        self._recv_buf = buf[n:]
        return buf[:n]

    def _close_socket(self) -> None:
        sock = self._sock
        self._sock = None
        self.connected = False
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass


def _read_http_headers(sock: socket.socket) -> tuple[str, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("SDRConnect closed during WebSocket upgrade")
        data += chunk
        if len(data) > 16_384:
            raise ConnectionError("WebSocket upgrade response too large")
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    status = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return status, headers, rest


def short_ws_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    if text:
        return text[-1]
    return exc.__class__.__name__


@dataclass
class FakeSdrconnect:
    """In-process WebSocket stand-in for check.sh. No GNU Radio, no SDRConnect."""

    host: str = "127.0.0.1"
    port: int = 0
    device: str = "primary"
    properties: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    iq_chunks: int = 4
    iq_samples: int = 256
    send_iq: bool = True
    refuse: bool = False
    _server: socket.socket | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    clients: int = 0

    def start(self) -> "FakeSdrconnect":
        self.properties.setdefault("api_version", "1.0.3")
        self.properties.setdefault("started", "true")
        self.properties.setdefault("audio_mute", "false")
        self.properties.setdefault("device_sample_rate", "2400000")
        self.properties.setdefault("device_center_frequency", "104400000")
        self.properties.setdefault("device_vfo_frequency", "104100000")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(4)
        sock.settimeout(0.3)
        self._server = sock
        self.host, self.port = sock.getsockname()[:2]
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.clients += 1
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        try:
            if self.refuse:
                conn.close()
                return
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            req, _ = data.split(b"\r\n\r\n", 1)
            key = ""
            for line in req.decode("iso-8859-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()).decode("ascii")
            conn.sendall(
                (f"HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii")
            )
            conn.settimeout(0.2)
            if self.send_iq:
                self._send_iq(conn)
            while not self._stop.is_set():
                try:
                    opcode, payload = _server_read_frame(conn)
                except socket.timeout:
                    if self.send_iq:
                        self._send_iq(conn)
                    continue
                except OSError:
                    break
                if opcode == 0x8:
                    break
                if opcode == 0x1:
                    self._on_text(conn, payload.decode("utf-8", errors="replace"))
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _on_text(self, conn: socket.socket, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.events.append(msg)
        event = msg.get("event_type")
        name = str(msg.get("property") or "")
        value = str(msg.get("value", ""))
        if event == "set_property" and name:
            self.properties[name] = value
            _server_write_text(conn, {
                "event_type": "property_changed",
                "property": name,
                "device": msg.get("device", "primary"),
                "value": value,
            })
        elif event == "get_property" and name:
            _server_write_text(conn, {
                "event_type": "get_property_response",
                "property": name,
                "device": msg.get("device", "primary"),
                "value": str(self.properties.get(name, "")),
            })

    def _send_iq(self, conn: socket.socket) -> None:
        kind = TYPE_IQ_SECONDARY if self.device == "secondary" else TYPE_IQ_PRIMARY
        # A quiet tone: I = 0x1000, Q = 0, repeated.
        sample = struct.pack("<hh", 0x1000, 0)
        payload = struct.pack("<H", kind) + sample * self.iq_samples
        for _ in range(self.iq_chunks):
            _server_write_binary(conn, payload)


def _server_read_frame(sock: socket.socket) -> tuple[int, bytes]:
    header = _recv_exact(sock, 2)
    first, second = header
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _server_write_text(sock: socket.socket, message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    _server_write_frame(sock, 0x1, payload)


def _server_write_binary(sock: socket.socket, payload: bytes) -> None:
    _server_write_frame(sock, 0x2, payload)


def _server_write_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    sock.sendall(header + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed")
        buf += chunk
    return buf
