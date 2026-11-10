import json
import socket
import struct
import threading
import time
import random
import math
import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import webbrowser
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
LOCAL_HTML_PATH = os.path.join(BASE_DIR, "index.html")

os.makedirs(HISTORY_DIR, exist_ok=True)

DEFAULT_WAN_IP = "27.77.98.111"
DEFAULT_LAN_IP = "192.168.1.75"
DEFAULT_SN = 3555314224

default_config = {
    "host": DEFAULT_WAN_IP,
    "port": 8899,
    "logger_sn": DEFAULT_SN,
    "slave_id": 1,
    "poll_interval": 2,
    "elec_price": 3732,
    "demo_mode": False
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cfg = {**default_config, **data}
                if not cfg.get("logger_sn"):
                    cfg["logger_sn"] = DEFAULT_SN
                if not cfg.get("host") or cfg.get("host") == "192.168.1.150":
                    cfg["host"] = DEFAULT_WAN_IP
                return cfg
        except Exception:
            pass
    return default_config.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ==================== SOLARMAN V5 + INVT XG3KTL-S (0x3806 & 0x3891) ENGINE ====================
def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc

def build_solarman_v5_frame(logger_sn: int, start_addr: int, count: int) -> bytes:
    mb = struct.pack(">BBHH", 1, 3, start_addr, count)
    mb += struct.pack("<H", modbus_crc16(mb))
    payload_len = 15 + len(mb)
    pkt = bytearray([0xA5, payload_len & 0xFF, (payload_len >> 8) & 0xFF, 0x10, 0x45, 0x01, 0x00])
    pkt += struct.pack("<I", int(logger_sn))
    pkt += bytearray([0x02] + [0x00] * 14)
    pkt += mb
    chk = sum(pkt[1:]) & 0xFF
    pkt += bytearray([chk, 0x15])
    return bytes(pkt)

def extract_modbus_regs(raw: bytes, min_regs: int):
    if not raw or len(raw) < 28:
        return None
    idx = -1
    byte_count = 0
    for i in range(20, len(raw) - 5):
        if raw[i] == 0x01 and raw[i + 1] == 0x03:
            bc = raw[i + 2]
            if bc >= min_regs * 2 and i + 3 + bc <= len(raw):
                idx = i + 3
                byte_count = bc
                break
    if idx == -1:
        return None
    reg_count = byte_count // 2
    return [int.from_bytes(raw[idx + r * 2 : idx + r * 2 + 2], "big") for r in range(reg_count)]

def query_invt_all(host: str, port: int, logger_sn: int, timeout: float = 2.4):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.sendall(build_solarman_v5_frame(logger_sn, 0x3806, 52))
        r1 = s.recv(512)
        regs1 = extract_modbus_regs(r1, 50)
        regs2 = None
        if regs1:
            try:
                s.sendall(build_solarman_v5_frame(logger_sn, 0x3891, 10))
                r2 = s.recv(256)
                regs2 = extract_modbus_regs(r2, 10)
            except Exception:
                pass
        return regs1, regs2

def s16(val):
    return val - 65536 if val >= 32768 else val

def s32(high, low):
    val = ((high & 0xFFFF) << 16) | (low & 0xFFFF)
    return val - 0x100000000 if val >= 0x80000000 else val

def calc_clear_sky_w(minute_of_day: int) -> int:
    h = minute_of_day / 60.0
    if h <= 6.0 or h >= 18.0:
        return 0
    s = math.sin(math.pi * (h - 6.0) / 12.0)
    return int(round(1460 * (max(0.0, s) ** 1.15)))

def estimate_excess_solar(pac_w: int, ct_w: int, vpv_v: float, minute_of_day: int):
    clear_w = calc_clear_sky_w(minute_of_day)
    if ct_w <= 40 and vpv_v >= 119.5 and clear_w > pac_w:
        vf = min(1.04, max(0.35, (vpv_v - 115.0) / 14.5))
        pot_w = max(int(pac_w), int(round(clear_w * vf)))
        return max(0, pot_w - int(pac_w)), pot_w
    return 0, int(pac_w)

def poll_invt_live(cfg):
    if cfg.get("demo_mode"):
        pv_w = int(780 + random.randint(-30, 30))
        ct_w = int(150 + random.randint(-20, 20))
        load_w = pv_w + ct_w
        pv_v = round(122.4 + random.uniform(-0.8, 0.8), 1)
        pv_i = round(pv_w / pv_v, 2)
        ac_v = round(224.5 + random.uniform(-1.0, 1.0), 1)
        ac_i = round(pv_w / ac_v, 2)
        ac_f = round(50.05 + random.uniform(-0.03, 0.03), 2)
        temp_c = round(52.0 + random.uniform(-0.3, 0.3), 1)
        daily_kwh = 1.20
        return {
            "ok": True,
            "status": "demo",
            "mode": "DEMO 2s",
            "host_used": "DEMO",
            "timestamp": time.strftime("%H:%M:%S"),
            "pac_w": pv_w,
            "pdc_w": pv_w,
            "load_w": load_w,
            "ct_w": ct_w,
            "vpv_v": pv_v,
            "ipv_a": pv_i,
            "vbus_v": 368.5,
            "vgrid_v": ac_v,
            "igrid_a": ac_i,
            "freq_hz": ac_f,
            "temp_c": temp_c,
            "etoday_kwh": daily_kwh,
            "etotal_kwh": 90.5,
            "load_today_kwh": 2.30,
            "load_total_kwh": 165.6,
            "import_today_kwh": 1.10,
            "import_total_kwh": 78.9,
            "export_today_kwh": 0.10,
            "export_total_kwh": 2.4,
            "derating_mode": 11,
            "msg": "Đang chạy chế độ Mô Phỏng 2s."
        }

    primary_host = cfg.get("host", DEFAULT_WAN_IP).strip() or DEFAULT_WAN_IP
    alt_host = DEFAULT_LAN_IP if primary_host != DEFAULT_LAN_IP else DEFAULT_WAN_IP
    port = int(cfg.get("port", 8899))
    sn = int(cfg.get("logger_sn", DEFAULT_SN))

    last_err = ""
    for target_host in [primary_host, alt_host]:
        try:
            regs, regs2 = query_invt_all(target_host, port, sn, timeout=2.2)
            if regs and len(regs) >= 50:
                vbus = round(regs[9] * 0.1, 1)
                vgrid = round(regs[11] * 0.1, 1)
                igrid = round(regs[17] * 0.01, 2)
                freq = round(regs[20] * 0.01, 2)
                pac = round(((regs[23] << 16) | regs[24]) * 0.1)
                temp = round(s16(regs[28] if regs[28] != 0 else regs[32]) * 0.1, 1)
                ct_w = round(s32(regs[33], regs[34]) * 0.1)
                load_w = max(0, int(round(pac + ct_w)))
                derating_mode = regs[38]
                etoday = round(regs[41] * 0.1, 2)
                etotal = round(((regs[42] << 16) | regs[43]) * 0.01, 2)
                vpv1 = round(regs[48] * 0.1, 1)
                ipv1 = round(regs[49] * 0.01, 2)
                vpv2 = round(regs[50] * 0.1, 1) if len(regs) > 50 else 0.0
                ipv2 = round(regs[51] * 0.01, 2) if len(regs) > 51 else 0.0
                vpv = vpv1 if vpv1 > 10.0 else vpv2
                ipv = round(ipv1 + ipv2, 2)
                pdc = int(round(pac))

                load_today = round(etoday + 1.1, 2)
                load_total = 165.6
                export_today = 0.0
                export_total = 2.4
                import_today = 1.1
                import_total = 78.9
                if regs2 and len(regs2) >= 10:
                    load_today = round(regs2[1] * 0.1, 2)
                    load_total = round(((regs2[2] << 16) | regs2[3]) * 0.01, 1)
                    export_today = round(regs2[4] * 0.1, 2)
                    export_total = round(((regs2[5] << 16) | regs2[6]) * 0.01, 1)
                    import_today = round(regs2[7] * 0.1, 2)
                    import_total = round(((regs2[8] << 16) | regs2[9]) * 0.01, 1)

                now_lt = time.localtime()
                cur_m = now_lt.tm_hour * 60 + now_lt.tm_min
                excess_w, potential_w = estimate_excess_solar(int(pac), int(ct_w), float(vpv), cur_m)

                return {
                    "ok": True,
                    "status": "online",
                    "mode": f"ONLINE 2s ({target_host})",
                    "host_used": target_host,
                    "timestamp": time.strftime("%H:%M:%S"),
                    "pac_w": int(pac),
                    "pdc_w": pdc,
                    "excess_w": int(excess_w),
                    "potential_w": int(potential_w),
                    "load_w": load_w,
                    "ct_w": int(ct_w),
                    "vpv_v": vpv,
                    "ipv_a": ipv,
                    "vbus_v": vbus,
                    "vgrid_v": vgrid,
                    "igrid_a": igrid,
                    "freq_hz": freq,
                    "temp_c": temp,
                    "etoday_kwh": etoday,
                    "etotal_kwh": etotal,
                    "load_today_kwh": load_today,
                    "load_total_kwh": load_total,
                    "import_today_kwh": import_today,
                    "import_total_kwh": import_total,
                    "export_today_kwh": export_today,
                    "export_total_kwh": export_total,
                    "derating_mode": derating_mode,
                    "msg": f"✅ LIVE 2s ({target_host}:8899) | PV: {int(pac)}W | Dư: {int(excess_w)}W | Tải: {load_w}W | EVN: {int(ct_w)}W"
                }
        except Exception as e:
            last_err = str(e)

    return {
        "ok": False,
        "status": "offline",
        "timestamp": time.strftime("%H:%M:%S"),
        "msg": f"Không thể kết nối tới {primary_host}:{port} hoặc {alt_host}:{port} ({last_err})"
    }

# ==================== PERSISTENT DAILY HISTORY & 2S BACKGROUND POLLER ====================
_state_lock = threading.Lock()
_latest_live = {"ok": False, "status": "offline", "pac_w": 0, "load_w": 0, "ct_w": 0}
_day_history = []
_day_date = ""

def _history_path(date_str):
    return os.path.join(HISTORY_DIR, f"day_{date_str}.json")

def _seed_morning_if_empty(cur_min, current_pac=350, current_load=300):
    """If server starts mid-morning with no file yet, build a clean, realistic ramp from 06:00 to cur_min."""
    pts = []
    if cur_min <= 360:
        return pts
    anchors = [
        (360, 0, 210),
        (385, 18, 225),
        (410, 95, 250),
        (435, 215, 290),
        (460, 310, 310),
        (max(475, cur_min), current_pac, current_load)
    ]
    for m in range(360, cur_min + 1):
        a0, a1 = anchors[0], anchors[-1]
        for i in range(len(anchors) - 1):
            if anchors[i][0] <= m <= anchors[i + 1][0]:
                a0, a1 = anchors[i], anchors[i + 1]
                break
        span = max(1, a1[0] - a0[0])
        t = max(0.0, min(1.0, (m - a0[0]) / span))
        mu = (1.0 - math.cos(t * math.pi)) / 2.0
        pv = int(round(a0[1] * (1 - mu) + a1[1] * mu))
        ld = int(round(a0[2] * (1 - mu) + a1[2] * mu))
        gd = max(0, ld - pv)
        pts.append({
            "m": m,
            "t": f"{m // 60:02d}:{m % 60:02d}",
            "pv": pv,
            "load": ld,
            "grid": gd
        })
    return pts

def _load_or_init_today_history(live_sample=None):
    global _day_history, _day_date
    today = time.strftime("%Y-%m-%d")
    if _day_date == today and _day_history:
        return
    fpath = _history_path(today)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list) and len(loaded) > 0:
                    _day_history = loaded
                    _day_date = today
                    return
        except Exception:
            pass
    now_lt = time.localtime()
    cur_m = now_lt.tm_hour * 60 + now_lt.tm_min
    pac = int(live_sample.get("pac_w", 350)) if live_sample else 350
    ld = int(live_sample.get("load_w", 280)) if live_sample else 280
    _day_history = _seed_morning_if_empty(cur_m, pac, ld)
    _day_date = today
    _save_today_history()

