#!/usr/bin/env python3
"""
harlo_fleet.py - view and sync Harlo OS settings across several miners.

Request bodies match the web UI embedded in Harlo OS 0.2.101 (dashboard.js).
Nothing is written unless you pass --apply; without it you get a dry run that
shows per miner what would change.

Examples
  # overview of all miners
  python tools/harlo_fleet.py show 172.25.36.231-235

  # copy theme + fan settings from RodAxe001 to the rest (dry run)
  python tools/harlo_fleet.py copy --from 172.25.36.231 --to 172.25.36.232-235 --what theme,fans

  # same, for real
  python tools/harlo_fleet.py copy --from 172.25.36.231 --to 172.25.36.232-235 --what theme,fans --apply

  # set explicit values on all five
  python tools/harlo_fleet.py set 172.25.36.231-235 --fan auto:asic:60 --psu 50 --unit C --apply
  python tools/harlo_fleet.py set 172.25.36.232-235 --fan 80 --theme-color "#a17e40" --apply

Settings that can be copied (--what):
  theme        accent color, palette, opacity, font (Google or uploaded font)
  fans         fan mode per channel (auto: sensor + target, manual: percent)
  power        power supply rating (W)
  protections  ASIC/VR full-fan and shutdown temperatures (sent as a set of four)
  unit         temperature unit (C/F)
  display      display mode and rotation
  offsets      temperature sensor offsets (per-board calibration; usually do NOT copy)
Default for copy: theme,fans,power,protections,unit,display.
Tuning (frequency/voltage) is deliberately not part of this tool: every miner
gets its own values from the benchmark.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

TIMEOUT = 8
PROTECTION_KEYS = ["asic_fan_c", "asic_shutdown_c", "vr_fan_c", "vr_shutdown_c"]
DEFAULT_COPY = ["theme", "fans", "power", "protections", "unit", "display"]
ALL_WHAT = DEFAULT_COPY + ["offsets"]


class HarloError(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers


def expand_targets(specs: List[str]) -> List[str]:
    """Accept 1.2.3.4, 1.2.3.4-9, 1.2.3.4-1.2.3.9, a CIDR, or comma lists."""
    out: List[str] = []
    for spec in specs:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "/" in part:
                out += [str(h) for h in ipaddress.ip_network(part, strict=False).hosts()]
            elif "-" in part:
                a, b = part.split("-", 1)
                start = ipaddress.ip_address(a)
                end = ipaddress.ip_address(b) if "." in b else ipaddress.ip_address(
                    a.rsplit(".", 1)[0] + "." + b
                )
                for n in range(int(start), int(end) + 1):
                    out.append(str(ipaddress.ip_address(n)))
            else:
                out.append(part)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _url(ip: str, path: str) -> str:
    base = ip if ip.startswith("http") else f"http://{ip}"
    return base.rstrip("/") + path


def _check(r: requests.Response, what: str) -> Any:
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("ok") is False:
        raise HarloError(f"{what}: {data.get('output') or 'rejected'}")
    if not r.ok:
        raise HarloError(f"{what}: HTTP {r.status_code}")
    return data


def get(ip: str, path: str) -> Any:
    return _check(requests.get(_url(ip, path), timeout=TIMEOUT), f"GET {path}")


def put(ip: str, path: str, body: Dict[str, Any]) -> Any:
    return _check(requests.put(_url(ip, path), json=body, timeout=TIMEOUT), f"PUT {path}")


def get_opt(ip: str, path: str) -> Optional[Any]:
    try:
        return get(ip, path)
    except Exception:
        return None


# ------------------------------------------------------------- read state


def read_state(ip: str) -> Dict[str, Any]:
    info = get(ip, "/api/info")
    if not str(info.get("firmware", "")).lower().startswith("harlo"):
        raise HarloError(f"{ip} does not look like Harlo OS (firmware={info.get('firmware')!r})")
    tel = get(ip, "/api/telemetry")
    prof = info.get("profile") or {}
    st: Dict[str, Any] = {
        "ip": ip,
        "name": tel.get("miner_name") or info.get("name"),
        "model": prof.get("name") or prof.get("id"),
        "version": tel.get("firmware_version") or info.get("version"),
        "fan_channels": int(prof.get("fan_channels") or 1),
        "tuning": (tel.get("target_frequency_mhz"), tel.get("target_core_mv")),
        "power": tel.get("power_supply_rating_w"),
        "unit": tel.get("temperature_unit"),
        "fans": fan_settings(tel.get("fans") or []),
        "theme": get_opt(ip, "/api/theme"),
        "protections": _pick(get_opt(ip, "/api/protections"), PROTECTION_KEYS),
        "display": _pick(get_opt(ip, "/api/display"), ["mode", "rotation"]),
        "offsets": _pick(get_opt(ip, "/api/temperature-offsets"), ["asic_offset_c", "vr_offset_c"]),
        "live": {
            "hashrate_ghs": tel.get("hashrate_ghs"),
            "asic_temp_c": tel.get("asic_temp_c"),
            "vr_temp_c": tel.get("vr_temp_c"),
            "input_voltage_v": tel.get("input_voltage_v"),
            "device_power_w": tel.get("device_power_w"),
            "fans": [
                (int(f.get("channel") or i + 1), f.get("percent"), f.get("rpm"))
                for i, f in enumerate(tel.get("fans") or [])
                if isinstance(f, dict)
            ],
        },
    }
    return st


def _pick(d: Any, keys: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(d, dict):
        return None
    # some endpoints may wrap their values
    for wrap in ("protections", "display", "offsets"):
        if isinstance(d.get(wrap), dict) and not any(k in d for k in keys):
            d = d[wrap]
    out = {k: d[k] for k in keys if k in d}
    return out or None


def fan_settings(fans: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Fan channel -> the body the web UI would PUT to /api/fans/{ch}."""
    out: Dict[int, Dict[str, Any]] = {}
    for i, f in enumerate(fans):
        if not isinstance(f, dict):
            continue
        ch = int(f.get("channel") or i + 1)
        if str(f.get("mode", "")).lower() == "auto":
            out[ch] = {"mode": "auto", "sensor": f.get("sensor") or "asic", "target_c": f.get("target_c")}
        else:
            pct = f.get("manual_percent", f.get("percent"))
            out[ch] = {"percent": pct}
    return out


