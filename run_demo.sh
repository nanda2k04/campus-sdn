#!/usr/bin/env bash
# Narrated live failure-and-recovery demo on the FULL topology, with the REST API
# up (:8080) so you can watch it on the dashboard at the same time.  Runs the
# scripted scene, then drops to the Mininet CLI.
#
#   sudo ./run_demo.sh
# In another terminal (to watch live):  cd dashboard && npm run dev   -> Topology tab
set -u
cd "$(dirname "$0")"
PORT=6653; API=8080
export CAMPUS_PODS=6 CAMPUS_ACCESS_PER_POD=6 CAMPUS_HOSTS_PER_ACCESS=2 CAMPUS_CORES=4 CAMPUS_FF=1

echo "[demo] cleaning stale state"; mn -c >/dev/null 2>&1
echo "[demo] starting Ryu (REST API :$API) -> ryu.log"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" \
    --wsapi-port "$API" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT" && break; sleep 0.5; done
echo "[demo] Ryu up (OpenFlow :$PORT, API http://localhost:$API)"
echo "[demo] open the dashboard now (Topology tab) to watch the scene live."
python3 topology.py --test demo --controller-port "$PORT" \
    --pods 6 --access-per-pod 6 --hosts-per-access 2 --cores 4
