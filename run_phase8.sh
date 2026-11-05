#!/usr/bin/env bash
# Phase 8 one-shot: run the full failure-experiment suite and emit results/*.json
# (baseline + local-FF cases + controller-reroute case), with measured loss,
# outage, detection and reroute times.
#
# Usage:
#   sudo ./run_phase8.sh            # SMOKE topology (12 sw)
#   sudo ./run_phase8.sh full       # FULL topology (52 sw / 72 hosts)
set -u
cd "$(dirname "$0")"

SCALE="${1:-smoke}"
PORT=6653

case "$SCALE" in
  smoke) PODS=2; APP=2; HPA=1; CORES=4; DESC="SMOKE (12 sw / 4 hosts)" ;;
  full)  PODS=6; APP=6; HPA=2; CORES=4; DESC="FULL (52 sw / 72 hosts)" ;;
  *) echo "scale must be 'smoke' or 'full'"; exit 2 ;;
esac
SIZE="--pods $PODS --access-per-pod $APP --hosts-per-access $HPA --cores $CORES"
export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES" CAMPUS_FF=1

echo "[run] scale=$SCALE -> $DESC   (metrics suite)"
echo "[run] cleaning stale Mininet/OVS state"
mn -c >/dev/null 2>&1

echo "[run] starting Ryu (--observe-links, CAMPUS_FF=1)  (log -> ryu.log)"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT

for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && break
    sleep 0.5
done
echo "[run] Ryu pid=$RYU_PID listening on :$PORT"

echo "[run] launching experiment suite"
# shellcheck disable=SC2086
python3 topology.py --test experiments --controller-port "$PORT" $SIZE

echo
echo "[run] ===== results/summary.json ====="
python3 -c "import json;[print('  %-18s mech=%-19s loss=%s/%s outage=%sms det=%sms reroute=%sms flows=%s'%(r['scenario'],r['mechanism'],r['packet_loss'],r['packets_tx'],r['outage_ms'],r.get('detection_ms'),r.get('reroute_ms'),r.get('affected_flows'))) for r in json.load(open('results/summary.json'))]" 2>/dev/null || echo "  (no summary.json -- check the run above)"
echo "[run] per-scenario JSON in ./results/"
