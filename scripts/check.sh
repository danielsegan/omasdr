#!/usr/bin/env bash
# Exercise the daemon over its socket: start a scratch daemon on a private
# runtime dir, walk the protocol, and stop it. No device needed for most of
# it; with a free dongle it also plays for two seconds.
set -euo pipefail
cd "$(dirname "$0")/.."
# Scratch runtime and config so the check never touches the real daemon,
# presets, or settings. XDG_RUNTIME_DIR itself stays put: PipeWire's ALSA
# plugin finds its server through it.
SCRATCH="$(mktemp -d)"
export OMASDR_RUNTIME_DIR="$SCRATCH/omasdr"
export XDG_CONFIG_HOME="$SCRATCH/config"
# Also scratch the cache, so a nearby search here never disturbs (or silently
# leans on) the real downloaded index.
export XDG_CACHE_HOME="$SCRATCH/cache"
cleanup() {
  status=$?
  /usr/bin/python3 daemon/omasdrd.py stop >/dev/null 2>&1 || true
  if (( status != 0 )) && [[ -f "$OMASDR_RUNTIME_DIR/daemon.log" ]]; then
    echo "--- daemon log (last 30 lines)"; tail -30 "$OMASDR_RUNTIME_DIR/daemon.log"
  fi
  rm -rf "$SCRATCH"
  exit $status
}
trap cleanup EXIT
/usr/bin/python3 -m py_compile daemon/omasdrd.py daemon/nearby.py daemon/sdrconnect.py

# Nearby search: the parts that need no network. The live search only runs
# with OMASDR_CHECK_NET=1, because this suite has to work anywhere.
/usr/bin/python3 - <<'NEARBY'
import sys
sys.path.insert(0, "daemon")
import nearby
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: raise SystemExit(1)
def near(a, b, tol=0.01): return abs(a - b) < tol

lat, lon = nearby.parse_grid("IO91wm")
check(near(lat, 51.52, 0.02) and near(lon, -0.125, 0.05), "maidenhead 6-character grid (IO91wm is London)")
lat, lon = nearby.parse_grid("FL96")
check(near(lat, 26.5) and near(lon, -61.0), "maidenhead 4-character grid centres its square")
check(nearby.parse_grid("Melbourne") is None, "a place name is not a grid square")
check(nearby.parse_latlon("28.0785, -80.6078") == (28.0785, -80.6078), "coordinate pair parsed")
check(nearby.parse_latlon("91, 0") is None, "impossible latitude refused")
check(round(nearby.haversine(51.5074, -0.1278, 48.8566, 2.3522)) == 344, "haversine London to Paris is 344 km")
check(nearby._offset_label(-600_000) == "-600 kHz" and nearby._offset_label(5_000_000) == "+5 MHz", "repeater offset labels")
check(nearby._offset_label(0) == "simplex", "no offset reads as simplex")
check("FM" in nearby._tokens("YSF/FM") and "FM" not in nearby._tokens("C4FM"), "mixed-mode repeaters kept, digital-only dropped")
check(nearby._repair("V\u00c3\u00a4stra") == "V\u00e4stra", "double-encoded city names repaired")
NEARBY

# Device listing: RTL path unchanged, SDRplay via Soapy args, SDRConnect last.
/usr/bin/python3 - <<'DEVICES'
import sys
sys.path.insert(0, "daemon")
import omasdrd
import sdrconnect

def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: raise SystemExit(1)

lsusb = (
    "Bus 001 Device 003: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T\n"
    "Bus 001 Device 004: ID 1df7:3030 SDRplay RSPdx\n"
    "Bus 001 Device 005: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
)
devs = omasdrd.enumerate_devices(lsusb_text=lsusb, soapy=[], sdrconnect_reachable=False)
check(len(devs) == 3, "lsusb lists RTL and SDRplay, plus the SDRConnect row")
check(devs[0].kind == "rtl" and devs[0].args == "rtl=0", "RTL stays first with rtl=0")
check(devs[1].kind == "sdrplay" and devs[1].args.startswith("soapy=") and "driver=sdrplay" in devs[1].args,
      "SDRplay USB fallback uses soapy=,driver=sdrplay")
check(devs[1].name.startswith("SDRplay"), "SDRplay USB id maps to a model name")
check(devs[2].kind == "sdrconnect" and devs[2].args == "sdrconnect=127.0.0.1:5454",
      "SDRConnect is listed last with the default host:port")
check(devs[2].status == "missing", "offline SDRConnect is missing, not free")

