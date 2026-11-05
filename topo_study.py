#!/usr/bin/env python3
"""Topology comparison study -- answers "why this fabric and not a mesh / Clos?".

We hold the WORKLOAD fixed (36 access/edge switches, 72 hosts, 6 pods x 6 access,
2 hosts/access) and vary ONLY how the fabric interconnects, then score each
candidate family on the axes a reviewer cares about:

  cost        switches, links, and a tiered cabling-cost proxy
              (edge/copper = 1, aggregation/fiber = 2, core/long-haul = 3)
  latency     average and worst-case hop distance between access switches
              (host-to-host = this + 2 host links)
  resilience  average node-disjoint paths between access pairs, the switch
              graph's edge-connectivity (min link cut), and Monte-Carlo
              connectivity survival after k random link failures (k=1,2,3)
  efficiency  derived trade-offs: cost per access switch, and
              resilience-per-cost (disjoint paths gained per unit cabling cost)

All metrics are computed on the real graph with networkx, so the numbers are
reproducible.  Run:  python3 topo_study.py   (writes results/ + a chart)
"""
from __future__ import annotations

import itertools
import json
import os
import random

import networkx as nx

random.seed(7)

PODS, ACCESS_PER_POD, HOSTS_PER_ACCESS = 6, 6, 2
N_ACCESS = PODS * ACCESS_PER_POD          # 36
N_HOSTS = N_ACCESS * HOSTS_PER_ACCESS     # 72

# node tier ranks for the cost model
HOST, ACCESS, AGG, CORE = 0, 1, 2, 3


def _cost(g, u, v):
    """Tiered cabling cost of a link by its higher-tier endpoint."""
    r = max(g.nodes[u]["tier"], g.nodes[v]["tier"])
    if r <= ACCESS:
        return 1          # copper / in-rack
    if r == AGG:
        return 2          # intra-building fiber
    return 3              # core / long-haul fiber


def _add(g, name, tier, pod=None):
    g.add_node(name, tier=tier, pod=pod)
    return name


def _hosts_on(g, access_names):
    """Attach the fixed host workload identically to every candidate."""
    for a in access_names:
        for h in range(HOSTS_PER_ACCESS):
            hn = "h_%s_%d" % (a, h)
            _add(g, hn, HOST)
            g.add_edge(hn, a)


def _access_grid():
    """(pod -> [access names]) and a flat list, shared by all builders."""
    by_pod = {}
    flat = []
    for p in range(PODS):
        names = ["a%d_%d" % (p, k) for k in range(ACCESS_PER_POD)]
        by_pod[p] = names
        flat += names
    return by_pod, flat


# --------------------------------------------------------------------------- #
# candidate topology builders (all serve the SAME 36 access / 72 hosts)
# --------------------------------------------------------------------------- #
def build_hybrid():
    """A: current design -- dual-plane, access dual-homed, core full mesh."""
    g = nx.Graph(); by_pod, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    cores = [_add(g, "c%d" % i, CORE) for i in range(4)]
    plane_a, plane_b = cores[:2], cores[2:]
    for i in range(len(cores)):
        for j in range(i + 1, len(cores)):
            g.add_edge(cores[i], cores[j])                 # core full mesh
    for p in range(PODS):
        dA = _add(g, "d%dA" % p, AGG, pod=p)
        dB = _add(g, "d%dB" % p, AGG, pod=p)
        for c in plane_a:
            g.add_edge(dA, c)
        for c in plane_b:
            g.add_edge(dB, c)
        for a in by_pod[p]:
            g.add_edge(a, dA); g.add_edge(a, dB)           # dual-homed
    _hosts_on(g, flat)
    return g


def build_single_homed():
    """B: classic 3-tier -- 1 dist/pod, access single-homed, 2-core mesh."""
    g = nx.Graph(); by_pod, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    cores = [_add(g, "c%d" % i, CORE) for i in range(2)]
    g.add_edge(*cores)
    for p in range(PODS):
        d = _add(g, "d%d" % p, AGG, pod=p)
        for c in cores:
            g.add_edge(d, c)
        for a in by_pod[p]:
            g.add_edge(a, d)                               # single-homed
    _hosts_on(g, flat)
    return g


