#!/bin/bash
# start_dashboard.sh — Inicia el dashboard con las credenciales RPC del nodo
# Extrae BTC_RPC_PASS del stratum server en ejecución si no está en el entorno
set -e
cd "$(dirname "$0")"

if [ -z "$BTC_RPC_PASS" ]; then
  # Extraer del proceso stratum existente
  STRATUM_PID=$(pgrep -f "stratum_server.py" | head -1)
  if [ -n "$STRATUM_PID" ]; then
    eval $(cat /proc/$STRATUM_PID/environ 2>/dev/null | tr '\0' '\n' | grep '^BTC_RPC_' | sed 's/^/export /')
    echo "✅ Credenciales extraídas del stratum server (PID $STRATUM_PID)"
  fi
fi

pkill -f dashboard_server.py 2>/dev/null || true
sleep 1

python3 -u dashboard_server.py 2>&1