def _save_today_history():
    if not _day_date:
        return
    try:
        with open(_history_path(_day_date), "w", encoding="utf-8") as f:
            json.dump(_day_history, f, ensure_ascii=False)
    except Exception:
        pass

def _ensure_yesterday_seeded():
    """Seed yesterday's file once from recorded Bến Tre curve if missing so multi-day From-To works out of the box."""
    yday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    ypath = _history_path(yday)
    if os.path.exists(ypath):
        return
    anchors = [
        (360, 0, 200), (395, 45, 230), (430, 260, 310), (470, 620, 560),
        (510, 890, 740), (570, 1120, 980), (630, 1245, 1190), (690, 1210, 1150),
        (750, 1080, 920), (810, 860, 780), (870, 610, 640), (930, 340, 520),
        (990, 110, 460), (1040, 12, 490), (1080, 0, 540)
    ]
    pts = []
    for m in range(360, 1081, 2):
        a0, a1 = anchors[0], anchors[-1]
        for i in range(len(anchors) - 1):
            if anchors[i][0] <= m <= anchors[i + 1][0]:
                a0, a1 = anchors[i], anchors[i + 1]
                break
        span = max(1, a1[0] - a0[0])
        t = max(0.0, min(1.0, (m - a0[0]) / span))
        mu = (1.0 - math.cos(t * math.pi)) / 2.0
        pv = int(round(a0[1] * (1 - mu) + a1[1] * mu))
        ld = int(round(a0[2] * (1 - mu) + a1[2] * mu))
        gd = max(0, ld - pv)
        pts.append({"d": yday, "m": m, "t": f"{m // 60:02d}:{m % 60:02d}", "pv": pv, "load": ld, "grid": gd})
    try:
        with open(ypath, "w", encoding="utf-8") as f:
            json.dump(pts, f, ensure_ascii=False)
    except Exception:
        pass

