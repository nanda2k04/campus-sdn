"""Phase 11 -- traditional distributed baseline: RSTP on the SAME topology.

Builds the identical campus topology but with NO SDN controller: every OVS bridge
runs in `standalone` fail-mode (normal L2 learning) with RSTP enabled to break the
redundant loops.  We then fail the access switch's currently-forwarding uplink and
measure how long RSTP takes to re-converge (unblock the alternate port) so traffic
resumes -- the apples-to-apples contrast to the SDN fast-failover numbers.

Driven standalone (root):
    sudo python3 baseline_rstp.py            # SMOKE (12 sw)   -- recommended first
    sudo EVAL_TRIALS=15 python3 baseline_rstp.py full

Writes results/baseline_rstp/{trials.jsonl,summary_stats.json,metrics.csv} in the
same shape make_report.py consumes.

NOTE: RSTP reconvergence is seconds-scale, so the fault window is long (link held
down ~18 s) to let traffic actually resume before we restore it.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time

from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.log import setLogLevel, info

import campus_model as cm
import experiments as ex
import eval_suite as ev
from topology import CampusTopo

OUT = "results/baseline_rstp"
RATE = 0.02                 # 50 pps
DURATION = 24.0
FAIL_AT = 4.0
RESTORE_AT = 22.0           # hold the link down 18 s -- RSTP needs seconds
TRIALS = int(os.environ.get("EVAL_TRIALS", "2"))
RECOVERED_MS = (RESTORE_AT - FAIL_AT) * 1000 * 0.9


def enable_rstp(net):
    info("*** enabling RSTP + standalone fail-mode on %d bridges\n"
         % len(net.switches))
    for sw in net.switches:
        sw.cmd("ovs-vsctl set-fail-mode %s standalone" % sw)
        sw.cmd("ovs-vsctl set bridge %s stp_enable=false" % sw)
        sw.cmd("ovs-vsctl set bridge %s rstp_enable=true" % sw)


def active_uplink(net, a_name, d_a, d_b):
    """Return (neighbor, port) for the access switch's FORWARDING uplink.

    Uplinks are port 1 (-> plane A dist) and port 2 (-> plane B dist); RSTP keeps
    exactly one forwarding (root port) and blocks the other (alternate).  We read
    the per-interface rstp_status directly -- more robust than parsing rstp/show.
    """
    sw = net.get(a_name)
    for port, nbr in ((1, d_a), (2, d_b)):
        st = sw.cmd("ovs-vsctl get interface %s-eth%d rstp_status" % (a_name, port))
        if "forwarding" in st.lower():
            return nbr, port
    return d_a, 1          # fallback


class RstpSuite:
    def __init__(self, net, model):
        self.net = net
        self.m = model
        v = model.dpid_view()
        self.n2d, self.d2n = v["name2dpid"], v["dpid2name"]
        self.trials = []

    def _one(self, src, dst, a_s, nbr):
        h1, h2 = self.net.get(src), self.net.get(dst)
        n = int(DURATION / RATE)
        p = h1.popen("ping -i %s -c %d %s" % (RATE, n, h2.IP()))
        time.sleep(FAIL_AT)
        self.net.configLinkStatus(a_s, nbr, "down")
        time.sleep(RESTORE_AT - FAIL_AT)
        self.net.configLinkStatus(a_s, nbr, "up")
        out = p.communicate()[0].decode()
        received, tx, rx, rtt = ex._parse_ping(out)
        gap = ex._largest_gap(received, tx)
        outage = round(gap * RATE * 1000, 1)
        return dict(scenario="access_uplink_rstp", mechanism="rstp",
                    src=src, dst=dst, failed_component="%s-%s" % (a_s, nbr),
                    baseline_rtt_ms=rtt, packets_tx=tx, packets_rx=rx,
                    packet_loss=tx - rx,
                    loss_pct=round(100.0 * (tx - rx) / tx, 3) if tx else 0,
                    outage_ms=outage,
                    detection_ms=None, reroute_ms=None, affected_flows=None,
                    recovered=outage < RECOVERED_MS)

    def run(self):
        os.makedirs(OUT, exist_ok=True)
        P, A = self.m.pods, self.m.access_per_pod
        info("*** RSTP initial convergence settle (35 s)\n")
        time.sleep(35)
        # sanity: one ping pair must work before measuring
        s0 = self.net.get(self.m.host_name(1, 1, 1))
        d0 = self.net.get(self.m.host_name(P, A, 1))
        chk = s0.cmd("ping -c2 -W2 %s" % d0.IP())
        if "0 received" in chk or ", 0 received" in chk:
            info("*** WARNING: baseline connectivity failed (RSTP not converged / "
                 "loop). Aborting -- try smoke scale.\n")
            return
        info("*** access_uplink_rstp: %d trials\n" % TRIALS)
        for i in range(TRIALS):
            sp, dp = random.sample(range(1, P + 1), 2)
            sa, da = random.randint(1, A), random.randint(1, A)
            src = self.m.host_name(sp, sa, 1)
            dst = self.m.host_name(dp, da, 1)
            a_s = self.m.access_name(sp, sa)
            d_a = self.m.dist_name(sp, cm.PLANE_A)
            d_b = self.m.dist_name(sp, cm.PLANE_B)
            nbr, _port = active_uplink(self.net, a_s, d_a, d_b)
            rec = self._one(src, dst, a_s, nbr)
            if rec["packet_loss"] == 0:        # missed the forwarding port -> retry other
                other = d_b if nbr == d_a else d_a
                info("    (0-loss: failed a blocked port; retrying %s-%s)\n"
                     % (a_s, other))
                time.sleep(6)
                rec = self._one(src, dst, a_s, other)
            self.trials.append(rec)
            info("    [%2d/%d] %s-%s loss=%d outage=%.0fms\n"
                 % (i + 1, TRIALS, a_s, nbr, rec["packet_loss"], rec["outage_ms"]))
            time.sleep(8)      # let RSTP re-stabilise before next trial
        self._write()

    def _write(self):
        with open(os.path.join(OUT, "trials.jsonl"), "w") as fh:
            for r in self.trials:
                fh.write(json.dumps(r) + "\n")
        s = dict(mechanism="rstp", trials=len(self.trials))
        for metr in ev.NUM_METRICS:
            s[metr] = ev._agg([r.get(metr) for r in self.trials])
        rec = sum(1 for r in self.trials if r.get("recovered"))
        s["recovery_success_rate"] = round(
            100.0 * rec / len(self.trials), 1) if self.trials else 0
        summary = dict(trials_per_scenario=TRIALS, total_trials=len(self.trials),
                       scenarios={"access_uplink_rstp": s})
        with open(os.path.join(OUT, "summary_stats.json"), "w") as fh:
            json.dump(summary, fh, indent=2)
        import csv
        cols = (["scenario", "mechanism", "src", "dst", "failed_component",
                 "recovered"] + ev.NUM_METRICS)
        with open(os.path.join(OUT, "metrics.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in self.trials:
                w.writerow([r.get(c) for c in cols])
        print("\n=== RSTP baseline written to %s/ ===" % OUT)
        print("  access_uplink_rstp outage med=%.0fms p95=%.0fms recovery=%s%%"
              % (s["outage_ms"].get("median", 0), s["outage_ms"].get("p95", 0),
                 s.get("recovery_success_rate")))


def main():
    scale = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if scale == "full":
        P, A, H, C = 6, 6, 2, 4
    else:
        P, A, H, C = 2, 2, 1, 4
    setLogLevel("info")
    topo = CampusTopo(pods=P, access_per_pod=A, hosts_per_access=H, cores=C)
    net = Mininet(topo=topo, switch=OVSSwitch, controller=None,
                  autoSetMacs=False, build=False)
    net.build()
    net.start()
    enable_rstp(net)
    try:
        RstpSuite(net, topo.model).run()
        if os.environ.get("SUDO_USER"):
            os.system("chown -R %s results" % os.environ["SUDO_USER"])
    finally:
        net.stop()


if __name__ == "__main__":
    main()
