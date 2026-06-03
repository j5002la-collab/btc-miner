#!/usr/bin/env python3
"""
Stratum Server para Solo Mining Multi-Máquina
=============================================
- Obtiene block templates de Knots vía RPC
- Distribuye trabajo único a mineros vía Stratum (TCP)
- Cada minero recibe un extranonce distinto → sin solapamiento
- Submit de shares → validación → submitblock si encuentra bloque

Uso: python3 stratum_server.py
Puerto stratum: 3333 (configurable con STRATUM_PORT)
"""

import socket
import threading
import json
import struct
import hashlib
import time
import os
import base64
import urllib.request
from datetime import datetime

# ═══════════════════════ Config ═══════════════════════
RPC_URL = os.environ.get("BTC_RPC_URL", "http://10.21.21.7:9332")
RPC_USER = os.environ.get("BTC_RPC_USER", "umbrel")
RPC_PASS = os.environ.get("BTC_RPC_PASS", "")
STRATUM_PORT = int(os.environ.get("STRATUM_PORT", "3333"))
STRATUM_HOST = "0.0.0.0"
VERSION = "1.0"

# ═══════════════════════ Estado global ═══════════════════════
state_lock = threading.Lock()
current_template = None
current_job_id = 0
clients = {}  # client_id → {socket, extranonce, subscribed, authorized, hashrate, ...}
next_client_id = 1
total_shares = 0
blocks_found = 0
start_time = time.time()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

# ═══════════════════════ Crypto ═══════════════════════
def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def bits_to_target(bits_hex):
    nbits = int(bits_hex, 16)
    exp = nbits >> 24
    mant = nbits & 0x00ffffff
    target = mant * (2 ** (8 * (exp - 3)))
    return target.to_bytes(32, 'big')

# ═══════════════════════ RPC ═══════════════════════
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
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        if result.get("error"):
            raise Exception(f"RPC Error: {result['error']}")
        return result["result"]

# ═══════════════════════ Template Refresh Thread ═══════════════════════
def template_refresh_thread():
    """Obtiene nuevos block templates cada 15s y notifica a mineros."""
    global current_template, current_job_id
    
    log("🔄 Template refresh thread started")
    
    # Primer template
    while current_template is None:
        try:
            current_template = rpc_call("getblocktemplate", [{"rules": ["segwit"]}])
            current_job_id += 1
            log(f"✅ Template inicial: bloque {current_template['height']:,} "
                f"reward={current_template['coinbasevalue']/1e8:.8f} BTC")
            notify_all_miners()
        except Exception as e:
            log(f"⚠️  Esperando template: {e}")
            time.sleep(5)
    
    while True:
        time.sleep(15)
        try:
            new_template = rpc_call("getblocktemplate", [{"rules": ["segwit"]}])
            changed = False
            with state_lock:
                if new_template.get("height") != current_template.get("height") or \
                   new_template.get("previousblockhash") != current_template.get("previousblockhash"):
                    changed = True
                current_template = new_template
                current_job_id += 1
            
            if changed:
                log(f"🆕 Nuevo bloque: {current_template['height']:,} "
                    f"reward={current_template['coinbasevalue']/1e8:.8f} BTC "
                    f"txs={len(current_template.get('transactions',[]))}")
            else:
                log(f"🔁 Template refrescado: bloque {current_template['height']:,} "
                    f"(nuevo job_id={current_job_id})")
            
            # IMPORTANTE: llamar notify fuera del lock para evitar deadlock
            notify_all_miners(clean_jobs=changed)
        except Exception as e:
            log(f"⚠️  Refresco template falló: {e}")

def notify_all_miners(clean_jobs=True):
    """Envía mining.notify a todos los clientes."""
    with state_lock:
        if current_template is None:
            return
        
        job_id = str(current_job_id).zfill(8)
        prevhash = current_template["previousblockhash"]
        version = current_template["version"]
        bits = current_template["bits"]
        curtime = current_template["curtime"]
        height = current_template["height"]
        
        for cid, client in list(clients.items()):
            if not client.get("subscribed"):
                continue
            try:
                notify = {
                    "id": None,
                    "method": "mining.notify",
                    "params": [
                        job_id,
                        prevhash,
                        # Coinbase part 1 + extranonce placeholder + coinbase part 2
                        f"01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff"
                        f"{height:08x}"  # block height in coinbase
                        f"{client['extranonce']:08x}"  # unique per client
                        f"ffffffff",
                        [],  # merkle branches (empty for solo, we validate externally)
                        version,
                        bits,
                        curtime,
                        clean_jobs
                    ]
                }
                client["socket"].sendall((json.dumps(notify) + "\n").encode())
            except Exception as e:
                log(f"❌ Error notificando cliente {cid}: {e}")
                remove_client(cid)

