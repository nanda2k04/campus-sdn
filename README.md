# Fast Failover & Resilience for Campus Networks using SDN

An SDN-based campus network that detects link/switch failures and fails traffic
over to redundant paths, using **Mininet + Open vSwitch + Ryu (OpenFlow 1.3)**.

## Quick start (fresh clone)

```bash
# 1. system prerequisites (once): Mininet, Open vSwitch, Python 3.9, Node.js
sudo apt install mininet openvswitch-switch python3.9 python3.9-venv nodejs npm

# 2. project setup (recreates the Ryu venv + dashboard deps; no root needed)
./setup.sh

# 3. run it — two terminals
sudo ./run_scale.sh 6 4 24 4 all      # Terminal 1: Ryu + Mininet fabric (root)
cd dashboard && npm run dev           # Terminal 2: dashboard -> http://localhost:5173
```

`ryu-venv/` and `node_modules/` are intentionally **not** committed; `setup.sh`
rebuilds them from `requirements.txt` and `dashboard/package-lock.json`.
**Ryu requires Python 3.9** (it breaks on 3.10+). Offline proof needs no root:
`python3 multifail_proof.py`.

## Architecture (Phase 1, approved)

Hybrid campus topology: a 3-tier Core/Distribution/Access hierarchy hardened with
data-center-grade redundancy.

| Tier | Count | Redundancy |
|------|------:|------------|
| Core | 4 | full mesh (6 core-core links) |
| Distribution | 12 | 2 per pod: plane A (primary) + plane B (backup) |
| Access | 36 | 6 per pod, **dual-homed** to both dist switches |
| Hosts | 72 | 2 per access (single-homed by design) |

Two node-disjoint forwarding planes between any two pods:
`plane A: access -> distA -> core -> distA -> access`, and the disjoint
`plane B` via the distribution-B switches and the other half of the core.

## Environment

- Mininet 2.3.0 + OVS 3.3.9 run on system **python3** (need root).
- Ryu 4.34 runs in **`ryu-venv`** (python3.9) with pinned `eventlet==0.30.2`,
  `dnspython==1.16.0` (newer versions break Ryu on this box).

Recreate the controller env if needed:

```bash
python3.9 -m venv ryu-venv
./ryu-venv/bin/pip install "eventlet==0.30.2" "dnspython==1.16.0" "ryu==4.34"
```

## Files

| File | Purpose | Runtime |
|------|---------|---------|
| `campus_model.py` | single source of truth: switches, links, ports, hosts, graph helpers | stdlib (any python3) |
| `topology.py` | Mininet builder (OF1.3, deterministic DPIDs/ports) | system python3, **root** |
| `validate_topology.py` | topology + failover-independence validation | any python3 |
| `topology.json` | exported model (for the controller & dashboard later) | generated |

## Phase 2 usage

```bash
# 1. validate the model (no root needed)
python3 validate_topology.py

# 2. (re)generate topology.json
python3 campus_model.py

# 3. inspect the live Mininet wiring (root; no controller needed)
sudo python3 topology.py --test dump

# 4. interactive Mininet CLI (needs Ryu running first, see Phase 3)
sudo python3 topology.py
```

## Phase 3 usage (controller + connectivity)

One-shot (starts Ryu, runs the Mininet connectivity test, tears down):

```bash
sudo ./run_phase3.sh            # representative pings (fast)
sudo ./run_phase3.sh pingall    # full 72-host pingall (slow)
```

Or two terminals:

```bash
# terminal 1 (controller)
./ryu-venv/bin/ryu-manager --ofp-tcp-listen-port 6653 controller.py
# terminal 2 (network)
sudo python3 topology.py --test conn      # or plain: sudo python3 topology.py
```

The controller forwards unicast along computed shortest paths and keeps
broadcast loop-free by setting non-tree inter-switch ports to `NO_FLOOD`
(51 tree edges out of 102 links). It logs structured `EVENT` lines
(`SWITCH_CONNECTED`, `HOST_LEARNED`, `PATH_INSTALLED`, ...).

## Phase 5/6 usage (path computation + fast failover)

