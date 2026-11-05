"""Phase 8 -- repeatable failure experiments with real measurements.

Driven from Mininet (root) via ``topology.py --test experiments``.  For each
scenario it: measures a baseline, starts a high-rate ping, injects a real link
failure, restores it, then computes -- from the RECEIVER-side ping stream and
the controller's own event log -- structured metrics and writes one JSON file
per scenario into ``results/``.

Metrics per scenario (Phase 1 sections 15/16):
  path_before / path_after      (deterministic, from paths.py)
  failed_component
  baseline_rtt_ms               (min/avg/max)
  packets_tx / packets_rx / packet_loss / loss_pct
  outage_ms                     (largest run of consecutive lost packets)
  detection_ms                  (LINK_DOWN - injection)      [controller cases]
  reroute_ms                    (FLOW_REINSTALLED - LINK_DOWN)
  affected_flows                (switches whose forwarding changed)
  recovery_outage_ms            (loss burst when the link is restored)
"""

from __future__ import annotations

import json
import os
import re
import time

import campus_model as cm
import paths

RATE = 0.02          # 50 packets/s  -> 20 ms outage resolution
DURATION = 8.0       # seconds of pinging per scenario
FAIL_AT = 3.0        # inject failure at t+3s
RESTORE_AT = 5.5     # restore at t+5.5s


def _parse_ping(out):
    seqs = [int(x) for x in re.findall(r"icmp_seq=(\d+)", out)]
    m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    tx, rx = (int(m.group(1)), int(m.group(2))) if m else (0, len(seqs))
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", out)
    rtt_ms = (dict(min=float(rtt.group(1)), avg=float(rtt.group(2)),
                   max=float(rtt.group(3))) if rtt else None)
    return set(seqs), tx, rx, rtt_ms


def _largest_gap(received, tx):
    """Largest run of consecutive missing seqs (in packets)."""
    if tx == 0:
        return 0
    best = run = 0
    for s in range(1, tx + 1):
        if s in received:
            run = 0
        else:
            run += 1
            best = max(best, run)
    return best


def _read_events(log_path, t0, window=8.0):
    """EVENT json records with ts in [t0-0.5, t0+window]."""
    evs = []
    try:
        with open(log_path) as fh:
            for line in fh:
                i = line.find("EVENT ")
                if i < 0:
                    continue
                try:
                    rec = json.loads(line[i + 6:])
                except ValueError:
                    continue
                ts = rec.get("ts")
                if ts is not None and (t0 - 0.5) <= ts <= (t0 + window):
                    evs.append(rec)
    except OSError:
        pass
    return sorted(evs, key=lambda r: r.get("ts", 0))


