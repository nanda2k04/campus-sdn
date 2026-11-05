#!/usr/bin/env bash
# Congestion + load-balancing demo on the FULL topology, with the REST API up
# (:8080) so you can watch the link-load heatmap on the dashboard at the same
# time.  Many clients stream to one server; the server's uplink is the bottleneck.
#
# Run it TWICE to get the before/after comparison:
#   sudo ./run_congest.sh         # LB OFF  -> plane A saturates, big loss
#   sudo ./run_congest.sh lb      # LB ON   -> load splits across A+B, loss gone
#
# In another terminal (to watch live):
#   cd dashboard && npm run dev    -> Topology tab -> turn Heatmap ON
#
# Tunables (override on the command line, e.g. CAMPUS_BW=10 sudo ./run_congest.sh):
#   CAMPUS_BW       inter-switch link cap in Mbit/s   (default 20)
#   CONGEST_FLOWS   number of concurrent client flows (default 16)
#   CONGEST_RATE_M  offered rate per flow in Mbit/s   (default 2.0)
#   CONGEST_DUR     traffic duration in seconds       (default 12)
set -u
cd "$(dirname "$0")"
PORT=6653; API=8080

export CAMPUS_PODS=6 CAMPUS_ACCESS_PER_POD=6 CAMPUS_HOSTS_PER_ACCESS=2 CAMPUS_CORES=4
export CAMPUS_FF=1
export CAMPUS_BW="${CAMPUS_BW:-20}"              # <-- the link cap (Mbit/s)
export CONGEST_FLOWS="${CONGEST_FLOWS:-16}"
export CONGEST_RATE_M="${CONGEST_RATE_M:-2.0}"
export CONGEST_DUR="${CONGEST_DUR:-12}"

# Flags (any order):  lb = ECMP load balancing,  qos = load-/QoS-aware routing
export CAMPUS_LB=0 CAMPUS_QOS=0
for arg in "$@"; do
    case "$arg" in
        lb)  export CAMPUS_LB=1 ;;
        qos) export CAMPUS_QOS=1 ;;
    esac
done
echo "[congest] LOAD BALANCING: $([ "$CAMPUS_LB" = 1 ] && echo 'ON (ECMP A+B)' || echo 'OFF (pins to plane A)')"
echo "[congest] QoS-AWARE:      $([ "$CAMPUS_QOS" = 1 ] && echo 'ON (EF->least-loaded plane, WCMP, congestion-aware)' || echo 'OFF')"
echo "[congest] link cap = ${CAMPUS_BW} Mbit/s   flows = ${CONGEST_FLOWS} x ${CONGEST_RATE_M} Mbit/s"

echo "[congest] cleaning stale state"; mn -c >/dev/null 2>&1
echo "[congest] starting Ryu (REST API :$API) -> ryu.log"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" \
    --wsapi-port "$API" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT" && break; sleep 0.5; done
echo "[congest] Ryu up (OpenFlow :$PORT, API http://localhost:$API)"
echo "[congest] open the dashboard now (Topology tab, Heatmap ON) to watch live."
python3 topology.py --test congest --controller-port "$PORT" \
    --pods 6 --access-per-pod 6 --hosts-per-access 2 --cores 4