Offline logic proof (no root needed):

```bash
python3 validate_paths.py     # 6/6: disjoint paths + FF delivery under each failure
```

Live fast-failover demo (drops a dist->core link during a continuous ping;
the FF group swings to the 2nd core locally, without a controller reroute):

```bash
sudo ./run_phase6.sh smoke        # 12-switch smoke first
sudo ./run_phase6.sh full         # 52-switch
sudo ./run_phase6.sh full noff    # control: FF disabled (needs controller reroute)
```

`CAMPUS_FF=1` (default) installs destination-rooted forwarding: fabric switches
match the destination's /40 MAC prefix and output via an `OFPGT_FF` group
(bucket0 = primary port, bucket1 = local backup). Local protection exists at the
access uplink, the dist->core uplink, and the core->dest-dist hop; the final
dist->access hop has no local backup (Phase 7 controller reroute).

## Phase 7 usage (controller rerouting)

Offline proof:

```bash
python3 validate_reroute.py   # 7/7: reroute delivers under last-hop + switch failures
```

Live demo (fails a destination's last-hop link, which FF cannot protect locally,
so the controller detects it and reroutes to the other plane):

```bash
sudo ./run_phase7.sh smoke
sudo ./run_phase7.sh full
```

Two-layer resilience:
- local FF (Phase 6) handles access-uplink / dist-core / core-dist link failures
  in the datapath (~1 packet).
- controller rerouting (Phase 7) handles the last-hop dist->access failure and
  distribution/core switch failures by recomputing `plan_dest_tree` over the live
  graph (down edges/nodes removed) and reinstalling flows/groups. Events:
  `FAILOVER_TRIGGERED`, `FLOW_REINSTALLED`, `LINK_RECOVERED`, `SWITCH_RECOVERED`.

## Phase 8 usage (metrics & experiments)

```bash
sudo ./run_phase8.sh smoke     # or: full
```

Runs baseline + access-uplink FF + dist-core FF + core-link FF + last-hop
controller-reroute, and writes one JSON per scenario to `results/` plus
`results/summary.json`. Each record has path_before/path_after, failed_component,
baseline RTT, packets tx/rx, packet_loss, outage_ms (from receiver-side seq gaps
at 50 pps = 20 ms resolution), and detection_ms/reroute_ms/affected_flows
(correlated from the controller event log for the reroute case). This JSON is
what Phase 9 (API) and Phase 10 (dashboard) will serve.

## Phase 9 usage (REST API)

The API lives inside the Ryu controller (Ryu WSGI, default `:8080`) and serves
REAL state; failures are injected as real OpenFlow `OFPPortMod PORT_DOWN`.

```bash
sudo ./run_phase9.sh smoke     # brings up network + API, drops to Mininet CLI
```

Endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/controller` | status, counts, reconcile |
| GET | `/api/topology` | switches + hosts + links (graph) |
| GET | `/api/switches` `/api/links` `/api/hosts` | component lists |
| GET | `/api/events?since=<seq>&limit=<n>` | event ring buffer |
| GET | `/api/metrics` | Phase 8 `results/summary.json` + live counters |
| POST | `/api/failures/link` `{a,b}` | down the a-b link (real) |
| POST | `/api/recovery/link` `{a,b}` | restore it |
| POST | `/api/failures/switch` `{name}` | isolate a switch |
| POST | `/api/recovery/switch` `{name}` | restore it |

## Phase 10 usage (NOC dashboard)

React + TypeScript + Vite. Proxies `/api` to the controller on `:8080`.

```bash
# terminal 1: network + controller + REST API
sudo ./run_phase9.sh smoke
# terminal 2: dashboard
cd dashboard && npm install && npm run dev      # http://localhost:5173
```

Tabs: Overview (health tiles + reconciliation + recent failovers), Topology
(layered SVG graph; click a link to fail/recover, a switch to isolate/restore),
Failover (Phase 8 metrics + live timeline), Switches / Links / Hosts tables with
inline failure buttons, and a filterable Events log. Every failure button POSTs
to the API, which injects a REAL OpenFlow port-down — no mock data.

## Phase 11 usage (evaluation, proofs & comparison)

Statistical resilience evaluation with real artifacts, plus an RSTP same-testbed
baseline and a literature/architecture comparison.

```bash
# 1. statistical suite: N randomized on-path trials per failure type + one-way FF
#    pass + controller CPU/RSS + flow counts + OpenFlow proof dumps
sudo EVAL_TRIALS=20 ./run_phase11.sh full       # -> results/eval/