def build_access_mesh():
    """C: professor's suggestion -- hybrid PLUS intra-pod access full mesh."""
    g = build_hybrid()
    by_pod, _ = _access_grid()
    for p in range(PODS):
        for u, v in itertools.combinations(by_pod[p], 2):
            g.add_edge(u, v)                               # edge-layer mesh
    return g


def build_spine_leaf(spines=4):
    """D: 2-tier Clos -- every access(leaf) to every spine."""
    g = nx.Graph(); _, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    sp = [_add(g, "s%d" % i, CORE) for i in range(spines)]
    for a in flat:
        for s in sp:
            g.add_edge(a, s)
    _hosts_on(g, flat)
    return g


def build_fat_tree():
    """E: folded Clos / fat-tree -- 2 agg/pod, agg fully connected to 6 spines."""
    g = nx.Graph(); by_pod, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    spines = [_add(g, "sp%d" % i, CORE) for i in range(6)]
    for p in range(PODS):
        aggs = [_add(g, "ag%d_%d" % (p, j), AGG, pod=p) for j in range(2)]
        for ag in aggs:
            for s in spines:                               # agg <-> every spine
                g.add_edge(ag, s)
            for a in by_pod[p]:
                g.add_edge(a, ag)                          # access dual-homed to aggs
    _hosts_on(g, flat)
    return g


def build_dist_ring():
    """F: cheap cabling -- 6 dist in a ring, access single-homed to pod dist."""
    g = nx.Graph(); by_pod, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    dists = [_add(g, "d%d" % p, AGG, pod=p) for p in range(PODS)]
    for i in range(PODS):
        g.add_edge(dists[i], dists[(i + 1) % PODS])        # ring
    for p in range(PODS):
        for a in by_pod[p]:
            g.add_edge(a, dists[p])
    _hosts_on(g, flat)
    return g


def build_access_full_mesh():
    """G: reference upper bound -- every access meshed to every access."""
    g = nx.Graph(); _, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    for u, v in itertools.combinations(flat, 2):
        g.add_edge(u, v)
    _hosts_on(g, flat)
    return g


def build_min_tree():
    """H: reference lower bound -- single core, star of dists, single-homed."""
    g = nx.Graph(); by_pod, flat = _access_grid()
    for a in flat:
        _add(g, a, ACCESS, pod=int(a[1:].split("_")[0]))
    core = _add(g, "c0", CORE)
    for p in range(PODS):
        d = _add(g, "d%d" % p, AGG, pod=p)
        g.add_edge(d, core)
        for a in by_pod[p]:
            g.add_edge(a, d)
    _hosts_on(g, flat)
    return g


CANDIDATES = {
    "A. Hybrid dual-plane (current)": build_hybrid,
    "B. 3-tier single-homed": build_single_homed,
    "C. Hybrid + access mesh": build_access_mesh,
    "D. Spine-leaf (Clos, 4 spine)": build_spine_leaf,
    "E. Fat-tree (folded Clos)": build_fat_tree,
    "F. Dist ring": build_dist_ring,
    "G. Access full mesh (ref)": build_access_full_mesh,
    "H. Minimal tree (ref)": build_min_tree,
}


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def switch_graph(g):
    return g.subgraph([n for n, d in g.nodes(data=True)
                       if d["tier"] != HOST]).copy()


def access_nodes(g):
    return [n for n, d in g.nodes(data=True) if d["tier"] == ACCESS]


def metrics(g):
    sw = switch_graph(g)
    acc = access_nodes(g)
    n_sw = sw.number_of_nodes()
    n_links = sw.number_of_edges()
    cost = sum(_cost(g, u, v) for u, v in sw.edges())

    # latency: hop distance between access switches
    dmat = dict(nx.all_pairs_shortest_path_length(sw))
    pair_hops = [dmat[a][b] for a, b in itertools.combinations(acc, 2)]
    avg_hops = sum(pair_hops) / len(pair_hops)
    diameter = max(pair_hops)

    # resilience: node-disjoint paths between a sample of access pairs
    sample = random.sample(list(itertools.combinations(acc, 2)),
                           min(200, len(pair_hops)))
    disj = []
    for a, b in sample:
        try:
            disj.append(len(list(nx.node_disjoint_paths(sw, a, b))))
        except Exception:
            disj.append(0)
    avg_disjoint = sum(disj) / len(disj)
    try:
        edge_conn = nx.edge_connectivity(sw)
    except Exception:
        edge_conn = 0

    # fault tolerance: fraction of access pairs still connected after k random
    # link failures (Monte Carlo)
    survive = {}
    edges = list(sw.edges())
    for k in (1, 2, 3):
        ok = tot = 0
        for _ in range(250):
            h = sw.copy()
            h.remove_edges_from(random.sample(edges, min(k, len(edges))))
            for a, b in random.sample(sample, min(40, len(sample))):
                tot += 1
                if nx.has_path(h, a, b):
                    ok += 1
        survive[k] = round(100.0 * ok / max(1, tot), 1)

    return dict(
        switches=n_sw, links=n_links, cost=cost,
        cost_per_access=round(cost / len(acc), 2),
        avg_hops=round(avg_hops, 2), diameter=diameter,
        avg_disjoint_paths=round(avg_disjoint, 2), edge_connectivity=edge_conn,
        survive_1=survive[1], survive_2=survive[2], survive_3=survive[3],
        resilience_per_cost=round(avg_disjoint / cost * 100, 2))


