#!/usr/bin/env python3
"""A-C re-validation of the dual-plane fabric across the scaling ladder.

Where topo_study.py scores DIFFERENT topology families at one fixed size, this
validates OUR hybrid dual-plane design at every scale on the ladder, on the real
campus_model graph, so we can show the resilience/performance invariants hold as
P/A/H/C grow. Metrics:
  A Resilience : edge connectivity, avg node-disjoint paths, 1/2/3-link survival
  B Performance: avg hops (latency proxy), diameter  (over access pairs)
  C Cost       : weighted cabling cost, cost/access, resilience-per-cost

Run:  PYTHONPATH=. python3 topo_validate.py
      PYTHONPATH=. python3 topo_validate.py P A H C
"""
import itertools
import json
import os
import random
import sys

import networkx as nx

import campus_model as cm
from topo_kpi import fabric_graph, LADDER

random.seed(7)

# cabling-cost rank by the higher-tier endpoint (matches topo_study.py:
# access-level link = 1, distribution fiber = 2, core fiber = 3)
RANK = {cm.CORE: 3, cm.DIST: 2, cm.DC: 2, cm.ACCESS: 1}
SAMPLE_PAIRS = 150     # access pairs sampled for hops/survival on large graphs
DISJOINT_PAIRS = 60    # pairs for the (costlier) node-disjoint-path count
MC_TRIALS = 200        # Monte-Carlo samples per k


def _access(model):
    return sorted(n for n, a in model.switches.items() if a["tier"] == cm.ACCESS)


def validate(P, A, H, C):
    model = cm.build(pods=P, access_per_pod=A, hosts_per_access=H, cores=C)
    g = fabric_graph(model)
    acc = _access(model)

    # C -- cost
    cost = sum(max(RANK.get(g.nodes[u]["tier"], 1), RANK.get(g.nodes[v]["tier"], 1))
               for u, v in g.edges())

    # sample access pairs (exhaustive if small)
    pairs = list(itertools.combinations(acc, 2))
    sample = pairs if len(pairs) <= SAMPLE_PAIRS else random.sample(pairs, SAMPLE_PAIRS)

    # B -- performance (hops + diameter over access pairs)
    hops = [nx.shortest_path_length(g, a, b) for a, b in sample]
    avg_hops = sum(hops) / len(hops)
    diameter = max(hops)

    # A -- resilience
    edge_conn = nx.edge_connectivity(g)
    dsample = sample if len(sample) <= DISJOINT_PAIRS else random.sample(sample, DISJOINT_PAIRS)
    disj = [len(list(nx.node_disjoint_paths(g, a, b))) for a, b in dsample]
    avg_disjoint = sum(disj) / len(disj)

    edges = list(g.edges())
    survive = {}
    for k in (1, 2, 3):
        ok = tot = 0
        for _ in range(MC_TRIALS):
            h = g.copy()
            h.remove_edges_from(random.sample(edges, min(k, len(edges))))
            for a, b in random.sample(sample, min(40, len(sample))):
                tot += 1
                if nx.has_path(h, a, b):
                    ok += 1
        survive[k] = round(100.0 * ok / max(1, tot), 1)

    return dict(
        P=P, A=A, H=H, C=C, switches=len(model.switches), access=len(acc),
        edge_conn=edge_conn, avg_disjoint=round(avg_disjoint, 2),
        survive_1=survive[1], survive_2=survive[2], survive_3=survive[3],
        avg_hops=round(avg_hops, 2), diameter=diameter,
        cost=cost, cost_per_access=round(cost / len(acc), 2),
        resil_per_cost=round(avg_disjoint / cost * 100, 3))


def main():
    if len(sys.argv) == 5:
        P, A, H, C = map(int, sys.argv[1:5])
        rows = [("custom", P, A, H, C)]
    else:
        rows = LADDER

    out = []
    print("%-18s %4s %6s | %5s %6s %5s %5s %5s | %5s %4s | %5s %7s %7s"
          % ("label", "sw", "access", "eConn", "disjP", "srv1", "srv2", "srv3",
             "hops", "diam", "cost", "c/acc", "resil/$"))
    print("-" * 104)
    for label, P, A, H, C in rows:
        v = validate(P, A, H, C)
        v["label"] = label
        out.append(v)
        print("%-18s %4d %6d | %5d %6.2f %5.1f %5.1f %5.1f | %5.2f %4d | %5d %7.2f %7.3f"
              % (label, v["switches"], v["access"], v["edge_conn"], v["avg_disjoint"],
                 v["survive_1"], v["survive_2"], v["survive_3"],
                 v["avg_hops"], v["diameter"], v["cost"], v["cost_per_access"],
                 v["resil_per_cost"]))
    print("\nInvariants to check: eConn=2, disjP~2, srv1=100, srv2>=99, diam<=4 at EVERY scale.")
    os.makedirs("results", exist_ok=True)
    with open("results/topo_validate.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("saved -> results/topo_validate.json")


if __name__ == "__main__":
    main()