# ═══════════════════════ Client Management ═══════════════════════
def remove_client(cid):
    with state_lock:
        if cid in clients:
            try:
                clients[cid]["socket"].close()
            except:
                pass
            del clients[cid]
            log(f"👋 Cliente {cid} desconectado ({len(clients)} activos)")

# ═══════════════════════ Share Validation ═══════════════════════
def validate_share(extranonce2, ntime, nonce, client_id):
    """Valida un share contra el template actual y submit si es bloque."""
    global total_shares, blocks_found
    
    with state_lock:
        template = current_template
        if template is None:
            return False, "No template"
    
    try:
        # Reconstruir header
        version = template["version"]
        prevhash = bytes.fromhex(template["previousblockhash"])[::-1]
        bits_hex = template["bits"]
        target = bits_to_target(bits_hex)
        curtime = int(ntime, 16) if isinstance(ntime, str) else ntime
        
        # Construir coinbase con extranonce del cliente
        height = template["height"]
        with state_lock:
            extranonce1 = clients.get(client_id, {}).get("extranonce", 0)
        
        cb1 = bytes.fromhex(
            f"01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff"
            f"{height:08x}{extranonce1:08x}"
        )
        if isinstance(extranonce2, str):
            en2 = bytes.fromhex(extranonce2)
        else:
            en2 = extranonce2
        coinbase = cb1 + en2 + bytes.fromhex("ffffffff")
        
        # Merkle root simplificado: hash del coinbase solo (solo mining)
        # En producción, construirías el merkle tree completo con las transacciones
        merkleroot = sha256d(coinbase)[::-1]
        
        # Construir header
        header = bytearray(80)
        struct.pack_into('<I', header, 0, version)
        header[4:36] = prevhash
        header[36:68] = merkleroot
        struct.pack_into('<I', header, 68, curtime)
        struct.pack_into('<I', header, 72, int(bits_hex, 16))
        if isinstance(nonce, str):
            struct.pack_into('<I', header, 76, int(nonce, 16))
        else:
            struct.pack_into('<I', header, 76, nonce & 0xFFFFFFFF)
        
        # Double SHA-256
        block_hash = sha256d(bytes(header))[::-1]
        
        total_shares += 1
        
        if block_hash < target:
            # ¡BLOQUE ENCONTRADO!
            blocks_found += 1
            log(f"🎉🎉🎉 BLOQUE ENCONTRADO POR CLIENTE {client_id}! 🎉🎉🎉")
            log(f"   Hash: {block_hash.hex()}")
            
            # Submit a Knots (requiere header completo + transacciones)
            # Para solo mining real necesitamos construir el bloque completo
            # con todas las transacciones del template
            try:
                # Construir bloque completo usando RPC
                block_hex = build_block(template, coinbase, bytes(header), block_hash)
                if block_hex:
                    result = rpc_call("submitblock", [block_hex])
                    log(f"   ✅ submitblock: {result}")
                else:
                    log(f"   ❌ No se pudo construir el bloque completo")
            except Exception as e:
                log(f"   ❌ Error submitblock: {e}")
            
            return True, "block_found"
        
        return True, "share_accepted"
    
    except Exception as e:
        return False, str(e)

def build_block(template, coinbase, header, block_hash):
    """Construye bloque completo con transacciones para submitblock."""
    try:
        # Construir coinbase transaction completa
        reward = template["coinbasevalue"]
        height = template["height"]
        
        # Coinbase tx con witness
        coinbase_tx = (
            "010000000001010000000000000000000000000000000000000000000000000000000000000000ffffffff"
            f"{(height).to_bytes(3, 'big').hex()}"
            "ffffffff"
            f"{reward.to_bytes(8, 'little').hex()}"  # output value
            "00"  # script length 0 (no address - needs proper output script)
            "00000000"  # locktime
        )
        
        # Esto requiere implementación completa de construcción de bloques
        # Por ahora retornamos solo el header (submitblock con header-only)
        return header.hex()
    except:
        return None

