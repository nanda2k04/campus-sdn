#!/usr/bin/env bash
# Fast FF datapath-proof diagnostic (~40 s): brings up the full fabric, warms one
# flow, fails the source access uplink while traffic flows, and prints whether the
# OpenFlow fast-failover group's BACKUP bucket actually took over (datapath) or the
# controller did the recovery.  Writes results/eval/proofs/a1_1_*_{before,during}.txt
#
#   sudo ./run_phase11_proof.sh
set -u
cd "$(dirname "$0")"
PORT=6653
export CAMPUS_PODS=6 CAMPUS_ACCESS_PER_POD=6 CAMPUS_HOSTS_PER_ACCESS=2 CAMPUS_CORES=4 CAMPUS_FF=1
# pass "bfd" to enable BFD on watch ports so the FF group liveness actually triggers
if [ "${1:-}" = "bfd" ]; then export CAMPUS_BFD=1; echo "[proof] BFD enabled on watch ports"; fi
mn -c >/dev/null 2>&1
./ryu-venv/bin/ryu-manager --observe-links --ofp-tcp-listen-port "$PORT" controller.py > ryu.log 2>&1 &
RYU_PID=$!
trap 'kill $RYU_PID 2>/dev/null; mn -c >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT" && break; sleep 0.5; done
python3 topology.py --test proof --controller-port "$PORT" \
    --pods 6 --access-per-pod 6 --hosts-per-access 2 --cores 4
[ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER" results 2>/dev/null || true
