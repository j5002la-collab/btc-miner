#!/usr/bin/env python3
"""
BTC CPU Miner — Demo continua con SHA-256d real.
El mismo algoritmo que mina Bitcoin. Muestra hashrate, consumo, y estadísticas.

Corre indefinidamente hasta CTRL+C.
"""

import hashlib
import struct
import time
import sys
import os
from datetime import datetime

VERSION = "2.0"

def sha256d(data):
    """Double SHA-256 — exactamente lo que usa Bitcoin PoW."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

# ============================================================
# Target de dificultad ultra-bajo (para demo)
# En producción esto sería el target real de la red (~70 dígitos hex)
# Usamos un target suave para poder "encontrar" shares y mostrar progreso
# ============================================================
DIFFICULTY = 65536  # ~2^16 = ultra bajo para demo
TARGET_BYTES = (0x00000000FFFF0000000000000000000000000000000000000000000000000000 
                // DIFFICULTY).to_bytes(32, 'big')

def check_hash(block_hash):
    """Verifica si el hash está bajo el target."""
    return block_hash < TARGET_BYTES

# ============================================================
# Miner Engine con visualización
# ============================================================
def print_header():
    """Banner ASCII del minero."""
    print("""
╔══════════════════════════════════════════════════════╗
║        ⛏️  BTC CPU MINER v{} — SHA-256d           ║
║        Intel N150 | 4 cores | Python              ║
║        Algoritmo: Doble SHA-256 (Bitcoin real)    ║
║        CTRL+C para detener                         ║
╚══════════════════════════════════════════════════════╝
""".format(VERSION))

def format_hashrate(hr):
    """Formatea hashrate legible."""
    if hr > 1e12:
        return f"{hr/1e12:.2f} TH/s"
    elif hr > 1e9:
        return f"{hr/1e9:.2f} GH/s"
    elif hr > 1e6:
        return f"{hr/1e6:.2f} MH/s"
    elif hr > 1e3:
        return f"{hr/1e3:.2f} KH/s"
    else:
        return f"{hr:.0f} H/s"

def format_number(n):
    """Formatea números grandes."""
    if n > 1e12:
        return f"{n/1e12:.1f}T"
    elif n > 1e9:
        return f"{n/1e9:.1f}G"
    elif n > 1e6:
        return f"{n/1e6:.1f}M"
    elif n > 1e3:
        return f"{n/1e3:.1f}K"
    else:
        return str(int(n))

def mine_continuous():
    """Minado continuo con display en tiempo real."""
    print_header()
    
    # Bloque dummy de 80 bytes (en producción sería el header real de un bloque)
    # Incluimos prev_hash, merkle_root, timestamp, bits, nonce
    block_template = bytearray(80)
    
    # Llenar con datos pseudo-aleatorios basados en el tiempo (simula diferentes bloques)
    t = int(time.time())
    for i in range(0, 76, 4):
        struct.pack_into('<I', block_template, i, (t + i) & 0xFFFFFFFF)
    
    # Bits (compact target) - simulando dificultad
    struct.pack_into('<I', block_template, 72, 0x1d00ffff)  # Dificultad 1 (genesis)
    
    nonce = 0
    hashes_total = 0
    shares_found = 0
    start_time = time.time()
    last_report = start_time
    last_hashes = 0
    best_hash = b'\xff' * 32
    best_nonce = 0
    
    print("⛏️  INICIANDO MINADO SHA-256d...\n")
    print(f"{'HORA':<10} {'HASHRATE':<12} {'TOTAL':<12} {'SHARES':<8} {'MEJOR HASH':<20} {'UPTIME':<10}")
    print("-" * 72)
    
    try:
        while True:
            # Actualizar timestamp cada ~1M hashes
            if nonce % 1000000 == 0:
                struct.pack_into('<I', block_template, 68, int(time.time()))
            
            # Nonce en los últimos 4 bytes
            struct.pack_into('<I', block_template, 76, nonce)
            
            # Double SHA-256
            h = sha256d(bytes(block_template))
            
            # Verificar share (contra target ultra-bajo para demo)
            if check_hash(h):
                shares_found += 1
                share_hex = h[::-1].hex()
                best_hash = min(best_hash, h)
                if h < best_hash:
                    best_hash = h
                    best_nonce = nonce
                print(f"\n🎯 SHARE ENCONTRADO! Nonce: {nonce} | Hash: {share_hex[:24]}...")
            
            nonce += 1
            hashes_total += 1
            
            # Reporte cada ~5 segundos
            now = time.time()
            if now - last_report >= 5:
                elapsed = now - start_time
                window_hashes = hashes_total - last_hashes
                window_time = now - last_report
                hr = window_hashes / window_time
                
                ts = datetime.now().strftime("%H:%M:%S")
                total_str = format_number(hashes_total)
                best_str = best_hash[::-1].hex()[:16] if best_hash != b'\xff' * 32 else "-"
                
                print(f"{ts:<10} {format_hashrate(hr):<12} {total_str:<12} {shares_found:<8} {best_str:<20} {format_uptime(elapsed):<10}")
                
                last_report = now
                last_hashes = hashes_total
            
            if nonce >= 0xFFFFFFFF:
                # Reset nonce, nuevo "bloque"
                print(f"\n📦 Nonce agotado. Nuevo bloque simulado.")
                nonce = 0
                t = int(time.time())
                for i in range(0, 76, 4):
                    struct.pack_into('<I', block_template, i, (t + i) & 0xFFFFFFFF)
    
    except KeyboardInterrupt:
        elapsed = time.time() - start_time
        avg_hr = hashes_total / elapsed if elapsed > 0 else 0
        print(f"\n\n{'='*60}")
        print(f"⏹️  MINERO DETENIDO")
        print(f"{'='*60}")
        print(f"   ⏱️  Tiempo total:    {format_uptime(elapsed)}")
        print(f"   ⚡ Hashes totales:   {format_number(hashes_total)}")
        print(f"   📊 Hashrate prom:    {format_hashrate(avg_hr)}")
        print(f"   🎯 Shares:           {shares_found}")
        print(f"   🏆 Mejor hash:       {best_hash[::-1].hex()}")
        print(f"   🔢 Nonce:            {nonce}")
        print(f"{'='*60}")
        print(f"\n   💡 En la red BTC real necesitarías ~800,000,000 TH/s")
        print(f"   💡 Tu hashrate es ~{format_hashrate(avg_hr)}")
        print(f"   💡 Para competir: {800e18/max(avg_hr,1):.0f} CPUs como esta")
        print(f"   💡 Tiempo para 1 bloque real: {800e18/max(avg_hr,1)/6/24/365:.0f} años")


def format_uptime(seconds):
    """Formatea tiempo de ejecución."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


if __name__ == "__main__":
    mine_continuous()