rtl_only = omasdrd.enumerate_devices(
    lsusb_text="Bus 001 Device 003: ID 0bda:2838 Realtek RTL2838\n", soapy=[],
    sdrconnect_reachable=False)
check(rtl_only[0].args == "rtl=0" and rtl_only[0].kind == "rtl",
      "RTL-only listing still puts the dongle first")
check(any(d.kind == "sdrconnect" for d in rtl_only), "SDRConnect remains listed without an RSP")

two_rtl = omasdrd.enumerate_devices(
    lsusb_text="Bus 001 Device 003: ID 0bda:2838 A\nBus 001 Device 006: ID 0bda:2832 B\n",
    soapy=[], sdrconnect_reachable=False)
check([d.args for d in two_rtl if d.kind == "rtl"] == ["rtl=0", "rtl=1"],
      "second RTL is rtl=1, not mixed with Soapy")

soapy = [{"driver": "sdrplay", "label": "SDRplay Dev 0 RSPdx  ABC123", "serial": "ABC123"}]
devs = omasdrd.enumerate_devices(lsusb_text=lsusb, soapy=soapy, sdrconnect_reachable=False)
sp = [d for d in devs if d.kind == "sdrplay"]
check(len(sp) == 1 and sp[0].args == "soapy=0,driver=sdrplay,serial=ABC123",
      "Soapy serial becomes the osmosdr args")
check("RSPdx" in sp[0].name, "Soapy label becomes the display name")

none = omasdrd.enumerate_devices(lsusb_text="", soapy=[], sdrconnect_reachable=False)
check(len(none) == 1 and none[0].kind == "sdrconnect",
      "no USB radios still lists the SDRConnect backend")
check(omasdrd.sdrplay_osmosdr_args() == "soapy=0,driver=sdrplay", "default SDRplay args match gqrx")

d = omasdrd.resolve_device("soapy=0,driver=sdrplay", [])
check(d is not None and d.kind == "sdrplay" and d.args == "soapy=0,driver=sdrplay",
      "set_device accepts a raw Soapy string")
d = omasdrd.resolve_device("sdrconnect=192.0.2.8:5454", [])
check(d is not None and d.kind == "sdrconnect" and d.args == "sdrconnect=192.0.2.8:5454",
      "set_device accepts a raw SDRConnect string")
check(omasdrd.resolve_device("missing-serial", []) is None,
      "unknown serial is missing, not synthesized")
check(omasdrd.resolve_device("", rtl_only) is rtl_only[0], "empty device picks the first radio")

ep = sdrconnect.parse_device_args("sdrconnect")
check(ep.host == "127.0.0.1" and ep.port == 5454 and ep.args == "sdrconnect=127.0.0.1:5454",
      "bare sdrconnect expands to the default endpoint")
ep = sdrconnect.parse_device_args("sdrconnect=10.0.0.5:6000,device=secondary")
check(ep.host == "10.0.0.5" and ep.port == 6000 and ep.device == "secondary",
      "host, port, and secondary tuner parse")
check(sdrconnect.parse_device_args("sdrconnect=:5455").port == 5455, "port-only form keeps the default host")
check(not sdrconnect.probe("127.0.0.1", 1), "probe of a closed port is false")

import struct
tone = sdrconnect.unpack_iq(struct.pack("<hh", 16384, -16384))
check(abs(tone[0].real - 0.5) < 1e-3 and abs(tone[0].imag + 0.5) < 1e-3, "int16 IQ unpacks to ±0.5")
kind, payload = sdrconnect.split_binary_frame(struct.pack("<H", 2) + struct.pack("<hh", 1, 2))
check(kind == 2 and len(payload) == 4, "binary frames start with a 2-byte type")

fake = sdrconnect.FakeSdrconnect().start()
try:
    check(sdrconnect.probe(fake.host, fake.port), "fake SDRConnect accepts a TCP probe")
    client = sdrconnect.SdrconnectClient(sdrconnect.Endpoint(fake.host, fake.port))
    client.start(frequency=101_100_000, sample_rate=2_400_000, gain=20, offset=300_000)
    check(client.iq_seen and client.connected, "client receives IQ from the fake server")
    check(len(client.ring) > 0, "IQ samples land in the ring")
    client.set_frequency(104_100_000, offset=300_000)
    # Give the reader a tick to process the set_property.
    import time; time.sleep(0.2)
    freqs = [e for e in fake.events if e.get("event_type") == "set_property"
             and e.get("property") == "device_center_frequency"]
    check(freqs and freqs[-1]["value"] == "104400000",
          "retune sends device_center_frequency = freq + offset")
    vfos = [e for e in fake.events if e.get("property") == "device_vfo_frequency"]
    check(vfos and vfos[-1]["value"] == "104100000", "VFO is the wanted channel")
    client.stop()
    check(not client.connected, "stop closes the WebSocket")
