#!/usr/bin/env bash
# Phase 6 one-shot: start Ryu (fast-failover groups ON) + LLDP, then run the
# local fast-failover demo (drop a dist->core link during a continuous ping).
#
# Usage:
#   sudo ./run_phase6.sh            # SMOKE topology (12 sw)
#   sudo ./run_phase6.sh full       # FULL topology (52 sw / 72 hosts)
#   sudo ./run_phase6.sh full noff  # control run: FF OFF (needs controller reroute)
set -u
cd "$(dirname "$0")"

SCALE="${1:-smoke}"
FFMODE="${2:-ff}"
PORT=6653

case "$SCALE" in
  smoke) PODS=2; APP=2; HPA=1; CORES=4; DESC="SMOKE (12 sw / 4 hosts)" ;;
  full)  PODS=6; APP=6; HPA=2; CORES=4; DESC="FULL (52 sw / 72 hosts)" ;;
  *) echo "scale must be 'smoke' or 'full'"; exit 2 ;;
esac
SIZE="--pods $PODS --access-per-pod $APP --hosts-per-access $HPA --cores $CORES"
export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES"

if [ "$FFMODE" = "noff" ]; then export CAMPUS_FF=0; FFDESC="FF OFF (plain, controller reroute)"
else export CAMPUS_FF=1; FFDESC="FF ON (OpenFlow fast-failover groups)"; fi

echo "[run] scale=$SCALE -> $DESC   mode=$FFDESC"
echo "[run] cleaning stale Mininet/OVS state"
mn -c >/dev/null 2>&1

echo "[run] starting Ryu (CAMPUS_FF=$CAMPUS_FF, --observe-links)  (log -> ryu.log)"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT

for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && break
    sleep 0.5
done
echo "[run] Ryu pid=$RYU_PID listening on :$PORT"

echo "[run] launching Mininet fast-failover test"
# shellcheck disable=SC2086
python3 topology.py --test failover --controller-port "$PORT" $SIZE

echo
echo "[run] ===== controller events (Phase 5/6) ====="
grep -E '"event": "(PROTECTED_DEST_INSTALLED|PRIMARY_PATH|BACKUP_PATH|LINK_DOWN)"' ryu.log | tail -12
echo "[run] full controller log in ./ryu.log"