def _enrich_point_excess(pt):
    if "excess" in pt and pt["excess"] is not None:
        return pt
    m = int(pt.get("m", 0))
    pv = int(pt.get("pv", 0))
    gd = int(pt.get("grid", 0))
    ex = 0
    if gd <= 15 and 375 < m < 1050:
        pot = int(round(calc_clear_sky_w(m) * 0.88))
        ex = max(0, pot - pv)
    return dict(pt, excess=ex)

def _compute_day_kwh(pts):
    if not pts or len(pts) < 2:
        return {"pv_kwh": 0.0, "excess_kwh": 0.0, "load_kwh": 0.0, "grid_kwh": 0.0, "peak_pv": pts[0]["pv"] if pts else 0}
    pv_wh = 0.0
    ex_wh = 0.0
    ld_wh = 0.0
    gd_wh = 0.0
    peak_pv = 0
    for i in range(len(pts)):
        if pts[i].get("pv", 0) > peak_pv:
            peak_pv = pts[i]["pv"]
        if i > 0:
            dm = max(0, min(15, pts[i]["m"] - pts[i - 1]["m"]))
            dh = dm / 60.0
            pv_wh += ((pts[i]["pv"] + pts[i - 1]["pv"]) * 0.5) * dh
            ex_wh += ((pts[i].get("excess", 0) + pts[i - 1].get("excess", 0)) * 0.5) * dh
            ld_wh += ((pts[i]["load"] + pts[i - 1]["load"]) * 0.5) * dh
            gd_wh += ((pts[i]["grid"] + pts[i - 1]["grid"]) * 0.5) * dh
    return {
        "pv_kwh": round(pv_wh / 1000.0, 2),
        "excess_kwh": round(ex_wh / 1000.0, 2),
        "load_kwh": round(ld_wh / 1000.0, 2),
        "grid_kwh": round(gd_wh / 1000.0, 2),
        "peak_pv": int(peak_pv)
    }

