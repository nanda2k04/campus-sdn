#!/usr/bin/env bash
# Phase 7 one-shot: start Ryu (FF groups + LLDP + controller rerouting), then
# fail a destination's LAST-HOP link (which local FF cannot protect) and show
# the controller detect it and reroute to the other plane.
#
# Usage:
#   sudo ./run_phase7.sh            # SMOKE topology (12 sw)
#   sudo ./run_phase7.sh full       # FULL topology (52 sw / 72 hosts)
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

echo "[run] scale=$SCALE -> $DESC   (FF ON + controller rerouting)"
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

echo "[run] launching Mininet controller-reroute test"
# shellcheck disable=SC2086
python3 topology.py --test reroute --controller-port "$PORT" $SIZE

echo
echo "[run] ===== Phase 7 controller events (in order) ====="
grep -E '"event": "(LINK_DOWN|FAILOVER_TRIGGERED|FLOW_REINSTALLED|LINK_RECOVERED)"' ryu.log \
  | grep -v '"reason": "SWITCH_DOWN"' | head -30
echo "[run] full controller log in ./ryu.log"
