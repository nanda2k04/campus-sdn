#!/usr/bin/env bash
# Phase 4 one-shot: start Ryu WITH LLDP discovery (--observe-links), run the
# discovery test (converge, inject a link down, restore), tear down.
#
# Usage:
#   sudo ./run_phase4.sh            # SMOKE topology (12 sw)
#   sudo ./run_phase4.sh full       # FULL topology (52 sw)
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
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES"

echo "[run] scale=$SCALE -> $DESC"
echo "[run] cleaning stale Mininet/OVS state"
mn -c >/dev/null 2>&1

echo "[run] starting Ryu controller WITH LLDP discovery (--observe-links)  (log -> ryu.log)"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT

for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && break
    sleep 0.5
done
echo "[run] Ryu pid=$RYU_PID listening on :$PORT"

echo "[run] launching Mininet discovery test"
# shellcheck disable=SC2086
python3 topology.py --test discover --controller-port "$PORT" $SIZE

echo
echo "[run] ===== discovery summary ====="
echo "[run] LINK_DISCOVERED count: $(grep -c LINK_DISCOVERED ryu.log)"
echo "[run] last RECONCILE lines:"
grep RECONCILE ryu.log | tail -4
echo "[run] LINK_DOWN events:"
grep LINK_DOWN ryu.log | tail -4
echo "[run] full controller log in ./ryu.log"
