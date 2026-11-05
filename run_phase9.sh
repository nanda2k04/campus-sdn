#!/usr/bin/env bash
# Phase 9 one-shot: start Ryu (REST API on :8080 + FF + LLDP) and the Mininet
# network, then drop into the Mininet CLI.  The REST API stays live -- query it
# or trigger REAL failures from another terminal with curl (examples below).
#
# Usage:
#   sudo ./run_phase9.sh            # SMOKE topology (12 sw)
#   sudo ./run_phase9.sh full       # FULL topology (52 sw / 72 hosts)
set -u
cd "$(dirname "$0")"

SCALE="${1:-smoke}"
PORT=6653
API=8080

case "$SCALE" in
  smoke) PODS=2; APP=2; HPA=1; CORES=4; DESC="SMOKE (12 sw / 4 hosts)" ;;
  full)  PODS=6; APP=6; HPA=2; CORES=4; DESC="FULL (52 sw / 72 hosts)" ;;
  *) echo "scale must be 'smoke' or 'full'"; exit 2 ;;
esac
SIZE="--pods $PODS --access-per-pod $APP --hosts-per-access $HPA --cores $CORES"
export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES" CAMPUS_FF=1

# extra args (any order): lb = ECMP load balancing, qos = load-/QoS-aware routing
for arg in "${@:2}"; do
  case "$arg" in
    lb)  export CAMPUS_LB=1;  echo "[run] ECMP load balancing ENABLED (both planes)" ;;
    qos) export CAMPUS_QOS=1; echo "[run] load-/QoS-aware routing ENABLED (EF plane-pin, WCMP)" ;;
  esac
done

echo "[run] scale=$SCALE -> $DESC"
echo "[run] cleaning stale Mininet/OVS state"
mn -c >/dev/null 2>&1

echo "[run] starting Ryu (REST API :$API, --observe-links)  (log -> ryu.log)"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" \
    --wsapi-port "$API" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT

for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && break
    sleep 0.5
done
echo "[run] Ryu pid=$RYU_PID  OpenFlow :$PORT  REST API http://localhost:$API"
cat <<EOF

  API (query from another terminal):
    curl -s localhost:$API/api/controller | python3 -m json.tool
    curl -s localhost:$API/api/topology   | python3 -m json.tool
    curl -s localhost:$API/api/links
    curl -s "localhost:$API/api/events?limit=20"
  Inject a REAL failure (controller sends OpenFlow port-down), then recover:
    curl -s -XPOST localhost:$API/api/failures/link  -d '{"a":"d1a","b":"c1"}'
    curl -s -XPOST localhost:$API/api/recovery/link  -d '{"a":"d1a","b":"c1"}'
    curl -s -XPOST localhost:$API/api/failures/switch -d '{"name":"d1a"}'

  In the Mininet CLI below: run 'pingall' first (installs forwarding), then
  trigger a failure via curl and watch pings / /api/events react.

EOF
# shellcheck disable=SC2086
python3 topology.py --test cli --controller-port "$PORT" $SIZE
