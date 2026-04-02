import threading
import socket
import time
import json
import os
import base64
import ipaddress
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ── Config ───────────────────────────────────────────────────────────────────
PORT        = 8765
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DEVICES_FILE = os.path.join(BASE_DIR, "cast_devices.json")
IMAGE_PATH   = os.path.join(BASE_DIR, "cast_current_image")   # no extension; we store it raw

import pychromecast
from pychromecast.controllers.dashcast import DashCastController

# ── Local IP ─────────────────────────────────────────────────────────────────
_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_s.connect(("192.168.4.1", 80))
LOCAL_IP = _s.getsockname()[0]
_s.close()

# ── Device persistence ────────────────────────────────────────────────────────
DEFAULT_DEVICES = [
    {"ip": "192.168.4.218", "name": "Kitchen display",    "enabled": True},
    {"ip": "192.168.4.42",  "name": "Bedroom mini",       "enabled": True},
    {"ip": "192.168.4.35",  "name": "LenovoCD-24502F1845","enabled": True},
    {"ip": "192.168.5.133", "name": "Hotel Smart Clock",  "enabled": True},
    {"ip": "192.168.5.154", "name": "1st Floor TV 2",     "enabled": True},
]

def load_devices():
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return list(DEFAULT_DEVICES)

def save_devices():
    with open(DEVICES_FILE, "w") as f:
        json.dump(devices, f, indent=2)

devices_lock = threading.Lock()
devices = load_devices()

# ── Cast state ────────────────────────────────────────────────────────────────
state      = {"message": "", "version": 0, "has_image": False, "image_type": ""}
state_lock = threading.Lock()

image_lock = threading.Lock()

# ── Scan state ────────────────────────────────────────────────────────────────
scan = {"running": False, "progress": 0, "results": []}
scan_lock = threading.Lock()

# ── Cast logic ────────────────────────────────────────────────────────────────
def cast_all(display_url):
    with devices_lock:
        active = [d for d in devices if d.get("enabled", True)]

    def cast_one(d):
        ip, name = d["ip"], d["name"]
        try:
            cc = pychromecast.get_chromecast_from_host((ip, 8009, None, None, name))
            cc.wait(timeout=10)
            cc.quit_app()
            time.sleep(1)
            dash = DashCastController()
            cc.register_handler(dash)
            dash.load_url(display_url, force=True, reload_seconds=0)
            print(f"  [CAST] {name}")
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")

    threads = [threading.Thread(target=cast_one, args=(d,), daemon=True) for d in active]
    for t in threads: t.start()
    for t in threads: t.join()
    print("Cast complete.")

# ── Network scanner ───────────────────────────────────────────────────────────
def run_scan():
    with scan_lock:
        scan["running"]  = True
        scan["progress"] = 0
        scan["results"]  = []

    subnets = ["192.168.4.0/24", "192.168.5.0/24", "192.168.6.0/24"]
    all_ips = []
    for s in subnets:
        all_ips.extend(ipaddress.IPv4Network(s).hosts())

    total   = len(all_ips)
    checked = [0]
    found   = []
    mu      = threading.Lock()
    sem     = threading.Semaphore(150)   # max concurrent connections

    def check(ip_obj):
        ip = str(ip_obj)
        try:
            with sem:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.4)
                open_ = sock.connect_ex((ip, 8009)) == 0
                sock.close()
            if open_:
                try:
                    req  = urllib.request.urlopen(
                        f"http://{ip}:8008/setup/eureka_info?params=name,device_info",
                        timeout=2)
                    data = json.loads(req.read())
                    entry = {
                        "ip":    ip,
                        "name":  data.get("name", ip),
                        "model": data.get("device_info", {}).get("model_name", "Unknown"),
                    }
                except Exception:
                    entry = {"ip": ip, "name": ip, "model": "Unknown"}
                with mu:
                    found.append(entry)
        except Exception:
            pass
        finally:
            with mu:
                checked[0] += 1
                with scan_lock:
                    scan["progress"] = int(checked[0] / total * 100)

    threads = [threading.Thread(target=check, args=(ip,), daemon=True) for ip in all_ips]
    for t in threads: t.start()
    for t in threads: t.join()

    with scan_lock:
        scan["running"]  = False
        scan["progress"] = 100
        scan["results"]  = sorted(found, key=lambda x: x["ip"])

    print(f"Scan complete — {len(found)} device(s) found.")

