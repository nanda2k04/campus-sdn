"""Topology validation (Phase 1 requirement section 23 + failover section 24).

Runs WITHOUT root -- it validates the *model*, not a live Mininet instance.
Exit code is non-zero if any check fails, so it is CI-friendly.

Checks:
  1.  DPIDs unique
  2.  every link endpoint refers to a real node
  3.  (switch, port) pairs unique  -> catches wiring collisions
  4.  switch graph is fully connected (no orphan switch)
  5.  every host is single-homed to exactly one existing access switch
  6.  redundancy: >=2 node-disjoint paths between representative pods & intra-pod
  7.  failover independence (Tests 2-5): after removing the failed component,
      the affected pair is still connected AND the new path excludes it
  8.  independent cross-check with networkx (if available)
"""

from __future__ import annotations

import sys
from collections import deque, defaultdict

import campus_model as cm


class Report:
    def __init__(self):
        self.rows = []
        self.failed = 0

    def check(self, name, ok, detail=""):
        self.rows.append((ok, name, detail))
        if not ok:
            self.failed += 1

    def dump(self):
        print("=" * 72)
        print("TOPOLOGY VALIDATION REPORT")
        print("=" * 72)
        for ok, name, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            line = "[%s] %s" % (mark, name)
            if detail:
                line += "  -- " + detail
            print(line)
        print("-" * 72)
        print("%d checks, %d failed" % (len(self.rows), self.failed))
        return self.failed == 0


# ---- small graph helpers that operate on a mutable adjacency copy --------- #
def adjacency(topo, drop_nodes=frozenset(), drop_edges=frozenset()):
    g = defaultdict(set)
    for n in topo.switches:
        if n not in drop_nodes:
            g[n]
    for lk in topo.links:
        if lk["kind"] == cm.K_HOST_ACCESS:
            continue
        a, b = lk["a"], lk["b"]
        if a in drop_nodes or b in drop_nodes:
            continue
        if frozenset((a, b)) in drop_edges:
            continue
        g[a].add(b)
        g[b].add(a)
    return g


def bfs(g, src, dst):
    if src == dst:
        return [src]
    if src not in g or dst not in g:
        return None
    seen = {src}
    q = deque([(src, [src])])
    while q:
        node, path = q.popleft()
        for nxt in sorted(g[node]):
            if nxt in seen:
                continue
            if nxt == dst:
                return path + [nxt]
            seen.add(nxt)
            q.append((nxt, path + [nxt]))
    return None


