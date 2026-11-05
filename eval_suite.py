"""Phase 11 -- statistical evaluation suite.

Runs N randomized trials of each failure type, with the failed component chosen
ON the primary path of a random src->dst pair (so every trial actually exercises
a failover), and aggregates per-metric statistics (n, mean, median, p95, std,
95%% CI).  Also samples controller CPU/RSS, counts installed flows, and saves raw
`dump-groups`/`dump-flows` proof snapshots.

Driven from Mininet (root):  sudo python3 topology.py --test eval
Trial count via env EVAL_TRIALS (default 15); EVAL_TRIALS=2 for a quick smoke.

Outputs (results/eval/):
  trials.jsonl         one JSON record per trial
  summary_stats.json   per-scenario aggregated statistics
  metrics.csv          flat per-trial table (for plotting)
  resources.csv        controller CPU%/RSS timeline
  flows.json           total installed flow count
  proofs/*.txt         raw OpenFlow group/flow dumps
"""
from __future__ import annotations

import json
import os
import random
import time

import numpy as np

import campus_model as cm
import experiments as ex
import metrics_sampler as ms

TRIALS = int(os.environ.get("EVAL_TRIALS", "15"))
OUT = "results/eval"
FAULT_WINDOW_MS = (ex.RESTORE_AT - ex.FAIL_AT) * 1000.0
RECOVERED_MS = FAULT_WINDOW_MS * 0.8     # outage below this => traffic resumed

# metrics we aggregate across trials
NUM_METRICS = ["outage_ms", "packet_loss", "loss_pct",
               "detection_ms", "reroute_ms", "affected_flows"]


def _agg(values):
    v = [x for x in values if x is not None]
    if not v:
        return dict(n=0)
    a = np.array(v, dtype=float)
    n = len(a)
    std = float(np.std(a, ddof=1)) if n > 1 else 0.0
    return dict(n=n,
                mean=round(float(np.mean(a)), 2),
                median=round(float(np.median(a)), 2),
                p95=round(float(np.percentile(a, 95)), 2),
                std=round(std, 2),
                ci95=round(1.96 * std / (n ** 0.5), 2) if n > 1 else 0.0,
                min=round(float(a.min()), 2),
                max=round(float(a.max()), 2))


