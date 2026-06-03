#!/usr/bin/env python3
"""
Stratum Miner HTTP — Cliente Ligero sin necesidad de puerto extra
==================================================================
Conecta al dashboard vía HTTP (pasa por el proxy de Umbrel).
No requiere túnel SSH ni puerto stratum abierto.

Uso: python3 stratum_miner_http.py [--url URL] [--user NOMBRE] [--cpu 100]

Flags:
  --url URL     URL del dashboard (default: http://192.168.100.81/)
  --user NAME   Nombre del minero (default: hostname)
  --cpu PCT     % de CPU a usar 1-100 (default: 100)
  --bench       Solo benchmark, no conecta
"""

import json
import struct
import hashlib
import time
import sys
import os
import argparse
import urllib.request
import socket as sock_module

VERSION = "1.0"

# ═══════════════════════ Crypto ═══════════════════════
def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

# ═══════════════════════ HTTP Stratum Miner ═══════════════════════
class HttpStratumMiner:
    def __init__(self, base_url, username, cpu_pct=100):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.cpu_pct = max(1, min(100, cpu_pct))
        
        # Estado del trabajo
        self.extranonce = None
        self.extranonce2_size = 4
        self.extranonce2 = 0
        self.job_id = None
        self.prevhash = None
        self.version = None
        self.bits = None
        self.ntime = None
        self.height = 0
        self.subscribed = False
        
        # Stats
        self.hashrate = 0
        self.total_hashes = 0
        self.shares_submitted = 0
        self.shares_accepted = 0
        self.last_job_id = None
    
    def api_call(self, method, endpoint, data=None):
        """Llama al API del dashboard."""
        url = f"{self.base_url}{endpoint}"
        try:
            if method == "GET":
                req = urllib.request.Request(url)
                req.add_header("User-Agent", f"stratum-miner-http/{VERSION}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())
            else:  # POST
                body = json.dumps(data).encode()
                req = urllib.request.Request(url, data=body, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", f"stratum-miner-http/{VERSION}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())
        except Exception as e:
            print(f"   ⚠️  API error: {e}")
            return None
    
    def subscribe(self):
        """Suscribe al stratum HTTP."""
        print(f"🔌 Conectando a {self.base_url}...")
        resp = self.api_call("GET", f"/api/mining/subscribe?user={self.username}")
        
        if not resp:
            print("❌ No se pudo conectar al dashboard")
            return False
        
        self.extranonce = resp.get("extranonce", "00000000")
        self.extranonce2_size = resp.get("extranonce2_size", 4)
        self.job_id = resp.get("job_id")
        self.prevhash = resp.get("prevhash")
        self.version = resp.get("version")
        self.bits = resp.get("bits")
        self.ntime = resp.get("ntime")
        self.height = resp.get("height", 0)
        self.subscribed = True
        
        print(f"✅ Conectado!")
        print(f"   Usuario: {self.username}")
        print(f"   Extranonce: {self.extranonce}")
        print(f"   Bloque: {self.height}")
        print(f"   Job: {self.job_id}")
        return True
    
    def refresh_work(self):
        """Refresca el trabajo desde el servidor."""
        resp = self.api_call("GET", f"/api/mining/subscribe?user={self.username}")
        if resp and resp.get("job_id"):
            new_job = resp.get("job_id")
            if new_job != self.job_id:
                self.job_id = new_job
                self.prevhash = resp.get("prevhash", self.prevhash)
                self.version = resp.get("version", self.version)
                self.bits = resp.get("bits", self.bits)
                self.ntime = resp.get("ntime", self.ntime)
                self.height = resp.get("height", self.height)
                self.last_job_id = new_job
                return True
        return False
    
    def submit_share(self, extranonce2, ntime, nonce):
        """Envía un share al servidor vía HTTP POST."""
        data = {
            "user": self.username,
            "extranonce": self.extranonce,
            "extranonce2": f"{extranonce2:08x}",
            "ntime": ntime,
            "nonce": f"{nonce:08x}",
        }
        resp = self.api_call("POST", "/api/mining/submit", data)
        self.shares_submitted += 1
        
        if resp and resp.get("result"):
            self.shares_accepted += 1
            if resp.get("block_found"):
                print(f"\n🎉🎉🎉 ¡BLOQUE ENCONTRADO! Hash: {resp.get('hash', '?')[:16]}...")
                return True
        return False
    
    def hash_header(self, extranonce2, ntime, nonce):
        """Hashea un header para un extranonce2 y nonce dados."""
        try:
            # Construir coinbase
            en1_bytes = bytes.fromhex(self.extranonce) if len(self.extranonce) <= 8 else struct.pack('<I', int(self.extranonce, 16))
            en2_bytes = struct.pack('<I', extranonce2)[:4]
            height = self.height
            cb_prefix = bytes.fromhex(
                f"01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff"
                f"{height:08x}{en1_bytes.hex()}"
            )
            coinbase = cb_prefix + en2_bytes + bytes.fromhex("ffffffff")
            merkleroot = sha256d(coinbase)[::-1]
            
            # Header
            header = bytearray(80)
            ver_int = int(self.version, 16) if isinstance(self.version, str) else self.version
            struct.pack_into('<I', header, 0, ver_int)
            
            prev_bytes = bytes.fromhex(self.prevhash) if isinstance(self.prevhash, str) else self.prevhash
            header[4:36] = prev_bytes[::-1]
            
            header[36:68] = merkleroot
            
            ntime_int = int(ntime, 16) if isinstance(ntime, str) else ntime
            struct.pack_into('<I', header, 68, ntime_int)
            
            bits_int = int(self.bits, 16) if isinstance(self.bits, str) else self.bits
            struct.pack_into('<I', header, 72, bits_int)
            
            struct.pack_into('<I', header, 76, nonce & 0xFFFFFFFF)
            
            return sha256d(bytes(header))[::-1]
        except:
            return b'\x00' * 32
    
    def mine_loop(self):
        """Loop principal de minería HTTP."""
        if not self.subscribed:
            return
        
        print(f"\n⛏️  Minando con {self.cpu_pct}% CPU (vía HTTP)...")
        print(f"   Ctrl+C para salir\n")
        
        work_ms = int(self.cpu_pct * 10)
        sleep_ms = max(0, 1000 - work_ms)
        
        last_report = time.time()
        hashes_this_second = 0
        last_work_refresh = 0
        
        while True:
            # Refrescar trabajo cada 10s
            if time.time() - last_work_refresh > 10:
                if self.refresh_work():
                    print(f"   🔄 Nuevo trabajo: job={self.job_id}")
                last_work_refresh = time.time()
            
            if self.job_id is None:
                time.sleep(1)
                continue
            
            cycle_start = time.time()
            
            while time.time() - cycle_start < 1.0:
                wstart = time.time()
                
                hashes = 0
                while time.time() - wstart < work_ms / 1000.0:
                    self.hash_header(self.extranonce2, self.ntime, self.extranonce2 + hashes)
                    hashes += 1
                    self.extranonce2 += 1
                    
                    # Submit cada ~2000 hashes
                    if hashes % 2000 == 0:
                        self.submit_share(self.extranonce2, self.ntime, self.extranonce2)
                    
                    if self.extranonce2 > 0xFFFF:
                        self.extranonce2 = 0
                    
                    if hashes % 50000 == 0:
                        break
                
                hashes_this_second += hashes
                
                if sleep_ms > 0:
                    time.sleep(sleep_ms / 1000.0)
            
            # Reporte cada 30s
            if time.time() - last_report >= 30:
                elapsed = time.time() - last_report
                hr = hashes_this_second / elapsed if elapsed > 0 else 0
                self.hashrate = hr
                self.total_hashes += hashes_this_second
                print(f"⚡ {hr/1000:.1f} KH/s | Total: {self.total_hashes/1e6:.1f} MH | "
                      f"Shares: {self.shares_submitted} ({self.shares_accepted} ok)")
                hashes_this_second = 0
                last_report = time.time()

# ═══════════════════════ Benchmark ═══════════════════════
def benchmark():
    print("⏱️  Benchmark de hashrate...")
    header = bytearray(80)
    for i in range(80):
        header[i] = i
    nonce = 0
    duration = 5
    start = time.time()
    hashes = 0
    while time.time() - start < duration:
        struct.pack_into('<I', header, 76, nonce)
        sha256d(bytes(header))
        hashes += 1
        nonce += 1
    elapsed = time.time() - start
    hr = hashes / elapsed
    print(f"   Hashrate: {hr/1000:.1f} KH/s ({hr:.0f} H/s)")
    print(f"   Tiempo: {elapsed:.1f}s, Hashes: {hashes:,}")
    return hr

# ═══════════════════════ Main ═══════════════════════
def main():
    parser = argparse.ArgumentParser(description="Stratum Miner HTTP - Solo Mining Client")
    parser.add_argument("--url", default="http://192.168.100.81/", help="Dashboard URL")
    parser.add_argument("--user", default=sock_module.gethostname(), help="Miner name")
    parser.add_argument("--cpu", type=int, default=100, help="CPU percent (1-100)")
    parser.add_argument("--bench", action="store_true", help="Benchmark only")
    args = parser.parse_args()
    
    print(f"""
╔══════════════════════════════════════════════════╗
║  ⛏️  STRATUM MINER HTTP v{VERSION}                  ║
╚══════════════════════════════════════════════════╝
""")
    
    if args.bench:
        benchmark()
        return
    
    print(f"   Dashboard: {args.url}")
    print(f"   User:      {args.user}")
    print(f"   CPU:       {args.cpu}%")
    print(f"   Transport: HTTP (via proxy Umbrel)")
    
    miner = HttpStratumMiner(args.url, args.user, args.cpu)
    
    try:
        if miner.subscribe():
            miner.mine_loop()
    except KeyboardInterrupt:
        print("\n👋 Apagando...")
        print(f"   Total: {miner.total_hashes/1e6:.1f} MH, Shares: {miner.shares_submitted}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