finally:
    fake.stop()

silent = sdrconnect.FakeSdrconnect(send_iq=False).start()
try:
    quiet = sdrconnect.SdrconnectClient(sdrconnect.Endpoint(silent.host, silent.port))
    try:
        quiet.start(frequency=100_000_000, sample_rate=2_400_000, gain=0)
        raise SystemExit("  FAIL start without IQ should raise")
    except ConnectionError as exc:
        check("no IQ" in str(exc), "connected-but-silent server is reported as no IQ")
finally:
    silent.stop()

down = sdrconnect.SdrconnectClient(sdrconnect.Endpoint("127.0.0.1", 1))
try:
    down.start(frequency=100_000_000, sample_rate=2_400_000, gain=0)
    raise SystemExit("  FAIL start against a closed port should raise")
except ConnectionError as exc:
    check("not running" in str(exc).lower() and "127.0.0.1:1" in str(exc),
          "offline start names the host:port (%s)" % exc)
DEVICES

/usr/bin/python3 daemon/omasdrd.py ensure
for _ in $(seq 40); do [[ -S "$OMASDR_RUNTIME_DIR/control.sock" ]] && break; sleep 0.1; done
/usr/bin/python3 - <<'PY'
import json, os, socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(os.environ["OMASDR_RUNTIME_DIR"] + "/control.sock"); s.settimeout(20)
buf = b""
def read():
    global buf
    while b"\n" not in buf: buf += s.recv(65536)
    line, buf = buf.split(b"\n", 1); return json.loads(line)
def until(*types):
    while True:
        m = read()
        if m["type"] in types: return m
def send(m): s.sendall((json.dumps(m) + "\n").encode())
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: raise SystemExit(1)

hello = until("hello"); check(hello["v"] == 1 and len(hello["demods"]) >= 6, "hello with demods")
state = until("state"); check(state["type"] == "state", "initial state")
send({"type": "set_frequency", "frequency": 101_100_000}); r = until("state"); check(r["frequency"] == 101_100_000, "set_frequency")
send({"type": "step", "delta": -1}); r = until("state"); check(r["frequency"] == 101_000_000, "step follows demod (100 kHz for WFM)")
send({"type": "set_demod", "demod": "nfm"}); r = until("state"); check(r["demod"] == "nfm" and r["step"] == 12_500, "set_demod changes step")
send({"type": "set_step", "step": 5000}); r = until("state"); check(r["step"] == 5000, "set_step override")
send({"type": "set_demod", "demod": "wfm"}); r = until("state"); check(r["step"] == 100_000, "demod change clears override")
send({"type": "set_demod", "demod": "nope"}); r = until("error"); check("Unknown demod" in r["message"], "bad demod refused")
send({"type": "save_preset", "name": "A", "frequency": 90_300_000, "demod": "wfm"}); r = until("presets"); check(len(r["presets"]) == 1, "save_preset")
send({"type": "save_preset", "name": "B", "frequency": 90_300_000, "demod": "nfm"}); r = until("presets"); check(len(r["presets"]) == 1 and r["presets"][0]["name"] == "B", "same frequency replaces")
send({"type": "delete_preset", "frequency": 90_300_000}); r = until("presets"); check(len(r["presets"]) == 0, "delete_preset")
send({"type": "list_devices"}); r = until("devices")
check(r["type"] == "devices" and isinstance(r["devices"], list), "list_devices")
check(all("args" in d and "kind" in d for d in r["devices"]), "list_devices rows carry args and kind")
send({"type": "get_state"}); prior = until("state")
send({"type": "set_device", "device": "soapy=0,driver=sdrplay"}); r = until("state")
check(r["device"]["args"] == "soapy=0,driver=sdrplay" and r["device"]["kind"] == "sdrplay",
      "set_device keeps a raw Soapy string")
send({"type": "set_device", "device": "sdrconnect=127.0.0.1:5454"}); r = until("state")
check(r["device"]["args"] == "sdrconnect=127.0.0.1:5454" and r["device"]["kind"] == "sdrconnect",
      "set_device keeps a raw SDRConnect string")
send({"type": "list_devices"}); r = until("devices")
check(any(d.get("kind") == "sdrconnect" for d in r["devices"]),
      "list_devices includes the SDRConnect backend")
