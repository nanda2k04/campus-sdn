#!/usr/bin/env python3
"""Offline resilience proof under MULTIPLE simultaneous failures (gap R2).

The live demos fail one element at a time.  This proves -- exhaustively and
without Mininet -- that the controller's destination-rooted reroute (the exact
``paths.plan_dest_tree`` + datapath walk the controller installs) delivers
traffic under EVERY single and EVERY double link failure, and that the only
cases it does NOT deliver are genuine physical partitions (no path exists at all
over the surviving graph), never a routing bug (loop/black-hole while a path
still exists).

For each failure set F and each test pair (src access -> dst access):
  * recompute the plan over the live graph (down edges = F),
  * walk it exactly as the switches would (paths.simulate),
  * and cross-check against whether a path PHYSICALLY still exists.

Verdict per case:
  RECOVERED              delivered over a rerouted path
  PARTITION (correct)    not delivered AND no path exists (unavoidable)
  BUG                    not delivered BUT a path existed  <-- must be zero
  LOOP                   simulate looped                    <-- must be zero

Run:  python3 multifail_proof.py           (uses the default 6x6x2x4 campus)
"""
from __future__ import annotations

import itertools
import json
import os
import sys

import campus_model as cm
import paths


def inter_switch_edges(model):
    v = model.dpid_view()
    n2d = v["name2dpid"]
    edges = []
    for lk in model.links:
        if lk["kind"] in (cm.K_HOST_ACCESS, cm.K_HOST_DC):
            continue
        edges.append(frozenset((n2d[lk["a"]], n2d[lk["b"]])))
    # de-dup (each physical link appears once already, but be safe)
    seen, uniq = set(), []
    for e in edges:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def test_pairs(model):
    """A representative spread of source/destination access pairs."""
    v = model.dpid_view()
    n2d = v["name2dpid"]
    P, A = model.pods, model.access_per_pod
    names = [
        (model.access_name(1, 1), model.access_name(P, A)),       # far corners
        (model.access_name(1, 1), model.access_name(1, A)),       # same pod
        (model.access_name(2, 3), model.access_name(5, 4)),       # cross-region
        (model.access_name(1, 2), model.access_name(4, 5)),
        (model.access_name(3, 1), model.access_name(6, 6)),
    ]
    out = []
    for s, d in names:
        if s in n2d and d in n2d:
            out.append((s, d, n2d[s], n2d[d]))
    return out


def evaluate(model, pairs, failset):
    pc = paths.PathComputer(model.dpid_view()["adj"])
    rec = part = bug = loop = 0
    worst = []
    for s_name, d_name, s, d in pairs:
        plan = paths.plan_dest_tree(model, d, down_edges=failset)
        status, path = paths.simulate(model, plan, s, d, failed_edges=failset)
        reachable = pc.shortest(s, d, down_edges=failset) is not None
        if status == "DELIVERED":
            rec += 1
        elif status == "LOOP":
            loop += 1
            worst.append((s_name, d_name, status))
        elif not reachable:
            part += 1                 # correct: physically partitioned
        else:
            bug += 1                  # a path existed but we didn't deliver
            worst.append((s_name, d_name, status))
    return rec, part, bug, loop, worst


def run(model=None):
    model = model or cm.build()
    pairs = test_pairs(model)
    edges = inter_switch_edges(model)
    print("Multi-failure resilience proof")
    print("  topology: %s" % model.summary())
    print("  inter-switch links: %d   test pairs: %d" % (len(edges), len(pairs)))

    totals = {}
    worst_all = []

    def tally(name, failsets):
        rec = part = bug = loop = n = 0
        for F in failsets:
            r, p, b, l, w = evaluate(model, pairs, F)
            rec += r; part += p; bug += b; loop += l
            n += len(pairs)
            worst_all.extend(w)
        totals[name] = dict(cases=n, recovered=rec, partition=part,
                            bug=bug, loop=loop)
        cover = 100.0 * rec / max(1, rec + part + bug + loop)
        print("  [%-12s] cases=%5d  recovered=%5d  partition=%4d  "
              "BUG=%d  LOOP=%d  (recover-of-recoverable=%.1f%%)"
              % (name, n, rec, part, bug, loop,
                 100.0 * rec / max(1, rec + bug + loop)))

    # Each failset is a SET whose elements are edge-frozensets (down_edges), to
    # match paths.plan_dest_tree / simulate (`frozenset((n,m)) in down_edges`).
    # 0 failures (baseline)
    tally("baseline", [frozenset()])
    # all single link failures: {e}
    tally("single", [frozenset([e]) for e in edges])
    # all double link failures: {e1, e2}
    doubles = [frozenset([a, b]) for a, b in itertools.combinations(edges, 2)]
    tally("double", doubles)

    # targeted TRUE partitions: cut BOTH uplinks of each test pair's source
    # access switch -> it is physically isolated.  The system must report this
    # as a partition (correct), never deliver or loop.  This exercises the
    # controller's DEST_UNREACHABLE path.
    adj = model.dpid_view()["adj"]
    part_sets = []
    for _s, _d, s, _dd in pairs:
        uplinks = frozenset(frozenset((s, nbr)) for nbr in adj.get(s, {}))
        part_sets.append(uplinks)
    tally("partition*", part_sets)

    bugs = sum(t["bug"] + t["loop"] for t in totals.values())
    print("\n  VERDICT: %s" % ("PASS -- no loops or black-holes while a path "
                               "existed, under any single or double failure"
                               if bugs == 0 else
                               "FAIL -- %d routing bug(s) found" % bugs))
    if worst_all:
        print("  first offending cases:", worst_all[:6])

    os.makedirs("results", exist_ok=True)
    out = dict(topology=model.summary(), links=len(edges), pairs=len(pairs),
               totals=totals, bugs=bugs,
               verdict=("PASS" if bugs == 0 else "FAIL"))
    with open("results/multifail_proof.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("  summary -> results/multifail_proof.json")
    return bugs == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