# 2. RSTP baseline on the SAME topology (no controller; rstp_enable)
sudo mn -c && sudo python3 baseline_rstp.py     # -> results/baseline_rstp/

# 3. scalability sweep (12 / 28 / 52 switches)
sudo ./run_phase11_scale.sh                     # -> results/scale/

# 4. render the report (charts + comparison + literature/architecture tables)
python3 make_report.py                          # -> results/report.pdf + .md + PNGs
```

Metrics per failure type (median / p95 / 95% CI over trials): outage, packet
loss, detection, controller reroute, affected flows, recovery-success rate; plus
one-way forward FF outage (isolates the datapath fast-failover), controller
CPU/RSS, installed-flow count, and `dump-group-stats` before/during a failure
(backup bucket's `packet_count` climbs = datapath proof). The dashboard
**Analysis** tab serves all of this live via `/api/eval`.

**Honest finding (verified by datapath capture):** in this software (Mininet/OVS)
testbed, OpenFlow fast-failover group liveness does **not** switch buckets on a
link-down -- `dump-group-stats` during a failure shows the primary bucket still
selected (packets dropped) and the backup bucket idle. Enabling **BFD** on all
watch ports (`CAMPUS_BFD=1`, 276 interfaces) did **not** change this: software OVS
datapath fast-failover is a known-finicky feature that relies on hardware-style
port-liveness events. So the measured recovery here is **controller-driven**
(~80 ms link, ~210 ms switch, 100% delivery). The FF groups are correctly built
and installed network-wide (`type=ff`, `watch_port`, dest-rooted) and would
provide sub-50 ms datapath-local protection on real hardware, where port-down is
an immediate hardware event.

**Comparison framing:** the decisive wins are (a) vs L3 routing (OSPF/IS-IS/BGP),
where controller recovery is 10-1000x faster and also handles switch failures,
and (b) architecture: SDN uses all links (RSTP blocks ~50%), with a global view,
deterministic node-disjoint backups, and programmable policy. RSTP's rapid
alternate-port failover is genuinely fast in software (~20 ms); our FF would match
it on hardware/BFD, but we do not overclaim that in emulation.

## Extra features: load balancing + link-load heatmap

**Live link-load heatmap.** The controller polls OpenFlow port stats every
`CAMPUS_STATS_S` seconds (default 2) and serves per-link throughput (`mbps`) in
`/api/links`. In the dashboard Topology tab, tick **Link-load heatmap** to colour
every link by live Mbit/s (idle -> green -> amber -> red; hover for the exact
value). Generate traffic from the Mininet CLI to watch it, e.g.
`h1_1_1 ping h6_6_1` or an `iperf` pair.

**ECMP load balancing across both planes.** By default each destination uses one
primary path (the other plane is backup). With `CAMPUS_LB=1` the controller
installs `OFPGT_SELECT` groups instead, so flows are hashed across **both**
node-disjoint planes (and both cores per plane) -- using every link instead of
leaving half idle (the key advantage over RSTP). Failover stays controller-driven.

```bash
sudo ./run_phase9.sh full lb        # full topology with ECMP enabled
# then, in the Mininet CLI, confirm it's still loop-free:
mininet> pingall                    # expect 0% loss
# ...and watch both planes light up under load with the dashboard heatmap.
```

The header shows `LB on` when enabled. ECMP is opt-in; validate with `pingall`
before relying on it.

## Phase 12: resolving the literature gaps (load/QoS-aware routing, HA, multi-failure)

A gap analysis against three SDN resilience surveys drove four additions. All are
opt-in and leave the default behaviour unchanged.

**Load- & QoS-aware routing (`CAMPUS_QOS=1`, module `qos.py`).** The controller
already measures per-link Mbit/s for the heatmap; this feeds that back into the
path decision:

- **EF/critical flows** (IP DSCP 46) are pinned, at each source access switch, to
  the **least-loaded plane** -- one `ip_dscp` flow per access switch (`PRIO_QOS_EF`),
  repinned whenever the per-plane load crosses a hysteresis threshold or a failure
  takes that plane's uplink down. (closes "static vs dynamic LSI" + QoS-aware gaps)
- **Best-effort ECMP** buckets become **WCMP** -- weighted by inverse plane load so
  the quieter plane takes more flows (recomputed on every (re)install/reroute).
- **Congestion-aware**: reroutes check the target plane's headroom vs `CAMPUS_BW`;
  if both planes are saturated a `CONGESTION_WARNING` is logged. `/api/qos` serves
  the live preferred plane, per-plane load, and EF pins.

```bash
# congestion scene with QoS on: best-effort saturates one plane, the EF
# "critical" flow is steered onto the least-loaded plane and stays low-loss.
sudo ./run_congest.sh lb qos
```

**Controller high availability (`CAMPUS_HA=1`, `run_ha.sh`).** Removes the
single-controller SPOF: two Ryu instances run the same app and every switch is
connected to **both**. The primary is active; the backup is a passive standby
(connected, installs no per-flow state) that heartbeats the primary's REST API and
**promotes itself** (`CONTROLLER_PROMOTED`) within ~2 s if the primary dies, then
takes over rerouting. It fails back to standby when the primary returns.

```bash
sudo ./run_ha.sh          # prints the primary PID; `kill` it to see failover,
                          # then fail a link in the CLI and watch ryu-backup.log
