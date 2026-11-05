#!/usr/bin/env bash
# Phase 3 one-shot: start Ryu, run the Mininet connectivity test, tear down.
#
# Usage:
#   sudo ./run_phase3.sh                 # SMOKE topology (12 sw / 4 hosts), conn test
#   sudo ./run_phase3.sh smoke conn      # same as above (explicit)
#   sudo ./run_phase3.sh full  conn      # FULL topology (52 sw / 72 hosts)
#   sudo ./run_phase3.sh full  pingall   # FULL topology, full 72-host pingall
#
# Smoke first: prove the controller is storm-free on a tiny redundant topology
# before scaling to 52 switches.
set -u
cd "$(dirname "$0")"

SCALE="${1:-smoke}"
MODE="${2:-conn}"
PORT=6653

case "$SCALE" in
  smoke) PODS=2; APP=2; HPA=1; CORES=4; DESC="SMOKE (12 sw / 4 hosts)" ;;
  full)  PODS=6; APP=6; HPA=2; CORES=4; DESC="FULL (52 sw / 72 hosts)" ;;
  *) echo "scale must be 'smoke' or 'full'"; exit 2 ;;
esac
SIZE="--pods $PODS --access-per-pod $APP --hosts-per-access $HPA --cores $CORES"

# the controller reads the SAME scale from the environment so its model matches
export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES"

echo "[run] scale=$SCALE  -> $DESC   mode=$MODE"
echo "[run] cleaning stale Mininet/OVS state"
mn -c >/dev/null 2>&1

echo "[run] starting Ryu controller  (log -> ryu.log)"
./ryu-venv/bin/ryu-manager --ofp-tcp-listen-port "$PORT" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT

for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && break
    sleep 0.5
done
echo "[run] Ryu pid=$RYU_PID listening on :$PORT"

echo "[run] launching Mininet topology (--test $MODE $SIZE)"
# shellcheck disable=SC2086
python3 topology.py --test "$MODE" --controller-port "$PORT" $SIZE

echo
echo "[run] ===== controller events (tail) ====="
grep '"event"' ryu.log | tail -25
echo "[run] full controller log in ./ryu.log"
