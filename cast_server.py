"""
cast_server.py — Local web control panel for Google Cast devices.

Configuration (environment variables):
  CAST_TOKEN    Auth token for the control panel. Auto-generated if not set.
  CAST_PORT     Port to listen on (default: 8765).
  CAST_SUBNETS  Comma-separated subnets to scan, e.g. "192.168.1.0/24,10.0.0.0/24"
                Defaults to scanning common RFC-1918 /24s on the local interface.
  CAST_HOST     Override the local IP advertised to Cast devices.
"""

import threading
import socket
import time
import json
import os
import base64
import ipaddress
import asyncio
import secrets
import html
import logging
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import edge_tts
import pychromecast
from pychromecast.controllers.dashcast import DashCastController

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cast")

# ── Config ────────────────────────────────────────────────────────────────────
PORT      = int(os.environ.get("CAST_PORT", 8765))
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
IMAGE_PATH = os.path.join(BASE_DIR, "cast_current_image")
TTS_PATH   = os.path.join(BASE_DIR, "cast_tts.mp3")
DEVICES_FILE = os.path.join(BASE_DIR, "cast_devices.json")

MAX_BODY_SIZE    = 10 * 1024 * 1024   # 10 MB upload limit
ALLOWED_IMG_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_VOICE_IDS = set()             # populated after VOICES is defined

VOICES = [
    ("en-US-JennyNeural",   "Jenny (US Female)"),
    ("en-US-GuyNeural",     "Guy (US Male)"),
    ("en-US-AriaNeural",    "Aria (US Female)"),
    ("en-GB-SoniaNeural",   "Sonia (UK Female)"),
    ("en-GB-RyanNeural",    "Ryan (UK Male)"),
    ("en-AU-NatashaNeural", "Natasha (AU Female)"),
    ("en-IN-PrabhatNeural", "Prabhat (Indian Male)"),
]
ALLOWED_VOICE_IDS = {v[0] for v in VOICES}

# RFC-1918 private ranges — device IPs must fall within these
PRIVATE_RANGES = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
]

def is_private_ip(ip_str):
    try:
        addr = ipaddress.IPv4Address(ip_str)
        return any(addr in r for r in PRIVATE_RANGES)
    except ValueError:
        return False

# ── Auth token ────────────────────────────────────────────────────────────────
CAST_TOKEN = os.environ.get("CAST_TOKEN") or secrets.token_urlsafe(24)

