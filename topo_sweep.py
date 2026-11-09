#!/usr/bin/env python3
"""Controlled one-variable-at-a-time sweeps of the dual-plane fabric.

Where topo_kpi.py / topo_validate.py walk a *mixed* ladder (P,A,H,C all change
together), this holds a baseline fixed and varies ONE knob at a time so we can
answer "for this architecture at this size, is the chosen config the optimum?"

Sweeps:
  * cores  C in {4,6,8,10}         (planes stay 2; this is cores-per-plane)
  * access A in {2,3,4,6,8}        (access switches per pod)
  * pods   P in {4,6,8,12}         (building count)
For each point: switches, hosts, max radix (+OK), bandwidth oversub (+OK),
bisection, edge-connectivity, avg node-disjoint paths, single/double-failure
survival, avg hops, diameter, weighted cost, and cost-per-access.

A config is "feasible" if radix<=48 AND edge_conn==2 (survives any single
failure). Among feasible points the optimum minimises cost-per-access while
keeping oversub healthy.  Run:  PYTHONPATH=. python3 topo_sweep.py
"""
import json
import os

import topo_kpi as K
import topo_validate as V

BASE = dict(P=6, A=4, H=24, C=4)   # the demo/reference campus
SWEEPS = {
    "cores":  [("C", c) for c in (4, 6, 8, 10)],
    "access": [("A", a) for a in (2, 3, 4, 6, 8)],
    "pods":   [("P", p) for p in (4, 6, 8, 12)],
}
HDR = ("var", "P", "A", "H", "C", "sw", "hosts", "radix", "rOK",
       "osBW", "osOK", "bisec", "eConn", "disjP", "srv1", "srv2",
       "hops", "diam", "cost", "c/acc")


def row(P, A, H, C):
    k = K.kpi(P, A, H, C)
    v = V.validate(P, A, H, C)
    feasible = k["radix_ok"] and v["edge_conn"] == 2
    return dict(
        P=P, A=A, H=H, C=C, switches=k["switches"], hosts=k["hosts"],
        max_radix=k["max_radix"], radix_ok=k["radix_ok"],
        oversub_bw=k["oversub_bw"], oversub_ok=k["oversub_ok"],
        bisection=k["bisection_links"], edge_conn=v["edge_conn"],
        disjoint=v["avg_disjoint"], survive_1=v["survive_1"],
        survive_2=v["survive_2"], avg_hops=v["avg_hops"],
        diameter=v["diameter"], cost=v["cost"],
        cost_per_access=v["cost_per_access"], feasible=feasible)


def pr(r, tag):
    print("%-7s %2d %2d %2d %2d %4d %6d %5d %-3s %5.2f %-4s %5d %5d %5.2f "
          "%5.1f %5.1f %5.2f %4d %5d %6.2f"
          % (tag, r["P"], r["A"], r["H"], r["C"], r["switches"], r["hosts"],
             r["max_radix"], "OK" if r["radix_ok"] else "NO",
             r["oversub_bw"], "OK" if r["oversub_ok"] else "HI",
             r["bisection"], r["edge_conn"], r["disjoint"],
             r["survive_1"], r["survive_2"], r["avg_hops"],
             r["diameter"], r["cost"], r["cost_per_access"]))


def main():
    print("BASELINE: P=%d A=%d H=%d C=%d\n" % (BASE["P"], BASE["A"], BASE["H"], BASE["C"]))
    print(("%-7s %2s %2s %2s %2s %4s %6s %5s %-3s %5s %-4s %5s %5s %5s "
           "%5s %5s %5s %4s %5s %6s") % HDR)
    print("-" * 118)
    results = {}
    for name, points in SWEEPS.items():
        rows = []
        for knob, val in points:
            cfg = dict(BASE)
            cfg[knob] = val
            r = row(cfg["P"], cfg["A"], cfg["H"], cfg["C"])
            rows.append(r)
            pr(r, "%s=%s" % (knob, val))
        results[name] = rows
        print()

    # optimum search over the full grid actually swept (feasible only)
    allrows = [r for rs in results.values() for r in rs if r["feasible"]]
    # dedup by (P,A,H,C)
    uniq = {(r["P"], r["A"], r["H"], r["C"]): r for r in allrows}.values()
    opt = min(uniq, key=lambda r: (r["cost_per_access"], r["oversub_bw"]))
    print("Feasibility rule: radix<=48 AND edge_conn==2")
    print("Most cost-efficient FEASIBLE config swept: "
          "P=%d A=%d H=%d C=%d -> cost/access=%.2f oversubBW=%.2f radix=%d"
          % (opt["P"], opt["A"], opt["H"], opt["C"], opt["cost_per_access"],
             opt["oversub_bw"], opt["max_radix"]))
    base_r = row(BASE["P"], BASE["A"], BASE["H"], BASE["C"])
    print("Baseline (P=6 A=4 H=24 C=4): cost/access=%.2f oversubBW=%.2f radix=%d feasible=%s"
          % (base_r["cost_per_access"], base_r["oversub_bw"],
             base_r["max_radix"], base_r["feasible"]))

    os.makedirs("results", exist_ok=True)
    with open("results/topo_sweep.json", "w") as fh:
        json.dump({"baseline": BASE, "sweeps": results}, fh, indent=2)
    print("saved -> results/topo_sweep.json")


if __name__ == "__main__":
    main()
