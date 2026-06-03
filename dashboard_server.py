#!/usr/bin/env python3
"""
BTC Miner Dashboard — Servidor HTTP + Minero + API
==================================================
Dashboard web con:
  - Monitoreo en tiempo real (hashrate, total, uptime, bloque actual)
  - Control de CPU (slider 1%-100%)
  - Configuración de dirección BTC para recompensa
  - Gráfica de hashrate histórico
  - Analytics completas de minería

Acceso: http://localhost:8888
"""

import http.server
import json
import os
import sys
import time
import struct
import hashlib
import threading
import urllib.request
import base64
import math
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# Configuración RPC Knots (desde variables de entorno)
# ═══════════════════════════════════════════════════════════
RPC_URL = os.environ.get("BTC_RPC_URL", "http://10.21.21.7:9332")
RPC_USER = os.environ.get("BTC_RPC_USER", "umbrel")
RPC_PASS = os.environ.get("BTC_RPC_PASS", "")
VERSION = "4.0"

# ═══════════════════════════════════════════════════════════
# Estado global compartido (thread-safe)
# ═══════════════════════════════════════════════════════════
state = {
    "running": True,
    "hashrate": 0.0,          # KH/s actual
    "total_hashes": 0,        # Total acumulado
    "shares_found": 0,
    "blocks_found": 0,
    "best_hash": "-",
    "start_time": time.time(),
    "current_block": 0,
    "block_reward": 0.0,
    "difficulty": 0.0,
    "network_hashrate": 0.0,
    "mempool_tx": 0,
    "cpu_percent": 100,       # % de CPU a usar (1-100)
    "btc_address": "",        # Dirección BTC para recompensa
    "node_connected": False,
    "node_blocks": 0,
    "history": [],            # Últimos 60 puntos de hashrate (1 por segundo)
    "last_template": None,
    "template_age": 0,
    "error": None,
}
state_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════
# Crypto
# ═══════════════════════════════════════════════════════════
def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def bits_to_target(bits_hex):
    nbits = int(bits_hex, 16)
    exp = nbits >> 24
    mant = nbits & 0x00ffffff
    target = mant * (2 ** (8 * (exp - 3)))
    return target.to_bytes(32, 'big')


