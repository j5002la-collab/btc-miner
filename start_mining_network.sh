#!/usr/bin/env bash
# start_mining_network.sh
# Inicia el stratum server + dashboard de minería
# Ejecutar en el container Hermes (Umbrel)

set -e
cd "$(dirname "$0")"

RPC_URL="${BTC_RPC_URL:-http://10.21.21.7:9332}"
RPC_USER="${BTC_RPC_USER:-umbrel}"
RPC_PASS="${BTC_RPC_PASS:?BTC_RPC_PASS no definida}"
STRATUM_PORT="${STRATUM_PORT:-3333}"
DASHBOARD_PORT="${DASHBOARD_PORT:-9119}"

echo "⛏️  Iniciando red de minería Bitcoin..."
echo "   Nodo RPC: $RPC_URL"
echo "   Stratum:  tcp://0.0.0.0:$STRATUM_PORT"
echo "   Dashboard: http://localhost:$DASHBOARD_PORT"
echo ""

# Matar procesos previos
pkill -f "stratum_server.py" 2>/dev/null || true
pkill -f "dashboard_server.py" 2>/dev/null || true
sleep 1

# Iniciar stratum server (distribuye trabajo a mineros)
echo "[1/2] Iniciando stratum server en :$STRATUM_PORT..."
python3 -u stratum_server.py > stratum_server.log 2>&1 &
STRATUM_PID=$!
echo "       PID: $STRATUM_PID"

sleep 2

# Iniciar dashboard web (monitoreo + minería local)
echo "[2/2] Iniciando dashboard en :$DASHBOARD_PORT..."
python3 -u dashboard_server.py > dashboard.log 2>&1 &
DASHBOARD_PID=$!
echo "       PID: $DASHBOARD_PID"

echo ""
echo "✅ Red de minería iniciada:"
echo "   Stratum server: PID $STRATUM_PID (log: stratum_server.log)"
echo "   Dashboard:      PID $DASHBOARD_PID (log: dashboard.log)"
echo ""
echo "Para añadir mineros desde otras máquinas:"
echo "   python3 stratum_miner.py --host <IP_DEL_UMBREL> --port $STRATUM_PORT"
echo ""
echo "Logs: tail -f stratum_server.log dashboard.log"
echo "Parar: pkill -f 'stratum_server.py|dashboard_server.py'"

# Mantener script corriendo para monitoreo
wait