send({"type": "play"}); r = until("state")
check(not r["playing"] and "SDRConnect" in (r.get("error") or ""),
      "play without SDRConnect running names the backend (" + (r.get("error") or "") + ")")
send({"type": "set_device", "device": ""}); r = until("state")
check(r["device"]["args"] == prior["device"]["args"] and r["device"]["kind"] == prior["device"]["kind"],
      "empty set_device restores the first radio")
send({"type": "get_state"}); r = until("state")
dev = r["device"]; print("  device:", dev["status"], dev.get("kind", ""), dev["name"], dev["held_by"])
check("kind" in dev, "state.device carries kind")
if dev["status"] == "free":
    send({"type": "play"}); r = until("state")
    check(r["playing"] and r["device"]["status"] == "ours", "play opens the device (" + r["error"] + ")")
    check(len(r["gain_range"]) > 5, "gain_range read from tuner")
    time.sleep(2)
    send({"type": "set_frequency", "frequency": 104_100_000}); r = until("state"); check(r["playing"], "live retune")
    # Spectrum socket: hello, then frames while playing; level messages ride the control socket.
    f = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); f.connect(os.environ["OMASDR_RUNTIME_DIR"] + "/fft.sock"); f.settimeout(10)
    fbuf = b""
    def fread():
        global fbuf
        while b"\n" not in fbuf: fbuf += f.recv(65536)
        line, fbuf = fbuf.split(b"\n", 1); return json.loads(line)
    fh = fread(); check(fh["type"] == "fft_hello" and fh["n"] == 1024, "fft hello")
    fr = fread(); bins = fr["bins"]
    check(fr["type"] == "fft" and len(bins) == fr["n"] == 1024 and fr["center"] == 104_100_000 + 300_000, "fft frame: 1024 bins, centre carries the tuning offset")
    check(30 < max(bins) < 256 and min(bins) < max(bins), "fft frame has dynamic range (%d..%d)" % (min(bins), max(bins)))
    t0 = time.time(); n = 0
    while time.time() - t0 < 1.0: fread(); n += 1
    check(8 <= n <= 20, "fft rate about 15/s (%d in 1 s)" % n)
    f.close()
    lvl = until("level"); check(-150 < lvl["db"] < 0, "level message on control socket (%.1f dB)" % lvl["db"])
    send({"type": "set_record_dir", "record_dir": os.environ["OMASDR_RUNTIME_DIR"] + "/rec"}); until("state")
    send({"type": "record", "enabled": True}); r = until("state"); check(r["recording"].endswith(".wav"), "record starts")
    time.sleep(1.5)
    send({"type": "record", "enabled": False}); r = until("state"); check(r["recording"] == "", "record stops")
    import glob, wave
    files = glob.glob(os.environ["OMASDR_RUNTIME_DIR"] + "/rec/*.wav")
    with wave.open(files[0]) as w: check(w.getnchannels() == 2 and w.getframerate() == 48000 and w.getnframes() > 40000, "wav is stereo 48 kHz with >1 s of audio (%d frames)" % w.getnframes())
    send({"type": "stop"}); r = until("state"); check(not r["playing"] and r["device"]["status"] == "free", "stop releases the device")
elif dev["status"] == "busy":
    send({"type": "play"}); r = until("state"); check(not r["playing"] and "held by" in r["error"].lower(), "busy device reported, not crashed")
else:
    print("  skip playback: no device")
check("location" in state, "state carries the saved location")
send({"type": "search_nearby"}); r = until("nearby", "error")
check(r["type"] == "error" and "location" in r["message"].lower(), "nearby search with no location refused")
if os.environ.get("OMASDR_CHECK_NET") == "1":
    send({"type": "search_nearby", "latitude": 28.0785, "longitude": -80.6078, "limit": 4, "radius_km": 40})
    r = until("nearby"); check(r["status"] == "searching", "nearby search answers before it works")
    s.settimeout(240)
    r = until("nearby")
    check(r["status"] == "ok", "nearby search returned (" + str(r.get("message", "")) + ")")
    kinds = {x["kind"] for x in r["results"]}
    check("airband" in kinds and "repeater" in kinds, "both airband and repeaters found")
    check(all(x["demod"] in ("am", "nfm") and x["frequency"] > 0 for x in r["results"]), "results are tunable presets")
    check(len(r["sources"]) == 2, "both sources credited")
    s.settimeout(20)
else:
    print("  skip nearby network check (set OMASDR_CHECK_NET=1)")
send({"type": "quit"}); check(until("bye")["type"] == "bye", "quit")
print("all checks passed")
PY