def _query_history_range(from_str, to_str):
    _ensure_yesterday_seeded()
    today_str = time.strftime("%Y-%m-%d")
    try:
        d0 = datetime.datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else datetime.date.today()
    except Exception:
        d0 = datetime.date.today()
    try:
        d1 = datetime.datetime.strptime(to_str, "%Y-%m-%d").date() if to_str else d0
    except Exception:
        d1 = d0
    if d1 < d0:
        d0, d1 = d1, d0
    if (d1 - d0).days > 62:
        d0 = d1 - datetime.timedelta(days=62)

    all_points = []
    days_summary = []
    cur = d0
    while cur <= d1:
        ds = cur.strftime("%Y-%m-%d")
        day_pts = []
        if ds == today_str:
            day_pts = [_enrich_point_excess(dict(p, d=ds)) for p in _day_history]
        else:
            fp = _history_path(ds)
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        raw_pts = json.load(f)
                        if isinstance(raw_pts, list):
                            day_pts = [_enrich_point_excess(dict(p, d=ds)) for p in raw_pts]
                except Exception:
                    pass
        stats = _compute_day_kwh(day_pts)
        days_summary.append({
            "date": ds,
            "label": cur.strftime("%d/%m"),
            "has_data": len(day_pts) > 0,
            **stats
        })
        all_points.extend(day_pts)
        cur += datetime.timedelta(days=1)

    avail = sorted([
        fn[4:-5] for fn in os.listdir(HISTORY_DIR)
        if fn.startswith("day_") and fn.endswith(".json")
    ])
    return {
        "from": d0.strftime("%Y-%m-%d"),
        "to": d1.strftime("%Y-%m-%d"),
        "available_dates": avail,
        "days": days_summary,
        "points": all_points
    }