class Experiments:
    def __init__(self, net, topo, log_path="ryu.log", out_dir="results"):
        self.net = net
        self.m = topo.model
        v = self.m.dpid_view()
        self.n2d, self.d2n = v["name2dpid"], v["dpid2name"]
        self.pc = paths.PathComputer(v["adj"])
        self.log_path = log_path
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.summary = []

    # --------------------------------------------------------------- helpers
    def _path(self, s_acc, d_acc, down_edges=frozenset()):
        p = self.pc.shortest(self.n2d[s_acc], self.n2d[d_acc], down_edges)
        return [self.d2n[x] for x in p] if p else None

    def _ping_with_faults(self, src, dst, edges):
        """Run the ping while dropping every (a,b) in ``edges`` mid-flight.

        ``edges`` is a list of switch-name pairs; a single-link fault passes one
        pair, a switch-isolation fault passes all of that switch's links.
        """
        h1, h2 = self.net.get(src), self.net.get(dst)
        n = int(DURATION / RATE)
        p = h1.popen("ping -i %s -c %d %s" % (RATE, n, h2.IP()))
        time.sleep(FAIL_AT)
        t_fail = time.time()
        for a, b in edges:
            self.net.configLinkStatus(a, b, "down")
        time.sleep(RESTORE_AT - FAIL_AT)
        t_restore = time.time()
        for a, b in edges:
            self.net.configLinkStatus(a, b, "up")
        out = p.communicate()[0].decode()
        return out, t_fail, t_restore

    def _ping_with_fault(self, src, dst, a, b):   # back-compat single-link wrapper
        return self._ping_with_faults(src, dst, [(a, b)])

    # --------------------------------------------------------------- runner
    def measure_baseline(self, src, dst):
        h1, h2 = self.net.get(src), self.net.get(dst)
        out = h1.cmd("ping -i %s -c %d %s" % (RATE, int(DURATION / RATE), h2.IP()))
        received, tx, rx, rtt = _parse_ping(out)
        return dict(scenario="baseline", mechanism="none", src=src, dst=dst,
                    failed_component=None,
                    path_before=self._path(_acc(src), _acc(dst)),
                    path_after=self._path(_acc(src), _acc(dst)),
                    baseline_rtt_ms=rtt, packets_tx=tx, packets_rx=rx,
                    packet_loss=tx - rx,
                    loss_pct=round(100.0 * (tx - rx) / tx, 3) if tx else 0,
                    outage_ms=0.0)

    def baseline(self, src, dst):
        self._emit(self.measure_baseline(src, dst))

    def measure(self, name, mechanism, src, dst, edges, failed_label):
        """Run one trial of a fault (list of edges) and RETURN the record."""
        out, t_fail, t_restore = self._ping_with_faults(src, dst, edges)
        received, tx, rx, rtt = _parse_ping(out)
        gap = _largest_gap(received, tx)
        down_edges = frozenset(frozenset((self.n2d[a], self.n2d[b]))
                               for a, b in edges)
        evs = _read_events(self.log_path, t_fail)

        det = rr = None
        affected = 0
        ld = next((e for e in evs if e["event"] == "LINK_DOWN"
                   and e.get("ts", 0) >= t_fail - 0.05), None)
        if ld:
            det = round((ld["ts"] - t_fail) * 1000, 1)
            fr = [e for e in evs if e["event"] == "FLOW_REINSTALLED"
                  and e["ts"] >= ld["ts"] and e.get("reason") == "LINK_DOWN"]
            if fr:
                rr = round((fr[-1]["ts"] - ld["ts"]) * 1000, 1)
                affected = sum(e.get("updated", 0) for e in fr)

        return dict(
            scenario=name, mechanism=mechanism, src=src, dst=dst,
            failed_component=failed_label,
            path_before=self._path(_acc(src), _acc(dst)),
            path_after=self._path(_acc(src), _acc(dst), down_edges),
            baseline_rtt_ms=rtt,
            packets_tx=tx, packets_rx=rx, packet_loss=tx - rx,
            loss_pct=round(100.0 * (tx - rx) / tx, 3) if tx else 0,
            outage_ms=round(gap * RATE * 1000, 1),
            detection_ms=det, reroute_ms=rr, affected_flows=affected,
        )

    def scenario(self, name, mechanism, src, dst, a, b):
        rec = self.measure(name, mechanism, src, dst, [(a, b)], "%s-%s" % (a, b))
        self._emit(rec)
        return rec

    def _emit(self, rec):
        path = os.path.join(self.out_dir, rec["scenario"] + ".json")
        with open(path, "w") as fh:
            json.dump(rec, fh, indent=2)
        self.summary.append(rec)
        print("  [%-18s] mech=%-10s loss=%d/%d outage=%.0fms det=%s reroute=%s -> %s"
              % (rec["scenario"], rec["mechanism"], rec["packet_loss"],
                 rec["packets_tx"], rec["outage_ms"],
                 rec.get("detection_ms"), rec.get("reroute_ms"), path))

    def write_summary(self):
        path = os.path.join(self.out_dir, "summary.json")
        with open(path, "w") as fh:
            json.dump(self.summary, fh, indent=2)
        print("  summary -> %s" % path)


def _acc(host):
    """host 'h<pod>_<acc>_<n>' -> access switch name 'a<pod>_<acc>'."""
    parts = host[1:].split("_")        # 'h1_2_1' -> ['1', '2', '1']
    return "a%s_%s" % (parts[0], parts[1])


def run_all(net, topo, log_path="ryu.log", out_dir="results"):
    from mininet.log import info
    ex = Experiments(net, topo, log_path, out_dir)
    m = topo.model
    P, A = m.pods, m.access_per_pod
    src, dst = m.host_name(1, 1, 1), m.host_name(P, A, 1)

    info("*** Waiting for switches / settling\n")
    if not net.waitConnected(timeout=40):
        info("*** ERROR: not all switches connected\n")
        return
    time.sleep(8)
    # warm up so protected forwarding + groups are installed before measuring
    net.get(src).cmd("ping -c1 -W2 %s" % net.get(dst).IP())

    info("*** Running experiments (src=%s dst=%s)\n" % (src, dst))
    ex.baseline(src, dst)
    ex.scenario("access_uplink_ff", "local_ff", src, dst,
                "a1_1", m.dist_name(1, cm.PLANE_A))         # a1_1 - d1a
    ex.scenario("dist_core_ff", "local_ff", src, dst,
                m.dist_name(1, cm.PLANE_A), m.core_name(1))  # d1a - c1
    ex.scenario("core_link_ff", "local_ff", src, dst,
                m.core_name(1), m.dist_name(P, cm.PLANE_A))  # c1 - d{P}a
    ex.scenario("last_hop_reroute", "controller_reroute", src, dst,
                m.dist_name(P, cm.PLANE_A), m.access_name(P, A))  # d{P}a - a{P}_A
    ex.write_summary()
    info("*** Experiments done -- see results/*.json\n")
