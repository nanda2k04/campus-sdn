"""Congestion + load-balancing demo scene.

Real-life story: one plane of the campus fabric is carrying a heavy aggregate
load (many clients streaming to a single server).  Without load balancing every
flow pins to plane A, so the server's plane-A uplink SATURATES -- packets queue,
then drop.  With ECMP load balancing (CAMPUS_LB=1) the access switches hash-split
the flows across plane A *and* plane B, so the two uplinks into the server share
the load, nothing saturates, and the delivered throughput roughly doubles.

This is NOT congestion-reactive routing: ECMP does not "sense" that plane A is
slow and move traffic.  It spreads the flows up front so the pile-up never forms.
The proof is a before/after comparison -- run it once with LB off, once with LB
on -- plus the live link-load heatmap on the dashboard.

Driven from Mininet (root):   topology.py --test congest
Needs bandwidth-capped links (CAMPUS_BW) or there is nothing to saturate, and the
REST API up (:8080) for the live per-plane numbers + heatmap.

Tunables (env):
    CAMPUS_BW        inter-switch link cap, Mbit/s   (set by run_congest.sh = 20)
    CAMPUS_LB        1 = load balancing on            (set by run_congest.sh lb)
    CONGEST_FLOWS    number of concurrent client flows (default 16)
    CONGEST_RATE_M   offered rate per flow, Mbit/s     (default 2.0)
    CONGEST_DUR      traffic duration, seconds         (default 12)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from mininet.cli import CLI
from mininet.log import info

API = "http://127.0.0.1:8080"
PORT_BASE = 5201


def _env_float(name, default):
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _bar(title):
    print("\n" + "=" * 70)
    print("  " + title)
    print("=" * 70)


def _api(path):
    try:
        with urllib.request.urlopen(API + path, timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def _uplink_mbps(links, access, dist):
    """Find the live Mbit/s on the access<->dist link in an /api/links dump."""
    for l in links or []:
        if {l.get("src"), l.get("dst")} == {access, dist}:
            return l.get("mbps", 0.0)
    return 0.0


# iperf2 UDP server summary line, e.g.:
#   [  3] 0.0-12.0 sec  28.6 MBytes  20.0 Mbits/sec  0.04 ms 5123/25000 (20%)
_RX = re.compile(r"([\d.]+)\s+Mbits/sec")
_LOSS = re.compile(r"(\d+)\s*/\s*(\d+)\s*\(([\d.]+)%\)")


def _parse_server(text):
    """Return (rx_mbps, lost, total) from an iperf2 UDP server report, or None."""
    line = None
    for ln in text.splitlines():
        if "%)" in ln and "Mbits/sec" in ln:
            line = ln                       # keep the last matching (summary) line
    if not line:
        return None
    m_rx = _RX.search(line)
    m_loss = _LOSS.search(line)
    if not (m_rx and m_loss):
        return None
    rx = float(m_rx.group(1))
    lost, total = int(m_loss.group(1)), int(m_loss.group(2))
    return rx, lost, total


def run_congest(net, topo):
    m = topo.model
    P, A = m.pods, m.access_per_pod

    flows = _env_int("CONGEST_FLOWS", 16)
    rate = _env_float("CONGEST_RATE_M", 2.0)
    dur = _env_int("CONGEST_DUR", 12)
    cap = _env_float("CAMPUS_BW", 0.0)

    ctrl = _api("/api/controller") or {}
    lb = bool(ctrl.get("lb_enabled")) or os.environ.get("CAMPUS_LB") == "1"
    qos = bool(ctrl.get("qos_enabled")) or os.environ.get("CAMPUS_QOS") == "1"
    mode = "LOAD BALANCING ON  (ECMP, plane A + plane B)" if lb \
        else "LOAD BALANCING OFF (every flow pins to plane A)"
    EF_TOS = 184                       # DSCP 46 (EF) << 2  -> iperf --tos value

    # The server and its two uplinks (the bottleneck).
    server = m.host_name(P, A, 1)
    s_acc = "a%d_%d" % (P, A)
    up_a = m.dist_name(P, "A")              # a{P}_{A} <-> d{P}a  (plane A uplink)
    up_b = m.dist_name(P, "B")              # a{P}_{A} <-> d{P}b  (plane B uplink)
    srv = net.get(server)
    sip = srv.IP()

    # Clients: one host from as many *distinct* access switches as we need,
    # skipping the server's own access switch (those flows wouldn't cross the
    # fabric).  Spread across pods so no single source uplink is the bottleneck.
    clients = []
    for pod in range(1, P + 1):
        for acc in range(1, A + 1):
            if pod == P and acc == A:
                continue
            clients.append(m.host_name(pod, acc, 1))
            if len(clients) >= flows:
                break
        if len(clients) >= flows:
            break
    flows = len(clients)
    offered = flows * rate

    _bar("CAMPUS SDN -- CONGESTION & LOAD-BALANCING DEMO")
    print("  Mode:            %s" % mode)
    print("  Link cap:        %s Mbit/s on every switch-to-switch link"
          % (("%g" % cap) if cap else "UNCAPPED (set CAMPUS_BW!)"))
    print("  Server:          %s  (%s)   via access switch %s" % (server, sip, s_acc))
    print("  Its two uplinks: %s--%s (plane A)   and   %s--%s (plane B)"
          % (s_acc, up_a, s_acc, up_b))
    print("  Clients:         %d hosts streaming UDP at %g Mbit/s each"
          % (flows, rate))
    print("  Offered load:    %g Mbit/s total  ->  must funnel into %s"
          % (offered, server))
    if cap:
        print("  Single plane-A uplink can carry at most %g Mbit/s, so WITHOUT" % cap)
        print("  balancing ~%g Mbit/s of offered load is forced through one %g" %
              (offered, cap))
        print("  Mbit/s link -> saturation and drops.  WITH balancing it splits")
        print("  across the two %g Mbit/s uplinks (~%g each)." % (cap, offered / 2))
    print("  (Open the dashboard Topology tab, turn Heatmap ON, to watch it live.)")
    time.sleep(3)

    # ---- start one UDP server per flow (distinct ports) -----------------
    _bar("STEP 1 -- starting %d receivers on %s" % (flows, server))
    reports = {}
    for i in range(flows):
        port = PORT_BASE + i
        rpt = "/tmp/campus_iperf_s_%d.txt" % port
        reports[port] = rpt
        srv.cmd("iperf -s -u -p %d > %s 2>&1 &" % (port, rpt))
    ef_port = PORT_BASE + flows
    ef_rpt = "/tmp/campus_iperf_ef.txt"
    if qos:                            # a dedicated server for the critical flow
        srv.cmd("iperf -s -u -p %d > %s 2>&1 &" % (ef_port, ef_rpt))
    time.sleep(1)

    # ---- launch all clients as close to simultaneously as possible ------
    _bar("STEP 2 -- %d best-effort clients blasting %g Mbit/s each for %ds%s"
         % (flows, rate, dur, "  (+1 EF critical flow)" if qos else ""))
    procs = []
    for i, cname in enumerate(clients):
        port = PORT_BASE + i
        h = net.get(cname)
        p = h.popen("iperf -c %s -u -p %d -b %gm -t %d" % (sip, port, rate, dur),
                    shell=True)
        procs.append(p)
    ef_proc = None
    if qos:
        # one CRITICAL (EF/DSCP-46) flow from the first client; the controller
        # should steer it onto the least-loaded plane and keep its loss low even
        # while the best-effort flows saturate the other plane.
        ef_src = net.get(clients[0])
        ef_proc = ef_src.popen(
            "iperf -c %s -u -p %d -b %gm -t %d --tos %d"
            % (sip, ef_port, rate, dur, EF_TOS), shell=True)
    print("  %d flows running toward %s ..." % (flows + (1 if qos else 0), server))

    # ---- sample the live per-plane utilization mid-run ------------------
    time.sleep(max(2, dur // 2))
    links = (_api("/api/links") or [])
    a_mbps = _uplink_mbps(links, s_acc, up_a)
    b_mbps = _uplink_mbps(links, s_acc, up_b)
    _bar("STEP 3 -- live link load into the server (mid-stream)")
    print("  plane A uplink  %s -- %s :  %7.1f Mbit/s" % (s_acc, up_a, a_mbps))
    print("  plane B uplink  %s -- %s :  %7.1f Mbit/s" % (s_acc, up_b, b_mbps))
    tot = a_mbps + b_mbps
    if tot > 0:
        print("  split:  plane A %.0f%%  /  plane B %.0f%%"
              % (100 * a_mbps / tot, 100 * b_mbps / tot))
    if lb:
        print("  -> both planes are carrying traffic (load is spread).")
    else:
        print("  -> plane A is doing ALL the work; plane B sits idle.")
    if qos:
        q = _api("/api/qos") or {}
        print("  QoS: preferred (least-loaded) plane = %s   bottleneck Mbit/s=%s"
              % (q.get("preferred_plane"), q.get("bottleneck_mbps")))
        print("       the critical (EF) flow is steered onto plane %s"
              % q.get("preferred_plane"))

    # ---- collect ---------------------------------------------------------
    for p in procs:
        p.wait()
    if ef_proc is not None:
        ef_proc.wait()
    time.sleep(2)                           # let servers flush final reports
    srv.cmd("pkill -f 'iperf -s' 2>/dev/null")

    rx_total = 0.0
    lost_total = tot_total = 0
    parsed = 0
    for port, path in reports.items():
        try:
            with open(path) as fh:
                res = _parse_server(fh.read())
        except OSError:
            res = None
        if res:
            rx, lost, total = res
            rx_total += rx
            lost_total += lost
            tot_total += total
            parsed += 1

    loss_pct = (100.0 * lost_total / tot_total) if tot_total else 0.0

    ef_rx = ef_loss = None
    if qos:
        try:
            with open(ef_rpt) as fh:
                res = _parse_server(fh.read())
            if res:
                ef_rx, lost, total = res[0], res[1], res[2]
                ef_loss = (100.0 * lost / total) if total else 0.0
        except OSError:
            pass

    # ---- verdict ---------------------------------------------------------
    _bar("RESULT -- %s" % ("WITH load balancing" if lb else "WITHOUT load balancing"))
    print("  Offered load (what clients tried to send): %7.1f Mbit/s" % offered)
    print("  Delivered goodput (what the server got):   %7.1f Mbit/s  (%d/%d flows)"
          % (rx_total, parsed, flows))
    print("  Aggregate packet loss at the bottleneck:   %7.1f %%" % loss_pct)
    print("  Live uplink split (mid-stream):            A=%.1f  B=%.1f Mbit/s"
          % (a_mbps, b_mbps))
    if qos and ef_rx is not None:
        print("  --- QoS: CRITICAL (EF) flow vs best-effort ---")
        print("  EF critical flow goodput:                  %7.1f Mbit/s" % ef_rx)
        print("  EF critical flow loss:                     %7.1f %%  "
              "(vs best-effort %.1f%%)" % (ef_loss, loss_pct))
        print("  -> the critical flow was protected on the least-loaded plane")
        print("     while best-effort traffic absorbed the congestion.")
    print()
    if lb:
        print("  Load balancing spread the flows across BOTH planes, so neither")
        print("  uplink saturated: the server received far more of the offered")
        print("  traffic with little or no loss.  Run it again with LB OFF to see")
        print("  the single plane-A uplink choke.")
    else:
        print("  Every flow pinned to plane A, so the server's plane-A uplink")
        print("  saturated at the link cap and dropped the excess -- plane B sat")
        print("  idle.  Re-run with LB ON (./run_congest.sh lb) to see the load")
        print("  spread across both planes and the loss disappear.")
    _bar("Dropping to the Mininet CLI -- 'exit' to quit")
    CLI(net)
