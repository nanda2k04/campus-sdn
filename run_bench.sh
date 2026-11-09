#!/usr/bin/env bash
# Multi-flow failover benchmark: K concurrent host->host UDP flows, primary
# (plane-A) path cut mid-transmission, per-flow + aggregate packet-loss /
# one-way-latency / outage stats written to results/failover_bench.json.
#
#   sudo ./run_bench.sh <pods> <access_per_pod> <hosts_per_access> <cores> [flags]
#
# Example (recommended — proactive install so forwarding is ready, no pingall):
#   sudo ./run_bench.sh 6 4 24 4 proactive
#
# Tune the run with env vars (all optional):
#   BENCH_FLOWS=12 BENCH_RATE=250 BENCH_DUR=10 BENCH_FAIL_AT=4 \
#     sudo -E ./run_bench.sh 6 4 24 4 proactive
#   (use sudo -E so BENCH_* reach the benchmark)
set -u
cd "$(dirname "$0")"
PODS="${1:?pods}"; APP="${2:?access_per_pod}"; HPA="${3:?hosts_per_access}"; CORES="${4:?cores}"
PORT=6653; API=8080

echo "[bench] topology: ${PODS} pods x ${APP} access x ${HPA} hosts + ${CORES} cores"
export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES" CAMPUS_FF=1
# Proactive install by default so every destination is reachable before the
# flows start (otherwise the first packets race the reactive install).
export CAMPUS_PROACTIVE="${CAMPUS_PROACTIVE:-1}"
for arg in "${@:5}"; do
  case "$arg" in
    lb)   export CAMPUS_LB=1;   echo "[bench] ECMP load balancing ON" ;;
    qos)  export CAMPUS_QOS=1;  echo "[bench] QoS-aware routing ON" ;;
    dpi)  export CAMPUS_DPI=1;  echo "[bench] app-aware DPI ON" ;;
    queues) export CAMPUS_QOS_QUEUES=1; echo "[bench] OVS priority queues ON" ;;
    mlag) export CAMPUS_MLAG=1; echo "[bench] MLAG ON" ;;
    dc)   export CAMPUS_DC=1;   echo "[bench] data-centre tier ON" ;;
    proactive) export CAMPUS_PROACTIVE=1 ;;
    all)  export CAMPUS_LB=1 CAMPUS_QOS=1 CAMPUS_DPI=1 CAMPUS_QOS_QUEUES=1 CAMPUS_MLAG=1 CAMPUS_DC=1 CAMPUS_PROACTIVE=1
          echo "[bench] ALL superset features ON" ;;
  esac
done
echo "[bench] flows=${BENCH_FLOWS:-8} rate=${BENCH_RATE:-200}pps dur=${BENCH_DUR:-8}s fail_at=${BENCH_FAIL_AT:-3}s"

echo "[bench] cleaning stale state"; mn -c >/dev/null 2>&1
echo "[bench] starting Ryu (REST API :$API) -> ryu.log"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" \
    --wsapi-port "$API" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT" && break; sleep 0.5; done
echo "[bench] Ryu up. Building network + running benchmark (non-interactive)…"
stdbuf -oL -eL python3 topology.py --test bench --controller-port "$PORT" \
    --pods "$PODS" --access-per-pod "$APP" --hosts-per-access "$HPA" --cores "$CORES" \
    2>&1 | tee mn.log
echo "[bench] done -> results/failover_bench.json"
