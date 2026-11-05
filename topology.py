#!/usr/bin/env python3
"""Mininet builder for the hybrid-campus SDN topology.

Consumes the single source of truth in ``campus_model.py`` and wires it up in
Mininet with:
  * OVS switches speaking OpenFlow 1.3 only,
  * deterministic DPIDs (== the model's dpid, as 16 hex digits),
  * explicit port numbers on both ends of every inter-switch link,
  * a remote Ryu controller (default 127.0.0.1:6653).

Must be run as root (Mininet requirement):

    sudo python3 topology.py                 # interactive CLI (needs Ryu running)
    sudo python3 topology.py --test dump      # build, print wiring, tear down
    sudo python3 topology.py --controller-ip 127.0.0.1 --controller-port 6653

Also exposes a ``topos`` dict so it works with:  sudo mn --custom topology.py --topo campus
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI

import campus_model as cm

OF_VERSION = "OpenFlow13"


class CampusTopo(Topo):
    """Mininet Topo built from a CampusTopology model."""

    def build(self, pods=6, access_per_pod=6, hosts_per_access=2, cores=4):
        # Superset features, opt-in via env so the base topology is unchanged:
        #   CAMPUS_MLAG=1 -> lateral dist-peer links; CAMPUS_DC=1 -> data-centre
        #   tier with CAMPUS_SERVERS servers.
        mlag = os.environ.get("CAMPUS_MLAG") == "1"
        dc = os.environ.get("CAMPUS_DC") == "1"
        servers = int(os.environ.get("CAMPUS_SERVERS", "3"))
        model = cm.CampusTopology(pods=pods, access_per_pod=access_per_pod,
                                  hosts_per_access=hosts_per_access, cores=cores,
                                  mlag=mlag, dc=dc, servers=servers)
        self.model = model

        # switches: dpid as 16 hex digits, OF1.3 only
        for name, attr in model.switches.items():
            self.addSwitch(name, dpid="%016x" % attr["dpid"], protocols=OF_VERSION)

        # hosts: deterministic IP + MAC
        for name, attr in model.hosts.items():
            self.addHost(name, ip=attr["ip"] + "/8", mac=attr["mac"])

        # Optional bandwidth cap on inter-switch links (CAMPUS_BW, in Mbit/s).
        # Host-access links stay uncapped so a client can always source its full
        # offered rate; the cap bites only on the switch fabric (where congestion
        # and the load-balancing benefit are meant to show up).
        bw = None
        _bw = os.environ.get("CAMPUS_BW")
        if _bw:
            try:
                bw = float(_bw)
            except ValueError:
                bw = None
        self.link_bw = bw

        # links: explicit ports on both ends
        for lk in model.links:
            if lk["kind"] in (cm.K_HOST_ACCESS, cm.K_HOST_DC):
                # host side auto (single interface); pin the switch-side port
                self.addLink(lk["a"], lk["b"], port2=lk["b_port"])
            else:
                kw = dict(port1=lk["a_port"], port2=lk["b_port"])
                if bw is not None:
                    kw["bw"] = bw          # requires TCLink (set in make_net)
                self.addLink(lk["a"], lk["b"], **kw)


# For:  sudo mn --custom topology.py --topo campus
topos = {"campus": (lambda **kw: CampusTopo(**kw))}


def make_net(args):
    topo = CampusTopo(pods=args.pods, access_per_pod=args.access_per_pod,
                      hosts_per_access=args.hosts_per_access, cores=args.cores)
    # Use TCLink only when a bandwidth cap is requested (CAMPUS_BW); otherwise
    # the default Link (no tc overhead) to keep normal runs fast.
    link_cls = TCLink if os.environ.get("CAMPUS_BW") else None
    mn_kwargs = dict(
        topo=topo,
        switch=OVSSwitch,
        controller=None,           # add remote controller explicitly below
        autoSetMacs=False,         # MACs come from the model
        build=False,
    )
    if link_cls is not None:
        mn_kwargs["link"] = link_cls
    net = Mininet(**mn_kwargs)
    net.addController(
        "c0", controller=RemoteController,
        ip=args.controller_ip, port=args.controller_port,
    )
    # R1 high-availability: connect every switch to a SECOND (backup) controller
    # as well, so the loss of one controller does not strand the data plane.
    if os.environ.get("CAMPUS_HA") == "1":
        port2 = int(os.environ.get("CAMPUS_PORT2", args.controller_port + 1))
        net.addController(
            "c1", controller=RemoteController,
            ip=args.controller_ip, port=port2,
        )
        info("*** HA: switches dual-homed to controllers :%d (primary) and "
             ":%d (backup)\n" % (args.controller_port, port2))
    net.build()
    return net, topo


def dump_wiring(net, topo):
    m = topo.model
    s = m.summary()
    info("*** Model summary: %s\n" % s)
    info("*** Switches (name dpid tier): %d\n" % len(m.switches))
    # sanity: show a representative switch's live OVS port map
    sample = m.access_name(1, 1)
    sw = net.get(sample)
    info("*** Sample access switch %s live ports:\n" % sample)
    for intf in sw.intfList():
        if intf.name == "lo":
            continue
        info("      port %s -> %s\n" % (sw.ports[intf], intf.name))
    info("*** Sample dist switch d1a live ports:\n")
    swd = net.get("d1a")
    for intf in swd.intfList():
        if intf.name == "lo":
            continue
        info("      port %s -> %s\n" % (swd.ports[intf], intf.name))
    info("*** Total links in Mininet: %d\n" % len(net.links))


# An unused address inside the flat 10.0.0.0/8 L2 subnet. Pinging it from a host
# with an empty ARP cache forces the host to broadcast one ARP who-has, which the
# controller punts up on the host-ingress port and learns -- without any reply.
WARMUP_IP = "10.255.255.254"


def enable_qos_queues(net, rate_mbps=100):
    """Configure three linux-htb priority queues (0=low, 1=med, 2=high) on every
    switch<->switch port, so the controller's OFPActionSetQueue (app-aware DPI /
    EF marking, CAMPUS_QOS_QUEUES=1) has real queues to schedule into.  High
    traffic is guaranteed the largest share under contention.  Opt-in; at large
    scale this is many ovs-vsctl calls, so use it on demo-sized topologies."""
    rate = int(rate_mbps * 1_000_000)
    lo, med, hi = int(rate * 0.1), int(rate * 0.3), int(rate * 0.6)
    n = 0
    for link in net.links:
        for intf in (link.intf1, link.intf2):
            if intf.node in net.switches and intf.name != "lo":
                cmd = (
                    "ovs-vsctl -- set port %s qos=@newqos -- "
                    "--id=@newqos create qos type=linux-htb other-config:max-rate=%d "
                    "queues:0=@q0 queues:1=@q1 queues:2=@q2 -- "
                    "--id=@q0 create queue other-config:min-rate=%d other-config:max-rate=%d -- "
                    "--id=@q1 create queue other-config:min-rate=%d other-config:max-rate=%d -- "
                    "--id=@q2 create queue other-config:min-rate=%d other-config:max-rate=%d"
                    % (intf.name, rate, lo, rate, med, rate, hi, rate))
                intf.node.cmd(cmd)
                n += 1
    info("*** configured HTB QoS queues (low/med/high) on %d switch ports\n" % n)


def enable_bfd(net, interval_ms=50):
    """Enable BFD on every switch<->switch interface so OpenFlow fast-failover
    group liveness actually tracks link health.  Without this, OVS treats a plain
    port as 'live' even after the link drops, so FF never switches buckets and
    recovery falls back to the controller.  With BFD, a watch_port's bucket goes
    not-live on BFD-down and the datapath fails over locally.  Opt-in via
    CAMPUS_BFD=1 (interval from CAMPUS_BFD_MS, default 50 ms)."""
    n = 0
    for link in net.links:
        for intf in (link.intf1, link.intf2):
            if intf.node in net.switches:          # switch-side port only
                intf.node.cmd(
                    "ovs-vsctl set interface %s bfd:enable=true "
                    "bfd:min_rx=%d bfd:min_tx=%d" % (intf.name, interval_ms, interval_ms))
                n += 1
    info("*** enabled BFD on %d switch interfaces (interval %dms) for FF liveness\n"
         % (n, interval_ms))


def warmup_learn(net, settle=4):
    """Make every host emit one broadcast ARP so the controller learns all 72.

    Connectivity does NOT depend on this -- forwarding is proactive (destination
    /40 flows seeded from the model). But reactive MAC-learning only fires when a
    host sends a *controller-bound* frame (its own broadcast ARP). In a sequential
    pingall the last host answers everyone else's ARP first, fills its cache, then
    never needs to ARP again -- so it is never 'learned' (hosts_learned = 71/72).
    One gratuitous ARP per host at startup (caches still empty) closes that gap.
    """
    info("*** Warming up host learning (one broadcast ARP from %d hosts)\n"
         % len(net.hosts))
    time.sleep(settle)                       # let switches connect + tree flows install
    for h in net.hosts:                      # non-blocking: fire the ARP, don't wait
        h.cmd("ping -c1 -W1 %s >/dev/null 2>&1 &" % WARMUP_IP)
    time.sleep(2)                            # let the ARP broadcasts reach the controller


def connectivity_test(net, topo, full=False, settle=8):
    """Phase 3 baseline-connectivity gate (Phase 1 section 12)."""
    info("*** Waiting for all switches to connect to the controller\n")
    if not net.waitConnected(timeout=40):
        info("*** ERROR: not all switches connected to the controller\n")
        return False
    info("*** Connected. Letting the controller push table-miss / broadcast "
         "tree flows (%ds)\n" % settle)
    time.sleep(settle)

    if full:
        info("*** Full pingall (72 hosts -- this takes a while)\n")
        loss = net.pingAll()
        info("*** pingall aggregate loss: %.1f%%\n" % loss)
        return loss == 0.0

    # representative pairs covering each connectivity class, derived from the
    # model so this works at any scale (smoke topology included)
    m = topo.model
    P, A, H = m.pods, m.access_per_pod, m.hosts_per_access
    pairs = []
    if H >= 2:
        pairs.append((m.host_name(1, 1, 1), m.host_name(1, 1, 2),
                      "same access switch"))
    if A >= 2:
        pairs.append((m.host_name(1, 1, 1), m.host_name(1, 2, 1),
                      "same pod, different access"))
    if P >= 2:
        pairs.append((m.host_name(1, 1, 1), m.host_name(P, A, H),
                      "cross-pod / cross-region"))
    if P >= 5 and A >= 4:
        pairs.append((m.host_name(2, 3, 1), m.host_name(5, 4, min(2, H)),
                      "cross-pod / cross-region"))
    ok = True
    info("*** Representative connectivity pings:\n")
    for a, b, label in pairs:
        loss = net.ping([net.get(a), net.get(b)])
        status = "OK" if loss == 0.0 else "LOSS"
        if loss != 0.0:
            ok = False
        info("      [%-4s] %-38s %s <-> %s  loss=%.0f%%\n"
             % (status, label, a, b, loss))
    info("*** Baseline connectivity: %s\n" % ("PASS" if ok else "FAIL"))
    return ok


def discover_test(net, topo, settle=22):
    """Phase 4: let LLDP discovery converge, then prove link-down detection."""
    info("*** Waiting for all switches to connect to the controller\n")
    if not net.waitConnected(timeout=40):
        info("*** ERROR: not all switches connected\n")
        return False
    info("*** LLDP discovery converging (%ds) -- watch controller for "
         "LINK_DISCOVERED / RECONCILE\n" % settle)
    time.sleep(settle)

    # demonstrate that discovery DETECTS a link failure (detection only here;
    # automatic rerouting/failover is Phase 7).  One link, quickly restored.
    a, b = "a1_1", "d1a"
    info("*** Injecting link DOWN: %s <-> %s  (expect LINK_DOWN + RECONCILE missing)\n"
         % (a, b))
    net.configLinkStatus(a, b, "down")
    time.sleep(8)
    info("*** Restoring link %s <-> %s  (expect LINK_DISCOVERED + RECONCILE converged)\n"
         % (a, b))
    net.configLinkStatus(a, b, "up")
    time.sleep(10)
    info("*** Discovery test done\n")
    return True


def failover_test(net, topo, settle=8):
    """Phase 6: prove LOCAL fast-failover on a dist->core uplink.

    Continuous ping across a path that uses d1a->c1; drop that link mid-flight
    and confirm the FF group swings to the 2nd core (c2) with minimal loss and
    WITHOUT the controller recomputing a route.
    """
    m = topo.model
    src, dst = m.host_name(1, 1, 1), m.host_name(m.pods, 1, 1)
    fa, fb = "d1a", "c1"          # dist->primary-core link to fail

    info("*** Waiting for switches to connect\n")
    if not net.waitConnected(timeout=40):
        info("*** ERROR: not all switches connected\n")
        return False
    info("*** Settling (%ds)\n" % settle)
    time.sleep(settle)

    h1, h2 = net.get(src), net.get(dst)
    info("*** Warm-up ping to install protected forwarding (%s -> %s)\n" % (src, dst))
    h1.cmd("ping -c1 -W2 %s" % h2.IP())

    try:
        g = subprocess.check_output(
            ["ovs-ofctl", "-O", "OpenFlow13", "dump-groups", fa],
            stderr=subprocess.STDOUT).decode()
        info("*** OFPGT_FF groups installed on %s:\n%s\n" % (fa, g))
    except Exception as e:                         # noqa: BLE001
        info("*** dump-groups on %s failed: %s\n" % (fa, e))

    info("*** Ping %s -> %s : 80 pkts @ 10/s (~8s). Failing %s-%s mid-flight\n"
         % (src, dst, fa, fb))
    p = h1.popen("ping -i 0.1 -c 80 %s" % h2.IP())
    time.sleep(3)
    info("*** LINK DOWN %s-%s  (expect local FF -> 2nd core, minimal loss)\n" % (fa, fb))
    net.configLinkStatus(fa, fb, "down")
    time.sleep(3)
    info("*** LINK UP %s-%s\n" % (fa, fb))
    net.configLinkStatus(fa, fb, "up")

    out = p.communicate()[0].decode()
    mm = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    if mm:
        tx, rx = int(mm.group(1)), int(mm.group(2))
        lost = tx - rx
        info("*** FAILOVER SUMMARY: tx=%d rx=%d lost=%d  approx_outage=%.1fs "
             "(local FF; controller not in the reroute path)\n"
             % (tx, rx, lost, lost * 0.1))
        info("*** Interpretation: a few lost packets = link-down detection time; "
             "0%% would mean sub-detection swing. Compare with a plain-forwarding "
             "run (CAMPUS_FF=0) which needs a controller reroute.\n")
    else:
        info("*** could not parse ping result:\n%s\n" % out)
    return True


def reroute_test(net, topo, settle=8):
    """Phase 7: prove CONTROLLER rerouting for a failure FF cannot handle.

    Fail the destination's last-hop (dist->access) link.  Local FF has no backup
    there, so the controller must detect LINK_DOWN and reroute the whole flow to
    the other plane.  (The reverse direction fails over locally via FF.)
    """
    m = topo.model
    P, A = m.pods, m.access_per_pod
    src, dst = m.host_name(1, 1, 1), m.host_name(P, A, 1)
    dpa = m.dist_name(P, cm.PLANE_A)          # e.g. d6a
    dacc = m.access_name(P, A)                # e.g. a6_6

    info("*** Waiting for switches to connect\n")
    if not net.waitConnected(timeout=40):
        info("*** ERROR: not all switches connected\n")
        return False
    info("*** Settling (%ds)\n" % settle)
    time.sleep(settle)

    h1, h2 = net.get(src), net.get(dst)
    info("*** Warm-up ping to install protected forwarding (%s -> %s)\n" % (src, dst))
    h1.cmd("ping -c1 -W2 %s" % h2.IP())

    info("*** Ping %s -> %s : 120 pkts @ 10/s (~12s). Failing LAST-HOP %s-%s "
         "(FF has no local backup here -> controller reroute to plane B)\n"
         % (src, dst, dpa, dacc))
    p = h1.popen("ping -i 0.1 -c 120 %s" % h2.IP())
    time.sleep(4)
    info("*** LINK DOWN %s-%s\n" % (dpa, dacc))
    net.configLinkStatus(dpa, dacc, "down")
    time.sleep(5)
    info("*** LINK UP %s-%s\n" % (dpa, dacc))
    net.configLinkStatus(dpa, dacc, "up")
    time.sleep(2)

    out = p.communicate()[0].decode()
    mm = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    if mm:
        tx, rx = int(mm.group(1)), int(mm.group(2))
        lost = tx - rx
        info("*** REROUTE SUMMARY: tx=%d rx=%d lost=%d  approx_outage=%.1fs\n"
             % (tx, rx, lost, lost * 0.1))
        info("*** Expect a larger gap than the FF case (controller IS in the "
             "reroute path here): LINK_DOWN detection + path recompute + flow "
             "reinstall. Traffic resumes via plane B (d?b).\n")
    else:
        info("*** could not parse ping result:\n%s\n" % out)
    return True


def main():
    ap = argparse.ArgumentParser(description="Hybrid-campus Mininet topology")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--pods", type=int, default=6)
    ap.add_argument("--access-per-pod", type=int, default=6)
    ap.add_argument("--hosts-per-access", type=int, default=2)
    ap.add_argument("--cores", type=int, default=4)
    ap.add_argument("--test",
                    choices=["cli", "dump", "conn", "pingall", "discover",
                             "failover", "reroute", "experiments", "eval",
                             "scale", "proof", "demo", "congest", "none"],
                    default="cli",
                    help="cli=interactive; dump=build+print+stop; "
                         "conn=representative connectivity pings; "
                         "pingall=full 72-host pingall; "
                         "discover=LLDP discovery + link-down detection; "
                         "failover=local fast-failover demo (dist-core link); "
                         "none=build+stop")
    args = ap.parse_args()

    setLogLevel("info")
    net, topo = make_net(args)
    net.start()
    if os.environ.get("CAMPUS_BFD") == "1":
        enable_bfd(net, int(os.environ.get("CAMPUS_BFD_MS", "50")))
    if os.environ.get("CAMPUS_QOS_QUEUES") == "1":
        enable_qos_queues(net, float(os.environ.get("CAMPUS_BW", "100") or 100))
    if args.test not in ("dump", "none"):    # these run without a controller
        warmup_learn(net)
    try:
        if args.test == "dump":
            dump_wiring(net, topo)
        elif args.test == "conn":
            connectivity_test(net, topo, full=False)
        elif args.test == "pingall":
            connectivity_test(net, topo, full=True)
        elif args.test == "discover":
            discover_test(net, topo)
        elif args.test == "failover":
            failover_test(net, topo)
        elif args.test == "reroute":
            reroute_test(net, topo)
        elif args.test == "experiments":
            import experiments
            experiments.run_all(net, topo)
        elif args.test == "eval":
            if not net.waitConnected(timeout=40):
                info("*** ERROR: not all switches connected\n")
            else:
                time.sleep(8)
                import eval_suite
                eval_suite.run_all(net, topo)
        elif args.test == "demo":
            if net.waitConnected(timeout=40):
                time.sleep(8)
                net.get(topo.model.host_name(1, 1, 1)).cmd(
                    "ping -c1 -W2 %s"
                    % net.get(topo.model.host_name(topo.model.pods,
                                                   topo.model.access_per_pod, 1)).IP())
                import demo_scenario
                demo_scenario.run_demo(net, topo)
        elif args.test == "congest":
            if net.waitConnected(timeout=40):
                time.sleep(8)
                import congest_scenario
                congest_scenario.run_congest(net, topo)
        elif args.test == "proof":
            if net.waitConnected(timeout=40):
                time.sleep(8)
                import eval_suite
                eval_suite.run_proof(net, topo)
        elif args.test == "scale":
            if not net.waitConnected(timeout=40):
                info("*** ERROR: not all switches connected\n")
            else:
                time.sleep(8)
                import eval_suite
                eval_suite.run_scale(net, topo)
        elif args.test == "cli":
            info("*** Ryu must be running on %s:%d for connectivity ***\n"
                 % (args.controller_ip, args.controller_port))
            CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