def main():
    os.makedirs("results", exist_ok=True)
    rows = {}
    print("Topology comparison -- fixed workload: %d access / %d hosts\n"
          % (N_ACCESS, N_HOSTS))
    hdr = ("sw", "links", "cost", "cost/acc", "avgHop", "diam",
           "disjoint", "edgeConn", "surv1", "surv2", "surv3", "res/cost")
    print("%-32s" % "topology" + "".join("%9s" % h for h in hdr))
    for name, build in CANDIDATES.items():
        m = metrics(build())
        rows[name] = m
        print("%-32s" % name + "".join("%9s" % m[k] for k in
              ("switches", "links", "cost", "cost_per_access", "avg_hops",
               "diameter", "avg_disjoint_paths", "edge_connectivity",
               "survive_1", "survive_2", "survive_3", "resilience_per_cost")))

    with open("results/topo_study.json", "w") as fh:
        json.dump(rows, fh, indent=2)
    print("\n  data -> results/topo_study.json")
    _chart(rows)
    _verdict(rows)


def _chart(rows):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    names = list(rows)
    short = [n.split(".")[0] for n in names]
    fig, ax = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle("Campus topology trade-offs (fixed 36 access / 72 hosts)",
                 fontsize=14, weight="bold")

    def bars(a, key, title, color):
        vals = [rows[n][key] for n in names]
        a.bar(short, vals, color=color)
        a.set_title(title); a.grid(axis="y", alpha=0.3)
        for i, v in enumerate(vals):
            a.text(i, v, str(v), ha="center", va="bottom", fontsize=8)

    bars(ax[0][0], "cost", "Cabling cost (lower = cheaper)", "#4c78a8")
    bars(ax[0][1], "avg_hops", "Avg access-access hops (lower = faster)", "#f58518")
    bars(ax[1][0], "avg_disjoint_paths", "Avg node-disjoint paths (higher = tougher)", "#54a24b")
    # scatter: cost vs resilience, labelled
    a = ax[1][1]
    xs = [rows[n]["cost"] for n in names]
    ys = [rows[n]["avg_disjoint_paths"] for n in names]
    a.scatter(xs, ys, c="#b279a2", s=60)
    for i, n in enumerate(short):
        a.annotate(n, (xs[i], ys[i]), fontsize=9,
                   xytext=(4, 4), textcoords="offset points")
    a.set_xlabel("cabling cost"); a.set_ylabel("avg disjoint paths")
    a.set_title("Cost vs resilience (top-left = best value)")
    a.grid(alpha=0.3)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig("results/topo_study.png", dpi=130)
    print("  chart -> results/topo_study.png")


def _verdict(rows):
    def best(key, lo=True):
        return (min if lo else max)(rows, key=lambda n: rows[n][key])
    print("\n  ---- verdict ----")
    print("  cheapest fabric:        %s" % best("cost"))
    print("  lowest latency:         %s" % best("avg_hops"))
    print("  most resilient:         %s" % best("avg_disjoint_paths", lo=False))
    print("  best resilience/cost:   %s" % best("resilience_per_cost", lo=False))
    print("  survives 2 link fails:  " +
          ", ".join("%s=%s%%" % (n.split('.')[0], rows[n]["survive_2"])
                    for n in rows))


if __name__ == "__main__":
    main()
