#!/usr/bin/env bash
# Parametrized launcher for scaling the campus topology to realistic sizes.
# Starts Ryu (REST API :8080) + a custom-sized Mininet network, then the CLI.
#
#   sudo ./run_scale.sh <pods> <access_per_pod> <hosts_per_access> <cores> [lb] [qos]
#
# Examples (48-port access = 46 hosts/access is the real-world density):
#   sudo ./run_scale.sh 6  4 46 4            # ~1100 hosts, 40 switches  (safe)
#   sudo ./run_scale.sh 8  4 46 4            # ~1470 hosts, 52 switches  (recommended)
#   sudo ./run_scale.sh 12 6 36 4            # ~2600 hosts, 100 switches (ceiling — watch RAM)
#
# Watch memory in another terminal while it starts:  watch -n1 free -g
set -u
cd "$(dirname "$0")"
PODS="${1:?pods}"; APP="${2:?access_per_pod}"; HPA="${3:?hosts_per_access}"; CORES="${4:?cores}"
PORT=6653; API=8080

SW=$(( CORES + 2*PODS + PODS*APP ))
HOSTS=$(( PODS*APP*HPA ))
ISL=$(( CORES*(CORES-1)/2 + 2*PODS*(CORES/2) + 2*PODS*APP ))   # core-mesh + dist-core + access-dist
echo "[scale] topology: ${PODS} pods x ${APP} access x ${HPA} hosts + ${CORES} cores"
echo "[scale]   -> ${SW} switches, ${HOSTS} hosts, ~${ISL} inter-switch links (+${HOSTS} host links)"

export CAMPUS_PODS="$PODS" CAMPUS_ACCESS_PER_POD="$APP"
export CAMPUS_HOSTS_PER_ACCESS="$HPA" CAMPUS_CORES="$CORES" CAMPUS_FF=1
export CAMPUS_STATS_S="${CAMPUS_STATS_S:-5}"      # slower heatmap polling eases load at scale
for arg in "${@:5}"; do
  case "$arg" in
    lb)   export CAMPUS_LB=1;   echo "[scale] ECMP load balancing ON" ;;
    qos)  export CAMPUS_QOS=1;  echo "[scale] load-/QoS-aware routing ON (EF plane-pin, WCMP)" ;;
    dpi)  export CAMPUS_DPI=1;  echo "[scale] app-aware DPI classification ON (VoIP/HTTPS/FTP -> queues/plane)" ;;
    queues) export CAMPUS_QOS_QUEUES=1; echo "[scale] OVS priority queues ON (SetQueue high/med/low)" ;;
    mlag) export CAMPUS_MLAG=1; echo "[scale] MLAG lateral dist-peer links ON" ;;
    dc)   export CAMPUS_DC=1;   echo "[scale] data-centre tier ON (+servers)" ;;
    proactive) export CAMPUS_PROACTIVE=1; echo "[scale] proactive install ON (all groups up front, no pingall)" ;;
    all)  export CAMPUS_LB=1 CAMPUS_QOS=1 CAMPUS_DPI=1 CAMPUS_QOS_QUEUES=1 CAMPUS_MLAG=1 CAMPUS_DC=1 CAMPUS_PROACTIVE=1
          echo "[scale] ALL superset features ON" ;;
  esac
done

echo "[scale] cleaning stale state"; mn -c >/dev/null 2>&1
echo "[scale] starting Ryu (REST API :$API) -> ryu.log"
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" \
    --wsapi-port "$API" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT" && break; sleep 0.5; done
echo "[scale] Ryu up. Building the network (large sizes take a few minutes)…"
echo "[scale] Mininet build + queue setup -> mn.log (also shown here)"
# stdbuf keeps the stream line-buffered so the interactive mininet> prompt
# still appears while a full copy (incl. enable_qos_queues / ovs-vsctl output
# and any errors) is captured to mn.log for later inspection.
stdbuf -oL -eL python3 topology.py --test cli --controller-port "$PORT" \
    --pods "$PODS" --access-per-pod "$APP" --hosts-per-access "$HPA" --cores "$CORES" \
    2>&1 | tee mn.log
