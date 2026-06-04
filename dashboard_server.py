#!/usr/bin/env python3
"""
BTC Miner Dashboard v5.0 — Servidor HTTP + Minero + API
========================================================
  - Monitoreo en tiempo real (hashrate, total, uptime, bloque actual)
  - Último bloque minado en la red (hash, altura, TXs, nonce)
  - Progreso "cuánto falta" vs difficulty
  - BTC Price ticker, calculadora eléctrica
  - Halving countdown, mempool fees
  - Stratum clients monitor, event log
  - Gráfica hashrate (60s) + daily history
  - Control CPU, dirección BTC

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
VERSION = "5.0"

# ═══════════════════════════════════════════════════════════
# Estado global compartido (thread-safe)
# ═══════════════════════════════════════════════════════════
state = {
    "running": True,
    "hashrate": 0.0,
    "total_hashes": 0,
    "shares_found": 0,
    "blocks_found": 0,
    "best_hash": "-",
    "start_time": time.time(),
    "current_block": 0,
    "block_reward": 0.0,
    "difficulty": 0.0,
    "network_hashrate": 0.0,
    "mempool_tx": 0,
    "cpu_percent": 100,
    "btc_address": "bc1qwfgyfslh8gej5zx79kc622auxt42msns8v2p2v",
    "node_connected": False,
    "node_blocks": 0,
    "history": [],
    "last_template": None,
    "template_age": 0,
    "error": None,
    # Nuevos campos v5
    "event_log": [],           # Últimos 50 eventos
    "best_block_hash": "",
    "best_block_height": 0,
    "best_block_time": 0,
    "best_block_txs": 0,
    "best_block_size": 0,
    "best_block_nonce": 0,
    "best_block_difficulty": 0.0,
    "mempool_fee_low": 0.0,    # BTC/kB
    "mempool_fee_med": 0.0,
    "mempool_fee_high": 0.0,
    "network_connections": 0,
    "daily_hashes": [],        # [{date, hashes, avg_hr}]
}
state_lock = threading.Lock()

# ═══════════════════════ HTTP Stratum Clients ═══════════════════════
stratum_clients = {}
stratum_client_lock = threading.Lock()
NEXT_STRATUM_EXTRANONCE = 100000

# Persistencia de daily stats
DAILY_FILE = Path(__file__).parent / "miner_daily.json"


def add_event(event_type, message):
    """Añade un evento al log (thread-safe)."""
    with state_lock:
        state["event_log"].append({
            "time": time.time(),
            "type": event_type,
            "msg": message,
        })
        if len(state["event_log"]) > 50:
            state["event_log"] = state["event_log"][-50:]


def load_daily_stats():
    if DAILY_FILE.exists():
        try:
            with open(DAILY_FILE) as f:
                return json.load(f)
        except:
            pass
    return []


def save_daily_stats(daily):
    with open(DAILY_FILE, 'w') as f:
        json.dump(daily[-90:], f)  # Guardar últimos 90 días


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


def fetch_block_info():
    """Obtiene info del último bloque minado en la red."""
    try:
        best_hash = rpc_call("getbestblockhash")
        block = rpc_call("getblock", [best_hash])
        with state_lock:
            state["best_block_hash"] = best_hash
            state["best_block_height"] = block["height"]
            state["best_block_time"] = block["time"]
            state["best_block_txs"] = block.get("nTx", 0)
            state["best_block_size"] = block.get("size", 0)
            state["best_block_nonce"] = block.get("nonce", 0)
            state["best_block_difficulty"] = block.get("difficulty", 0.0)
            state["node_blocks"] = block["height"]
        add_event("block", f"Network block #{block['height']:,} mined: {best_hash[:16]}...")
        return True
    except Exception as e:
        add_event("error", f"Block info: {e}")
        return False


def fetch_fees():
    """Obtiene estimaciones de fees del mempool."""
    try:
        mp = rpc_call("getmempoolinfo")
        with state_lock:
            state["mempool_tx"] = mp.get("size", 0)
        # Fee estimation por prioridad
        for priority, key in [(1, "mempool_fee_high"), (3, "mempool_fee_med"), (6, "mempool_fee_low")]:
            try:
                fees = rpc_call("estimatesmartfee", [priority])
                rate = fees.get("feerate", 0)
                if rate and rate > 0:
                    with state_lock:
                        sat_per_vbyte = round(rate * 1e5, 1)  # BTC/kB → sat/vB
                        state[key] = sat_per_vbyte
            except:
                pass
    except:
        pass


def fetch_network():
    """Obtiene info de red."""
    try:
        net = rpc_call("getnetworkinfo")
        with state_lock:
            state["network_connections"] = net.get("connections", 0)
    except:
        pass


def block_info_poller():
    """Thread que actualiza info de bloque/fees periódicamente."""
    add_event("system", "Miner started — monitoring network")
    while True:
        with state_lock:
            if not state["running"]:
                break
        fetch_block_info()
        fetch_fees()
        fetch_network()
        time.sleep(30)  # Cada 30 segundos


def daily_checkpoint():
    """Guarda daily stats y resetea contador."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    daily = load_daily_stats()
    with state_lock:
        total_hashes = state["total_hashes"]
        hashrate = state["hashrate"]
    # Buscar si ya existe entry para hoy
    updated = False
    for d in daily:
        if d.get("date") == today:
            d["hashes"] = total_hashes
            d["avg_hr"] = round(hashrate / 1000, 1)
            updated = True
            break
    if not updated:
        daily.append({"date": today, "hashes": total_hashes, "avg_hr": round(hashrate / 1000, 1)})
    save_daily_stats(daily)
    with state_lock:
        state["daily_hashes"] = daily


