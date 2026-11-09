#!/usr/bin/env bash
# HA detection-latency probe. Run this in a SECOND terminal while run_ha.sh is
# up in the first (and sitting at its Mininet CLI). It finds the primary Ryu
# controller, kills it, and measures how long the backup takes to promote --
# the controller-failover detection latency for the current CAMPUS_HB_MS /
# CAMPUS_HB_MISSES setting.
#
#   sudo ./ha_probe.sh
#
# (sudo because run_ha.sh runs the controllers as root, so the kill and the
#  log reads need root.)  After it reports PROMOTED, go to the Mininet CLI and
#  run  pingall  to confirm the promoted backup still forwards the data plane.
set -u
cd "$(dirname "$0")"
PRIMARY_PORT=6653
BACKUP_PORT=6654
BACKUP_LOG=ryu-backup.log

P=$(pgrep -f "ofp-tcp-listen-port ${PRIMARY_PORT}" || true)
B=$(pgrep -f "ofp-tcp-listen-port ${BACKUP_PORT}" || true)
if [ -z "$P" ]; then
  echo "[probe] ERROR: no primary controller on :${PRIMARY_PORT}. Is run_ha.sh up?"
  exit 1
fi
if [ -z "$B" ]; then
  echo "[probe] ERROR: no backup controller on :${BACKUP_PORT}. Is run_ha.sh up?"
  exit 1
fi
echo "[probe] primary PID=$P  backup PID=$B"

# baseline: how many promotions already in the log (should be 0).
# NB: grep -c prints 0 AND exits 1 on no matches, so DON'T use "|| echo 0"
# (that would append a second 0 and corrupt the integer compare below).
BEFORE=$(grep -c CONTROLLER_PROMOTED "$BACKUP_LOG" 2>/dev/null)
BEFORE=${BEFORE:-0}
echo "[probe] promotions in log before kill: $BEFORE  (expect 0 = no false positive)"

T_KILL=$(date +%s.%N)
echo "[probe] killing primary at ${T_KILL}"
kill "$P"

# wait for the backup to log a NEW CONTROLLER_PROMOTED
PROMO_TS=""
for _ in $(seq 1 40); do          # up to ~12s
  LINE=$(grep CONTROLLER_PROMOTED "$BACKUP_LOG" 2>/dev/null | tail -1)
  if [ -n "$LINE" ]; then
    COUNT=$(grep -c CONTROLLER_PROMOTED "$BACKUP_LOG" 2>/dev/null); COUNT=${COUNT:-0}
    if [ "$COUNT" -gt "$BEFORE" ]; then
      PROMO_TS=$(echo "$LINE" | grep -oE '"ts": [0-9.]+' | grep -oE '[0-9.]+')
      break
    fi
  fi
  sleep 0.3
done

echo
if [ -z "$PROMO_TS" ]; then
  echo "[probe] NO promotion within timeout. Primary state:"
  pgrep -f "ofp-tcp-listen-port ${PRIMARY_PORT}" >/dev/null \
      && echo "        primary STILL ALIVE (kill failed -- run this with sudo)" \
      || echo "        primary dead but backup never promoted (REAL finding)"
  echo "[probe] last backup events:"; grep EVENT "$BACKUP_LOG" | tail -6
  exit 2
fi

echo "[probe] PROMOTED. event ts=${PROMO_TS}  kill ts=${T_KILL}"
awk -v a="$T_KILL" -v b="$PROMO_TS" \
  'BEGIN{printf "[probe] detection latency = %.3f s  (CAMPUS_HB_MS x CAMPUS_HB_MISSES)\n", b-a}'
pgrep -f "ofp-tcp-listen-port ${PRIMARY_PORT}" >/dev/null \
    && echo "[probe] WARNING: primary still alive?" \
    || echo "[probe] primary confirmed dead."
echo "[probe] NOW: in the Mininet CLI run 'pingall' to confirm the data plane."