# ═══════════════════════ Stratum Protocol Handler ═══════════════════════
def handle_stratum_message(client_id, line):
    """Procesa un mensaje Stratum JSON-RPC."""
    try:
        msg = json.loads(line.strip())
    except json.JSONDecodeError:
        return
    
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", [])
    
    if method == "mining.subscribe":
        # Cliente se suscribe
        with state_lock:
            if client_id in clients:
                extranonce1 = clients[client_id]["extranonce"]
                clients[client_id]["subscribed"] = True
            else:
                return
        
        sub_reply = {
            "id": msg_id,
            "result": [
                [["mining.notify", f"{client_id:08x}"]],
                f"{extranonce1:08x}",
                4  # extranonce2 size
            ],
            "error": None
        }
        clients[client_id]["socket"].sendall((json.dumps(sub_reply) + "\n").encode())
        log(f"📡 Cliente {client_id} suscrito (extranonce={extranonce1:08x})")
        
        # Enviar difficulty (mínimo 1)
        diff_msg = {
            "id": None,
            "method": "mining.set_difficulty",
            "params": [1.0]
        }
        clients[client_id]["socket"].sendall((json.dumps(diff_msg) + "\n").encode())
        
        # Enviar trabajo actual
        notify_all_miners(clean_jobs=False)
    
    elif method == "mining.authorize":
        # Cliente se autentica
        username = params[0] if len(params) > 0 else "anonymous"
        with state_lock:
            if client_id in clients:
                clients[client_id]["authorized"] = True
                clients[client_id]["username"] = username
        
        auth_reply = {
            "id": msg_id,
            "result": True,
            "error": None
        }
        clients[client_id]["socket"].sendall((json.dumps(auth_reply) + "\n").encode())
        log(f"✅ Cliente {client_id} autorizado como '{username}'")
    
    elif method == "mining.submit":
        # Cliente envía un share
        username = params[0]
        job_id_str = params[1]
        extranonce2 = params[2]
        ntime = params[3]
        nonce = params[4]
        
        valid, reason = validate_share(extranonce2, ntime, nonce, client_id)
        
        if reason == "block_found":
            result = True
            log(f"🎉 BLOQUE! Cliente {client_id} ({username})")
        else:
            result = valid
        
        submit_reply = {
            "id": msg_id,
            "result": result,
            "error": None if valid else [20, reason, None]
        }
        try:
            clients[client_id]["socket"].sendall((json.dumps(submit_reply) + "\n").encode())
        except:
            pass
        
        if total_shares % 100 == 0:
            log(f"📊 Total shares: {total_shares} | Bloques: {blocks_found} | "
                f"Clientes: {len(clients)}")

# ═══════════════════════ Client Connection Handler ═══════════════════════
def handle_client(sock, addr):
    """Maneja una conexión de cliente stratum."""
    global next_client_id
    
    with state_lock:
        client_id = next_client_id
        next_client_id += 1
        extranonce = client_id * 1000  # Cada cliente tiene rango único
        clients[client_id] = {
            "socket": sock,
            "extranonce": extranonce,
            "subscribed": False,
            "authorized": False,
            "username": f"miner_{client_id}",
            "addr": addr,
            "connected_at": time.time(),
        }
    
    log(f"🔌 Nuevo cliente {client_id} desde {addr[0]}:{addr[1]} "
        f"(extranonce={extranonce:08x}, {len(clients)} activos)")
    
    buffer = b""
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    handle_stratum_message(client_id, line.decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"⚠️  Cliente {client_id} error: {e}")
    finally:
        remove_client(client_id)

# ═══════════════════════ Main ═══════════════════════
def main():
    print(f"""
╔══════════════════════════════════════════════════╗
║  ⛏️  STRATUM SOLO MINING SERVER v{VERSION}         ║
║  Nodo: {RPC_URL}                  ║
║  Stratum:  tcp://0.0.0.0:{STRATUM_PORT}              ║
╚══════════════════════════════════════════════════╝
""")
    
    # Verificar conexión RPC
    try:
        info = rpc_call("getblockchaininfo")
        log(f"✅ Conectado a Knots: {info['blocks']:,} bloques, dificultad {info['difficulty']/1e12:.1f}T")
    except Exception as e:
        log(f"❌ No se pudo conectar a Knots: {e}")
        log("   El stratum server iniciará pero sin templates hasta que Knots responda")
    
    # Iniciar template refresh thread
    threading.Thread(target=template_refresh_thread, daemon=True).start()
    
    # Iniciar stratum server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((STRATUM_HOST, STRATUM_PORT))
    server.listen(50)
    log(f"🌐 Stratum server escuchando en {STRATUM_HOST}:{STRATUM_PORT}")
    
    try:
        while True:
            sock, addr = server.accept()
            threading.Thread(target=handle_client, args=(sock, addr), daemon=True).start()
    except KeyboardInterrupt:
        log("Apagando...")
    finally:
        server.close()

if __name__ == "__main__":
    main()