class EvalSuite:
    def __init__(self, net, topo, log_path="ryu.log"):
        self.net = net
        self.m = topo.model
        v = self.m.dpid_view()
        self.adj, self.d2n = v["adj"], v["dpid2name"]
        self.n2d = v["name2dpid"]
        self.ex = ex.Experiments(net, topo, log_path, OUT)
        self.trials = []
        self.oneway = {}        # scenario -> [forward_outage_ms, ...]

    # ---- choose a random src/dst and the on-path failing component ----------
    def _pair(self):
        P, A = self.m.pods, self.m.access_per_pod
        sp, dp = random.sample(range(1, P + 1), 2)
        sa, da = random.randint(1, A), random.randint(1, A)
        src = self.m.host_name(sp, sa, 1)
        dst = self.m.host_name(dp, da, 1)
        return src, dst, sp, sa, dp, da

    # ---- D: capture datapath proof BEFORE vs DURING a real failure ----------
    def capture_proof(self):
        """With traffic flowing, fail the source access uplink and snapshot the
        OpenFlow group stats + port state before and during the failure.  The
        backup bucket's packet_count climbing (while the primary freezes) is
        datapath proof the fast-failover group actually carried traffic over."""
        pdir = os.path.join(OUT, "proofs")
        os.makedirs(pdir, exist_ok=True)
        P, A = self.m.pods, self.m.access_per_pod
        src = self.m.host_name(1, 1, 1)
        dst = self.m.host_name(P, A, 1)
        a_s = self.m.access_name(1, 1)
        d_s = self.m.dist_name(1, cm.PLANE_A)
        h1, h2 = self.net.get(src), self.net.get(dst)
        import re
        from mininet.log import info
        info("*** capturing fast-failover datapath proof on %s (dst %s)\n"
             % (a_s, dst))
        # the dest-rooted flow at a_s matches the dst's /40 MAC prefix -> a FF group
        dmac = self.m.hosts[dst]["mac"]
        prefix = dmac.rsplit(":", 1)[0] + ":00"
        p = h1.popen("ping -i 0.004 -c 1500 %s" % h2.IP())   # 250 pps background
        time.sleep(1.0)

        def group_for_dest():
            for ln in ms.dump_flows(a_s).splitlines():
                if prefix in ln:
                    mo = re.search(r"group:(\d+)", ln)
                    if mo:
                        return int(mo.group(1))
            return None

        def buckets(gid):
            for ln in ms.dump_group_stats(a_s).splitlines():
                if ("group_id=%d," % gid) in ln:
                    b = [int(x) for x in re.findall(r"bucket\d+:packet_count=(\d+)", ln)]
                    return b
            return []

        def snap(tag):
            for name, fn in (("group-stats", ms.dump_group_stats),
                             ("ports", ms.dump_ports), ("groups", ms.dump_groups)):
                with open(os.path.join(pdir, "%s_%s_%s.txt" % (a_s, name, tag)), "w") as fh:
                    fh.write(fn(a_s))

        gid = group_for_dest()
        b0 = buckets(gid) if gid else []
        snap("1_before")
        self.net.configLinkStatus(a_s, d_s, "down")        # fail primary uplink
        time.sleep(1.5)
        snap("2_during")
        b1 = buckets(gid) if gid else []
        self.net.configLinkStatus(a_s, d_s, "up")
        try:
            p.communicate(timeout=4)
        except Exception:
            p.kill()
        if gid and len(b0) >= 2 and len(b1) >= 2:
            dp = b1[0] - b0[0]       # primary bucket delta
            db = b1[1] - b0[1]       # backup bucket delta
            verdict = ("FF SWITCHED to backup" if db > dp else
                       "backup did NOT take over (controller-driven)")
            info("    group %d during-failure: primary bucket +%d pkts, "
                 "backup bucket +%d pkts  -> %s\n" % (gid, dp, db, verdict))
            self.proof_verdict = dict(group=gid, primary_delta=dp,
                                      backup_delta=db, verdict=verdict)
        else:
            info("    (could not resolve dest group for %s)\n" % a_s)
        info("    proof written to %s/%s_group-stats_{1_before,2_during}.txt\n"
             % (pdir, a_s))

    # ---- one-way (forward only) outage via UDP probe ------------------------
    def _measure_oneway(self, src, dst, edges, rate=0.002, dur=6.0,
                        fail=2.0, restore=4.0, port=5005):
        h1, h2 = self.net.get(src), self.net.get(dst)
        n = int(dur / rate)
        r = h2.popen("python3 udp_probe.py recv %d %d %f" % (port, n, dur + 3))
        time.sleep(0.4)
        s = h1.popen("python3 udp_probe.py send %s %d %d %f"
                     % (h2.IP(), port, n, rate))
        time.sleep(fail)
        for a, b in edges:
            self.net.configLinkStatus(a, b, "down")
        time.sleep(restore - fail)
        for a, b in edges:
            self.net.configLinkStatus(a, b, "up")
        s.wait()
        out = r.communicate()[0].decode()
        times = sorted(float(x) for x in out.split() if x)
        gaps = [t2 - t1 for t1, t2 in zip(times, times[1:])]
        return round((max(gaps) if gaps else dur) * 1000, 1)

    def _switch_edges(self, sw):
        d = self.n2d[sw]
        return [(sw, self.d2n[nbr]) for nbr in self.adj.get(d, {})]

    def _on_path(self, src, dst, edges):
        """True if removing `edges` changes the computed primary path (i.e. the
        fault is actually on the active route, not a no-op somewhere else)."""
        sa, da = ex._acc(src), ex._acc(dst)
        down = frozenset(frozenset((self.n2d[a], self.n2d[b])) for a, b in edges)
        before = self.ex._path(sa, da)
        after = self.ex._path(sa, da, down)
        return after is not None and before != after

    def _candidate(self, name):
        src, dst, sp, sa, dp, da = self._pair()
        c1 = self.m.core_name(1)
        d_s = self.m.dist_name(sp, cm.PLANE_A)
        d_d = self.m.dist_name(dp, cm.PLANE_A)
        a_s = self.m.access_name(sp, sa)
        a_d = self.m.access_name(dp, da)
        if name == "access_uplink_ff":
            return src, dst, "local_ff", [(a_s, d_s)], "%s-%s" % (a_s, d_s)
        if name == "dist_core_ff":
            return src, dst, "local_ff", [(d_s, c1)], "%s-%s" % (d_s, c1)
        if name == "core_dist_ff":
            return src, dst, "local_ff", [(c1, d_d)], "%s-%s" % (c1, d_d)
        if name == "last_hop_reroute":
            return src, dst, "controller_reroute", [(d_d, a_d)], "%s-%s" % (d_d, a_d)
        if name == "switch_isolation_reroute":
            return src, dst, "controller_reroute", self._switch_edges(d_d), "switch:%s" % d_d
        raise ValueError(name)

    def _plan(self, name):
        """Pick a randomized trial of `name` whose fault is on the active path
        (resample up to 12 times; fall back to the last candidate otherwise)."""
        cand = None
        for _ in range(12):
            cand = self._candidate(name)
            if self._on_path(cand[0], cand[1], cand[3]):
                return cand
        return cand

    # ------------------------------------------------------------- run --------
    def run(self):
        os.makedirs(OUT, exist_ok=True)
        from mininet.log import info

        sampler = ms.ResourceSampler(out_dir=OUT)
        sampler.start()

        # baseline (no fault), a few times
        for _ in range(min(3, TRIALS)):
            src, dst, *_ = self._pair()
            self.trials.append(self.ex.measure_baseline(src, dst))

        self.capture_proof()    # D: before/during datapath proof

        scenarios = ["access_uplink_ff", "dist_core_ff", "core_dist_ff",
                     "last_hop_reroute", "switch_isolation_reroute"]
        for name in scenarios:
            info("*** %s: %d trials\n" % (name, TRIALS))
            for i in range(TRIALS):
                src, dst, mech, edges, label = self._plan(name)
                rec = self.ex.measure(name, mech, src, dst, edges, label)
                rec["rerouted"] = rec["path_after"] != rec["path_before"]
                rec["recovered"] = rec["outage_ms"] < RECOVERED_MS
                rec["on_path"] = self._on_path(src, dst, edges)
                self.trials.append(rec)
                info("    [%2d/%d] %s loss=%d outage=%.0fms reroute=%s\n"
                     % (i + 1, TRIALS, label, rec["packet_loss"],
                        rec["outage_ms"], rec.get("reroute_ms")))

        # one-way (forward-only) pass for the FF scenarios: isolates the pure
        # datapath fast-failover, which the bidirectional ping masks behind the
        # reverse path's controller reroute.
        ow_trials = min(5, TRIALS)
        for name in ["access_uplink_ff", "dist_core_ff", "core_dist_ff"]:
            info("*** %s: %d one-way trials\n" % (name, ow_trials))
            vals = []
            for i in range(ow_trials):
                src, dst, _mech, edges, label = self._plan(name)
                fo = self._measure_oneway(src, dst, edges)
                vals.append(fo)
                info("    [%2d/%d] %s forward_outage=%.0fms\n"
                     % (i + 1, ow_trials, label, fo))
            self.oneway[name] = vals

        res_csv = sampler.stop()
        total_flows, per_flows, proofs = 0, {}, []
        try:
            total_flows, per_flows = ms.count_flows()
            # proof snapshots from a representative access + dist + core switch
            proofs = ms.save_proofs([self.m.access_name(1, 1),
                                     self.m.dist_name(1, cm.PLANE_A),
                                     self.m.core_name(1)], tag="steady")
        except Exception as e:
            print("  [warn] flow/proof capture failed: %s" % e)

        self._write(res_csv, sampler.summary(), total_flows, per_flows, proofs)

    # ---------------------------------------------------------- outputs -------
    def _write(self, res_csv, res_sum, total_flows, per_flows, proofs):
        with open(os.path.join(OUT, "trials.jsonl"), "w") as fh:
            for r in self.trials:
                fh.write(json.dumps(r) + "\n")

        # per-scenario aggregation
        stats = {}
        scen_names = []
        for r in self.trials:
            if r["scenario"] not in scen_names:
                scen_names.append(r["scenario"])
        for scen in scen_names:
            rows = [r for r in self.trials if r["scenario"] == scen]
            s = dict(mechanism=rows[0]["mechanism"], trials=len(rows))
            for metr in NUM_METRICS:
                s[metr] = _agg([r.get(metr) for r in rows])
            if scen != "baseline":
                rec = sum(1 for r in rows if r.get("recovered"))
                s["recovery_success_rate"] = round(100.0 * rec / len(rows), 1)
                s["rerouted_pct"] = round(
                    100.0 * sum(1 for r in rows if r.get("rerouted")) / len(rows), 1)
            stats[scen] = s

        oneway = {k: _agg(v) for k, v in self.oneway.items()}
        summary = dict(trials_per_scenario=TRIALS,
                       total_trials=len(self.trials),
                       controller=res_sum, flows_total=total_flows,
                       flows_per_bridge=per_flows,
                       resources_csv=res_csv, proofs=proofs,
                       oneway_forward_outage_ms=oneway,
                       scenarios=stats)
        with open(os.path.join(OUT, "summary_stats.json"), "w") as fh:
            json.dump(summary, fh, indent=2)
        with open(os.path.join(OUT, "flows.json"), "w") as fh:
            json.dump(dict(total=total_flows, per_bridge=per_flows), fh, indent=2)

        # flat CSV for plotting
        import csv
        cols = (["scenario", "mechanism", "src", "dst", "failed_component",
                 "on_path", "rerouted", "recovered"] + NUM_METRICS)
        with open(os.path.join(OUT, "metrics.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in self.trials:
                w.writerow([r.get(c) for c in cols])

        print("\n=== Phase 11 evaluation written to %s/ ===" % OUT)
        print("  trials: %d   flows installed: %d   controller: %s"
              % (len(self.trials), total_flows, res_sum))
        for scen, s in stats.items():
            if scen == "baseline":
                continue
            print("  %-26s outage med=%.0fms p95=%.0fms  reroute med=%s  "
                  "recovery=%s%%"
                  % (scen, s["outage_ms"].get("median", 0),
                     s["outage_ms"].get("p95", 0),
                     s["reroute_ms"].get("median"),
                     s.get("recovery_success_rate")))
        for k, a in oneway.items():
            print("  %-26s FORWARD one-way outage med=%.0fms (pure FF)"
                  % (k, a.get("median", 0)))


def run_all(net, topo, log_path="ryu.log"):
    EvalSuite(net, topo, log_path).run()


def run_proof(net, topo, log_path="ryu.log"):
    """Fast standalone: just the FF datapath-proof capture (warm up + one failure)."""
    es = EvalSuite(net, topo, log_path)
    P, A = es.m.pods, es.m.access_per_pod
    net.get(es.m.host_name(1, 1, 1)).cmd(
        "ping -c2 -W2 %s" % net.get(es.m.host_name(P, A, 1)).IP())
    es.capture_proof()


def run_scale(net, topo, log_path="ryu.log"):
    """F: one size-point of the scalability sweep -- measure reroute time, flow
    count and controller RSS/CPU at this topology size, append to results/scale/."""
    import json as _json
    from mininet.log import info
    es = EvalSuite(net, topo, log_path)
    nsw = len(es.m.switches)
    trials = int(os.environ.get("SCALE_TRIALS", "8"))
    sampler = ms.ResourceSampler(out_dir="results/scale")
    sampler.start()
    # warm up protected forwarding
    P, A = es.m.pods, es.m.access_per_pod
    net.get(es.m.host_name(1, 1, 1)).cmd(
        "ping -c1 -W2 %s" % net.get(es.m.host_name(P, A, 1)).IP())
    link_rr, switch_rr = [], []
    info("*** scale point: %d switches, %d reroute trials\n" % (nsw, trials))
    for name, bucket in (("last_hop_reroute", link_rr),
                         ("switch_isolation_reroute", switch_rr)):
        for _ in range(trials):
            src, dst, mech, edges, label = es._plan(name)
            rec = es.ex.measure(name, mech, src, dst, edges, label)
            if rec.get("reroute_ms") is not None:
                bucket.append(rec["reroute_ms"])
    sampler.stop()
    total_flows, _ = ms.count_flows()
    rs = sampler.summary()
    rec = dict(switches=nsw, pods=P, access_per_pod=A,
               link_reroute_ms=_agg(link_rr), switch_reroute_ms=_agg(switch_rr),
               flows_total=total_flows,
               cpu_avg=rs.get("cpu_avg"), rss_max_mb=rs.get("rss_max_mb"))
    os.makedirs("results/scale", exist_ok=True)
    with open("results/scale/%02dsw.json" % nsw, "w") as fh:
        _json.dump(rec, fh, indent=2)
    info("*** scale %d sw: flows=%d link_reroute_med=%s switch_reroute_med=%s "
         "rss_max=%s\n" % (nsw, total_flows,
                           rec["link_reroute_ms"].get("median"),
                           rec["switch_reroute_ms"].get("median"),
                           rec["rss_max_mb"]))