```

**Multiple simultaneous failures.** The controller flags true partitions
(`DEST_UNREACHABLE`) when both disjoint planes are cut. An exhaustive **offline
proof** validates the reroute under every single and double link failure:

```bash
python3 multifail_proof.py    # no root needed; writes results/multifail_proof.json
# PASS: 100% recovery over all 102 single-link failures; 99.96% over all 25,755
# double-link failures (the rest are genuine partitions); zero loops/black-holes.
```

**Datapath sub-50 ms protection (honest).** OpenFlow fast-failover groups are
installed and are hardware-ready, but software OVS does not trip watch-port
liveness without BFD/hardware (confirmed in Phase 11), so recovery is
controller-driven (~80-210 ms) -- consistent with Sharma et al. (restoration
80-100 ms; sub-50 ms needs BFD+FF protection). Not faked in software.

## Roadmap

- [x] Phase 1 - architecture selection
- [x] Phase 2 - Mininet topology + validation
- [x] Phase 3 - Ryu controller + baseline L2 connectivity (PASS: 52 sw / 72 hosts, 0% loss)
- [x] Phase 4 - topology discovery (LLDP; reconcile 204/204 links, model match, link-down detected)
- [x] Phase 5 - path computation (primary + node-disjoint backup; validated offline)
- [x] Phase 6 - OpenFlow fast-failover groups (live smoke: dist-core link drop = 1 pkt lost, ~0.1s, local FF)
- [x] Phase 7 - failure detection & controller rerouting (live smoke: last-hop fail = 0 loss, ~4ms reroute)
- [x] Phase 8 - metrics & experiments (live: FF ~20ms/1pkt, controller reroute ~40ms/2pkt; results/*.json)
- [x] Phase 9 - backend REST API (live: real state + OpenFlow port-down failure injection)
- [x] Phase 10 - React NOC dashboard (Vite + TS; live overview, topology graph, failover metrics, failure buttons)
- [x] Phase 11 - evaluation & comparison (N-trial stats, one-way FF, CPU/RSS, flow counts, datapath proofs; RSTP baseline; scalability sweep; PDF report + dashboard Analysis tab)
- [x] Phase 12 - literature-gap closure (load/QoS-aware routing + WCMP; controller HA/failover removing the SPOF; multi-failure proof 100%/99.96% single/double; honest FF framing)
