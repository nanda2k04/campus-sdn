#!/usr/bin/env bash
# One-shot setup for a fresh clone. Recreates the Ryu virtualenv and installs
# the dashboard's node modules. Does NOT need root (the sudo parts are only when
# you RUN the network, not when you set it up).
#
#   ./setup.sh
#
# System prerequisites that this script cannot install for you (install once,
# with your package manager) are checked and reported at the end.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Campus SDN setup"

# ---- 1. Ryu control-plane virtualenv (Python 3.9 REQUIRED) ------------------
# Ryu 4.34 does not run on Python 3.10+ (removed stdlib ABCs / eventlet pin),
# so the venv must be built with python3.9.
PY39="$(command -v python3.9 || true)"
if [[ -z "$PY39" ]]; then
  echo "!! python3.9 not found. Install it first, e.g.:"
  echo "     sudo apt install python3.9 python3.9-venv"
  exit 1
fi

if [[ ! -d ryu-venv ]]; then
  echo "==> creating ryu-venv with $PY39"
  "$PY39" -m venv ryu-venv
fi
echo "==> installing pinned Python deps into ryu-venv"
./ryu-venv/bin/pip install --upgrade pip >/dev/null
./ryu-venv/bin/pip install -r requirements.txt
./ryu-venv/bin/python -c "import ryu; print('    ryu', ryu.version if hasattr(ryu,'version') else 'OK')" 2>/dev/null || true

# ---- 2. Dashboard (Node) ----------------------------------------------------
if command -v npm >/dev/null 2>&1; then
  echo "==> installing dashboard node modules"
  ( cd dashboard && npm install )
else
  echo "!! npm not found — install Node.js 18+ to build the dashboard"
fi

# ---- 3. Report system prerequisites (needed to RUN, not to set up) ----------
echo
echo "==> checking system prerequisites (needed when you RUN the network):"
check() { if command -v "$1" >/dev/null 2>&1; then echo "    [ok] $1"; else echo "    [MISSING] $1 — $2"; fi; }
check mn         "Mininet: sudo apt install mininet"
check ovs-vsctl  "Open vSwitch: sudo apt install openvswitch-switch"
check python3    "system python3 (runs Mininet as root)"
check node       "Node.js (dashboard)"

echo
echo "==> done. To run everything:"
echo "    Terminal 1:  sudo ./run_scale.sh 6 4 24 4 all"
echo "    Terminal 2:  cd dashboard && npm run dev    # http://localhost:5173"
