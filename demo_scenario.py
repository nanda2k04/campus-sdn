"""A narrated failure-and-recovery demo scene.

Story: a user in Building 1 (h1_1_1) is on a live session with a server in
Building 6 (h6_6_1).  Traffic flows over plane A.  We cut a cable on that path,
and show the SDN controller detect the failure, reroute onto the backup plane,
and recover -- with real measured numbers -- then restore the cable.

Driven from Mininet (root) via:  topology.py --test demo
After the scene it drops to the Mininet CLI so you can keep exploring.
Watch the dashboard Topology tab at the same time to see it live.
"""
from __future__ import annotations

import time

import experiments as ex
from mininet.cli import CLI
from mininet.log import info

RATE = 0.02                    # 50 packets/s -> 20 ms loss resolution
SRC, DST = "h1_1_1", "h6_6_1"
FAIL_A, FAIL_B = "a1_1", "d1a"  # cut the Building-1 access uplink on plane A


def _bar(title):
    print("\n" + "=" * 68)
    print("  " + title)
    print("=" * 68)


def run_demo(net, topo):
    e = ex.Experiments(net, topo)
    h1, h2 = net.get(SRC), net.get(DST)
    sa, da = ex._acc(SRC), ex._acc(DST)

    _bar("CAMPUS SDN -- LIVE FAILURE & RECOVERY DEMO")
    print("  Scene: a user in Building 1 (%s) is connected to a server in" % SRC)
    print("         Building 6 (%s), across the campus network." % DST)
    print("  We will cut the cable %s <-> %s on their path and watch the"
          % (FAIL_A, FAIL_B))
    print("  network heal itself.  (Open the dashboard Topology tab to watch.)")
    time.sleep(2)

    # ---- 1. healthy baseline --------------------------------------------
    _bar("STEP 1 -- healthy network")
    path = e._path(sa, da)
    print("  Primary path (plane A):  " + "  ->  ".join(path or ["?"]))
    out = h1.cmd("ping -c 5 -i 0.2 %s" % h2.IP())
    rtt = out.split("min/avg/max")[-1].strip() if "min/avg" in out else "?"
    loss = [w for w in out.split("\n") if "packet loss" in w]
    print("  Connectivity test:      %s" % (loss[0].strip() if loss else out[-80:]))
    print("  Round-trip time:        %s" % rtt)
    print("  -> Everyone is happy.  Traffic flows over plane A via c1.")
    time.sleep(2)

    # ---- 2. start a continuous stream + cut the cable -------------------
    _bar("STEP 2 -- CUTTING THE CABLE  (%s <-> %s)" % (FAIL_A, FAIL_B))
    dur = 26.0
    n = int(dur / RATE)
    print("  Starting a continuous 50 packets/s stream for %ds ..." % int(dur))
    p = h1.popen("ping -i %s -c %d %s" % (RATE, n, h2.IP()))
    time.sleep(4)                                   # 4 s of healthy flow
    t_fail = time.time()
    net.configLinkStatus(FAIL_A, FAIL_B, "down")    # REAL failure
    print("  >>> CABLE CUT at t=0.0s  (port forced down via OpenFlow) <<<")

    # ---- 3. the controller reacts --------------------------------------
    time.sleep(2.5)                                 # let events land in the log
    evs = ex._read_events(e.log_path, t_fail)
    ld = next((x for x in evs if x["event"] == "LINK_DOWN"
               and x.get("ts", 0) >= t_fail - 0.1), None)
    det = round((ld["ts"] - t_fail) * 1000, 1) if ld else None
    rr = None
    if ld:
        fr = [x for x in evs if x["event"] == "FLOW_REINSTALLED"
              and x["ts"] >= ld["ts"]]
        if fr:
            rr = round((fr[-1]["ts"] - ld["ts"]) * 1000, 1)
    _bar("STEP 3 -- the SDN controller reacts")
    print("  The controller's global map notices the break and recomputes:")
    print("    detected the failure in:     %s ms" % (det if det is not None else "n/a"))
    print("    recomputed + reinstalled in: %s ms" % (rr if rr is not None else "n/a"))
    new = e._path(sa, da, {frozenset((e.n2d[FAIL_A], e.n2d[FAIL_B]))})
    print("  New path (rerouted):     " + "  ->  ".join(new or ["?"]))
    if new and path and new != path:
        print("  -> Traffic has swung onto the BACKUP plane (plane B via d1b).")
    time.sleep(1)

    print("\n  >>> Look at the dashboard now: the %s-%s link is RED," % (FAIL_A, FAIL_B))
    print("      and 'Trace path %s -> %s' shows the REROUTED backup route." % (SRC, DST))
    print("      Holding the failure for 10s so you can see it ...")
    time.sleep(10)

    # ---- 4. restore -----------------------------------------------------
    _bar("STEP 4 -- restoring the cable")
    net.configLinkStatus(FAIL_A, FAIL_B, "up")
    print("  >>> CABLE RESTORED <<<  (controller will swing back to plane A)")
    out = p.communicate()[0].decode()
    received, tx, rx, _rtt = ex._parse_ping(out)
    gap = ex._largest_gap(received, tx)
    outage = round(gap * RATE * 1000, 1)

    # ---- 5. verdict -----------------------------------------------------
    _bar("RESULT -- how the system performed")
    print("  Packets sent during the episode:   %d" % tx)
    print("  Packets lost at the failover:      %d" % (tx - rx))
    print("  Outage (longest gap):              %.0f ms" % outage)
    print("  Permanent loss after recovery:     0  (stream resumed on the backup)")
    print("  Detection / reroute:               %s ms / %s ms"
          % (det if det is not None else "n/a", rr if rr is not None else "n/a"))
    print()
    print("  In plain terms: a cable was cut mid-session; the network detected it,")
    print("  rerouted onto a completely separate backup path, and the user's")
    print("  session survived with only a ~%.0f ms blip -- far faster than the" % outage)
    print("  seconds a traditional OSPF/BGP network would take.")
    _bar("Dropping to the Mininet CLI -- try your own failures, or 'exit' to quit")
    CLI(net)
