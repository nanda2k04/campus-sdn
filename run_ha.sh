#!/usr/bin/env bash
# High-availability demo: TWO Ryu controllers (primary + backup) with every
# switch connected to BOTH.  Removes the single-controller SPOF (gap R1).
#
#   sudo ./run_ha.sh
#
# What to do in the demo:
#   1. Let it come up (both controllers connect; backup is passive standby).
#   2. In ANOTHER terminal, simulate the primary controller dying:
#          kill <PRIMARY_PID>        # the script prints PRIMARY_PID below
#   3. Watch ryu-backup.log: within ~2s it logs CONTROLLER_PROMOTED.
#   4. Back in the Mininet CLI, fail a link, e.g.:  link a1_1 d1a down
#      -> the (now-active) BACKUP controller reroutes it (FLOW_REINSTALLED in
#         ryu-backup.log).  The data plane never lost its controller.
#
# Logs:  ryu-primary.log  (:6653 / API :8080)   ryu-backup.log (:6654 / API :8081)
set -u
cd "$(dirname "$0")"
P1=6653; A1=8080; P2=6654; A2=8081
export CAMPUS_PODS=6 CAMPUS_ACCESS_PER_POD=6 CAMPUS_HOSTS_PER_ACCESS=2 CAMPUS_CORES=4
export CAMPUS_FF=1 CAMPUS_HA=1 CAMPUS_PORT2=$P2

echo "[ha] cleaning stale state"; mn -c >/dev/null 2>&1

echo "[ha] starting PRIMARY controller (:$P1, API :$A1) -> ryu-primary.log"
CAMPUS_ROLE=primary CAMPUS_PEER_API="http://127.0.0.1:$A2" \
  ./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$P1" \
    --wsapi-port "$A1" controller.py > ryu-primary.log 2>&1 &
PRIMARY_PID=$!

echo "[ha] starting BACKUP controller (:$P2, API :$A2) -> ryu-backup.log"
CAMPUS_ROLE=backup CAMPUS_PEER_API="http://127.0.0.1:$A1" \
  ./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$P2" \
    --wsapi-port "$A2" controller.py > ryu-backup.log 2>&1 &
BACKUP_PID=$!

trap 'kill $PRIMARY_PID $BACKUP_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$P1" && \
                         ss -ltn 2>/dev/null | grep -q ":$P2" && break; sleep 0.5; done

echo "============================================================"
echo "  HA UP.  primary PID = $PRIMARY_PID   backup PID = $BACKUP_PID"
echo "  To simulate the primary controller FAILING, in another terminal run:"
echo "        kill $PRIMARY_PID"
echo "  then watch:   tail -f ryu-backup.log   (look for CONTROLLER_PROMOTED)"
echo "  and fail a link in the Mininet CLI below, e.g.:  link a1_1 d1a down"
echo "============================================================"
python3 topology.py --test cli --controller-port "$P1" \
    --pods 6 --access-per-pod 6 --hosts-per-access 2 --cores 4