# ------------------------------------------------------------ plan writes


Write = Tuple[str, str, Dict[str, Any], str]  # (kind, path, body, description)


def theme_writes(src: Optional[Dict[str, Any]], dst: Optional[Dict[str, Any]]) -> List[Write]:
    if not src:
        return []
    dst = dst or {}
    w: List[Write] = []
    if (src.get("color") or "") != (dst.get("color") or ""):
        w.append(("theme", "/api/theme", {"color": src.get("color") or ""},
                  f"accent color {dst.get('color') or 'default'} -> {src.get('color') or 'default'}"))
    if isinstance(src.get("palette"), list) and src.get("palette") != dst.get("palette"):
        w.append(("theme", "/api/theme", {"palette": src["palette"]}, "palette"))
    if isinstance(src.get("opacity"), list) and src.get("opacity") != dst.get("opacity"):
        w.append(("theme", "/api/theme", {"opacity": src["opacity"]}, "opacity"))
    s_src, d_src = src.get("source"), dst.get("source")
    if s_src == "google" and (d_src != "google" or src.get("family") != dst.get("family")):
        w.append(("theme", "/api/theme", {"family": src.get("family")}, f"font -> {src.get('family')} (Google)"))
    elif s_src == "custom" and (d_src != "custom" or src.get("name") != dst.get("name")):
        w.append(("theme-font", "/api/theme/font", {"name": src.get("name")},
                  f"uploaded font -> {src.get('name')}"))
    elif s_src not in (None, "google", "custom") and d_src != s_src:
        w.append(("note", "", {}, f"font: source uses '{s_src}', target uses '{d_src}' - reset the font by hand"))
    return w