# ── Local IP ──────────────────────────────────────────────────────────────────
def detect_local_ip():
    override = os.environ.get("CAST_HOST")
    if override:
        return override
    # Find the interface used to reach the default route
    for candidate in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((candidate, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass
    return socket.gethostbyname(socket.gethostname())

LOCAL_IP = detect_local_ip()

# ── Scan subnets ──────────────────────────────────────────────────────────────
def default_scan_subnets():
    """Derive common /24 subnets to scan."""
    # Based on README: scan 192.168.4.x, 192.168.5.x, 192.168.6.x
    # Also include the current local subnet just in case it's different.
    subnets = ["192.168.4.0/24", "192.168.5.0/24", "192.168.6.0/24"]
    local_prefix = ".".join(LOCAL_IP.split(".")[:3])
    local_subnet = f"{local_prefix}.0/24"
    if local_subnet not in subnets:
        subnets.append(local_subnet)
    return subnets

def get_scan_subnets():
    raw = os.environ.get("CAST_SUBNETS", "")
    if raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return default_scan_subnets()

# ── Device persistence ────────────────────────────────────────────────────────
def load_devices():
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []   # start empty — users add devices via scan or manually

def save_devices():
    with open(DEVICES_FILE, "w") as f:
        json.dump(devices, f, indent=2)

devices_lock = threading.Lock()
devices = load_devices()

# ── Cast state ────────────────────────────────────────────────────────────────
state      = {"message": "", "version": 0, "has_image": False, "image_type": "", "has_tts": False}
state_lock = threading.Lock()
image_lock = threading.Lock()
tts_lock   = threading.Lock()

# ── Scan state ────────────────────────────────────────────────────────────────
scan      = {"running": False, "progress": 0, "results": []}
scan_lock = threading.Lock()

# ── TTS ───────────────────────────────────────────────────────────────────────
def generate_tts(text, voice):
    if voice not in ALLOWED_VOICE_IDS:
        voice = "en-US-JennyNeural"
    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(TTS_PATH)
    asyncio.run(_run())

# ── Cast ──────────────────────────────────────────────────────────────────────
def cast_all(display_url, tts_url=None):
    with devices_lock:
        active = [d for d in devices if d.get("enabled", True)]

    def cast_one(d):
        ip, name = d["ip"], d["name"]
        dtype = d.get("type", "display")
        try:
            cc = pychromecast.get_chromecast_from_host((ip, 8009, None, None, name))
            cc.wait(timeout=10)
            cc.quit_app()
            time.sleep(1)
            if dtype == "speaker" and tts_url:
                cc.media_controller.play_media(tts_url, "audio/mpeg")
                cc.media_controller.block_until_active(timeout=10)
            else:
                dash = DashCastController()
                cc.register_handler(dash)
                dash.load_url(display_url, force=True, reload_seconds=0)
            log.info("CAST %s (%s)", name, dtype)
        except Exception as e:
            log.warning("FAIL %s: %s", name, e)

    threads = [threading.Thread(target=cast_one, args=(d,), daemon=True) for d in active]
    for t in threads: t.start()
    for t in threads: t.join()
    log.info("Cast complete (%d devices)", len(active))

def stop_cast_device(d):
    """Tell a single device to quit its current app."""
    ip, name = d["ip"], d["name"]
    cc = None
    try:
        cc = pychromecast.get_chromecast_from_host((ip, 8009, None, None, name))
        cc.wait(timeout=8)
        cc.quit_app()
        log.info("STOP %s (%s)", name, ip)
    except Exception as e:
        log.warning("STOP FAIL %s: %s", name, e)
    finally:
        if cc:
            try:
                cc.socket_client.stop()
            except Exception:
                pass

# ── Network scanner ───────────────────────────────────────────────────────────
MAX_SCAN_RESP = 4096   # max bytes read from each device info response

def run_scan():
    with scan_lock:
        scan["running"]  = True
        scan["progress"] = 0
        scan["results"]  = []

    subnets = get_scan_subnets()
    all_ips = []
    for s in subnets:
        try:
            all_ips.extend(ipaddress.IPv4Network(s, strict=False).hosts())
        except ValueError:
            log.warning("Invalid subnet ignored: %s", s)

    total   = len(all_ips) or 1
    checked = [0]
    found   = []
    mu      = threading.Lock()
    sem     = threading.Semaphore(150)

    def check(ip_obj):
        ip = str(ip_obj)
        try:
            with sem:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.8)
                open_ = sock.connect_ex((ip, 8009)) == 0
                sock.close()
            if open_:
                try:
                    req  = urllib.request.urlopen(
                        f"http://{ip}:8008/setup/eureka_info?params=name,device_info",
                        timeout=2)
                    raw  = req.read(MAX_SCAN_RESP)
                    data = json.loads(raw)
                    name  = str(data.get("name", ip))[:128]
                    model = str(data.get("device_info", {}).get("model_name", "Unknown"))[:128]
                    entry = {"ip": ip, "name": name, "model": model}
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

    log.info("Scan complete — %d device(s) found", len(found))

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
.img-zone{border:2px dashed #333;border-radius:10px;padding:28px;text-align:center;cursor:pointer;transition:border-color .2s,background .2s;margin-top:14px;position:relative}
.img-zone:hover,.img-zone.drag{border-color:#555;background:#111}
.img-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.img-zone .label{font-size:.9rem;color:#555;pointer-events:none}
.img-zone .label span{display:block;font-size:1.5rem;margin-bottom:6px}
#img-preview-wrap{margin-top:14px;display:none}
#img-preview{max-width:100%;max-height:220px;border-radius:8px;object-fit:contain;border:1px solid #2a2a2a}
.img-preview-actions{display:flex;gap:8px;margin-top:8px}
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
.status{margin-top:14px;padding:10px 14px;border-radius:7px;font-size:.85rem;display:none}
.ok{background:#0f2a0f;color:#6f6;display:block}
.err{background:#2a0f0f;color:#f66;display:block}
.info{background:#0f1e2a;color:#6af;display:block}
.preview-label{font-size:.7rem;text-transform:uppercase;letter-spacing:1px;color:#444;margin-top:20px;margin-bottom:6px}
.preview-box{background:#000;border-radius:10px;min-height:90px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px;gap:14px;overflow:hidden}
#preview-img{max-width:100%;max-height:180px;border-radius:4px;object-fit:contain;display:none}
#preview-txt{color:#fff;font-size:clamp(13px,2.5vw,22px);font-weight:700;text-align:center;word-break:break-word}
.device-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #1f1f1f}
.device-row:last-child{border-bottom:none}
.device-info{flex:1;min-width:0}
.device-name{font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.device-ip{font-size:.75rem;color:#555}
.toggle{position:relative;width:38px;height:22px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#2a2a2a;border-radius:22px;cursor:pointer;transition:.2s}
.toggle input:checked+.toggle-slider{background:#1e90ff}
.toggle-slider:before{content:'';position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
.toggle input:checked+.toggle-slider:before{transform:translateX(16px)}
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
<h1>Cast Control</h1>
<p class="sub">Send messages and images to your Cast devices.</p>

<div class="card">
  <h2>Message &amp; Image</h2>
  <textarea id="msg" placeholder="Type a message... (Ctrl+Enter to send)"></textarea>

  <div class="img-zone" id="drop-zone">
    <input type="file" id="file-input" accept="image/jpeg,image/png,image/gif,image/webp">
    <div class="label"><span>🖼️</span>Click or drag an image here (optional)</div>
  </div>

  <div id="img-preview-wrap">
    <img id="img-preview" src="" alt="preview">
    <div class="img-preview-actions">
      <button class="btn-muted btn-sm" onclick="clearImage()">Remove image</button>
    </div>
  </div>

  <div style="display:flex;align-items:center;gap:14px;margin-top:16px;flex-wrap:wrap">
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:.9rem">
      <span class="toggle" style="width:44px;height:26px">
        <input type="checkbox" id="tts-toggle">
        <span class="toggle-slider"></span>
      </span>
      <span>Read aloud</span>
    </label>
    <select id="voice-select" style="background:#111;color:#eee;border:2px solid #2a2a2a;border-radius:8px;padding:6px 10px;font-size:.85rem;outline:none;flex:1;min-width:160px">
    </select>
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
// Token injected server-side
const TOKEN = '%%TOKEN%%';
const headers = (extra={}) => ({ 'Content-Type':'application/json', 'Authorization':'Bearer '+TOKEN, ...extra });
const api = (path, opts={}) => fetch(path, { ...opts, headers: { ...headers(), ...(opts.headers||{}) } });

let imageData = null, imageType = null;

document.getElementById('msg').addEventListener('input', updatePreview);
document.getElementById('msg').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendMsg();
});

function updatePreview() {
  document.getElementById('preview-txt').textContent = document.getElementById('msg').value || '';
}

const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
fileInput.addEventListener('change', () => loadFile(fileInput.files[0]));
dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drag'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag');
  if (e.dataTransfer.files[0]) loadFile(e.dataTransfer.files[0]);
});

const ALLOWED_TYPES = new Set(['image/jpeg','image/png','image/gif','image/webp']);
function loadFile(file) {
  if (!file || !ALLOWED_TYPES.has(file.type)) {
    setStatus('cast-status','Unsupported image type. Use JPEG, PNG, GIF or WebP.','err'); return;
  }
  const reader = new FileReader();
  reader.onload = ev => {
    imageData = ev.target.result; imageType = file.type;
    document.getElementById('img-preview').src = imageData;
    document.getElementById('img-preview-wrap').style.display = 'block';
    document.getElementById('preview-img').src = imageData;
    document.getElementById('preview-img').style.display = 'block';
    dropZone.style.display = 'none';
  };
  reader.readAsDataURL(file);
}

function clearImage() {
  imageData = imageType = null;
  document.getElementById('img-preview-wrap').style.display = 'none';
  document.getElementById('preview-img').style.display = 'none';
  document.getElementById('preview-img').src = '';
  document.getElementById('img-preview').src = '';
  document.getElementById('file-input').value = '';
  dropZone.style.display = 'block';
}

function clearAll() { document.getElementById('msg').value = ''; updatePreview(); clearImage(); }

function setStatus(id, msg, type) {
  const el = document.getElementById(id);
  el.textContent = msg; el.className = 'status ' + type;
}
function setBusy(b) { ['sendBtn','recastBtn'].forEach(id => document.getElementById(id).disabled = b); }

async function loadVoices() {
  const r = await api('/voices');
  const voices = await r.json();
  document.getElementById('voice-select').innerHTML =
    voices.map(v => `<option value="${v.id}">${v.label}</option>`).join('');
}

async function sendMsg() {
  const message = document.getElementById('msg').value.trim();
  if (!message && !imageData) { setStatus('cast-status','Enter a message or choose an image.','err'); return; }
  const tts   = document.getElementById('tts-toggle').checked;
  const voice = document.getElementById('voice-select').value;
  setBusy(true);
  setStatus('cast-status', tts ? 'Generating audio and casting...' : 'Casting to all devices...', 'info');
  try {
    await api('/send', {
      method: 'POST',
      body: JSON.stringify({ message, image: imageData, image_type: imageType, tts, voice })
    });
    setStatus('cast-status', tts ? 'Message cast with audio!' : 'Message cast!', 'ok');
  } catch(e) {
    setStatus('cast-status','Error: ' + e.message,'err');
  } finally { setBusy(false); }
}

async function recast() {
  setBusy(true); setStatus('cast-status','Re-casting...','info');
  try {
    await api('/recast', { method: 'POST' });
    setStatus('cast-status','Re-cast complete!','ok');
  } catch(e) {
    setStatus('cast-status','Error: ' + e.message,'err');
  } finally { setBusy(false); }
}

async function loadDevices() {
  const r = await api('/devices');
  const devs = await r.json();
  const el = document.getElementById('device-list');
  if (!devs.length) { el.innerHTML = '<p style="color:#444;font-size:.85rem">No devices. Use Scan to discover them.</p>'; return; }
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
      <select onchange="setDeviceType(${i}, this.value)" style="background:#111;color:#aaa;border:1px solid #2a2a2a;border-radius:6px;padding:4px 8px;font-size:.78rem">
        <option value="display" ${(d.type||'display')==='display'?'selected':''}>Display</option>
        <option value="speaker" ${d.type==='speaker'?'selected':''}>Speaker</option>
      </select>
      <button class="btn-red btn-sm" onclick="removeDevice(${i})">Remove</button>
    </div>`).join('');
}

async function toggleDevice(idx, enabled) {
  await api('/devices/'+idx, { method:'PATCH', body: JSON.stringify({ enabled }) });
}
async function setDeviceType(idx, type) {
  await api('/devices/'+idx, { method:'PATCH', body: JSON.stringify({ type }) });
}
async function removeDevice(idx) {
  await api('/devices/'+idx, { method:'DELETE' }); loadDevices();
}

let scanPoll = null;
async function startScan() {
  document.getElementById('scanBtn').disabled = true;
  document.getElementById('scan-results').innerHTML = '';
  document.getElementById('scan-bar').style.display = 'block';
  document.getElementById('scan-fill').style.width = '0%';
  await api('/scan', { method: 'POST' });
  scanPoll = setInterval(pollScan, 800);
}

async function pollScan() {
  const r = await api('/scan/status');
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
  const devResp = await api('/devices');
  const devs    = await devResp.json();
  const knownIPs = new Set(devs.map(d => d.ip));
  const el = document.getElementById('scan-results');
  if (!results.length) { el.innerHTML = '<p style="color:#555;font-size:.85rem;margin-top:10px">No Cast devices found.</p>'; return; }
  el.innerHTML = `<p style="color:#555;font-size:.75rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">${results.length} device(s) found</p>` +
    results.map(d => `
      <div class="scan-result">
        <div class="info">
          <div style="font-size:.9rem">${esc(d.name)}</div>
          <div style="font-size:.75rem;color:#555">${esc(d.ip)}${d.model !== 'Unknown' ? ' &middot; '+esc(d.model) : ''}</div>
        </div>
        ${knownIPs.has(d.ip)
          ? '<span class="already">Added</span>'
          : `<button class="btn-green btn-sm" onclick='addDevice(${JSON.stringify(d)}, this)'>+ Add</button>`}
      </div>`).join('');
}

async function addDevice(d, btn) {
  btn.disabled = true;
  await api('/devices', { method:'POST', body: JSON.stringify({ ip: d.ip, name: d.name, enabled: true }) });
  btn.textContent = 'Added'; btn.className = 'btn-muted btn-sm';
  loadDevices();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadDevices();
loadVoices();
</script>
</body>
</html>
"""

DISPLAY_TEMPLATE = """\
<!DOCTYPE html>
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
{init_tts}
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
      if(d.has_image){{ img.src='/image?v='+d.version; img.style.display='block'; }}
      else {{ img.src=''; img.style.display='none'; }}
      if(d.has_tts){{ new Audio('/tts?v='+d.version).play().catch(()=>{{}}); }}
    }}
  }}catch(e){{}}
}}, 2000);
</script>
</body>
</html>
"""

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; media-src 'self'; img-src 'self' data:",
}

# Endpoints that Cast devices need — no auth required
PUBLIC_PATHS = {"/", "/display", "/state", "/tts", "/image"}

# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def _authorized(self):
        """Check Bearer token. Public paths skip auth."""
        if urlparse(self.path).path in PUBLIC_PATHS:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], CAST_TOKEN):
            return True
        return False

    def _read_body(self):
        """Read body with size limit. Returns bytes or None on error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send(400, "text/plain", b"Bad request")
            return None
        if length > MAX_BODY_SIZE:
            self._send(413, "text/plain", b"Payload too large")
            return None
        return self.rfile.read(length)

    def do_GET(self):
        if not self._authorized():
            self._send(401, "text/plain", b"Unauthorized")
            return
        path = urlparse(self.path).path

        if path == "/":
            page = CONTROL_HTML.replace("%%TOKEN%%", CAST_TOKEN)
            self._ok("text/html", page.encode())

        elif path == "/display":
            with state_lock:
                msg       = state["message"]
                ver       = state["version"]
                has_image = state["has_image"]
                has_tts   = state["has_tts"]

            if has_image:
                img_src, img_display, img_max = f"/image?v={ver}", "block", 55 if msg else 85
                font_vw, txt_display = 5, "block" if msg else "none"
            else:
                img_src, img_display, img_max = "", "none", 0
                font_vw, txt_display = 8, "block" if msg else "none"

            init_tts = f"new Audio('/tts?v={ver}').play().catch(()=>{{}});" if has_tts else ""
            page = DISPLAY_TEMPLATE.format(
                img_src=img_src, img_display=img_display, img_max=img_max,
                font_vw=font_vw, txt_display=txt_display,
                message=html.escape(msg),
                version=ver, init_tts=init_tts,
            )
            self._ok("text/html", page.encode())

        elif path == "/image":
            with image_lock:
                if os.path.exists(IMAGE_PATH):
                    with open(IMAGE_PATH, "rb") as f:
                        data = f.read()
                    with state_lock:
                        ctype = state["image_type"] if state["image_type"] in ALLOWED_IMG_TYPES else "image/jpeg"
                    self._ok(ctype, data)
                else:
                    self._send(404, "text/plain", b"No image")

        elif path == "/tts":
            with tts_lock:
                if os.path.exists(TTS_PATH):
                    with open(TTS_PATH, "rb") as f:
                        data = f.read()
                    self._ok("audio/mpeg", data)
                else:
                    self._send(404, "text/plain", b"No TTS")

        elif path == "/voices":
            self._ok("application/json", json.dumps([{"id": v[0], "label": v[1]} for v in VOICES]).encode())

        elif path == "/state":
            with state_lock:
                data = json.dumps({
                    "message":   state["message"],
                    "version":   state["version"],
                    "has_image": state["has_image"],
                    "has_tts":   state["has_tts"],
                })
            self._ok("application/json", data.encode())

        elif path == "/devices":
            with devices_lock:
                self._ok("application/json", json.dumps(devices).encode())

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
        if not self._authorized():
            self._send(401, "text/plain", b"Unauthorized")
            return
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            return

        if path == "/send":
            try:
                payload = json.loads(body)
            except ValueError:
                self._send(400, "text/plain", b"Bad request")
                return

            msg        = str(payload.get("message", ""))[:2000]
            img_data64 = payload.get("image")
            img_type   = payload.get("image_type", "image/jpeg")
            tts_on     = bool(payload.get("tts", False))
            voice      = str(payload.get("voice", "en-US-JennyNeural"))

            # Validate image type
            if img_type not in ALLOWED_IMG_TYPES:
                img_type = "image/jpeg"

            # Validate voice
            if voice not in ALLOWED_VOICE_IDS:
                voice = "en-US-JennyNeural"

            # Save image
            if img_data64 and isinstance(img_data64, str) and "," in img_data64:
                try:
                    raw = base64.b64decode(img_data64.split(",", 1)[1])
                    with image_lock:
                        with open(IMAGE_PATH, "wb") as f:
                            f.write(raw)
                    has_image = True
                except Exception:
                    has_image = False
            else:
                has_image = False
                if os.path.exists(IMAGE_PATH):
                    os.remove(IMAGE_PATH)

            # Generate TTS
            has_tts = False
            if tts_on and msg.strip():
                try:
                    with tts_lock:
                        generate_tts(msg.strip(), voice)
                    has_tts = True
                except Exception as e:
                    log.warning("TTS generation failed: %s", e)

            with state_lock:
                state["message"]    = msg
                state["has_image"]  = has_image
                state["image_type"] = img_type if has_image else ""
                state["has_tts"]    = has_tts
                state["version"]   += 1

            display_url = f"http://{LOCAL_IP}:{PORT}/display"
            tts_url     = f"http://{LOCAL_IP}:{PORT}/tts" if has_tts else None
            threading.Thread(target=cast_all, args=(display_url, tts_url), daemon=True).start()
            self._ok("application/json", b'{"status":"ok"}')

        elif path == "/recast":
            display_url = f"http://{LOCAL_IP}:{PORT}/display"
            with state_lock:
                has_tts = state["has_tts"]
            tts_url = f"http://{LOCAL_IP}:{PORT}/tts" if has_tts else None
            threading.Thread(target=cast_all, args=(display_url, tts_url), daemon=True).start()
            self._ok("application/json", b'{"status":"ok"}')

        elif path == "/devices":
            try:
                d = json.loads(body)
                ip   = str(d.get("ip", ""))
                name = str(d.get("name", ip))[:128]
                dtype = str(d.get("type", "display"))
                if dtype not in ("display", "speaker"):
                    dtype = "display"
                if not is_private_ip(ip):
                    self._send(400, "text/plain", b"Invalid IP address")
                    return
                with devices_lock:
                    devices.append({"ip": ip, "name": name, "enabled": bool(d.get("enabled", True)), "type": dtype})
                    save_devices()
                self._ok("application/json", b'{"status":"ok"}')
            except (ValueError, KeyError):
                self._send(400, "text/plain", b"Bad request")

        elif path == "/scan":
            with scan_lock:
                if not scan["running"]:
                    threading.Thread(target=run_scan, daemon=True).start()
            self._ok("application/json", b'{"status":"ok"}')

        else:
            self._send(404, "text/plain", b"Not found")

    def do_PATCH(self):
        if not self._authorized():
            self._send(401, "text/plain", b"Unauthorized")
            return
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            return

        if path.startswith("/devices/"):
            try:
                idx = int(path.rsplit("/", 1)[-1])
                if idx < 0:
                    raise ValueError
                data = json.loads(body)
                allowed = {}
                if "enabled" in data:
                    allowed["enabled"] = bool(data["enabled"])
                if "name" in data:
                    allowed["name"] = str(data["name"])[:128]
                if "type" in data and data["type"] in ("display", "speaker"):
                    allowed["type"] = data["type"]
                with devices_lock:
                    if 0 <= idx < len(devices):
                        # If disabling, stop the cast
                        if "enabled" in allowed and not allowed["enabled"] and devices[idx].get("enabled", True):
                            threading.Thread(target=stop_cast_device, args=(devices[idx].copy(),), daemon=True).start()
                        devices[idx].update(allowed)
                        save_devices()
                self._ok("application/json", b'{"status":"ok"}')
            except (ValueError, KeyError):
                self._send(400, "text/plain", b"Bad request")
        else:
            self._send(404, "text/plain", b"Not found")

    def do_DELETE(self):
        if not self._authorized():
            self._send(401, "text/plain", b"Unauthorized")
            return
        path = urlparse(self.path).path
        if path.startswith("/devices/"):
            try:
                idx = int(path.rsplit("/", 1)[-1])
                if idx < 0:
                    raise ValueError
                with devices_lock:
                    if 0 <= idx < len(devices):
                        threading.Thread(target=stop_cast_device, args=(devices[idx].copy(),), daemon=True).start()
                        devices.pop(idx)
                        save_devices()
                self._ok("application/json", b'{"status":"ok"}')
            except ValueError:
                self._send(400, "text/plain", b"Bad request")
        else:
            self._send(404, "text/plain", b"Not found")

    def _ok(self, ctype, body):
        self._send(200, ctype, body)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("Cast server running on port %d", PORT)
    log.info("Local IP detected: %s", LOCAL_IP)
    log.info("Scan subnets: %s", ", ".join(get_scan_subnets()))
    log.info("")
    log.info("  Open: http://localhost:%d", PORT)
    log.info("  Auth token: %s", CAST_TOKEN)
    log.info("")
    log.info("Set CAST_TOKEN env var to use a fixed token across restarts.")
    log.info("Set CAST_SUBNETS env var to customize network scan ranges.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
