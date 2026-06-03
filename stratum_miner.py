#!/usr/bin/env python3
"""
Stratum Miner — Cliente Ligero para Solo Mining
================================================
Conecta al stratum server local y mina con CPU.
Ejecutar en cualquier máquina de la red.

Uso: python3 stratum_miner.py [--host IP] [--port 3333] [--user NOMBRE] [--cpu 100]

Flags:
  --host HOST     IP del stratum server (default: 192.168.100.81)
  --port PORT     Puerto stratum (default: 3333)
  --user NAME     Nombre del minero (default: hostname)
  --cpu PCT       % de CPU a usar 1-100 (default: 100)
  --bench         Solo benchmark, no conecta
"""

import socket
import json
import struct
import hashlib
import time
import threading
import sys
import os
import argparse
import socket as sock_module

VERSION = "1.0"

# ═══════════════════════ Crypto ═══════════════════════
def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

# ═══════════════════════ Stratum Miner ═══════════════════════
class StratumMiner:
    def __init__(self, host, port, username, cpu_pct=100):
        self.host = host
        self.port = port
        self.username = username
        self.cpu_pct = max(1, min(100, cpu_pct))
        self.sock = None
        
        # Estado
        self.extranonce1 = None
        self.extranonce2_size = 4
        self.extranonce2 = 0
        self.job_id = None
        self.prevhash = None
        self.coinbase1 = None
        self.coinbase2 = None
        self.merkle_branches = []
        self.version = None
        self.bits = None
        self.ntime = None
        self.clean_jobs = True
        
        # Stats
        self.hashrate = 0
        self.total_hashes = 0
        self.shares_submitted = 0
        self.start_time = time.time()
        self.running = True
        
        # Lock para trabajo
        self.work_lock = threading.Lock()
        self.new_work = threading.Event()
    
    def connect(self):
        """Conecta al stratum server."""
        print(f"🔌 Conectando a {self.host}:{self.port}...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(300)
        self.sock.connect((self.host, self.port))
        print(f"✅ Conectado!")
        self._buffer = b""
        
        # Suscribirse
        self._send({"id": 1, "method": "mining.subscribe", "params": [
            f"stratum-miner/{VERSION}", "EthereumStratum/1.0.0"
        ]})
        
        # Leer respuestas hasta recibir notify con trabajo
        while self.job_id is None and self.running:
            msg = self._recv_line()
            if msg is None:
                time.sleep(0.1)
                continue
            
            msg_id = msg.get("id")
            method = msg.get("method")
            
            if msg_id == 1 and "result" in msg:
                # Subscribe response
                result = msg["result"]
                self.extranonce1 = result[1]
                self.extranonce2_size = result[2]
                print(f"   Subscrito: extranonce1={self.extranonce1}, size={self.extranonce2_size}")
            
            elif method == "mining.set_difficulty":
                print(f"   Difficulty: {msg['params'][0]}")
            
            elif msg_id == 2 and "result" in msg:
                print(f"   ✅ Autorizado como '{self.username}'")
            
            elif method == "mining.notify":
                self._process_notify(msg["params"])
                print(f"   📦 Trabajo: job={self.job_id[:8]}...")
            
            # Si ya tenemos extranonce y no hemos autorizado, autorizar
            if self.extranonce1 and not hasattr(self, '_auth_sent'):
                self._auth_sent = True
                self._send({"id": 2, "method": "mining.authorize", "params": [self.username, "x"]})
        
        # Iniciar thread de recepción para resto de mensajes
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()
    
    def _send(self, msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode())
    
    def _recv_line(self):
        """Lee una línea JSON del socket con buffer."""
        try:
            while b"\n" not in self._buffer:
                data = self.sock.recv(4096)
                if not data:
                    return None
                self._buffer += data
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line.strip():
                return json.loads(line.decode())
        except:
            pass
        return None
    
    def _recv(self):
        """Legado - usa _recv_line internamente."""
        return self._recv_line()
    
    def _recv_loop(self):
        """Loop de recepción para mining.notify y mining.set_difficulty."""
        buffer = b""
        while self.running:
            try:
                data = self.sock.recv(4096)
                if not data:
                    print("❌ Servidor cerró conexión")
                    self.running = False
                    break
                
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        msg = json.loads(line.decode())
                        method = msg.get("method")
                        
                        if method == "mining.notify":
                            self._process_notify(msg["params"])
                        elif method == "mining.set_difficulty":
                            pass  # Ignorar por ahora
                        elif msg.get("result") is not None:
                            pass  # Respuesta a submit
            except Exception as e:
                if self.running:
                    print(f"⚠️  Error recv: {e}")
                time.sleep(5)
    
    def _process_notify(self, params):
        """Procesa mining.notify con nuevo trabajo."""
        with self.work_lock:
            self.job_id = params[0]
            self.prevhash = params[1]
            self.coinbase1 = params[2]
            self.merkle_branches = params[3]
            self.version = params[4]
            self.bits = params[5]
            self.ntime = params[6]
            self.clean_jobs = params[7]
            
            if self.clean_jobs:
                self.extranonce2 = 0
        
        self.new_work.set()
    
    def mine_loop(self):
        """Loop principal de minería."""
        print(f"\n⛏️  Minando con {self.cpu_pct}% CPU...")
        print(f"   Ctrl+C para salir\n")
        
        work_ms = int(self.cpu_pct * 10)
        sleep_ms = max(0, 1000 - work_ms)
        
        last_report = time.time()
        hashes_this_second = 0
        
        while self.running:
            # Esperar trabajo
            if self.job_id is None:
                print("   Esperando trabajo del servidor...")
                self.new_work.wait(timeout=10)
                self.new_work.clear()
                continue
            
            cycle_start = time.time()
            
            while time.time() - cycle_start < 1.0:
                wstart = time.time()
                
                # Minar
                hashes = 0
                while time.time() - wstart < work_ms / 1000.0:
                    with self.work_lock:
                        job = self.job_id
                        prev = self.prevhash
                        cb1 = self.coinbase1
                        ver = self.version
                        bts = self.bits
                        ntime_val = self.ntime
                        en1 = self.extranonce1
                        en2 = self.extranonce2
                    
                    if job is None:
                        break
                    
                    # Construir y hashear header
                    share = self._hash_header(ver, prev, cb1, en1, en2, ntime_val, bts)
                    hashes += 1
                    self.extranonce2 += 1
                    
                    # Submit cada 1000 hashes (opcional, ajustable)
                    if hashes % 1000 == 0:
                        self._submit_share(job, en2, ntime_val, self.extranonce2 - 1)
                    
                    if self.extranonce2 > 0xFFFF:
                        self.extranonce2 = 0
                    
                    if hashes % 50000 == 0 and time.time() - wstart > 0.05:
                        break
                
                hashes_this_second += hashes
                
                # Dormir
                if sleep_ms > 0:
                    time.sleep(sleep_ms / 1000.0)
            
            # Reporte cada 30s
            if time.time() - last_report >= 30:
                elapsed = time.time() - last_report
                hr = hashes_this_second / elapsed if elapsed > 0 else 0
                self.hashrate = hr
                print(f"⚡ {hr/1000:.1f} KH/s | Total: {self.total_hashes/1e6:.1f} MH | "
                      f"Shares: {self.shares_submitted}")
                self.total_hashes += hashes_this_second
                hashes_this_second = 0
                last_report = time.time()
    
    def _hash_header(self, version, prevhash, coinbase1, extranonce1_hex, extranonce2, ntime, bits):
        """Hashea un header de 80 bytes."""
        try:
            # Construir coinbase
            cb1 = bytes.fromhex(coinbase1) if isinstance(coinbase1, str) else coinbase1
            en2_bytes = struct.pack('<I', extranonce2)[:4]
            coinbase = cb1 + en2_bytes
            
            # Merkle root simplificado (coinbase solo)
            merkleroot = sha256d(coinbase)[::-1]
            
            # Header
            header = bytearray(80)
            struct.pack_into('<I', header, 0, int(version, 16) if isinstance(version, str) else version)
            
            prev_bytes = bytes.fromhex(prevhash) if isinstance(prevhash, str) else prevhash
            header[4:36] = prev_bytes[::-1]
            
            header[36:68] = merkleroot
            
            ntime_int = int(ntime, 16) if isinstance(ntime, str) else ntime
            struct.pack_into('<I', header, 68, ntime_int)
            
            bits_int = int(bits, 16) if isinstance(bits, str) else bits
            struct.pack_into('<I', header, 72, bits_int)
            
            # Nonce = extranonce2
            struct.pack_into('<I', header, 76, extranonce2 & 0xFFFFFFFF)
            
            return sha256d(bytes(header))[::-1]
        except:
            return b'\x00' * 32
    
    def _submit_share(self, job_id, extranonce2, ntime, nonce):
        """Envía share al servidor."""
        if not self.sock:
            return
        try:
            self._send({
                "id": int(time.time() * 1000) % 1000000,
                "method": "mining.submit",
                "params": [
                    self.username,
                    job_id,
                    f"{extranonce2:08x}",
                    f"{ntime:08x}" if isinstance(ntime, int) else ntime,
                    f"{nonce:08x}"
                ]
            })
            self.shares_submitted += 1
        except:
            pass
    
    def disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass

# ═══════════════════════ Benchmark ═══════════════════════
def benchmark():
    """Benchmark local."""
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
    parser = argparse.ArgumentParser(description="Stratum Miner - Solo Mining Client")
    parser.add_argument("--host", default="192.168.100.81", help="Stratum server IP")
    parser.add_argument("--port", type=int, default=3333, help="Stratum server port")
    parser.add_argument("--user", default=sock_module.gethostname(), help="Miner name")
    parser.add_argument("--cpu", type=int, default=100, help="CPU percent (1-100)")
    parser.add_argument("--bench", action="store_true", help="Benchmark only")
    args = parser.parse_args()
    
    print(f"""
╔══════════════════════════════════════════════════╗
║  ⛏️  STRATUM SOLO MINER v{VERSION}                   ║
╚══════════════════════════════════════════════════╝
""")
    
    if args.bench:
        benchmark()
        return
    
    print(f"   Server: {args.host}:{args.port}")
    print(f"   User:   {args.user}")
    print(f"   CPU:    {args.cpu}%")
    
    miner = StratumMiner(args.host, args.port, args.user, args.cpu)
    
    try:
        miner.connect()
        miner.mine_loop()
    except KeyboardInterrupt:
        print("\n👋 Apagando...")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        miner.disconnect()

if __name__ == "__main__":
    main()
