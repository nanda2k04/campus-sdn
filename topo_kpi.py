#!/usr/bin/env python3
"""Category-D KPIs: hardware feasibility & scalability of the dual-plane fabric.

Complements topo_study.py (which scores resilience/cost across topology families).
This module answers the "is it buildable and does it scale?" questions for OUR
hybrid dual-plane design, as closed-form radix/oversubscription formulas plus a
measured bisection, evaluated across a scaling ladder.

KPIs (per (P,A,H,C) instance):
  switches, hosts
  core/dist/access radix (ports per switch) and max radix  -> must fit commodity
  oversubscription: port-ratio (H:2) and BANDWIDTH ratio given link speeds
  bisection: min switch-links separating half the access tier from the other half
Run:  PYTHONPATH=. python3 topo_kpi.py            # prints the ladder
      PYTHONPATH=. python3 topo_kpi.py P A H C     # one custom instance
"""
import json
import sys

import networkx as nx

import campus_model as cm

# --- validation targets (tune to your hardware) ---
COMMODITY_RADIX = 48          # a standard access/aggregation switch
HOST_GBPS = 1.0               # host access-port speed
UPLINK_GBPS = 10.0            # inter-switch link speed (10G uplinks)
OVERSUB_BW_TARGET = 4.0       # bandwidth oversubscription we consider healthy


def fabric_graph(model):
    """Switch-only graph (inter-switch links; host links excluded)."""
    g = nx.Graph()
    for n, a in model.switches.items():
        g.add_node(n, tier=a["tier"])
    for l in model.links:
        if l["kind"] in (cm.K_HOST_ACCESS, cm.K_HOST_DC):
            continue
        g.add_edge(l["a"], l["b"])
    return g


def bisection_links(g, model):
    """Min # of switch-switch links to separate half the access switches from
    the other half (a balanced edge-cut = bisection width in link units)."""
    access = sorted(n for n, a in model.switches.items() if a["tier"] == cm.ACCESS)
    if len(access) < 2:
        return 0
    half = len(access) // 2
    left, right = access[:half], access[half:]
    h = g.copy()
    for u, v in h.edges():
        h[u][v]["capacity"] = 1
    h.add_node("_S"); h.add_node("_T")
    for a in left:
        h.add_edge("_S", a, capacity=float("inf"))
    for b in right:
        h.add_edge("_T", b, capacity=float("inf"))
    return int(nx.minimum_cut_value(h, "_S", "_T"))


def kpi(P, A, H, C, mlag=False):
    model = cm.build(pods=P, access_per_pod=A, hosts_per_access=H, cores=C, mlag=mlag)
    g = fabric_graph(model)

    core_r = (C - 1) + P
    dist_r = 2 + A + (1 if mlag else 0)
    acc_r = 2 + H
    max_r = max(core_r, dist_r, acc_r)

    oversub_ports = H / 2.0
    oversub_bw = (H * HOST_GBPS) / (2 * UPLINK_GBPS)
    hosts = P * A * H
    bis = bisection_links(g, model)
    bis_gbps = bis * UPLINK_GBPS

    return dict(
        P=P, A=A, H=H, C=C,
        switches=len(model.switches), hosts=hosts,
        core_radix=core_r, dist_radix=dist_r, access_radix=acc_r, max_radix=max_r,
        radix_ok=max_r <= COMMODITY_RADIX,
        oversub_ports=round(oversub_ports, 1),
        oversub_bw=round(oversub_bw, 2),
        oversub_ok=oversub_bw <= OVERSUB_BW_TARGET,
        bisection_links=bis, bisection_gbps=round(bis_gbps, 1),
        bisection_per_host_mbps=round(1000 * bis_gbps / hosts, 1) if hosts else 0.0,
    )


# scaling ladder: same 3-tier dual-plane design, only P/A/H/C turned
LADDER = [
    ("Small campus",      4, 3, 24, 4),
    ("Paper eval (light)", 6, 6, 2, 4),
    ("Dept realistic",    6, 6, 24, 4),
    ("Recommended deploy", 8, 4, 46, 4),
    ("Large (~100 sw)",   12, 6, 24, 4),
    ("XL (~220 sw)",      24, 7, 24, 4),
]


def main():
    if len(sys.argv) == 5:
        P, A, H, C = map(int, sys.argv[1:5])
        rows = [("custom", P, A, H, C)]
    else:
        rows = LADDER

    out = []
    hdr = ("label", "P", "A", "H", "C", "sw", "hosts", "radix(c/d/a=max)",
           "radixOK", "oversubBW", "osOK", "bisect(lnk/Gbps)", "per-host")
    print("%-18s %2s %2s %2s %2s %4s %6s  %-16s %-7s %-9s %-4s %-16s %-9s"
          % hdr)
    print("-" * 118)
    for label, P, A, H, C in rows:
        k = kpi(P, A, H, C)
        k["label"] = label
        out.append(k)
        print("%-18s %2d %2d %2d %2d %4d %6d  %2d/%2d/%2d=%-7d %-7s %-9.2f %-4s %3d / %-9.1f %-9.1f"
              % (label, P, A, H, C, k["switches"], k["hosts"],
                 k["core_radix"], k["dist_radix"], k["access_radix"], k["max_radix"],
                 "OK" if k["radix_ok"] else "OVER",
                 k["oversub_bw"], "OK" if k["oversub_ok"] else "HIGH",
                 k["bisection_links"], k["bisection_gbps"],
                 k["bisection_per_host_mbps"]))
    print("\ntargets: radix<=%d ports, oversub_bw<=%.1f:1 (host %gG / uplink %gG)"
          % (COMMODITY_RADIX, OVERSUB_BW_TARGET, HOST_GBPS, UPLINK_GBPS))
    import os
    os.makedirs("results", exist_ok=True)
    with open("results/topo_kpi.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("saved -> results/topo_kpi.json")


if __name__ == "__main__":
    main()
