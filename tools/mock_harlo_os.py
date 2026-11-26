#!/usr/bin/env python3
"""
Tiny Harlo OS 0.2.101 look-alike for dry runs of the benchmark without real hardware.

    python3 tools/mock_harlo_os.py --port 8081
    # then benchmark against 127.0.0.1:8081

Implements only what the benchmark uses: GET /api/info, GET /api/telemetry,
PUT /api/tuning, POST /api/reboot. Everything else returns the same
{"ok":false,"output":"Unknown API endpoint"} body real Harlo OS sends.
The "physics" are made up; this only exercises the code paths.
"""
import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROFILE = {
    "id": "bitaxe-gamma-601",
    "name": "Bitaxe Gamma 601",
    "algorithm": "sha256d",
    "asic_count": 1,
    "frequency_min_mhz": 400,
    "frequency_max_mhz": 1000,
    "stock_frequency_mhz": 525,
    "core_min_mv": 1000,
    "core_max_mv": 1350,
    "stock_core_mv": 1150,
    "fan_channels": 1,
}


class State:
    def __init__(self, no_vin: bool, reboot_resets: bool):
        self.freq = PROFILE["stock_frequency_mhz"]
        self.core = PROFILE["stock_core_mv"]
        self.saved = (self.freq, self.core)
        self.no_vin = no_vin
        self.reboot_resets = reboot_resets
        self.down_until = 0.0
        self.boot = time.time()
        self.acc = 0
        self.rej = 0
        self.lock = threading.Lock()
        self.calls = []
        # settings used by tools/harlo_fleet.py
        self.fan = {"channel": 1, "mode": "auto", "manual_percent": 100, "sensor": "asic", "target_c": 60, "percent": 60}
        self.psu = 50
        self.unit = "C"
        self.theme = {"color": "", "palette": [""] * 15, "opacity": [100] * 16, "source": "default", "family": "Chakra Petch"}
        self.font = b""
        self.protections = {"asic_fan_c": 70, "asic_shutdown_c": 80, "vr_fan_c": 85, "vr_shutdown_c": 95}
        self.display = {"mode": "dashboard", "rotation": 0}
        self.offsets = {"asic_offset_c": 0, "vr_offset_c": 0}

    def telemetry(self):
        f, mv = self.freq, self.core
        # Need roughly 1.0 mV per MHz above 300 MHz to be stable in this toy model.
        need = 1000 + (f - 450) * 0.6
        stable = 1.0 if mv >= need else max(0.5, 1 - (need - mv) / 150)
        ghs = f * 2040 / 1000 * stable * random.uniform(0.97, 1.02)
        power = 0.0000135 * f * mv * mv / 1000 + 2.0
        temp = 38 + power * 0.9
        self.acc += random.randint(1, 3)
        if random.random() > stable:
            self.rej += 1
        t = {
            "miner_name": "gamma-mock",
            "firmware_version": "0.2.101",
            "hashrate_ghs": round(ghs, 2),
            "device_power_w": round(power, 2),
            "asic_temp_c": round(temp, 1),
            "vr_temp_c": round(temp + 8, 1),
            "target_frequency_mhz": f,
            "active_pll_mhz": f - 0.5,
            "target_core_mv": mv,
            "core_voltage_mv": mv - 4,
            "uptime_seconds": int(time.time() - self.boot),
            "asic_paused": False,
            "fans": [dict(self.fan, rpm=4200)],
            "power_supply_rating_w": self.psu,
            "temperature_unit": self.unit,
            "pools": [{"slot": 1, "host": "pool.example", "port": 3333, "user": "x", "connected": True,
                       "active_endpoint": "primary", "accepted_shares": self.acc, "rejected_shares": self.rej}],
        }
        if not self.no_vin:
            t["input_voltage_v"] = 5.12
        return t


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _down(self):
            if time.time() < st.down_until:
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except Exception:
                    pass
                return True
            return False

        def _unknown(self):
            self._send(404, {"ok": False, "output": "Unknown API endpoint"})

        def do_GET(self):
            if self._down():
                return
            with st.lock:
                st.calls.append(("GET", self.path))
                if self.path == "/api/info":
                    return self._send(200, {"firmware": "Harlo-OS-V2", "firmware_version": "0.2.101",
                                            "fleet_api_version": 1, "macAddr": "02:11:22:33:44:55",
                                            "profile": PROFILE})
                if self.path == "/api/telemetry":
                    return self._send(200, st.telemetry())
                simple = {"/api/theme": st.theme, "/api/protections": st.protections,
                          "/api/display": st.display, "/api/temperature-offsets": st.offsets}
                if self.path in simple:
                    return self._send(200, simple[self.path])
                if self.path == "/api/theme/font":
                    self.send_response(200); self.send_header("Content-Length", str(len(st.font))); self.end_headers()
                    self.wfile.write(st.font); return
                return self._unknown()

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return None

        def do_PUT(self):
            if self._down():
                return
            body = self._body()
            with st.lock:
                st.calls.append(("PUT", self.path, body))
                ok = {"ok": True}
                if self.path.startswith("/api/fans/"):
                    if "mode" in body and body["mode"] == "auto":
                        st.fan.update(mode="auto", sensor=body["sensor"], target_c=body["target_c"])
                    elif "percent" in body:
                        st.fan.update(mode="manual", manual_percent=body["percent"], percent=body["percent"])
                    else:
                        return self._send(400, {"ok": False, "output": "bad fan body"})
                    return self._send(200, ok)
                if self.path == "/api/power-supply":
                    st.psu = int(body["rating_w"]); return self._send(200, ok)
                if self.path == "/api/temperature-unit":
                    st.unit = body["unit"]; return self._send(200, ok)
                if self.path == "/api/theme":
                    for k in ("color", "palette", "opacity"):
                        if k in body:
                            st.theme[k] = body[k]
                    if "family" in body:
                        st.theme.update(source="google", family=body["family"])
                    return self._send(200, ok)
                if self.path == "/api/protections":
                    if set(body) != set(st.protections):
                        return self._send(400, {"ok": False, "output": "send all four thresholds"})
                    st.protections = dict(body); return self._send(200, ok)
                if self.path == "/api/display":
                    st.display.update(body); return self._send(200, ok)
                if self.path == "/api/temperature-offsets":
                    st.offsets.update(body); return self._send(200, ok)
                if self.path != "/api/tuning":
                    return self._unknown()
                if not isinstance(body, dict) or "frequency_mhz" not in body or "core_mv" not in body:
                    return self._send(400, {"ok": False, "output": "frequency_mhz and core_mv are required"})
                f, mv = int(body["frequency_mhz"]), int(body["core_mv"])
                if not (PROFILE["frequency_min_mhz"] <= f <= PROFILE["frequency_max_mhz"]):
                    return self._send(200, {"ok": False, "output": "frequency out of range"})
                if not (PROFILE["core_min_mv"] <= mv <= PROFILE["core_max_mv"]):
                    return self._send(200, {"ok": False, "output": "core voltage out of range"})
                st.freq, st.core = f, mv
                if not st.reboot_resets:
                    st.saved = (f, mv)
                return self._send(200, {"ok": True})

        def do_POST(self):
            if self._down():
                return
            if self.path.startswith("/api/theme/font"):
                n = int(self.headers.get("Content-Length") or 0)
                st.font = self.rfile.read(n)
                from urllib.parse import urlparse, parse_qs
                st.theme.update(source="custom", name=parse_qs(urlparse(self.path).query).get("name", [""])[0])
                return self._send(200, {"ok": True})
            self._body()
            with st.lock:
                st.calls.append(("POST", self.path))
                if self.path == "/api/reboot":
                    self._send(200, {"ok": True})
                    st.down_until = time.time() + 2
                    st.boot = time.time() + 2
                    st.freq, st.core = st.saved
                    st.acc = st.rej = 0
                    return
                return self._unknown()

        def do_PATCH(self):
            self._unknown()

    return H


def serve(port: int, no_vin=False, reboot_resets=False):
    st = State(no_vin=no_vin, reboot_resets=reboot_resets)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--no-input-voltage", action="store_true")
    ap.add_argument("--reboot-resets-tuning", action="store_true",
                    help="simulate tuning that is not persisted across reboot")
    a = ap.parse_args()
    serve(a.port, a.no_input_voltage, a.reboot_resets_tuning)
    print(f"mock Harlo OS on http://127.0.0.1:{a.port}")
    while True:
        time.sleep(3600)