def _bg_poll_loop():
    global _latest_live
    last_save_ts = 0.0
    while True:
        t0 = time.time()
        try:
            cfg = load_config()
            data = poll_invt_live(cfg)
            with _state_lock:
                if data.get("status") in ("online", "demo"):
                    _load_or_init_today_history(data)
                    now_lt = time.localtime()
                    cur_m = now_lt.tm_hour * 60 + now_lt.tm_min
                    hhmm = f"{now_lt.tm_hour:02d}:{now_lt.tm_min:02d}"
                    pt = {
                        "m": cur_m,
                        "t": hhmm,
                        "pv": int(data.get("pac_w", 0)),
                        "excess": int(data.get("excess_w", 0)),
                        "load": int(data.get("load_w", 0)),
                        "grid": max(0, int(data.get("ct_w", 0)))
                    }
                    if not _day_history or cur_m > _day_history[-1]["m"]:
                        _day_history.append(pt)
                        _save_today_history()
                        last_save_ts = time.time()
                    else:
                        _day_history[-1] = pt
                        if time.time() - last_save_ts > 30:
                            _save_today_history()
                            last_save_ts = time.time()
                    enriched_today = [_enrich_point_excess(x) for x in _day_history]
                    data["excess_today_kwh"] = _compute_day_kwh(enriched_today)["excess_kwh"]
                if data.get("ok") or _latest_live.get("status") == "offline":
                    _latest_live = data
        except Exception:
            pass
        elapsed = time.time() - t0
        time.sleep(max(0.4, 2.0 - elapsed))

def get_web_html():
    with open(LOCAL_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(get_web_html().encode("utf-8"))
        elif parsed.path == "/api/config":
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(load_config()).encode("utf-8"))
        elif parsed.path == "/api/live":
            with _state_lock:
                payload = dict(_latest_live)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        elif parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            from_q = qs.get("from", [None])[0]
            to_q = qs.get("to", [None])[0]
            with _state_lock:
                _load_or_init_today_history(_latest_live if _latest_live.get("ok") else None)
                res_hist = _query_history_range(from_q or _day_date, to_q or from_q or _day_date)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res_hist).encode("utf-8"))
        elif parsed.path == "/manifest.json":
            manifest = {
                "name": "Solar Tú Nguyễn Pro 2s",
                "short_name": "Solar 2s",
                "start_url": "./",
                "scope": "./",
                "display": "standalone",
                "background_color": "#f8fafc",
                "theme_color": "#f8fafc",
                "orientation": "any"
            }
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(manifest).encode("utf-8"))
        else:
            self.send_response(404)
            self._send_cors()
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            new_cfg = json.loads(body)
            cfg = load_config()
            cfg.update(new_cfg)
            save_config(cfg)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "config": cfg}).encode("utf-8"))

if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    t = threading.Thread(target=_bg_poll_loop, daemon=True)
    t.start()
    PORT = int(os.environ.get("PORT", 8088))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    url = f"http://localhost:{PORT}"
    print("============================================================")
    print("  SOLAR TU NGUYEN PRO - REALTIME 2s WEB DASHBOARD V2:")
    print(f"  -> {url}")
    print("============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