def plan_copy(src: Dict[str, Any], dst: Dict[str, Any], what: List[str]) -> List[Write]:
    w: List[Write] = []
    if "theme" in what:
        w += theme_writes(src.get("theme"), dst.get("theme"))
    if "fans" in what:
        for ch, body in sorted(src["fans"].items()):
            if ch > dst["fan_channels"]:
                continue
            if dst["fans"].get(ch) != body:
                w.append(("fans", f"/api/fans/{ch}", body, f"fan {ch}: {_fan_txt(dst['fans'].get(ch))} -> {_fan_txt(body)}"))
    if "power" in what and src.get("power") and src["power"] != dst.get("power"):
        w.append(("power", "/api/power-supply", {"rating_w": int(src["power"])},
                  f"power supply rating {dst.get('power')} W -> {src['power']} W"))
    if "protections" in what and src.get("protections") and src["protections"] != dst.get("protections"):
        if all(k in src["protections"] for k in PROTECTION_KEYS):
            w.append(("protections", "/api/protections", dict(src["protections"]),
                      f"protections {dst.get('protections')} -> {src['protections']}"))
    if "unit" in what and src.get("unit") and src["unit"] != dst.get("unit"):
        w.append(("unit", "/api/temperature-unit", {"unit": src["unit"]}, f"unit {dst.get('unit')} -> {src['unit']}"))
    if "display" in what and src.get("display") and src["display"] != dst.get("display"):
        w.append(("display", "/api/display", dict(src["display"]), f"display {dst.get('display')} -> {src['display']}"))
    if "offsets" in what and src.get("offsets") and src["offsets"] != dst.get("offsets"):
        w.append(("offsets", "/api/temperature-offsets", dict(src["offsets"]),
                  f"temperature offsets {dst.get('offsets')} -> {src['offsets']}"))
    return w


def _fan_txt(b: Optional[Dict[str, Any]]) -> str:
    if not b:
        return "?"
    if b.get("mode") == "auto":
        return f"auto {b.get('sensor')} {b.get('target_c')}C"
    return f"manual {b.get('percent')}%"


def parse_fan(spec: str) -> Dict[str, Any]:
    """'auto:asic:60', 'auto:60', or '80' (manual percent)."""
    parts = spec.lower().split(":")
    if parts[0] == "auto":
        sensor = parts[1] if len(parts) == 3 else "asic"
        target = int(parts[-1])
        if not 30 <= target <= 70:
            raise SystemExit("fan target must be 30-70 C")
        return {"mode": "auto", "sensor": sensor, "target_c": target}
    pct = int(parts[0])
    if not 20 <= pct <= 100:
        raise SystemExit("manual fan speed must be 20-100 %")
    return {"percent": pct}


# --------------------------------------------------------------- execute


def apply_writes(src_ip: Optional[str], ip: str, writes: List[Write]) -> List[str]:
    errors = []
    for kind, path, body, desc in writes:
        if kind == "note":
            continue
        try:
            if kind == "theme-font":
                data = requests.get(_url(src_ip, "/api/theme/font"), timeout=30)
                data.raise_for_status()
                r = requests.post(
                    _url(ip, "/api/theme/font"),
                    params={"name": body["name"]},
                    data=data.content,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=30,
                )
                _check(r, "POST /api/theme/font")
            else:
                put(ip, path, body)
            print(f"    ok    {desc}")
        except Exception as e:
            errors.append(f"{desc}: {e}")
            print(f"    FAIL  {desc}: {e}")
    return errors


def print_plan(st: Dict[str, Any], writes: List[Write], applying: bool = False) -> None:
    print(f"  {st['ip']:<15} {st['name'] or '?':<12} ({st['model']})")
    if not writes:
        print("    already in sync")
    for kind, _p, _b, desc in writes:
        if kind == "note":
            print(f"    note  {desc}")
        elif not applying:
            print(f"    would {desc}")


# ------------------------------------------------------------------ CLI


def cmd_show(args) -> int:
    rc = 0
    rows = []
    for ip in expand_targets(args.targets):
        try:
            rows.append(read_state(ip))
        except Exception as e:
            print(f"{ip}: {e}", file=sys.stderr)
            rc = 1
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return rc
    for s in rows:
        lv = s["live"]
        th = s["theme"] or {}
        print(f"{s['ip']:<15} {s['name'] or '?':<12} {s['model']} fw {s['version']}")
        print(f"   tuning {s['tuning'][0]} MHz / {s['tuning'][1]} mV   live {lv['hashrate_ghs']} GH/s, "
              f"ASIC {lv['asic_temp_c']} C, VR {lv['vr_temp_c']} C, Vin {lv['input_voltage_v']} V, ~{lv['device_power_w']} W")
        live_fans = {ch: (pct, rpm) for ch, pct, rpm in lv.get("fans") or []}

        def fan_line(ch, b):
            pct, rpm = live_fans.get(ch, (None, None))
            now = f"now {pct}%" if pct is not None else "now ?"
            if rpm is not None:
                now += f" / {rpm} rpm"
            return f"{ch}: {_fan_txt(b)} ({now})"

        print(f"   fans   {', '.join(fan_line(ch, b) for ch, b in sorted(s['fans'].items())) or '?'}")
        print(f"   psu    {s['power']} W   unit {s['unit']}   display {s['display']}")
        print(f"   prot   {s['protections']}   offsets {s['offsets']}")
        print(f"   theme  color {th.get('color') or 'default'}, font {(th.get('name') if th.get('source') == 'custom' else th.get('family')) or 'default'} ({th.get('source')})")
    return rc


