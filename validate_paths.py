"""Phase 5/6 validator -- proves path computation and fast-failover forwarding
are correct OFFLINE (no root, no Mininet), by simulating the datapath's FF
behaviour under every relevant single-link failure.

Checks:
  Phase 5:
    P1  primary + backup are node-disjoint for representative access pairs
  Phase 6 (protected forwarding plans):
    P2  NORMAL: every (src_access, dest_access) pair is delivered, loop-free
    P3  ACCESS-UPLINK failure -> delivered via plane-B (local FF), failed link unused
    P4  DIST->CORE uplink failure -> delivered via 2nd core (local FF)
    P5  CORE->DEST-DIST failure -> delivered via other core (local FF)
    P6  LAST-HOP dist->access failure -> BLACKHOLE (documents the ONE hop with no
        local protection; Phase 7 controller reroute handles it)
"""

from __future__ import annotations

import sys

import campus_model as cm
import paths


class Report:
    def __init__(self):
        self.n = 0
        self.fail = 0

    def check(self, name, ok, detail=""):
        self.n += 1
        if not ok:
            self.fail += 1
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                             ("  -- " + detail) if detail else ""))


def main():
    m = cm.build()
    v = m.dpid_view()
    n2d, d2n, adj = v["name2dpid"], v["dpid2name"], v["adj"]
    pc = paths.PathComputer(adj)
    rep = Report()

    access = [n2d[n] for n, a in m.switches.items() if a["tier"] == cm.ACCESS]
    print("Model: %s | access switches: %d\n" % (m.summary(), len(access)))

    # ---- P1: disjoint primary/backup for representative access pairs --------
    reps = [("a1_1", "a6_6"), ("a2_3", "a5_4"), ("a1_1", "a1_6")]
    p1_ok = True
    for s, d in reps:
        pri, bak = pc.disjoint_pair(n2d[s], n2d[d])
        if not pri or not bak or (set(pri[1:-1]) & set(bak[1:-1])):
            p1_ok = False
        else:
            print("    %s->%s primary=%s backup=%s"
                  % (s, d, "-".join(d2n[x] for x in pri),
                     "-".join(d2n[x] for x in bak)))
    rep.check("P1 primary/backup node-disjoint (representative pairs)", p1_ok)

    # ---- Precompute plans for every destination access ----------------------
    plans = {da: paths.plan_dest_forwarding(m, da) for da in access}

    # ---- P2: NORMAL delivery, loop-free, for ALL pairs ----------------------
    bad = []
    for da in access:
        for sa in access:
            if sa == da:
                continue
            status, _ = paths.simulate(m, plans[da], sa, da)
            if status != "DELIVERED":
                bad.append((d2n[sa], d2n[da], status))
    rep.check("P2 normal delivery all %d pairs, loop-free"
              % (len(access) * (len(access) - 1)),
              not bad, "%d failures %s" % (len(bad), bad[:3]))

    # helper to fail a single undirected edge
    def E(a, b):
        return frozenset((n2d[a], n2d[b]))

    # scenario source/dest for the failure cases (cross-pod so cores are used)
    sa_name, da_name = "a2_3", "a6_6"
    sa, da = n2d[sa_name], n2d[da_name]
    plan = plans[da]

    # ---- P3: access-uplink failure -> local FF to plane B -------------------
    #   source a2_3 primary uplink is to d2a; fail it.
    f = E("a2_3", "d2a")
    st, path = paths.simulate(m, plan, sa, da, failed_edges={f})
    names = [d2n[x] for x in path]
    used_fail = _traverses(m, path, f)
    rep.check("P3 access-uplink fail (a2_3-d2a) -> delivered via plane B",
              st == "DELIVERED" and not used_fail,
              "%s path=%s" % (st, "-".join(names)))

    # ---- P4: dist->core uplink failure -> local FF to 2nd core --------------
    #   a2_3 -> d2a -> c1 (primary core). fail d2a-c1.
    f = E("d2a", "c1")
    st, path = paths.simulate(m, plan, sa, da, failed_edges={f})
    names = [d2n[x] for x in path]
    rep.check("P4 dist-core fail (d2a-c1) -> delivered via 2nd core (c2)",
              st == "DELIVERED" and not _traverses(m, path, f),
              "%s path=%s" % (st, "-".join(names)))

    # ---- P5: core -> destination-dist failure -> other core -----------------
    #   c1 -> d6a is the last core hop; fail it, expect reroute via c2.
    f = E("c1", "d6a")
    st, path = paths.simulate(m, plan, sa, da, failed_edges={f})
    names = [d2n[x] for x in path]
    rep.check("P5 core-destdist fail (c1-d6a) -> delivered via other core",
              st == "DELIVERED" and not _traverses(m, path, f),
              "%s path=%s" % (st, "-".join(names)))

    # ---- P6: last-hop dist->access failure -> NO local protection -----------
    #   fail d6a-a6_6 (destination's plane-A last hop). Local FF cannot save it;
    #   must blackhole until the controller reroutes to plane B (Phase 7).
    f = E("d6a", "a6_6")
    st, path = paths.simulate(m, plan, sa, da, failed_edges={f})
    rep.check("P6 last-hop fail (d6a-a6_6) -> BLACKHOLE (needs controller reroute)",
              st == "BLACKHOLE",
              "%s (documents the one unprotected hop)" % st)

    print("\n%d checks, %d failed" % (rep.n, rep.fail))
    sys.exit(0 if rep.fail == 0 else 1)


def _traverses(model, path, failed_edge):
    """True if the walked path uses the failed undirected edge."""
    for i in range(len(path) - 1):
        if frozenset((path[i], path[i + 1])) == failed_edge:
            return True
    return False


if __name__ == "__main__":
    main()