# ── HTML ──────────────────────────────────────────────────────────────────────
CONTROL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cast Control</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0d;color:#eee;min-height:100vh;padding:24px}
h1{font-size:1.5rem;margin-bottom:4px}
.sub{color:#666;font-size:.85rem;margin-bottom:28px}
.card{background:#1a1a1a;border-radius:14px;padding:28px;margin-bottom:20px}
.card h2{font-size:.75rem;text-transform:uppercase;letter-spacing:1px;color:#555;margin-bottom:16px}
textarea{width:100%;min-height:110px;padding:12px;font-size:1rem;background:#111;color:#eee;border:2px solid #2a2a2a;border-radius:8px;resize:vertical;outline:none;font-family:inherit}
textarea:focus{border-color:#444}

/* Image upload */
.img-zone{border:2px dashed #333;border-radius:10px;padding:28px;text-align:center;cursor:pointer;transition:border-color .2s,background .2s;margin-top:14px;position:relative}
.img-zone:hover,.img-zone.drag{border-color:#555;background:#111}
.img-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.img-zone .label{font-size:.9rem;color:#555;pointer-events:none}
.img-zone .label span{display:block;font-size:1.5rem;margin-bottom:6px}
#img-preview-wrap{margin-top:14px;display:none}
#img-preview{max-width:100%;max-height:220px;border-radius:8px;object-fit:contain;border:1px solid #2a2a2a}
.img-preview-actions{display:flex;gap:8px;margin-top:8px}

/* Buttons */
.actions{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
button{padding:12px 20px;font-size:.9rem;font-weight:600;border:none;border-radius:8px;cursor:pointer;transition:opacity .15s,transform .1s;white-space:nowrap}
button:active{transform:scale(.97)}
button:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:#fff;color:#000;flex:1}
.btn-blue{background:#1e90ff;color:#fff;flex:1}
.btn-muted{background:#252525;color:#888}
.btn-red{background:#3a1010;color:#f66}
.btn-green{background:#103a10;color:#6f6}
.btn-sm{padding:7px 14px;font-size:.8rem}

/* Status */
.status{margin-top:14px;padding:10px 14px;border-radius:7px;font-size:.85rem;display:none}
.ok{background:#0f2a0f;color:#6f6;display:block}
.err{background:#2a0f0f;color:#f66;display:block}
.info{background:#0f1e2a;color:#6af;display:block}

/* Preview */
.preview-label{font-size:.7rem;text-transform:uppercase;letter-spacing:1px;color:#444;margin-top:20px;margin-bottom:6px}
.preview-box{background:#000;border-radius:10px;min-height:90px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px;gap:14px;overflow:hidden}
#preview-img{max-width:100%;max-height:180px;border-radius:4px;object-fit:contain;display:none}
#preview-txt{color:#fff;font-size:clamp(13px,2.5vw,22px);font-weight:700;text-align:center;word-break:break-word}

/* Devices */
.device-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #1f1f1f}
.device-row:last-child{border-bottom:none}
.device-info{flex:1;min-width:0}
.device-name{font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.device-ip{font-size:.75rem;color:#555}
.device-model{font-size:.72rem;color:#444;margin-top:1px}
.toggle{position:relative;width:38px;height:22px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#2a2a2a;border-radius:22px;cursor:pointer;transition:.2s}
.toggle input:checked+.toggle-slider{background:#1e90ff}
.toggle-slider:before{content:'';position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
.toggle input:checked+.toggle-slider:before{transform:translateX(16px)}

/* Scan */
.scan-result{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #1a1a1a}
.scan-result:last-child{border-bottom:none}
.scan-result .info{flex:1}
.already{font-size:.72rem;color:#555;font-style:italic}
.progress-bar{width:100%;height:4px;background:#222;border-radius:2px;margin:10px 0;display:none}
.progress-bar-fill{height:100%;background:#1e90ff;border-radius:2px;transition:width .3s}
#scan-results{margin-top:14px}
</style>
</head>
<body>
<h1>📡 Cast Control</h1>
<p class="sub">Send messages and images to all your Cast devices.</p>

<!-- Cast card -->
<div class="card">
  <h2>Message &amp; Image</h2>
  <textarea id="msg" placeholder="Type a message… (Ctrl+Enter to send)"></textarea>

  <div class="img-zone" id="drop-zone">
    <input type="file" id="file-input" accept="image/*">
    <div class="label"><span>🖼️</span>Click or drag an image here (optional)</div>
  </div>

  <div id="img-preview-wrap">
    <img id="img-preview" src="" alt="preview">
    <div class="img-preview-actions">
      <button class="btn-muted btn-sm" onclick="clearImage()">Remove image</button>
    </div>
  </div>

  <div class="actions">
    <button class="btn-primary" id="sendBtn" onclick="sendMsg()">Send &amp; Cast</button>
    <button class="btn-blue"    id="recastBtn" onclick="recast()">Re-Cast</button>
    <button class="btn-muted"   onclick="clearAll()">Clear</button>
  </div>
  <div id="cast-status" class="status"></div>

  <div class="preview-label">Preview</div>
  <div class="preview-box">
    <img id="preview-img" src="" alt="">
    <div id="preview-txt">Your message will appear here</div>
  </div>
</div>

<!-- Devices card -->
<div class="card">
  <h2>Devices</h2>
  <div id="device-list"></div>
  <div class="actions" style="margin-top:16px">
    <button class="btn-blue" id="scanBtn" onclick="startScan()">🔍 Scan Network</button>
  </div>
  <div class="progress-bar" id="scan-bar"><div class="progress-bar-fill" id="scan-fill"></div></div>
  <div id="scan-results"></div>
</div>

<script>
let imageData   = null;   // base64 data URL
let imageType   = null;   // mime type

// ── Preview ──────────────────────────────────────────────────────────────────
document.getElementById('msg').addEventListener('input', updatePreview);
document.getElementById('msg').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendMsg();
});

function updatePreview() {
  const txt = document.getElementById('msg').value;
  document.getElementById('preview-txt').textContent = txt || '';
}

// ── Image upload ──────────────────────────────────────────────────────────────
const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

fileInput.addEventListener('change', () => loadFile(fileInput.files[0]));

dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drag'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag');
  if (e.dataTransfer.files[0]) loadFile(e.dataTransfer.files[0]);
});

function loadFile(file) {
  if (!file || !file.type.startsWith('image/')) return;
  const reader = new FileReader();
  reader.onload = ev => {
    imageData = ev.target.result;
    imageType = file.type;
    document.getElementById('img-preview').src       = imageData;
    document.getElementById('img-preview-wrap').style.display = 'block';
    document.getElementById('preview-img').src       = imageData;
    document.getElementById('preview-img').style.display     = 'block';
    dropZone.style.display = 'none';
  };
  reader.readAsDataURL(file);
}

function clearImage() {
  imageData = imageType = null;
  document.getElementById('img-preview-wrap').style.display = 'none';
  document.getElementById('preview-img').style.display      = 'none';
  document.getElementById('preview-img').src                = '';
  document.getElementById('img-preview').src                = '';
  document.getElementById('file-input').value               = '';
  dropZone.style.display = 'block';
}

function clearAll() {
  document.getElementById('msg').value = '';
  updatePreview();
  clearImage();
}

// ── Status helper ─────────────────────────────────────────────────────────────
function setStatus(id, msg, type) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.className   = 'status ' + type;
}

function setBusy(busy) {
  ['sendBtn','recastBtn'].forEach(id => document.getElementById(id).disabled = busy);
}

// ── Send ──────────────────────────────────────────────────────────────────────
async function sendMsg() {
  const message = document.getElementById('msg').value.trim();
  if (!message && !imageData) { setStatus('cast-status','Enter a message or choose an image.','err'); return; }
  setBusy(true);
  setStatus('cast-status','Casting to all devices…','info');
  try {
    const r = await fetch('/send', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ message, image: imageData, image_type: imageType })
    });
    await r.json();
    setStatus('cast-status','Message cast to all devices!','ok');
  } catch(e) {
    setStatus('cast-status','Error: ' + e.message,'err');
  } finally {
    setBusy(false);
  }
}

async function recast() {
  setBusy(true);
  setStatus('cast-status','Re-casting…','info');
  try {
    await fetch('/recast', { method: 'POST' });
    setStatus('cast-status','Re-cast complete!','ok');
  } catch(e) {
    setStatus('cast-status','Error: ' + e.message,'err');
  } finally {
    setBusy(false);
  }
}

// ── Device list ───────────────────────────────────────────────────────────────
async function loadDevices() {
  const r = await fetch('/devices');
  const devs = await r.json();
  const el = document.getElementById('device-list');
  if (!devs.length) { el.innerHTML = '<p style="color:#444;font-size:.85rem">No devices configured.</p>'; return; }
  el.innerHTML = devs.map((d,i) => `
    <div class="device-row">
      <label class="toggle">
        <input type="checkbox" ${d.enabled ? 'checked' : ''} onchange="toggleDevice(${i}, this.checked)">
        <span class="toggle-slider"></span>
      </label>
      <div class="device-info">
        <div class="device-name">${esc(d.name)}</div>
        <div class="device-ip">${esc(d.ip)}</div>
      </div>
      <button class="btn-red btn-sm" onclick="removeDevice(${i})">Remove</button>
    </div>`).join('');
}

async function toggleDevice(idx, enabled) {
  await fetch('/devices/' + idx, {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ enabled })
  });
}

async function removeDevice(idx) {
  await fetch('/devices/' + idx, { method: 'DELETE' });
  loadDevices();
}

// ── Network scanner ───────────────────────────────────────────────────────────
let scanPoll = null;

async function startScan() {
  document.getElementById('scanBtn').disabled = true;
  document.getElementById('scan-results').innerHTML = '';
  document.getElementById('scan-bar').style.display = 'block';
  document.getElementById('scan-fill').style.width  = '0%';
  setStatus('cast-status','','');

  await fetch('/scan', { method: 'POST' });
  scanPoll = setInterval(pollScan, 800);
}

async function pollScan() {
  const r = await fetch('/scan/status');
  const d = await r.json();
  document.getElementById('scan-fill').style.width = d.progress + '%';

  if (!d.running) {
    clearInterval(scanPoll);
    document.getElementById('scan-bar').style.display = 'none';
    document.getElementById('scanBtn').disabled = false;
    renderScanResults(d.results);
  }
}

async function renderScanResults(results) {
  const devResp = await fetch('/devices');
  const devs    = await devResp.json();
  const knownIPs = new Set(devs.map(d => d.ip));

  const el = document.getElementById('scan-results');
  if (!results.length) { el.innerHTML = '<p style="color:#555;font-size:.85rem;margin-top:10px">No Cast devices found.</p>'; return; }

  el.innerHTML = `<p style="color:#555;font-size:.75rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">${results.length} device(s) found</p>` +
    results.map(d => `
      <div class="scan-result">
        <div class="info">
          <div style="font-size:.9rem">${esc(d.name)}</div>
          <div style="font-size:.75rem;color:#555">${esc(d.ip)} ${d.model !== 'Unknown' ? '· '+esc(d.model) : ''}</div>
        </div>
        ${knownIPs.has(d.ip)
          ? `<span class="already">Added</span>`
          : `<button class="btn-green btn-sm" onclick='addDevice(${JSON.stringify(d)}, this)'>+ Add</button>`}
      </div>`).join('');
}

async function addDevice(d, btn) {
  btn.disabled = true;
  await fetch('/devices', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ ip: d.ip, name: d.name, enabled: true })
  });
  btn.textContent = 'Added';
  btn.className   = 'btn-muted btn-sm';
  loadDevices();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── State polling (auto-refresh preview from server) ──────────────────────────
setInterval(async () => {
  try {
    const r = await fetch('/state');
    const d = await r.json();
    // just keep the preview in sync if another tab changed it
  } catch(e) {}
}, 5000);

loadDevices();
</script>
</body>
</html>
"""

DISPLAY_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#000;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;overflow:hidden;gap:24px;padding:40px}}
#img{{max-width:90vw;max-height:{img_max}vh;object-fit:contain;display:{img_display};border-radius:6px}}
#txt{{color:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:clamp(24px,{font_vw}vw,120px);font-weight:700;text-align:center;word-break:break-word;line-height:1.2;display:{txt_display}}}
</style>
</head>
<body>
<img id="img" src="{img_src}">
<div id="txt">{message}</div>
<script>
let ver = {version};
setInterval(async()=>{{
  try{{
    const r = await fetch('/state');
    const d = await r.json();
    if(d.version !== ver){{
      ver = d.version;
      const txt = document.getElementById('txt');
      const img = document.getElementById('img');
      txt.textContent = d.message;
      txt.style.display = d.message ? 'block' : 'none';
      if(d.has_image){{
        img.src = '/image?v='+d.version;
        img.style.display = 'block';
      }} else {{
        img.src = '';
        img.style.display = 'none';
      }}
    }}
  }}catch(e){{}}
}}, 2000);
</script>
</body>
</html>
"""

# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self._ok("text/html", CONTROL_HTML.encode())

        elif path == "/display":
            with state_lock:
                msg       = state["message"]
                ver       = state["version"]
                has_image = state["has_image"]
                img_type  = state["image_type"]

            if has_image:
                img_src     = f"/image?v={ver}"
                img_display = "block"
                img_max     = 55 if msg else 85
                font_vw     = 5
                txt_display = "block" if msg else "none"
            else:
                img_src     = ""
                img_display = "none"
                img_max     = 0
                font_vw     = 8
                txt_display = "block" if msg else "none"

            html = DISPLAY_TEMPLATE.format(
                img_src=img_src, img_display=img_display, img_max=img_max,
                font_vw=font_vw, txt_display=txt_display,
                message=msg.replace("<","&lt;").replace(">","&gt;"),
                version=ver
            )
            self._ok("text/html", html.encode())

        elif path == "/image":
            with image_lock:
                if os.path.exists(IMAGE_PATH):
                    with open(IMAGE_PATH, "rb") as f:
                        data = f.read()
                    with state_lock:
                        ctype = state["image_type"] or "image/jpeg"
                    self._ok(ctype, data)
                else:
                    self._send(404, "text/plain", b"No image")

        elif path == "/state":
            with state_lock:
                data = json.dumps({
                    "message":    state["message"],
                    "version":    state["version"],
                    "has_image":  state["has_image"],
                })
            self._ok("application/json", data.encode())

        elif path == "/devices":
            with devices_lock:
                data = json.dumps(devices)
            self._ok("application/json", data.encode())

        elif path == "/scan/status":
            with scan_lock:
                data = json.dumps({
                    "running":  scan["running"],
                    "progress": scan["progress"],
                    "results":  scan["results"],
                })
            self._ok("application/json", data.encode())

        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        path   = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if path == "/send":
            payload = json.loads(body)
            msg        = payload.get("message", "")
            img_data64 = payload.get("image")       # data URL or None
            img_type   = payload.get("image_type", "image/jpeg")

            # Save image if provided
            if img_data64 and "," in img_data64:
                raw = base64.b64decode(img_data64.split(",", 1)[1])
                with image_lock:
                    with open(IMAGE_PATH, "wb") as f:
                        f.write(raw)
                has_image = True
            else:
                has_image = False
                if os.path.exists(IMAGE_PATH):
                    os.remove(IMAGE_PATH)

            with state_lock:
                state["message"]    = msg
                state["has_image"]  = has_image
                state["image_type"] = img_type if has_image else ""
                state["version"]   += 1

            display_url = f"http://{LOCAL_IP}:{PORT}/display"
            threading.Thread(target=cast_all, args=(display_url,), daemon=True).start()
            self._ok("application/json", b'{"status":"ok"}')

        elif path == "/recast":
            display_url = f"http://{LOCAL_IP}:{PORT}/display"
            threading.Thread(target=cast_all, args=(display_url,), daemon=True).start()
            self._ok("application/json", b'{"status":"ok"}')

        elif path == "/devices":
            d = json.loads(body)
            with devices_lock:
                devices.append({"ip": d["ip"], "name": d["name"], "enabled": d.get("enabled", True)})
                save_devices()
            self._ok("application/json", b'{"status":"ok"}')

        elif path == "/scan":
            with scan_lock:
                if not scan["running"]:
                    threading.Thread(target=run_scan, daemon=True).start()
            self._ok("application/json", b'{"status":"ok"}')

        else:
            self._send(404, "text/plain", b"Not found")

    def do_PATCH(self):
        path   = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        # PATCH /devices/<idx>
        if path.startswith("/devices/"):
            try:
                idx  = int(path.split("/")[-1])
                data = json.loads(body)
                with devices_lock:
                    if 0 <= idx < len(devices):
                        devices[idx].update({k: v for k, v in data.items() if k in ("enabled","name")})
                        save_devices()
                self._ok("application/json", b'{"status":"ok"}')
            except Exception as e:
                self._send(400, "text/plain", str(e).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/devices/"):
            try:
                idx = int(path.split("/")[-1])
                with devices_lock:
                    if 0 <= idx < len(devices):
                        devices.pop(idx)
                        save_devices()
                self._ok("application/json", b'{"status":"ok"}')
            except Exception as e:
                self._send(400, "text/plain", str(e).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def _ok(self, ctype, body):
        self._send(200, ctype, body)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Cast server: http://{LOCAL_IP}:{PORT}")
    print(f"Open:        http://localhost:{PORT}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
