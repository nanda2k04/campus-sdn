"""Multi-flow failover benchmark.

Runs K concurrent host->host UDP flows across the fabric, fails each flow's
PRIMARY (plane-A) uplink partway through the transmission, and reports, per flow
and in aggregate: packets sent/received/lost, loss %, one-way latency (before vs
during the failure), and the outage gap (largest inter-arrival gap = the time
the flow was black-holed before the controller rerouted it to plane B).

Invoked by:  sudo python3 topology.py --test bench ...   (Ryu must be running)
or the runner:  sudo ./run_bench.sh <pods> <access> <hosts> <cores> [flags]

Tunables (environment):
  BENCH_FLOWS      number of concurrent flows            (default 8)
  BENCH_RATE       packets/sec per flow                  (default 200)
  BENCH_DUR        flow duration, seconds                (default 8.0)
  BENCH_FAIL_AT    when to fail the primary path, s      (default 3.0)
  BENCH_RESTORE_AT when to restore it, s (0 = never)     (default 0)
Writes results/failover_bench.json.
"""
import json
import os
import time

import campus_model as cm
from mininet.log import info


def _pctl(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def _access_plane_uplinks(model):
    """access switch name -> {'A': distA, 'B': distB}."""
    up = {}
    for lk in model.links:
        if lk["kind"] != cm.K_ACCESS_DIST:
            continue
        a, b = lk["a"], lk["b"]
        acc = a if model.switches[a]["tier"] == cm.ACCESS else b
        dist = b if acc == a else a
        up.setdefault(acc, {})[model.switches[dist]["plane"]] = dist
    return up


def _pick_flows(model, k):
    """K cross-pod (src host -> dst host) pairs that traverse the core."""
    P, A = model.pods, model.access_per_pod
    flows = []
    for i in range(k):
        spod = (i % P) + 1
        sacc = (i % A) + 1
        dpod = ((spod - 1 + P // 2) % P) + 1      # a far pod
        if dpod == spod:
            dpod = (dpod % P) + 1
        src = model.host_name(spod, sacc, 1)
        dst = model.host_name(dpod, 1, 1)
        flows.append(dict(src=src, dst=dst, port=5000 + i))
    return flows


def run_bench(net, topo):
    model = topo.model
    K = int(os.environ.get("BENCH_FLOWS", "8"))
    rate = float(os.environ.get("BENCH_RATE", "200"))
    dur = float(os.environ.get("BENCH_DUR", "8.0"))
    fail_at = float(os.environ.get("BENCH_FAIL_AT", "3.0"))
    restore_at = float(os.environ.get("BENCH_RESTORE_AT", "0") or 0)
    interval = 1.0 / rate
    count = int(dur / interval)

    uplinks = _access_plane_uplinks(model)
    flows = _pick_flows(model, K)

    info("\n*** failover benchmark: %d flows, %d pkt/s, %.1fs, fail primary @ %.1fs%s\n"
         % (K, rate, dur, fail_at, (", restore @ %.1fs" % restore_at) if restore_at else ""))

    # distinct source access switches whose plane-A uplink we will cut
    fail_links = []
    seen = set()
    for f in flows:
        acc = model.hosts[f["src"]]["access"]
        da = uplinks.get(acc, {}).get("A")
        if da and (acc, da) not in seen:
            seen.add((acc, da))
            fail_links.append((acc, da))

    # 1. start receivers, then senders
    recvs = []
    for f in flows:
        h = net.get(f["dst"])
        recvs.append(h.popen("python3 flow_probe.py recv %d %d %f"
                             % (f["port"], count, dur + 4.0)))
    time.sleep(0.4)
    t0 = time.time()
    for f in flows:
        hs = net.get(f["src"])
        dip = net.get(f["dst"]).IP()
        f["proc"] = hs.popen("python3 flow_probe.py send %s %d %d %f"
                             % (dip, f["port"], count, interval))

    # 2. fail the primary path partway through
    time.sleep(max(0.0, fail_at - (time.time() - t0)))
    fail_wall = time.time()
    for a, b in fail_links:
        net.configLinkStatus(a, b, "down")
    info("    primary down: cut %d plane-A uplink(s)\n" % len(fail_links))

    # 3. optional restore
    if restore_at and restore_at > fail_at:
        time.sleep(max(0.0, restore_at - (time.time() - t0)))
        for a, b in fail_links:
            net.configLinkStatus(a, b, "up")
        info("    primary restored\n")

    # 4. let the flows finish, then collect
    time.sleep(max(0.0, dur + 1.0 - (time.time() - t0)))
    agg = dict(sent=0, recv=0, lost=0, outages=[], lat_before=[], lat_during=[])
    rows = []
    for f, rp in zip(flows, recvs):
        out = ""
        try:
            out = rp.communicate(timeout=dur + 6)[0].decode("utf-8", "replace")
        except Exception:
            try:
                rp.kill()
            except Exception:
                pass
        arr = []
        for ln in out.splitlines():
            p = ln.split()
            if len(p) == 3:
                arr.append((int(p[0]), float(p[1]), float(p[2])))
        received = len(arr)
        lost = count - received
        rts = sorted(a[1] for a in arr)
        gaps = [(rts[i + 1] - rts[i]) for i in range(len(rts) - 1)]
        outage_ms = (max(gaps) * 1000.0) if gaps else (dur * 1000.0 if received == 0 else 0.0)
        lat_b = [(rt - st) * 1000.0 for _, rt, st in arr if rt < fail_wall]
        lat_d = [(rt - st) * 1000.0 for _, rt, st in arr if rt >= fail_wall]
        rows.append(dict(
            src=f["src"], dst=f["dst"], sent=count, recv=received, lost=lost,
            loss_pct=round(100.0 * lost / count, 2) if count else 0.0,
            outage_ms=round(outage_ms, 1),
            lat_before_ms=round(sum(lat_b) / len(lat_b), 3) if lat_b else None,
            lat_during_ms=round(sum(lat_d) / len(lat_d), 3) if lat_d else None,
            lat_p95_ms=round(_pctl(lat_b + lat_d, 95), 3) if arr else None))
        agg["sent"] += count
        agg["recv"] += received
        agg["lost"] += lost
        agg["outages"].append(outage_ms)
        agg["lat_before"] += lat_b
        agg["lat_during"] += lat_d

    # 5. report
    info("\n    %-10s %-10s %6s %6s %6s %7s %9s %9s %9s\n"
         % ("src", "dst", "sent", "recv", "lost", "loss%", "outage", "lat_b", "lat_d"))
    for r in rows:
        info("    %-10s %-10s %6d %6d %6d %6.1f %8.1f %9s %9s\n"
             % (r["src"], r["dst"], r["sent"], r["recv"], r["lost"], r["loss_pct"],
                r["outage_ms"],
                ("%.3f" % r["lat_before_ms"]) if r["lat_before_ms"] is not None else "-",
                ("%.3f" % r["lat_during_ms"]) if r["lat_during_ms"] is not None else "-"))

    overall_loss = round(100.0 * agg["lost"] / agg["sent"], 3) if agg["sent"] else 0.0
    summary = dict(
        flows=K, rate_pps=rate, duration_s=dur, fail_at_s=fail_at,
        restore_at_s=restore_at, primary_links_cut=len(fail_links),
        total_sent=agg["sent"], total_recv=agg["recv"], total_lost=agg["lost"],
        overall_loss_pct=overall_loss,
        worst_outage_ms=round(max(agg["outages"]), 1) if agg["outages"] else 0.0,
        mean_outage_ms=round(sum(agg["outages"]) / len(agg["outages"]), 1) if agg["outages"] else 0.0,
        latency_before_ms=round(sum(agg["lat_before"]) / len(agg["lat_before"]), 3) if agg["lat_before"] else None,
        latency_during_ms=round(sum(agg["lat_during"]) / len(agg["lat_during"]), 3) if agg["lat_during"] else None,
        flows_detail=rows)

    info("\n    SUMMARY: %d flows, %d/%d pkts delivered, overall loss %.3f%%, "
         "worst outage %.0f ms, mean outage %.0f ms\n"
         % (K, agg["recv"], agg["sent"], overall_loss,
            summary["worst_outage_ms"], summary["mean_outage_ms"]))
    if summary["latency_before_ms"] is not None and summary["latency_during_ms"] is not None:
        info("    one-way latency: %.3f ms before -> %.3f ms during failure\n"
             % (summary["latency_before_ms"], summary["latency_during_ms"]))

    os.makedirs("results", exist_ok=True)
    with open("results/failover_bench.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    info("    saved -> results/failover_bench.json\n")
    return summary