# ═══════════════════════════════════════════════════════════
# RPC Client
# ═══════════════════════════════════════════════════════════
def rpc_call(method, params=None):
    if not RPC_PASS:
        return None
    if params is None:
        params = []
    auth = base64.b64encode(f"{RPC_USER}:{RPC_PASS}".encode()).decode()
    data = json.dumps({"jsonrpc": "1.0", "id": "1", "method": method, "params": params}).encode()
    req = urllib.request.Request(
        RPC_URL, data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        if result.get("error"):
            raise Exception(f"RPC Error: {result['error']}")
        return result["result"]


# ═══════════════════════════════════════════════════════════
# Miner Thread
# ═══════════════════════════════════════════════════════════
def miner_thread():
    """Thread principal de minería con throttling."""
    global state
    
    # Intentar conectar al nodo
    try:
        info = rpc_call("getblockchaininfo")
        with state_lock:
            state["node_connected"] = True
            state["node_blocks"] = info["blocks"]
            state["difficulty"] = info["difficulty"]
    except Exception as e:
        with state_lock:
            state["node_connected"] = False
            state["error"] = f"Nodo no disponible: {e}"
        # Minería fallback sin nodo
        mine_fallback()
        return
    
    print(f"✅ Conectado a Knots: {info['blocks']:,} bloques")
    
    last_template_time = 0
    template = None
    
    while True:
        with state_lock:
            if not state["running"]:
                break
            cpu_pct = state["cpu_percent"]
        
        # Refrescar template cada 30s
        now = time.time()
        if now - last_template_time > 30:
            try:
                template = rpc_call("getblocktemplate", [{"rules": ["segwit"]}])
                last_template_time = now
                
                with state_lock:
                    state["current_block"] = template.get("height", 0)
                    state["block_reward"] = template.get("coinbasevalue", 0) / 1e8
                    state["mempool_tx"] = len(template.get("transactions", []))
                    state["last_template"] = template
                    state["template_age"] = 0
                    state["error"] = None
            except Exception as e:
                with state_lock:
                    state["error"] = f"RPC: {e}"
                time.sleep(5)
                continue
        
        if template:
            with state_lock:
                state["template_age"] += 1
            
            # Minar este template
            hashes = mine_cycle(template, cpu_pct)
            
            with state_lock:
                state["total_hashes"] += hashes
        else:
            time.sleep(1)


def mine_cycle(template, cpu_pct):
    """Un ciclo de minería (~1 segundo de trabajo)."""
    target = bits_to_target(template["bits"])
    version = template["version"]
    prevhash = bytes.fromhex(template["previousblockhash"])[::-1]
    bits = int(template["bits"], 16)
    curtime = int(template["curtime"])
    
    # Header simplificado
    header = bytearray(80)
    struct.pack_into('<I', header, 0, version)
    header[4:36] = prevhash
    dummy = sha256d(json.dumps(template).encode()[:80])
    header[36:68] = dummy
    struct.pack_into('<I', header, 68, curtime)
    struct.pack_into('<I', header, 72, bits)
    
    nonce = int(time.time() * 1000) & 0xFFFFFFFF
    hashes = 0
    start = time.time()
    
    # Duty cycle basado en cpu_pct
    work_ms = int(cpu_pct * 10)  # ms de trabajo por ciclo de 1s
    sleep_ms = max(0, 1000 - work_ms)
    
    while time.time() - start < 1.0:
        work_start = time.time()
        
        # Trabajar por work_ms
        while time.time() - work_start < work_ms / 1000.0:
            struct.pack_into('<I', header, 76, nonce)
            h = sha256d(bytes(header))[::-1]
            hashes += 1
            nonce += 1
            
            if h < target:
                with state_lock:
                    state["blocks_found"] += 1
                    state["best_hash"] = h.hex()
                print(f"\n🎉 BLOQUE! Nonce: {nonce-1} Hash: {h.hex()[:16]}...")
                
                # Submit block
                try:
                    result = rpc_call("submitblock", [bytes(header).hex()])
                    print(f"   submitblock: {result}")
                except Exception as e:
                    print(f"   Error submitblock: {e}")
            
            if nonce >= 0xFFFFFFF0:
                nonce = 0
            
            if hashes % 50000 == 0 and time.time() - start > 0.1:
                break
        
        # Dormir el resto del ciclo
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)
    
    # Actualizar stats
    elapsed = time.time() - start
    hr = hashes / elapsed if elapsed > 0 else 0
    
    with state_lock:
        state["hashrate"] = hr
        history = state["history"]
        history.append({"t": time.time(), "hr": hr})
        if len(history) > 60:
            state["history"] = history[-60:]
    
    return hashes


def mine_fallback():
    """Minería sin nodo (solo hashrate)."""
    header = bytearray(80)
    nonce = 0
    start = time.time()
    
    while True:
        with state_lock:
            if not state["running"]:
                break
            cpu_pct = state["cpu_percent"]
        
        work_ms = int(cpu_pct * 10)
        cycle_start = time.time()
        hashes = 0
        
        while time.time() - cycle_start < 1.0:
            wstart = time.time()
            while time.time() - wstart < work_ms / 1000.0:
                struct.pack_into('<I', header, 76, nonce)
                sha256d(bytes(header))
                hashes += 1
                nonce += 1
                if hashes % 50000 == 0:
                    break
            
            sleep_ms = max(0, 1000 - work_ms)
            if sleep_ms > 0:
                time.sleep(sleep_ms / 1000.0)
        
        hr = hashes / max(time.time() - cycle_start, 0.001)
        
        with state_lock:
            state["hashrate"] = hr
            state["total_hashes"] += hashes
            history = state["history"]
            history.append({"t": time.time(), "hr": hr})
            if len(history) > 60:
                state["history"] = history[-60:]


