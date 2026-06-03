#!/usr/bin/env bash
# connect_miner.sh — Conecta esta máquina a la red de minería
# Corre en la máquina que quieras añadir (Mac, Linux, WSL)
# Uso: bash connect_miner.sh [NOMBRE] [CPU%] [IP_UMBREL]

MINER_NAME="${1:-$(hostname)}"
CPU_PCT="${2:-80}"
UMBREL_IP="${3:-192.168.100.81}"
STRATUM_PORT="3333"

echo "⛏️  Conectando '$MINER_NAME' a la red de minería..."
echo "   Umbrel: $UMBREL_IP"
echo "   CPU:    $CPU_PCT%"
echo ""

# Verificar que python3 existe
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 no encontrado. Instálalo primero."; exit 1; }

# Verificar que stratum_miner.py existe, sino descargarlo
if [ ! -f "stratum_miner.py" ]; then
    echo "📥 Descargando stratum_miner.py..."
    curl -sL "https://raw.githubusercontent.com/j5002la-collab/btc-miner/main/stratum_miner.py" -o stratum_miner.py 2>/dev/null || {
        echo "⚠️  No se pudo descargar. Copia stratum_miner.py manualmente a este directorio."
        exit 1
    }
fi

# Verificar conectividad al stratum server
echo "🔍 Verificando conectividad a $UMBREL_IP:$STRATUM_PORT..."
timeout 3 bash -c "echo > /dev/tcp/$UMBREL_IP/$STRATUM_PORT" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "⚠️  No se puede alcanzar $UMBREL_IP:$STRATUM_PORT directamente."
    echo ""
    echo "Opciones:"
    echo "  A) Túnel SSH (recomendado):"
    echo "     ssh -L $STRATUM_PORT:localhost:$STRATUM_PORT user@$UMBREL_IP"
    echo "     Luego en otra terminal: python3 stratum_miner.py --user $MINER_NAME --cpu $CPU_PCT"
    echo ""
    echo "  B) Pide exponer el puerto $STRATUM_PORT desde el host Umbrel"
    exit 1
fi

echo "✅ Conectividad OK. Iniciando minero..."
echo ""

python3 -u stratum_miner.py \
    --host "$UMBREL_IP" \
    --port "$STRATUM_PORT" \
    --user "$MINER_NAME" \
    --cpu "$CPU_PCT"

echo "👋 Minero detenido."
