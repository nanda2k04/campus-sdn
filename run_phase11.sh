#!/usr/bin/env bash
# Phase 11 one-shot: statistical evaluation suite (N randomized trials per
# failure type) + controller CPU/RSS sampling + flow counts + OpenFlow proof
# dumps.  Writes results/eval/{trials.jsonl,summary_stats.json,metrics.csv,
# resources.csv,flows.json,proofs/}.
#
# Usage:
#   sudo ./run_phase11.sh                 # SMOKE (12 sw), EVAL_TRIALS=2
#   sudo EVAL_TRIALS=20 ./run_phase11.sh full   # FULL (52 sw), 20 trials/scenario
set -u
cd "$(dirname "$0")"

SCALE="${1:-smoke}"
PORT=6653

case "$SCALE" in
  smoke) PODS=2; APP=2; HPA=1; CORES=4; DESC="SMOKE (12 sw)"; : "${EVAL_TRIALS:=2}" ;;
  full)  PODS=6; APP=6; HPA=2; CORES=4; DESC="FULL (52 sw / 72 hosts)"; : "${EVAL_TRIALS:=15}" ;;
  *) echo "scale must be 'smoke' or 'full'"; exit 2 ;;
esac
export EVAL_TRIALS
SIZE="--pods $PODS --access-per-pod $APP --hosts-per-access $HPA --cores $CORES"
export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES" CAMPUS_FF=1

echo "[run] scale=$SCALE -> $DESC   trials/scenario=$EVAL_TRIALS"
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

echo "[run] launching evaluation suite (this takes a while)"
# shellcheck disable=SC2086
python3 topology.py --test eval --controller-port "$PORT" $SIZE

# hand ownership back so the dashboard/report (run as you) can read/write them
if [ -n "${SUDO_USER:-}" ]; then
    chown -R "$SUDO_USER" results 2>/dev/null || true
fi
echo "[run] done -- see results/eval/"
