"""Phase 7 validator -- proves controller RE-ROUTING is correct, offline.

The controller reacts to LINK_DOWN / SWITCH_DISCONNECTED by recomputing the
destination plan over the live graph (paths.plan_dest_tree with the failed
edges/nodes removed) and reinstalling flows.  Here we recompute exactly that and
simulate delivery, covering the cases local fast-failover CANNOT handle.

Checks (destination = a6_6):
  R1  NORMAL: plan_dest_tree delivers all sources, loop-free (parity w/ Phase 6)
  R2  local single-link failures still delivered by FF backup (no recompute)
  R3  LAST-HOP d6a-a6_6 down -> recompute -> all sources delivered via plane B
  R4  DIST switch d6a dead   -> recompute -> all sources delivered, d6a unused
  R5  CORE switch c1 dead    -> recompute -> all sources delivered, c1 unused
"""

from __future__ import annotations

import sys

import campus_model as cm
import paths


def main():
    m = cm.build()
    v = m.dpid_view()
    n2d, d2n = v["name2dpid"], v["dpid2name"]
    access = [n2d[n] for n, a in m.switches.items() if a["tier"] == cm.ACCESS]
    dest = n2d["a6_6"]
    srcs = [s for s in access if s != dest]
    fails = 0
    total = 0

    def E(a, b):
        return frozenset((n2d[a], n2d[b]))

    def check(name, ok, detail=""):
        nonlocal fails, total
        total += 1
        if not ok:
            fails += 1
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                             ("  -- " + detail) if detail else ""))

    def all_delivered(plan, failed_edges=frozenset(), down_nodes=frozenset(),
                      forbid=None):
        bad = []
        for s in srcs:
            if s in down_nodes:
                continue
            st, path = paths.simulate(m, plan, s, dest,
                                      failed_edges=failed_edges,
                                      down_nodes=down_nodes)
            ok = st == "DELIVERED"
            if ok and forbid is not None and forbid in path:
                ok = False
                st = "USES_FAILED"
            if not ok:
                bad.append((d2n[s], st))
        return bad

    print("Model: %s | dest=a6_6 | sources=%d\n" % (m.summary(), len(srcs)))

    # R1 normal
    plan0 = paths.plan_dest_tree(m, dest)
    check("R1 normal: all sources delivered, loop-free",
          not all_delivered(plan0), str(all_delivered(plan0)[:3]))

    # R2 local single-link failures handled by FF backup (plan computed w/o fault)
    for a, b, label in [("a2_3", "d2a", "access-uplink"),
                        ("d2a", "c1", "dist-core"),
                        ("c1", "d6a", "core-destdist")]:
        st, path = paths.simulate(m, plan0, n2d["a2_3"], dest,
                                  failed_edges={E(a, b)})
        used = E(a, b) in {frozenset((path[i], path[i + 1]))
                           for i in range(len(path) - 1)}
        check("R2 local FF (%s %s-%s) delivered, failed link unused" % (label, a, b),
              st == "DELIVERED" and not used,
              "%s %s" % (st, "-".join(d2n[x] for x in path)))

    # R3 last-hop d6a-a6_6 down -> recompute -> deliver via plane B (d6b)
    de = {E("d6a", "a6_6")}
    plan3 = paths.plan_dest_tree(m, dest, down_edges=frozenset(de))
    bad3 = all_delivered(plan3, failed_edges=de)
    # spot-check that a plane-A source now reaches a6_6 via d6b
    _, p3 = paths.simulate(m, plan3, n2d["a1_1"], dest, failed_edges=de)
    via_b = n2d["d6b"] in p3
    check("R3 last-hop d6a-a6_6 down: recompute delivers all via plane B",
          not bad3 and via_b,
          "bad=%s a1_1 path=%s" % (bad3[:2], "-".join(d2n[x] for x in p3)))

    # R4 whole distribution switch d6a dead -> recompute
    dn = {n2d["d6a"]}
    plan4 = paths.plan_dest_tree(m, dest, down_nodes=frozenset(dn))
    check("R4 dist switch d6a DEAD: recompute delivers all, d6a unused",
          not all_delivered(plan4, down_nodes=dn, forbid=n2d["d6a"]),
          str(all_delivered(plan4, down_nodes=dn, forbid=n2d["d6a"])[:3]))

    # R5 whole core switch c1 dead -> recompute
    dn = {n2d["c1"]}
    plan5 = paths.plan_dest_tree(m, dest, down_nodes=frozenset(dn))
    check("R5 core switch c1 DEAD: recompute delivers all, c1 unused",
          not all_delivered(plan5, down_nodes=dn, forbid=n2d["c1"]),
          str(all_delivered(plan5, down_nodes=dn, forbid=n2d["c1"])[:3]))

    print("\n%d checks, %d failed" % (total, fails))
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