# ═══════════════════════════════════════════════════════════
# Miner Thread
# ═══════════════════════════════════════════════════════════
def miner_thread():
    global state

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
        mine_fallback()
        return

    print(f"✅ Conectado a Knots: {info['blocks']:,} bloques")
    add_event("system", f"Connected to Knots node — {info['blocks']:,} blocks")

    last_template_time = 0
    template = None
    last_daily_check = time.time()

    while True:
        with state_lock:
            if not state["running"]:
                break
            cpu_pct = state["cpu_percent"]

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
                add_event("error", f"Template fetch: {e}")
                time.sleep(5)
                continue

        if template:
            with state_lock:
                state["template_age"] += 1
            hashes = mine_cycle(template, cpu_pct)
            with state_lock:
                state["total_hashes"] += hashes
        else:
            time.sleep(1)

        # Daily checkpoint cada 60s
        if now - last_daily_check > 60:
            daily_checkpoint()
            last_daily_check = now


def mine_cycle(template, cpu_pct):
    target = bits_to_target(template["bits"])
    version = template["version"]
    prevhash = bytes.fromhex(template["previousblockhash"])[::-1]
    bits = int(template["bits"], 16)
    curtime = int(template["curtime"])

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

    work_ms = int(cpu_pct * 10)
    sleep_ms = max(0, 1000 - work_ms)

    while time.time() - start < 1.0:
        work_start = time.time()
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
                add_event("block_found", f"BLOCK FOUND! Nonce: {nonce-1} Hash: {h.hex()[:20]}...")
                try:
                    result = rpc_call("submitblock", [bytes(header).hex()])
                    print(f"   submitblock: {result}")
                    add_event("block_found", f"Block submitted: {result}")
                except Exception as e:
                    print(f"   Error submitblock: {e}")
                    add_event("error", f"Submit block failed: {e}")

            if nonce >= 0xFFFFFFF0:
                nonce = 0
            if hashes % 50000 == 0 and time.time() - start > 0.1:
                break

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

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
    header = bytearray(80)
    nonce = 0
    add_event("error", "Node unavailable — mining in fallback mode")

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
    with state_lock:
        elapsed = time.time() - state["start_time"]
        total_mh = state["total_hashes"] / 1e6
        hashrate_hs = state["hashrate"]

        # Halving
        current_height = state["node_blocks"]
        halving_interval = 210000
        if current_height > 0:
            next_halving = ((current_height // halving_interval) + 1) * halving_interval
            blocks_remaining = next_halving - current_height
            halving_days = blocks_remaining * 10 / 60 / 24
        else:
            blocks_remaining = 0
            halving_days = 0

        # "Cuánto falta" — hashes hechos vs esperados por bloque
        avg_hashes_per_block = state["difficulty"] * (2**32) if state["difficulty"] > 0 else 0
        progress_pct = (state["total_hashes"] / avg_hashes_per_block * 100) if avg_hashes_per_block > 0 else 0

        return {
            "hashrate_khs": round(hashrate_hs / 1000, 2),
            "hashrate_hs": round(hashrate_hs, 0),
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
            "probability": calc_probability(hashrate_hs, state["difficulty"]),
            # Nuevos v5
            "best_block_hash": state["best_block_hash"],
            "best_block_height": state["best_block_height"],
            "best_block_time": state["best_block_time"],
            "best_block_txs": state["best_block_txs"],
            "best_block_size": state["best_block_size"],
            "best_block_nonce": state["best_block_nonce"],
            "best_block_difficulty": state["best_block_difficulty"],
            "mempool_fee_low": state["mempool_fee_low"],
            "mempool_fee_med": state["mempool_fee_med"],
            "mempool_fee_high": state["mempool_fee_high"],
            "network_connections": state["network_connections"],
            "halving_blocks": blocks_remaining,
            "halving_days": round(halving_days, 0),
            "avg_hashes_per_block": avg_hashes_per_block,
            "progress_pct": round(progress_pct, 10),
            "daily_hashes": state["daily_hashes"],
            "event_count": len(state["event_log"]),
        }


def api_events():
    """GET /api/events — últimos 50 eventos."""
    with state_lock:
        return {"events": list(state["event_log"])}


def api_stratum_clients():
    """GET /api/stratum — clientes conectados."""
    with stratum_client_lock:
        clients = []
        for user, data in stratum_clients.items():
            clients.append({
                "user": user,
                "extranonce": f"{data['extranonce']:08x}",
                "last_seen": round(time.time() - data["last_seen"], 0),
                "shares": data["shares"],
            })
        return {"clients": clients, "total": len(clients)}


def format_uptime(seconds):
    d, h = divmod(int(seconds), 86400)
    h, m = divmod(h, 3600)
    m, s = divmod(m, 60)
    if d > 0:
        return f"{d}d {h:02d}h {m:02d}m"
    elif h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def calc_probability(hashrate_hs, difficulty):
    if hashrate_hs <= 0 or difficulty <= 0:
        return {
            "expected_seconds": None,
            "expected_time_str": "N/A",
            "chance_per_day_pct": 0,
            "chance_per_year_pct": 0,
            "network_share_pct": 0,
            "avg_hashes_per_block": 0,
        }

    avg_hashes_per_block = difficulty * (2**32)
    expected_seconds = avg_hashes_per_block / hashrate_hs

    years = expected_seconds / (365.25 * 86400)
    days = expected_seconds / 86400
    hours = expected_seconds / 3600

    if years >= 1e6:
        time_str = f"{years/1e6:,.1f}M años"
    elif years >= 1:
        time_str = f"{years:,.0f} años"
    elif days >= 1:
        time_str = f"{days:,.0f} días"
    elif hours >= 1:
        time_str = f"{hours:,.0f} horas"
    else:
        mins = expected_seconds / 60
        time_str = f"{mins:,.1f} min"

    chance_per_day = (hashrate_hs * 86400) / avg_hashes_per_block * 100
    chance_per_year = chance_per_day * 365.25
    network_hashrate = 800e18
    network_share = (hashrate_hs / network_hashrate) * 100

    return {
        "expected_seconds": round(expected_seconds),
        "expected_time_str": time_str,
        "chance_per_day_pct": chance_per_day,
        "chance_per_year_pct": chance_per_year,
        "network_share_pct": network_share,
        "avg_hashes_per_block": avg_hashes_per_block,
    }


# ═══════════════════════════════════════════════════════════
# HTML Dashboard (inline)
# ═══════════════════════════════════════════════════════════
DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⛏️ BTC Solo Miner v''' + VERSION + r'''</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:'JetBrains Mono','Courier New',monospace;min-height:100vh;font-size:13px}
.header{background:#111118;border-bottom:1px solid #222;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.header h1{font-size:18px;color:#f7931a}
.header .btc-price{font-size:14px;color:#00d4ff;font-weight:bold}
.status-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}
.status-dot.online{background:#00ff88;box-shadow:0 0 8px #00ff88;animation:pulse 2s infinite}
.status-dot.offline{background:#ff4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.container{max-width:1500px;margin:0 auto;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:12px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.grid-3{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-bottom:12px}
.card{background:#111118;border:1px solid #222;border-radius:8px;padding:14px}
.card.full{grid-column:1/-1}
.card h3{font-size:10px;text-transform:uppercase;color:#888;margin-bottom:10px;letter-spacing:1.5px}
.big-number{font-size:30px;font-weight:bold;color:#f7931a;font-family:monospace}
.big-number.green{color:#00ff88}
.big-number.cyan{color:#00d4ff}
.big-number.red{color:#ff4444}
.big-number.small{font-size:22px}
.label{font-size:10px;color:#666;margin-top:3px}
.stat-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1a22;font-size:11px}
.stat-row .key{color:#888}
.stat-row .val{color:#ccc;font-family:monospace;font-size:11px}
.hash-display{font-family:monospace;font-size:11px;color:#f7931a;word-break:break-all;background:#0a0a0f;padding:6px 8px;border-radius:4px;margin-top:6px}
.slider-container{margin:10px 0}
.slider-container input[type=range]{width:100%;accent-color:#f7931a}
.slider-labels{display:flex;justify-content:space-between;font-size:9px;color:#555;margin-top:3px}
.input-group{margin:8px 0}
.input-group input{width:100%;background:#1a1a24;border:1px solid #333;color:#e0e0e0;padding:7px 10px;border-radius:4px;font-family:monospace;font-size:12px}
.input-group input:focus{outline:none;border-color:#f7931a}
.btn{background:#f7931a;color:#000;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:bold;font-family:monospace;font-size:11px;transition:background .2s}
.btn:hover{background:#ffab40}
.btn.small{padding:3px 8px;font-size:10px}
.chart-container{background:#111118;border:1px solid #222;border-radius:8px;padding:14px;margin-bottom:12px}
.chart-container h3{font-size:10px;text-transform:uppercase;color:#888;margin-bottom:6px;letter-spacing:1.5px}
canvas{width:100%;display:block}
.progress-bar{height:6px;background:#1a1a22;border-radius:3px;overflow:hidden;margin-top:8px}
.progress-fill{height:100%;background:linear-gradient(90deg,#f7931a,#ffab40);transition:width 0.5s;border-radius:3px}
.progress-fill.green{background:linear-gradient(90deg,#00ff88,#00d4ff)}
.event-log{max-height:220px;overflow-y:auto;font-size:10px}
.event-log::-webkit-scrollbar{width:4px}
.event-log::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
.event-item{padding:3px 6px;border-bottom:1px solid #1a1a22;display:flex;gap:8px}
.event-time{color:#555;white-space:nowrap;min-width:70px}
.event-msg{color:#aaa}
.event-item.type-block_found .event-msg{color:#00ff88;font-weight:bold}
.event-item.type-error .event-msg{color:#ff6666}
.event-item.type-block .event-msg{color:#f7931a}
.footer{text-align:center;padding:14px;color:#444;font-size:10px}
.error-banner{background:#331111;border:1px solid #661111;color:#ff6666;padding:8px 14px;border-radius:4px;margin-bottom:12px;font-size:11px}
.info-box{background:#0a0a0f;border:1px solid #2a2a33;border-radius:4px;padding:8px 10px;margin-top:6px;font-size:10px;color:#888;line-height:1.5}

@media(max-width:768px){
  .grid-2{grid-template-columns:1fr}
  .big-number{font-size:24px}
  .header{flex-direction:column;text-align:center}
}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1><span class="status-dot online" id="statusDot"></span>⛏️ BTC SOLO MINER v''' + VERSION + r'''</h1>
  </div>
  <div style="display:flex;gap:20px;align-items:center;font-size:11px;color:#888">
    <span>Nodo: <b id="nodeStatus" style="color:#ccc">---</b></span>
    <span>Red: <b id="netConns" style="color:#00d4ff">---</b></span>
    <span class="btc-price" id="btcPrice">BTC: ---</span>
    <span>⛏️ Halving en <b id="halvingCount" style="color:#f7931a">---</b></span>
  </div>
</div>

<div class="container">
  <div id="errorBanner" class="error-banner" style="display:none"></div>

  <!-- ROW 1: KPIs -->
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
      <div class="label" id="hashCount" style="color:#888;margin-top:4px"></div>
    </div>
    <div class="card">
      <h3>⏱️ Uptime</h3>
      <div class="big-number green" id="uptime">0s</div>
      <div class="label">minando</div>
      <div class="label" id="startTime" style="color:#555;margin-top:4px"></div>
    </div>
    <div class="card">
      <h3>📦 Bloque Actual</h3>
      <div class="big-number small" id="currentBlock">---</div>
      <div class="label">Reward: <span id="reward">0</span> BTC</div>
      <div class="label" style="color:#888">Mempool: <span id="mempool">---</span> TXs</div>
    </div>
    <div class="card">
      <h3>⚡ Eficiencia</h3>
      <div class="big-number green small" id="efficiency">---</div>
      <div class="label">KH/s por watt</div>
      <div class="info-box" style="margin-top:8px">
        <span id="elecCost">Ajusta TDP y $/kWh abajo</span>
      </div>
    </div>
  </div>

  <!-- ROW 2: ÚLTIMO BLOQUE (FULL WIDTH) -->
  <div class="card full" style="margin-bottom:12px">
    <h3>🔗 ÚLTIMO BLOQUE MINADO EN LA RED</h3>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:6px">
      <div>
        <div class="label">Hash</div>
        <div class="hash-display" id="bestBlockHash" style="font-size:11px">---</div>
      </div>
      <div>
        <div class="label">Altura</div>
        <div class="big-number small" id="bestBlockHeight" style="font-size:20px">---</div>
      </div>
      <div>
        <div class="label">Hora (UTC)</div>
        <div class="big-number cyan small" id="bestBlockTime" style="font-size:18px">---</div>
      </div>
      <div>
        <div class="label">TXs</div>
        <div class="big-number green small" id="bestBlockTxs" style="font-size:20px">---</div>
      </div>
      <div>
        <div class="label">Nonce</div>
        <div class="big-number small" id="bestBlockNonce" style="font-size:18px">---</div>
      </div>
      <div>
        <div class="label">Tamaño</div>
        <div class="big-number small" id="bestBlockSize" style="font-size:18px">---</div>
      </div>
    </div>
  </div>

  <!-- ROW 3: ¿CUÁNTO FALTA? + PROBABILIDAD -->
  <div class="grid-2">
    <div class="card">
      <h3>🎯 ¿CUÁNTO FALTA PARA MINAR UN BLOQUE?</h3>
      <div class="big-number green small" id="expectedTime" style="font-size:20px;margin-bottom:4px">---</div>
      <div class="label">Tiempo estimado a este hashrate</div>
      <div class="progress-bar" style="margin-top:12px;height:8px">
        <div class="progress-fill green" id="progressBar" style="width:0%"></div>
      </div>
      <div class="label" style="margin-top:4px" id="progressLabel"></div>
      <div class="info-box" style="margin-top:10px" id="howFarBox"></div>
    </div>
    <div class="card">
      <h3>📊 PROBABILIDAD</h3>
      <div class="stat-row"><span class="key">Chance por día</span><span class="val" id="probDay">---</span></div>
      <div class="stat-row"><span class="key">Chance por año</span><span class="val" id="probYear">---</span></div>
      <div class="stat-row"><span class="key">Chance esta década</span><span class="val" id="probDecade">---</span></div>
      <div class="stat-row"><span class="key">Share vs red</span><span class="val" id="netShare">---</span></div>
      <div class="stat-row"><span class="key">Hashes por bloque</span><span class="val" id="hashesPerBlock">---</span></div>
      <div class="stat-row"><span class="key">Dificultad</span><span class="val" id="difficulty">---</span></div>
    </div>
  </div>

  <!-- ROW 4: CHARTS -->
  <div class="grid-2">
    <div class="chart-container">
      <h3>📈 Hashrate (últimos 60s)</h3>
      <canvas id="chartRealtime" style="height:180px"></canvas>
    </div>
    <div class="chart-container">
      <h3>📊 Hashes Diarios</h3>
      <canvas id="chartDaily" style="height:180px"></canvas>
    </div>
  </div>

  <!-- ROW 5: EVENT LOG + STRATUM + MEMPOOL -->
  <div class="grid-3">
    <div class="card">
      <h3>📋 EVENT LOG</h3>
      <div class="event-log" id="eventLog"></div>
    </div>
    <div class="card">
      <h3>🌐 STRATUM CLIENTS</h3>
      <div id="stratumClients" style="font-size:11px;color:#888">---</div>
    </div>
    <div class="card">
      <h3>💸 MEMPOOL FEES</h3>
      <div class="stat-row"><span class="key">Alta prioridad (1 bloque)</span><span class="val" id="feeHigh">---</span></div>
      <div class="stat-row"><span class="key">Media (3 bloques)</span><span class="val" id="feeMed">---</span></div>
      <div class="stat-row"><span class="key">Baja (6 bloques)</span><span class="val" id="feeLow">---</span></div>
      <div class="stat-row"><span class="key">TXs en mempool</span><span class="val" id="mempoolTxs2">---</span></div>
      <div class="info-box" style="margin-top:8px" id="feeInfo">
        Fees extra por bloque ≈ TXs × fee medio
      </div>
    </div>
  </div>

  <!-- ROW 6: CONTROLS + CALCULATOR -->
  <div class="grid-3">
    <div class="card">
      <h3>🎛️ CPU THROTTLE</h3>
      <div class="big-number small" id="cpuPct" style="font-size:22px">100%</div>
      <div class="slider-container">
        <input type="range" id="cpuSlider" min="1" max="100" value="100" oninput="updateCPU(this.value)">
        <div class="slider-labels"><span>1%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div>
      </div>
    </div>
    <div class="card">
      <h3>💰 BTC ADDRESS</h3>
      <div class="input-group">
        <input type="text" id="btcAddress" placeholder="bc1q... (tu dirección BTC)" onchange="saveAddress()">
      </div>
      <button class="btn" onclick="saveAddress()">💾 Guardar</button>
      <div class="label" style="margin-top:6px" id="addrSaved"></div>
    </div>
    <div class="card">
      <h3>💵 ELECTRICIDAD</h3>
      <div class="input-group">
        <input type="number" id="elecTDP" placeholder="TDP CPU (watts, ej: 6)" value="6" oninput="calcElectricity()" style="width:48%;display:inline-block">
        <input type="number" id="elecRate" placeholder="$/kWh (ej: 0.12)" value="0.12" step="0.01" oninput="calcElectricity()" style="width:48%;display:inline-block;margin-left:4%">
      </div>
      <div class="stat-row"><span class="key">Costo diario</span><span class="val" id="costDaily">---</span></div>
      <div class="stat-row"><span class="key">Costo mensual</span><span class="val" id="costMonthly">---</span></div>
      <div class="stat-row"><span class="key">Costo anual</span><span class="val" id="costYearly">---</span></div>
      <div class="info-box" style="margin-top:8px" id="profitNote"></div>
    </div>
  </div>
</div>

<div class="footer">BTC Solo Miner v''' + VERSION + r''' — Nodo Knots — Umbrel — N150 @ ~600 KH/s</div>

<script>
// State
let history = [];
let dailyData = [];
let btcPriceUsd = 0;
let lastBlockTime = 0;

// CoinGecko free API
async function fetchBTCPrice() {
  try {
    const r = await fetch('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd');
    const d = await r.json();
    btcPriceUsd = d.bitcoin.usd;
    document.getElementById('btcPrice').textContent = 'BTC: $' + btcPriceUsd.toLocaleString();
  } catch(e) {
    document.getElementById('btcPrice').textContent = 'BTC: ---';
  }
}
fetchBTCPrice();
setInterval(fetchBTCPrice, 120000);

function updateCPU(val) {
  document.getElementById('cpuPct').textContent = val + '%';
  document.getElementById('cpuBar').style.width = val + '%';
  fetch('/api/cpu', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cpu_percent:parseInt(val)})
  });
}

function saveAddress() {
  const addr = document.getElementById('btcAddress').value.trim();
  fetch('/api/address', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({btc_address:addr})
  });
  document.getElementById('addrSaved').textContent = addr ? '✅ Guardada' : '';
}

function formatHash(n) {
  if (n >= 1e18) return (n/1e18).toFixed(1) + ' EH';
  if (n >= 1e15) return (n/1e15).toFixed(1) + ' PH';
  if (n >= 1e12) return (n/1e12).toFixed(1) + ' TH';
  if (n >= 1e9) return (n/1e9).toFixed(1) + ' GH';
  if (n >= 1e6) return (n/1e6).toFixed(1) + ' MH';
  if (n >= 1e3) return (n/1e3).toFixed(1) + ' KH';
  return n.toFixed(0) + ' H';
}

function formatNum(n) {
  if (n === null || n === undefined) return '---';
  if (typeof n === 'number') {
    if (n >= 1e12) return (n/1e12).toFixed(2) + ' T';
    if (n >= 1e9) return (n/1e9).toFixed(2) + ' B';
    if (n >= 1e6) return (n/1e6).toFixed(2) + ' M';
    if (n >= 1e3) return n.toLocaleString();
    if (n < 0.000001) return n.toExponential(2);
    return n.toFixed(6);
  }
  return String(n);
}

function formatTime(ts) {
  if (!ts || ts === 0) return '---';
  const d = new Date((ts + 21600) * 1000); // UTC-6 rough
  return d.toISOString().replace('T',' ').substring(0,19);
}

// Event log update
let lastEventCount = 0;
async function updateEvents() {
  try {
    const r = await fetch('/api/events');
    const d = await r.json();
    if (d.events.length > lastEventCount) {
      const container = document.getElementById('eventLog');
      const frag = document.createDocumentFragment();
      for (let i = lastEventCount; i < d.events.length; i++) {
        const ev = d.events[i];
        const div = document.createElement('div');
        div.className = 'event-item type-' + ev.type;
        const ts = new Date(ev.time * 1000).toISOString().substring(11,19);
        div.innerHTML = '<span class="event-time">' + ts + '</span><span class="event-msg">' + ev.msg + '</span>';
        frag.appendChild(div);
      }
      container.appendChild(frag);
      container.scrollTop = container.scrollHeight;
      lastEventCount = d.events.length;
    }
  } catch(e) {}
}

// Stratum clients
async function updateStratum() {
  try {
    const r = await fetch('/api/stratum');
    const d = await r.json();
    const el = document.getElementById('stratumClients');
    if (!d.clients || d.clients.length === 0) {
      el.innerHTML = '<div style="color:#555;font-style:italic">No hay clientes stratum conectados</div>';
      return;
    }
    let html = '<table style="width:100%;font-size:10px;border-collapse:collapse">';
    html += '<tr style="color:#888"><th style="text-align:left">User</th><th>Extranonce</th><th>Seen</th><th>Shares</th></tr>';
    for (const c of d.clients) {
      html += '<tr style="border-bottom:1px solid #1a1a22">';
      html += '<td style="padding:3px 0;color:#ccc">' + c.user.substring(0,15) + '</td>';
      html += '<td style="color:#f7931a;text-align:center">' + c.extranonce + '</td>';
      html += '<td style="text-align:center;color:' + (c.last_seen < 60 ? '#00ff88' : '#ffaa00') + '">' + c.last_seen + 's</td>';
      html += '<td style="text-align:center;color:#00d4ff">' + c.shares + '</td>';
      html += '</tr>';
    }
    html += '</table>';
    el.innerHTML = html;
  } catch(e) {}
}

function calcElectricity() {
  const tdp = parseFloat(document.getElementById('elecTDP').value) || 6;
  const rate = parseFloat(document.getElementById('elecRate').value) || 0.12;
  const kWhPerDay = (tdp / 1000) * 24;
  const costDay = kWhPerDay * rate;
  document.getElementById('costDaily').textContent = '$' + costDay.toFixed(4);
  document.getElementById('costMonthly').textContent = '$' + (costDay * 30).toFixed(2);
  document.getElementById('costYearly').textContent = '$' + (costDay * 365).toFixed(2);
  document.getElementById('profitNote').textContent = '⚡ ' + tdp + 'W × 24h = ' + kWhPerDay.toFixed(2) + ' kWh/día';
}

async function refresh() {
  try {
    const resp = await fetch('/api/stats');
    const s = await resp.json();

    // Hashrate
    document.getElementById('hashrate').textContent = s.hashrate_khs.toFixed(1);
    document.getElementById('totalHashes').textContent = s.total_mh >= 1000 ? (s.total_mh/1000).toFixed(2) : s.total_mh.toFixed(1);
    document.getElementById('totalLabel').textContent = s.total_mh >= 1000 ? 'GH' : 'MH';
    document.getElementById('hashCount').textContent = s.total_hashes.toLocaleString() + ' hashes totales';
    document.getElementById('uptime').textContent = s.uptime_str;
    document.getElementById('currentBlock').textContent = s.current_block > 0 ? s.current_block.toLocaleString() : '---';
    document.getElementById('reward').textContent = s.block_reward_btc.toFixed(8);
    document.getElementById('mempool').textContent = s.mempool_tx > 0 ? s.mempool_tx.toLocaleString() : '---';
    document.getElementById('difficulty').textContent = (s.difficulty/1e12).toFixed(1) + ' T';
    document.getElementById('nodeStatus').textContent = s.node_connected ? '✅ ' + s.node_blocks.toLocaleString() + ' bloques' : '❌ Offline';
    document.getElementById('netConns').textContent = (s.network_connections || 0) + ' peers';

    const dot = document.getElementById('statusDot');
    dot.className = 'status-dot ' + (s.node_connected ? 'online' : 'offline');

    // CPU
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

    // ÚLTIMO BLOQUE
    document.getElementById('bestBlockHash').textContent = s.best_block_hash || '---';
    document.getElementById('bestBlockHeight').textContent = s.best_block_height ? s.best_block_height.toLocaleString() : '---';
    document.getElementById('bestBlockTime').textContent = s.best_block_time ? formatTime(s.best_block_time) : '---';
    document.getElementById('bestBlockTxs').textContent = s.best_block_txs ? s.best_block_txs.toLocaleString() : '---';
    document.getElementById('bestBlockNonce').textContent = s.best_block_nonce ? s.best_block_nonce.toLocaleString() : '---';
    document.getElementById('bestBlockSize').textContent = s.best_block_size ? (s.best_block_size/1e6).toFixed(2) + ' MB' : '---';

    // Halving
    if (s.halving_blocks > 0) {
      document.getElementById('halvingCount').textContent = s.halving_blocks.toLocaleString() + ' bloques (~' + s.halving_days.toLocaleString() + ' días)';
    }

    // ¿CUÁNTO FALTA?
    if (s.probability) {
      const p = s.probability;
      document.getElementById('expectedTime').textContent = p.expected_time_str;
      document.getElementById('progressBar').style.width = Math.min(100, s.progress_pct).toFixed(6) + '%';
      document.getElementById('progressLabel').textContent = 'Progreso de sesión: ' + formatHash(s.total_hashes) + ' de ' + formatHash(s.avg_hashes_per_block) + ' necesarios (promedio)';
      
      const howFar = document.getElementById('howFarBox');
      const hrs = p.expected_seconds / 3600;
      howFar.innerHTML = '⏱️ A ' + formatHash(s.hashrate_hs) + '/s, necesitas ~' + 
        (hrs >= 8760 ? (hrs/8760).toFixed(1) + ' años' : hrs.toFixed(0) + ' horas') + 
        ' para expectativa de 1 bloque<br>📊 ' + 
        formatHash(s.total_hashes) + ' hashes hechos de ' + formatHash(s.avg_hashes_per_block) + ' necesarios por bloque';

      // Probability
      if (p.chance_per_day_pct < 0.000001) {
        document.getElementById('probDay').textContent = p.chance_per_day_pct.toExponential(2) + '%';
      } else {
        document.getElementById('probDay').textContent = p.chance_per_day_pct.toFixed(8) + '%';
      }
      if (p.chance_per_year_pct < 0.000001) {
        document.getElementById('probYear').textContent = p.chance_per_year_pct.toExponential(2) + '%';
      } else {
        document.getElementById('probYear').textContent = p.chance_per_year_pct.toFixed(6) + '%';
      }
      document.getElementById('probDecade').textContent = (p.chance_per_year_pct * 10).toExponential(2) + '%';
      document.getElementById('netShare').textContent = p.network_share_pct.toExponential(2) + '%';
      document.getElementById('hashesPerBlock').textContent = formatHash(p.avg_hashes_per_block);
    }

    // Efficiency
    const tdp = parseFloat(document.getElementById('elecTDP').value) || 6;
    const eff = s.hashrate_hs / tdp;
    document.getElementById('efficiency').textContent = (eff/1000).toFixed(0);
    document.getElementById('elecCost').textContent = '~' + (eff/1000).toFixed(0) + ' KH/s/W @ ' + tdp + 'W';

    // Fees
    document.getElementById('feeHigh').textContent = s.mempool_fee_high > 0 ? s.mempool_fee_high.toFixed(1) + ' sat/vB' : '---';
    document.getElementById('feeMed').textContent = s.mempool_fee_med > 0 ? s.mempool_fee_med.toFixed(1) + ' sat/vB' : '---';
    document.getElementById('feeLow').textContent = s.mempool_fee_low > 0 ? s.mempool_fee_low.toFixed(1) + ' sat/vB' : '---';
    document.getElementById('mempoolTxs2').textContent = s.mempool_tx > 0 ? s.mempool_tx.toLocaleString() : '---';

    // Charts
    if (s.history && s.history.length > 0) {
      history = s.history;
      drawRealtimeChart();
    }
    if (s.daily_hashes && s.daily_hashes.length > 0) {
      dailyData = s.daily_hashes;
      drawDailyChart();
    }

  } catch(e) {
    console.error(e);
  }
}

function drawRealtimeChart() {
  const canvas = document.getElementById('chartRealtime');
  if (!canvas || history.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.parentElement.clientWidth - 28;
  const H = 180;
  canvas.width = W;
  canvas.height = H;

  const maxHR = Math.max(...history.map(h => h.hr), 1000);
  ctx.fillStyle = '#111118';
  ctx.fillRect(0, 0, W, H);

  ctx.strokeStyle = '#1a1a22';
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {
    const y = H * i / 4;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  ctx.beginPath();
  ctx.strokeStyle = '#f7931a';
  ctx.lineWidth = 2;
  ctx.shadowColor = '#f7931a';
  ctx.shadowBlur = 8;
  for (let i = 0; i < history.length; i++) {
    const x = (i / Math.max(history.length - 1, 1)) * W;
    const y = H - (history[i].hr / maxHR) * H;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.shadowBlur = 0;

  ctx.fillStyle = '#666';
  ctx.font = '9px monospace';
  ctx.fillText((maxHR/1000).toFixed(1) + ' KH/s', 4, 12);
  ctx.fillText('0', 4, H-4);
}

function drawDailyChart() {
  const canvas = document.getElementById('chartDaily');
  if (!canvas || dailyData.length === 0) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.parentElement.clientWidth - 28;
  const H = 180;
  canvas.width = W;
  canvas.height = H;

  const maxH = Math.max(...dailyData.map(d => d.hashes || 0), 1);
  ctx.fillStyle = '#111118';
  ctx.fillRect(0, 0, W, H);

  ctx.strokeStyle = '#1a1a22';
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {
    const y = H * i / 4;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  const barW = Math.max(2, (W / dailyData.length) - 2);
  for (let i = 0; i < dailyData.length; i++) {
    const x = (i / dailyData.length) * W;
    const barH = (dailyData[i].hashes / maxH) * (H - 20);
    ctx.fillStyle = i === dailyData.length - 1 ? '#00ff88' : '#f7931a';
    ctx.fillRect(x + 1, H - barH - 15, barW, barH);
  }

  ctx.fillStyle = '#666';
  ctx.font = '9px monospace';
  ctx.fillText(formatHash(maxH) + '/día', 4, 12);
}

// Polling
setInterval(refresh, 1000);
setInterval(updateEvents, 5000);
setInterval(updateStratum, 10000);
refresh();
updateEvents();
updateStratum();
calcElectricity();
</script>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════════════════
class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
        elif self.path.startswith("/api/mining/subscribe"):
            self.handle_stratum_subscribe()
        elif self.path == "/api/stats":
            self.send_json(api_stats())
        elif self.path == "/api/events":
            self.send_json(api_events())
        elif self.path == "/api/stratum":
            self.send_json(api_stratum_clients())
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

        elif self.path == "/api/mining/submit":
            self.handle_stratum_submit(data)

        else:
            self.send_error(404)

    def handle_stratum_subscribe(self):
        global NEXT_STRATUM_EXTRANONCE
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        user = qs.get("user", ["anonymous"])[0]

        with stratum_client_lock:
            if user in stratum_clients:
                extranonce = stratum_clients[user]["extranonce"]
            else:
                extranonce = NEXT_STRATUM_EXTRANONCE
                NEXT_STRATUM_EXTRANONCE += 1
                stratum_clients[user] = {
                    "extranonce": extranonce,
                    "last_seen": time.time(),
                    "shares": 0,
                    "hashrate": 0,
                }
            stratum_clients[user]["last_seen"] = time.time()

        with state_lock:
            tpl = state.get("last_template")

        work = {
            "subscription": f"{user}:{extranonce:08x}",
            "extranonce": f"{extranonce:08x}",
            "extranonce2_size": 4,
            "user": user,
        }
        if tpl:
            work.update({
                "job_id": f"{tpl.get('height', 0)}:{tpl.get('curtime', 0)}",
                "prevhash": tpl.get("previousblockhash", ""),
                "version": f"{tpl.get('version', 0):08x}",
                "bits": tpl.get("bits", ""),
                "ntime": f"{tpl.get('curtime', 0):08x}",
                "height": tpl.get("height", 0),
                "target": tpl.get("target", ""),
            })

        self.send_json(work)

    def handle_stratum_submit(self, data):
        user = data.get("user", "unknown")
        extranonce = data.get("extranonce", "0")
        extranonce2 = data.get("extranonce2", "0")
        ntime = data.get("ntime", "0")
        nonce = data.get("nonce", "0")

        with stratum_client_lock:
            if user in stratum_clients:
                stratum_clients[user]["last_seen"] = time.time()
                stratum_clients[user]["shares"] += 1

        with state_lock:
            tpl = state.get("last_template")
            difficulty = state["difficulty"]

        if not tpl:
            self.send_json({"result": False, "reason": "no template"})
            return

        try:
            version = int(tpl["version"])
            prevhash = bytes.fromhex(tpl["previousblockhash"])[::-1]
            bits_hex = tpl["bits"]
            target = bits_to_target(bits_hex)
            curtime = int(ntime, 16) if isinstance(ntime, str) else ntime
            en2 = struct.pack('<I', int(extranonce2, 16) if isinstance(extranonce2, str) else extranonce2)[:4]

            height = tpl["height"]
            en1_int = int(extranonce, 16) if isinstance(extranonce, str) else extranonce
            cb_prefix = bytes.fromhex(
                f"01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff"
                f"{height:08x}{en1_int:08x}"
            )
            coinbase = cb_prefix + en2 + bytes.fromhex("ffffffff")
            merkleroot = sha256d(coinbase)[::-1]

            header = bytearray(80)
            struct.pack_into('<I', header, 0, version)
            header[4:36] = prevhash
            header[36:68] = merkleroot
            struct.pack_into('<I', header, 68, curtime)
            struct.pack_into('<I', header, 72, int(bits_hex, 16))
            n = int(nonce, 16) if isinstance(nonce, str) else nonce
            struct.pack_into('<I', header, 76, n & 0xFFFFFFFF)

            block_hash = sha256d(bytes(header))[::-1]

            with state_lock:
                state["shares_found"] += 1

            if block_hash < target:
                with state_lock:
                    state["blocks_found"] += 1
                    state["best_hash"] = block_hash.hex()
                print(f"\n🎉 HTTP BLOQUE! User: {user} Nonce: {n} Hash: {block_hash.hex()[:16]}...")
                add_event("block_found", f"HTTP BLOCK FOUND! {block_hash.hex()[:20]}... by {user}")
                try:
                    result = rpc_call("submitblock", [bytes(header).hex()])
                    print(f"   submitblock: {result}")
                except Exception as e:
                    print(f"   Error submitblock: {e}")
                self.send_json({"result": True, "block_found": True, "hash": block_hash.hex()})
            else:
                self.send_json({"result": True, "block_found": False})

        except Exception as e:
            self.send_json({"result": False, "reason": str(e)})

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

    # Cargar daily stats
    with state_lock:
        state["daily_hashes"] = load_daily_stats()

    # Iniciar miner thread
    miner = threading.Thread(target=miner_thread, daemon=True)
    miner.start()

    # Iniciar block info poller
    poller = threading.Thread(target=block_info_poller, daemon=True)
    poller.start()

    # Daily checkpoint inicial
    daily_checkpoint()

    # Iniciar servidor HTTP
    run_server(PORT)