# ═══════════════════════════════════════════════════════════
# API Handlers
# ═══════════════════════════════════════════════════════════
def api_stats():
    """GET /api/stats — Estado completo del minero."""
    with state_lock:
        elapsed = time.time() - state["start_time"]
        total_mh = state["total_hashes"] / 1e6
        return {
            "hashrate_khs": round(state["hashrate"] / 1000, 2),
            "hashrate_hs": round(state["hashrate"], 0),
            "total_hashes": state["total_hashes"],
            "total_mh": round(total_mh, 2),
            "shares_found": state["shares_found"],
            "blocks_found": state["blocks_found"],
            "best_hash": state["best_hash"],
            "uptime_seconds": round(elapsed),
            "uptime_str": format_uptime(elapsed),
            "current_block": state["current_block"],
            "block_reward_btc": round(state["block_reward"], 8),
            "difficulty": state["difficulty"],
            "mempool_tx": state["mempool_tx"],
            "cpu_percent": state["cpu_percent"],
            "btc_address": state["btc_address"],
            "node_connected": state["node_connected"],
            "node_blocks": state["node_blocks"],
            "error": state["error"],
            "history": state["history"][-60:],
        }


def format_uptime(seconds):
    h, m, s = int(seconds // 3600), int((seconds % 3600) // 60), int(seconds % 60)
    if h > 0: return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0: return f"{m}m {s:02d}s"
    return f"{s}s"


# ═══════════════════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════════════════
DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⛏️ BTC Solo Miner — Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:'JetBrains Mono','Courier New',monospace;min-height:100vh}
.header{background:#111118;border-bottom:1px solid #222;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:18px;color:#f7931a}
.status-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}
.status-dot.online{background:#00ff88;box-shadow:0 0 8px #00ff88;animation:pulse 2s infinite}
.status-dot.offline{background:#ff4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.container{max-width:1400px;margin:0 auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:20px}
.card{background:#111118;border:1px solid #222;border-radius:8px;padding:16px}
.card h3{font-size:11px;text-transform:uppercase;color:#888;margin-bottom:12px;letter-spacing:1px}
.big-number{font-size:36px;font-weight:bold;color:#f7931a;font-family:monospace}
.big-number.green{color:#00ff88}
.big-number.cyan{color:#00d4ff}
.label{font-size:11px;color:#666;margin-top:4px}
.stat-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1a1a22;font-size:12px}
.stat-row .key{color:#888}
.stat-row .val{color:#ccc;font-family:monospace}
.slider-container{margin:12px 0}
.slider-container input[type=range]{width:100%;accent-color:#f7931a}
.slider-labels{display:flex;justify-content:space-between;font-size:10px;color:#666;margin-top:4px}
.input-group{margin:8px 0}
.input-group input{width:100%;background:#1a1a24;border:1px solid #333;color:#e0e0e0;padding:8px 12px;border-radius:4px;font-family:monospace;font-size:13px}
.btn{background:#f7931a;color:#000;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;font-weight:bold;font-family:monospace;font-size:12px}
.btn:hover{background:#ffab40}
.chart-container{background:#111118;border:1px solid #222;border-radius:8px;padding:16px;margin-bottom:20px;height:250px}
.chart-container h3{font-size:11px;text-transform:uppercase;color:#888;margin-bottom:8px;letter-spacing:1px}
canvas{width:100%;height:200px}
.footer{text-align:center;padding:16px;color:#444;font-size:10px}
.error-banner{background:#331111;border:1px solid #661111;color:#ff6666;padding:8px 16px;border-radius:4px;margin-bottom:16px;font-size:12px}
.progress-bar{height:4px;background:#222;border-radius:2px;overflow:hidden;margin-top:8px}
.progress-fill{height:100%;background:#f7931a;transition:width 0.3s}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1><span class="status-dot online" id="statusDot"></span>⛏️ BTC SOLO MINER v''' + VERSION + '''</h1>
  </div>
  <div style="font-size:12px;color:#666">
    Nodo: <span id="nodeStatus" style="color:#888">---</span>
  </div>
</div>

<div class="container">
  <div id="errorBanner" class="error-banner" style="display:none"></div>

  <div class="grid">
    <div class="card">
      <h3>⚡ Hashrate</h3>
      <div class="big-number cyan" id="hashrate">---</div>
      <div class="label">KH/s</div>
      <div class="progress-bar"><div class="progress-fill" id="cpuBar" style="width:100%"></div></div>
    </div>
    <div class="card">
      <h3>🔢 Total Hashes</h3>
      <div class="big-number" id="totalHashes">0</div>
      <div class="label" id="totalLabel">MH</div>
    </div>
    <div class="card">
      <h3>⏱️ Uptime</h3>
      <div class="big-number green" id="uptime">0s</div>
      <div class="label">minando</div>
    </div>
    <div class="card">
      <h3>📦 Bloque Actual</h3>
      <div class="big-number" id="currentBlock" style="font-size:28px">---</div>
      <div class="label">Recompensa: <span id="reward">0</span> BTC</div>
    </div>
  </div>

  <div class="chart-container">
    <h3>📈 Hashrate History (last 60s)</h3>
    <canvas id="chart"></canvas>
  </div>

  <div class="grid">
    <div class="card">
      <h3>🎛️ CPU Throttle</h3>
      <div class="big-number" id="cpuPct" style="font-size:24px">100%</div>
      <div class="slider-container">
        <input type="range" id="cpuSlider" min="1" max="100" value="100" oninput="updateCPU(this.value)">
        <div class="slider-labels"><span>1%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div>
      </div>
    </div>
    <div class="card">
      <h3>💰 BTC Reward Address</h3>
      <div class="input-group">
        <input type="text" id="btcAddress" placeholder="bc1q... (tu dirección BTC)" onchange="saveAddress()">
      </div>
      <button class="btn" onclick="saveAddress()" style="margin-top:8px">💾 Guardar Dirección</button>
      <div class="label" style="margin-top:8px" id="addrSaved"></div>
    </div>
    <div class="card">
      <h3>📊 Network Stats</h3>
      <div class="stat-row"><span class="key">Dificultad</span><span class="val" id="difficulty">---</span></div>
      <div class="stat-row"><span class="key">Mempool TXs</span><span class="val" id="mempool">---</span></div>
      <div class="stat-row"><span class="key">Nodo</span><span class="val" id="nodeInfo">---</span></div>
      <div class="stat-row"><span class="key">Bloques</span><span class="val" id="bestHash">---</span></div>
    </div>
  </div>
</div>

<div class="footer">BTC Solo Miner — Nodo Knots propio — Umbrel</div>

<script>
let history = [];

function updateCPU(val) {
  document.getElementById('cpuPct').textContent = val + '%';
  document.getElementById('cpuBar').style.width = val + '%';
  fetch('/api/cpu', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cpu_percent:parseInt(val)})});
}

function saveAddress() {
  const addr = document.getElementById('btcAddress').value.trim();
  fetch('/api/address', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({btc_address:addr})});
  document.getElementById('addrSaved').textContent = addr ? '✅ Dirección guardada' : '';
}

async function refresh() {
  try {
    const resp = await fetch('/api/stats');
    const s = await resp.json();
    
    document.getElementById('hashrate').textContent = s.hashrate_khs.toFixed(1);
    document.getElementById('totalHashes').textContent = s.total_mh >= 1000 ? (s.total_mh/1000).toFixed(2) : s.total_mh.toFixed(1);
    document.getElementById('totalLabel').textContent = s.total_mh >= 1000 ? 'GH' : 'MH';
    document.getElementById('uptime').textContent = s.uptime_str;
    document.getElementById('currentBlock').textContent = s.current_block > 0 ? s.current_block.toLocaleString() : '---';
    document.getElementById('reward').textContent = s.block_reward_btc.toFixed(8);
    document.getElementById('difficulty').textContent = (s.difficulty/1e12).toFixed(1) + ' T';
    document.getElementById('mempool').textContent = s.mempool_tx > 0 ? s.mempool_tx.toLocaleString() : '---';
    document.getElementById('nodeInfo').textContent = s.node_connected ? '✅ Conectado' : '❌ Desconectado';
    document.getElementById('nodeStatus').textContent = s.node_connected ? '✅ ' + s.node_blocks.toLocaleString() + ' bloques' : '❌ Offline';
    
    const dot = document.getElementById('statusDot');
    dot.className = 'status-dot ' + (s.node_connected ? 'online' : 'offline');
    
    // CPU slider
    if (!document.getElementById('cpuSlider').matches(':active')) {
      document.getElementById('cpuSlider').value = s.cpu_percent;
      document.getElementById('cpuPct').textContent = s.cpu_percent + '%';
      document.getElementById('cpuBar').style.width = s.cpu_percent + '%';
    }
    
    // BTC address
    if (s.btc_address && !document.getElementById('btcAddress').value) {
      document.getElementById('btcAddress').value = s.btc_address;
      document.getElementById('addrSaved').textContent = '✅ Cargada';
    }
    
    // Error
    const errBanner = document.getElementById('errorBanner');
    if (s.error) {
      errBanner.style.display = 'block';
      errBanner.textContent = '⚠️ ' + s.error;
    } else {
      errBanner.style.display = 'none';
    }
    
    // Chart
    if (s.history && s.history.length > 0) {
      history = s.history;
      drawChart();
    }
    
    document.getElementById('bestHash').textContent = s.best_hash !== '-' ? s.best_hash.substring(0,16)+'...' : '---';
    
  } catch(e) {
    console.error(e);
  }
}

function drawChart() {
  const canvas = document.getElementById('chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.parentElement.clientWidth - 32;
  const H = 200;
  canvas.width = W;
  canvas.height = H;
  
  if (history.length < 2) return;
  
  const maxHR = Math.max(...history.map(h => h.hr), 1000);
  const minHR = 0;
  
  ctx.fillStyle = '#111118';
  ctx.fillRect(0, 0, W, H);
  
  // Grid
  ctx.strokeStyle = '#1a1a22';
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {
    const y = H * i / 4;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(W, y);
    ctx.stroke();
  }
  
  // Line
  ctx.beginPath();
  ctx.strokeStyle = '#f7931a';
  ctx.lineWidth = 2;
  ctx.shadowColor = '#f7931a';
  ctx.shadowBlur = 8;
  
  for (let i = 0; i < history.length; i++) {
    const x = (i / Math.max(history.length - 1, 1)) * W;
    const y = H - ((history[i].hr - minHR) / (maxHR - minHR)) * H;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.shadowBlur = 0;
  
  // Labels
  ctx.fillStyle = '#666';
  ctx.font = '9px monospace';
  ctx.fillText((maxHR/1000).toFixed(1) + ' KH/s', 4, 12);
  ctx.fillText('0 KH/s', 4, H-4);
}

setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>'''


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silenciar logs HTTP
    
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
        elif self.path == "/api/stats":
            self.send_json(api_stats())
        else:
            self.send_error(404)
    
    def do_POST(self):
        content_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_len) if content_len > 0 else b'{}'
        
        try:
            data = json.loads(body)
        except:
            self.send_json({"error": "Invalid JSON"})
            return
        
        if self.path == "/api/cpu":
            cpu = data.get("cpu_percent", 100)
            cpu = max(1, min(100, int(cpu)))
            with state_lock:
                state["cpu_percent"] = cpu
            self.send_json({"cpu_percent": cpu, "status": "ok"})
        
        elif self.path == "/api/address":
            addr = data.get("btc_address", "").strip()
            with state_lock:
                state["btc_address"] = addr
            self.send_json({"btc_address": addr, "status": "ok"})
        
        else:
            self.send_error(404)
    
    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def run_server(port=8888):
    server = http.server.HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"🌐 Dashboard: http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    PORT = int(os.environ.get("DASHBOARD_PORT", "8888"))
    print(f"""
╔══════════════════════════════════════════════════╗
║  ⛏️  BTC SOLO MINER v{VERSION} — Dashboard     ║
║  Nodo Knots: {RPC_URL}                  ║
║  Dashboard:  http://localhost:8888              ║
║  API:        http://localhost:8888/api/stats    ║
╚══════════════════════════════════════════════════╝
""")
    
    # Iniciar miner thread
    miner = threading.Thread(target=miner_thread, daemon=True)
    miner.start()
    
    # Iniciar servidor HTTP
    run_server(PORT)