def cmd_copy(args) -> int:
    what = [w.strip() for w in (args.what or ",".join(DEFAULT_COPY)).split(",") if w.strip()]
    bad = [w for w in what if w not in ALL_WHAT]
    if bad:
        raise SystemExit(f"unknown --what: {', '.join(bad)} (choose from {', '.join(ALL_WHAT)})")
    src = read_state(args.source)
    print(f"source {src['ip']} {src['name']} - copying: {', '.join(what)}")
    print("DRY RUN (add --apply to write)" if not args.apply else "APPLYING")
    rc = 0
    for ip in expand_targets(args.to):
        if ip == src["ip"]:
            continue
        try:
            dst = read_state(ip)
        except Exception as e:
            print(f"  {ip}: skipped - {e}")
            rc = 1
            continue
        writes = plan_copy(src, dst, what)
        print_plan(dst, writes, args.apply)
        if args.apply and writes:
            if apply_writes(src["ip"], ip, writes):
                rc = 1
    return rc


def cmd_set(args) -> int:
    print("DRY RUN (add --apply to write)" if not args.apply else "APPLYING")
    rc = 0
    for ip in expand_targets(args.targets):
        try:
            dst = read_state(ip)
        except Exception as e:
            print(f"  {ip}: skipped - {e}")
            rc = 1
            continue
        writes: List[Write] = []
        if args.fan:
            body = parse_fan(args.fan)
            chans = [args.fan_channel] if args.fan_channel else range(1, dst["fan_channels"] + 1)
            for ch in chans:
                if dst["fans"].get(ch) != body:
                    writes.append(("fans", f"/api/fans/{ch}", body, f"fan {ch}: {_fan_txt(dst['fans'].get(ch))} -> {_fan_txt(body)}"))
        if args.psu is not None and args.psu != dst.get("power"):
            if not 1 <= args.psu <= 2000:
                raise SystemExit("--psu must be 1-2000 W")
            writes.append(("power", "/api/power-supply", {"rating_w": args.psu}, f"power supply rating {dst.get('power')} W -> {args.psu} W"))
        if args.unit and args.unit.upper() != dst.get("unit"):
            writes.append(("unit", "/api/temperature-unit", {"unit": args.unit.upper()}, f"unit {dst.get('unit')} -> {args.unit.upper()}"))
        if args.theme_color is not None:
            color = args.theme_color if args.theme_color.lower() != "default" else ""
            cur = (dst.get("theme") or {}).get("color") or ""
            if color != cur:
                writes.append(("theme", "/api/theme", {"color": color}, f"accent color {cur or 'default'} -> {color or 'default'}"))
        if args.display:
            body = {"mode": args.display}
            if dst.get("display") and "rotation" in dst["display"]:
                body["rotation"] = dst["display"]["rotation"]
            if dst.get("display") != body:
                writes.append(("display", "/api/display", body, f"display {dst.get('display')} -> {body}"))
        print_plan(dst, writes, args.apply)
        if args.apply and writes and apply_writes(None, ip, writes):
            rc = 1
    return rc


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="show settings of one or more miners")
    p.add_argument("targets", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("copy", help="copy settings from one miner to others")
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--to", nargs="+", required=True)
    p.add_argument("--what", help=f"comma list, default {','.join(DEFAULT_COPY)}; also: offsets")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_copy)

    p = sub.add_parser("set", help="set explicit values on miners")
    p.add_argument("targets", nargs="+")
    p.add_argument("--fan", help="auto:asic:60 | auto:60 | 80 (manual %%)")
    p.add_argument("--fan-channel", type=int, help="only this fan channel (default: all)")
    p.add_argument("--psu", type=int, help="power supply rating in W")
    p.add_argument("--unit", choices=["C", "F", "c", "f"])
    p.add_argument("--theme-color", help="#rrggbb or 'default'")
    p.add_argument("--display", choices=["dashboard", "clock", "off"])
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_set)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
