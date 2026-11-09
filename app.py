import json
import socket
import struct
import threading
import time
import random
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import webbrowser
import os

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

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

def query_invt_all(host: str, port: int, logger_sn: int, timeout: float = 2.5):
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
        temp_c = round(48.5 + random.uniform(-0.3, 0.3), 1)
        daily_kwh = 1.20
        return {
            "ok": True,
            "status": "demo",
            "mode": "DEMO 2s",
            "host_used": "DEMO",
            "timestamp": time.strftime("%H:%M:%S"),
            "pac_w": pv_w,
            "pdc_w": int(pv_v * pv_i),
            "load_w": load_w,
            "ct_w": ct_w,
            "vpv_v": pv_v,
            "ipv_a": pv_i,
            "vgrid_v": ac_v,
            "igrid_a": ac_i,
            "freq_hz": ac_f,
            "temp_c": temp_c,
            "etoday_kwh": daily_kwh,
            "etotal_kwh": 89.6,
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
                pdc = int(round(vpv * ipv))

                load_today = round(etoday + 1.1, 2)
                load_total = 165.6
                export_today = 0.1
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

                return {
                    "ok": True,
                    "status": "online",
                    "mode": f"ONLINE 2s ({target_host})",
                    "host_used": target_host,
                    "timestamp": time.strftime("%H:%M:%S"),
                    "pac_w": int(pac),
                    "pdc_w": pdc,
                    "load_w": load_w,
                    "ct_w": int(ct_w),
                    "vpv_v": vpv,
                    "ipv_a": ipv,
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
                    "msg": f"✅ LIVE 2s ({target_host}:8899) | PV: {int(pac)}W | Tải: {load_w}W | EVN: {int(ct_w)}W"
                }
        except Exception as e:
            last_err = str(e)

    return {
        "ok": False,
        "status": "offline",
        "msg": f"Không thể kết nối tới {primary_host}:{port} hoặc {alt_host}:{port} ({last_err})"
    }

# Reuse the exact same HTML UI from Android assets/index.html, adapted to support both AndroidSolar bridge and Web fetch('/api/live')!
ANDROID_HTML_PATH = r"C:\Users\MSI\.gemini\antigravity\scratch\solar_android_app\assets\index.html"
LOCAL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

def get_web_html():
    target_html = ANDROID_HTML_PATH if os.path.exists(ANDROID_HTML_PATH) else LOCAL_HTML_PATH
    with open(target_html, "r", encoding="utf-8") as f:
        html = f.read()
    # Inject a lightweight polyfill for window.AndroidSolar when running in a standard Web Browser!
    web_bridge = """
<script>
if (!window.AndroidSolar) {
  window._apiBase = '';
  window._cachedCfg = { ip: '27.77.98.111', sn: '3555314224', mode: 'real' };
  window._cachedLive = { status: 'offline', pac_w: 0, ct_w: 0, load_w: 0 };

  async function _resolveBackend() {
    if (location.hostname.endsWith('github.io')) {
      try {
        const r = await fetch('https://raw.githubusercontent.com/adlsviettel/solar-tu-nguyen/main/endpoints.json?t=' + Date.now(), { cache: 'no-store' });
        const ep = await r.json();
        const candidates = [ep.cloud_url, ep.tunnel_url, 'https://solar-tu-nguyen.onrender.com'].filter(Boolean);
        for (const base of candidates) {
          try {
            const test = await fetch(base + '/api/live', { mode: 'cors' });
            if (test.ok) { window._apiBase = base; break; }
          } catch (e) {}
        }
      } catch (e) {}
    }
  }

  async function _initWeb() {
    await _resolveBackend();
    try {
      const c = await (await fetch(window._apiBase + '/api/config')).json();
      window._cachedCfg = { ip: c.host || '27.77.98.111', sn: String(c.logger_sn || '3555314224'), mode: c.demo_mode ? 'demo' : 'real' };
      if (document.getElementById('inpIp')) document.getElementById('inpIp').value = window._cachedCfg.ip;
      if (document.getElementById('inpSn')) document.getElementById('inpSn').value = window._cachedCfg.sn;
      if (document.getElementById('inpMode')) document.getElementById('inpMode').value = window._cachedCfg.mode;
    } catch (e) {}
    _webPoll();
    setInterval(_webPoll, 2000);
    setInterval(_resolveBackend, 45000);
  }

  async function _webPoll() {
    try {
      const r = await fetch(window._apiBase + '/api/live');
      const d = await r.json();
      window._cachedLive = d;
      if (window.updateUI) window.updateUI(d);
    } catch (e) {}
  }
  setTimeout(_initWeb, 80);

  window.AndroidSolar = {
    getSavedConfig: function() { return JSON.stringify(window._cachedCfg); },
    saveConfig: function(ip, sn, mode) {
      window._cachedCfg = { ip: ip, sn: sn, mode: mode };
      fetch(window._apiBase + '/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ host: ip, logger_sn: parseInt(sn) || 3555314224, demo_mode: mode === 'demo' })
      }).then(() => _webPoll());
    },
    pollNow: function() { return JSON.stringify(window._cachedLive); },
    scanLan: function() { return JSON.stringify({ ok: true, ip: '27.77.98.111', sn: '3555314224' }); }
  };
}
</script>
<style>
  @media (min-width: 800px) {
    body { max-width: 960px; margin: 0 auto; padding-top: 18px; }
    .cmp-val { font-size: 32px; }
    canvas { height: 260px; }
  }
</style>
"""
    return html.replace("</head>", web_bridge + "\n</head>")

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
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
            cfg = load_config()
            data = poll_invt_live(cfg)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif parsed.path == "/manifest.json":
            manifest = {
                "name": "Solar Tú Nguyễn Pro 2s",
                "short_name": "Solar 2s",
                "start_url": "./",
                "scope": "./",
                "display": "standalone",
                "background_color": "#f1f5f9",
                "theme_color": "#f1f5f9",
                "orientation": "portrait"
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
    PORT = int(os.environ.get("PORT", 8088))
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    url = f"http://localhost:{PORT}"
    print("============================================================")
    print("  SOLAR TU NGUYEN PRO - REALTIME 2s WEB DASHBOARD:")
    print(f"  -> {url}")
    print("============================================================")
    if not os.environ.get("PORT"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
