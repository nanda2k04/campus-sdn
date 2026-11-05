#!/usr/bin/env bash
# Phase 11 scalability sweep: run the reroute/flow/resource measurement at three
# topology sizes (12 / 28 / 52 switches), each with its own controller instance.
# Writes results/scale/<N>sw.json; then run `python3 make_report.py` for the chart.
#
#   sudo ./run_phase11_scale.sh
set -u
cd "$(dirname "$0")"
PORT=6653
: "${SCALE_TRIALS:=8}"
export SCALE_TRIALS

# pods access-per-pod hosts-per-access cores   (-> switch count)
SIZES=(
  "2 2 1 4"     # 12 sw
  "4 4 1 4"     # 28 sw
  "6 6 2 4"     # 52 sw
)

for S in "${SIZES[@]}"; do
  read -r PODS APP HPA CORES <<< "$S"
  export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
  export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES" CAMPUS_FF=1
  NSW=$(( CORES + PODS*2 + PODS*APP ))
  echo "[scale] ===== size: $NSW switches (pods=$PODS app=$APP) ====="
  mn -c >/dev/null 2>&1
  ./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" controller.py > ryu.log 2>&1 &
  RYU_PID=$!
  for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT" && break; sleep 0.5; done
  # shellcheck disable=SC2086
  python3 topology.py --test scale --controller-port "$PORT" \
      --pods "$PODS" --access-per-pod "$APP" --hosts-per-access "$HPA" --cores "$CORES"
  kill $RYU_PID 2>/dev/null
  sleep 2
done
mn -c >/dev/null 2>&1
[ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER" results 2>/dev/null || true
echo "[scale] done -- results/scale/*.json ; now run: python3 make_report.py"