def main():
    topo = cm.build()
    rep = Report()
    s = topo.summary()

    print("Model: %s" % s)
    print()

    # 1. unique DPIDs
    dpids = [a["dpid"] for a in topo.switches.values()]
    rep.check("unique DPIDs", len(dpids) == len(set(dpids)),
              "%d switches, %d distinct dpids" % (len(dpids), len(set(dpids))))

    # 2. link endpoints exist
    node_names = set(topo.switches) | set(topo.hosts)
    bad_ep = [lk for lk in topo.links
              if lk["a"] not in node_names or lk["b"] not in node_names]
    rep.check("every link endpoint exists", not bad_ep,
              "%d dangling endpoints" % len(bad_ep))

    # 3. (device, port) uniqueness  (hosts use port 0 -> skip host side)
    used = defaultdict(list)
    for lk in topo.links:
        if lk["kind"] != cm.K_HOST_ACCESS:
            used[(lk["a"], lk["a_port"])].append(lk)
        used[(lk["b"], lk["b_port"])].append(lk)
    collisions = {k: v for k, v in used.items() if len(v) > 1}
    rep.check("(device, port) unique -> no wiring collision", not collisions,
              "%d colliding ports" % len(collisions))
    for k in list(collisions)[:5]:
        print("    port collision at %s used by %d links" % (k, len(collisions[k])))

    # 4. switch graph connected (no orphan switch)
    g = adjacency(topo)
    start = next(iter(topo.switches))
    reached = {start}
    q = deque([start])
    while q:
        n = q.popleft()
        for m in g[n]:
            if m not in reached:
                reached.add(m)
                q.append(m)
    rep.check("switch graph fully connected (no orphan switch)",
              len(reached) == len(topo.switches),
              "%d/%d switches reachable" % (len(reached), len(topo.switches)))

    # 5. hosts single-homed to a real access switch
    host_links = defaultdict(list)
    for lk in topo.links:
        if lk["kind"] == cm.K_HOST_ACCESS:
            host_links[lk["a"]].append(lk["b"])
    ok_hosts = True
    for h, attrs in topo.hosts.items():
        ups = host_links.get(h, [])
        if len(ups) != 1 or ups[0] != attrs["access"] or attrs["access"] not in topo.switches:
            ok_hosts = False
            break
    rep.check("every host single-homed to a real access switch", ok_hosts,
              "%d hosts" % len(topo.hosts))

    # 6. redundancy: >=2 node-disjoint paths
    # inter-pod (pod 1 access 1  <->  pod (last) access 1)
    a_src = topo.access_name(1, 1)
    a_dst = topo.access_name(topo.pods, 1)
    dj = topo.two_node_disjoint_paths(a_src, a_dst)
    rep.check("inter-pod: >=2 node-disjoint paths (%s -> %s)" % (a_src, a_dst),
              dj is not None,
              (" | ".join("->".join(p) for p in dj)) if dj else "NONE")
    # intra-pod (pod 1 access 1 <-> pod 1 access 2)
    if topo.access_per_pod >= 2:
        ia_src, ia_dst = topo.access_name(1, 1), topo.access_name(1, 2)
        dj2 = topo.two_node_disjoint_paths(ia_src, ia_dst)
        rep.check("intra-pod: >=2 node-disjoint paths (%s -> %s)" % (ia_src, ia_dst),
                  dj2 is not None,
                  (" | ".join("->".join(p) for p in dj2)) if dj2 else "NONE")

    # 7. failover independence (Tests 2-5)
    #    Test 2: access uplink (a1_1 -> d1a) fails -> a1_1 still reaches network
    acc = topo.access_name(1, 1)
    da = topo.dist_name(1, cm.PLANE_A)
    db = topo.dist_name(1, cm.PLANE_B)
    far = topo.access_name(topo.pods, 1)
    g2 = adjacency(topo, drop_edges={frozenset((acc, da))})
    p = bfs(g2, acc, far)
    rep.check("Test2 access-uplink fail: reroute via plane B exists",
              p is not None and da not in set(p[1:-1]),
              ("->".join(p)) if p else "NONE")

    #    Test 3: dist->core uplink (d1a -> c1) fails -> d1a still reaches a core plane
    c1 = topo.core_name(1)
    g3 = adjacency(topo, drop_edges={frozenset((da, c1))})
    # d1a must still reach the far pod's access
    p3 = bfs(g3, da, far)
    rep.check("Test3 dist-core uplink fail: alternate core uplink works",
              p3 is not None,
              ("->".join(p3)) if p3 else "NONE")

    #    Test 4: core mesh link (c1 - c3) fails -> cores still connected
    half = topo.cores // 2
    c_a, c_b = topo.core_name(1), topo.core_name(half + 1)
    g4 = adjacency(topo, drop_edges={frozenset((c_a, c_b))})
    p4 = bfs(g4, c_a, c_b)
    rep.check("Test4 core-mesh link fail: cores remain connected",
              p4 is not None,
              ("->".join(p4)) if p4 else "NONE")

    #    Test 5: whole distribution switch d1a down -> pod-1 access still reachable,
    #            and new path must NOT contain d1a
    g5 = adjacency(topo, drop_nodes={da})
    p5 = bfs(g5, acc, far)
    rep.check("Test5 dist-switch down: reroute exists and excludes failed node",
              p5 is not None and da not in p5,
              ("->".join(p5)) if p5 else "NONE")

    # 8. independent cross-check with networkx (optional)
    try:
        import networkx as nx
        G = nx.Graph()
        G.add_nodes_from(topo.switches)
        for lk in topo.links:
            if lk["kind"] != cm.K_HOST_ACCESS:
                G.add_edge(lk["a"], lk["b"])
        conn = nx.is_connected(G)
        # node connectivity between representative pods (Menger)
        kappa = nx.node_connectivity(G, a_src, a_dst)
        rep.check("networkx cross-check: connected & node-connectivity>=2",
                  conn and kappa >= 2,
                  "connected=%s node_connectivity(%s,%s)=%d" % (conn, a_src, a_dst, kappa))
    except ImportError:
        print("    (networkx not available -- skipping independent cross-check)")

    print()
    ok = rep.dump()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
