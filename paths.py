"""Phase 5/6 -- path computation and protected (fast-failover) forwarding plans.

Pure Python (stdlib only), importable by the controller (ryu-venv) and the
validators (system python).  Two responsibilities:

Phase 5 -- PATH COMPUTATION
    PathComputer over a DPID adjacency: shortest path, and a node-disjoint
    primary/backup pair.  Works on the *live* graph (edges/nodes can be removed
    to reflect real failures), which is what Phase 7 rerouting will use.

Phase 6 -- PROTECTED FORWARDING PLAN (for OpenFlow fast-failover groups)
    ``plan_dest_forwarding(model, dest_access_dpid)`` returns, for every switch,
    the primary output port toward that destination access switch and a LOCAL
    backup port that still reaches it (or None where no local protection exists,
    e.g. the final distribution->access hop).  This maps directly onto OF1.3
    ``OFPGT_FF`` groups: bucket0 = primary (watch primary port), bucket1 = backup.

    ``simulate(...)`` walks the plan hop-by-hop under an arbitrary set of failed
    links, exactly as the datapath's FF buckets would, so we can PROVE offline
    that traffic is delivered loop-free before touching Mininet.

Reachability facts this exploits (cores even, split into two equal planes):
  * every plane-A core connects to every plane-A distribution switch (same for B)
  * cores are fully meshed
  * each access switch is dual-homed to both distribution switches of its pod
So a distribution switch's two core uplinks are interchangeable, and the two
core switches of a plane are interchangeable -- genuine local protection.
"""

from __future__ import annotations

from collections import deque, defaultdict

import campus_model as cm


class PathComputer:
    """Shortest / disjoint paths over a DPID adjacency {dpid: {peer: out_port}}."""

    def __init__(self, adj):
        self.adj = adj

    def _nbrs(self, n, down_edges, down_nodes):
        for m in self.adj.get(n, {}):
            if m in down_nodes:
                continue
            if frozenset((n, m)) in down_edges:
                continue
            yield m

    def shortest(self, src, dst, down_edges=frozenset(), down_nodes=frozenset()):
        if src == dst:
            return [src]
        if src in down_nodes or dst in down_nodes:
            return None
        seen = {src}
        q = deque([(src, [src])])
        while q:
            node, path = q.popleft()
            for nxt in sorted(self._nbrs(node, down_edges, down_nodes)):
                if nxt in seen:
                    continue
                if nxt == dst:
                    return path + [nxt]
                seen.add(nxt)
                q.append((nxt, path + [nxt]))
        return None

    def disjoint_pair(self, src, dst, down_edges=frozenset(), down_nodes=frozenset()):
        """Primary (shortest) + max node-disjoint backup, or (primary, None)."""
        p1 = self.shortest(src, dst, down_edges, down_nodes)
        if not p1:
            return None, None
        internal = set(p1[1:-1])
        p2 = self.shortest(src, dst, down_edges, down_nodes | internal)
        return p1, p2

    def ports_along(self, path):
        """[(dpid, out_port_toward_next)] for all but the last hop."""
        out = []
        for i in range(len(path) - 1):
            out.append((path[i], self.adj[path[i]][path[i + 1]]))
        return out


# --------------------------------------------------------------------------- #
# Phase 6: destination(access)-rooted protected forwarding plan
# --------------------------------------------------------------------------- #
def plan_dest_forwarding(model, dest_access_dpid):
    """Per-switch primary/backup output ports toward a destination access sw.

    Returns {dpid: {"primary": port, "backup": port|None}} for every switch that
    forwards toward ``dest_access_dpid`` (the destination access switch itself is
    excluded -- it delivers to its local host ports instead).
    """
    v = model.dpid_view()
    adj = v["adj"]
    n2d = v["name2dpid"]
    d2n = v["dpid2name"]

    dest_name = d2n[dest_access_dpid]
    P = model.switches[dest_name]["pod"]
    dPa = n2d[model.dist_name(P, cm.PLANE_A)]
    dPb = n2d[model.dist_name(P, cm.PLANE_B)]

    cores = [n2d[model.core_name(i)] for i in range(1, model.cores + 1)]
    half = model.cores // 2
    plane_cores = {cm.PLANE_A: cores[:half], cm.PLANE_B: cores[half:]}
    dest_dist = {cm.PLANE_A: dPa, cm.PLANE_B: dPb}

    plan = {}

    # last hop: destination-pod distribution switches deliver down to the access.
    # single link -> NO local backup (honest limitation; needs controller reroute).
    plan[dPa] = {"primary": adj[dPa][dest_access_dpid], "backup": None}
    plan[dPb] = {"primary": adj[dPb][dest_access_dpid], "backup": None}

    # cores: forward to the destination-pod dist of their plane; local backup is
    # via the other core of the same plane (which also reaches that dist).
    for plane, pcs in plane_cores.items():
        dd = dest_dist[plane]
        for ci in pcs:
            others = [c for c in pcs if c != ci]
            plan[ci] = {"primary": adj[ci][dd],
                        "backup": adj[ci][others[0]] if others else None}

    # every OTHER distribution switch forwards UP to a core of its plane; local
    # backup is its second core uplink (interchangeable).
    for name, attr in model.switches.items():
        if attr["tier"] != cm.DIST:
            continue
        dp = n2d[name]
        if dp in (dPa, dPb):
            continue
        pcs = plane_cores[attr["plane"]]
        plan[dp] = {"primary": adj[dp][pcs[0]],
                    "backup": adj[dp][pcs[1]] if len(pcs) > 1 else None}

    # every access switch (except the destination) forwards UP to its pod's
    # plane-A dist (primary) with the plane-B dist as local backup (dual-homing).
    for name, attr in model.switches.items():
        if attr["tier"] != cm.ACCESS:
            continue
        dp = n2d[name]
        if dp == dest_access_dpid:
            continue
        da = n2d[model.dist_name(attr["pod"], cm.PLANE_A)]
        db = n2d[model.dist_name(attr["pod"], cm.PLANE_B)]
        plan[dp] = {"primary": adj[dp][da], "backup": adj[dp][db]}

    return plan


def _bfs_dist(adj, root, down_edges, down_nodes):
    """Hop distance from root to every reachable node over the live graph."""
    dist = {root: 0}
    q = deque([root])
    while q:
        u = q.popleft()
        for w in adj.get(u, {}):
            if w in down_nodes or frozenset((u, w)) in down_edges:
                continue
            if w not in dist:
                dist[w] = dist[u] + 1
                q.append(w)
    return dist


def plan_dest_tree(model, dest_access_dpid,
                   down_edges=frozenset(), down_nodes=frozenset()):
    """Failure-aware destination-rooted plan (Phase 7).

    Builds a shortest-path sink tree toward ``dest_access_dpid`` over the LIVE
    graph (down edges/nodes removed).  For every switch:
      primary = next hop on a shortest path to the destination
      backup  = a neighbour that reaches the destination WITHOUT passing back
                through this switch (loop-safe local fast-failover), else None
    With an empty down-set this reproduces the normal protected forwarding; with
    failures applied it yields the rerouted forwarding the controller installs.
    """
    v = model.dpid_view()
    adj = v["adj"]
    pc = PathComputer(adj)
    dist = _bfs_dist(adj, dest_access_dpid, down_edges, down_nodes)

    def alive_nbrs(u):
        for w in adj.get(u, {}):
            if w in down_nodes or frozenset((u, w)) in down_edges:
                continue
            if w in dist:                      # must be able to reach the dest
                yield w

    # 1) primary next hop for every node = parent in the sink tree
    parent = {}
    for u in adj:
        if u == dest_access_dpid or u in down_nodes or u not in dist:
            continue
        cands = list(alive_nbrs(u))
        if not cands:
            continue
        on_sp = [w for w in cands if dist[w] == dist[u] - 1]
        parent[u] = min(on_sp) if on_sp else min(cands, key=lambda w: (dist[w], w))

    def routes_through(w, u):
        """True if w's sink-tree path to the destination passes through u."""
        x, seen = w, set()
        while x != dest_access_dpid and x in parent and x not in seen:
            if x == u:
                return True
            seen.add(x)
            x = parent[x]
        return x == u

    # 2) loop-safe backup next hop
    plan = {}
    for u, prim in parent.items():
        backup = None
        best = None
        for w in sorted(alive_nbrs(u)):
            if w == prim or routes_through(w, u):
                continue
            if best is None or dist[w] < best:
                best, backup = dist[w], w
        plan[u] = {"primary": adj[u][prim],
                   "backup": adj[u][backup] if backup is not None else None}
    return plan


def simulate(model, plan, src_dpid, dest_dpid, failed_edges=frozenset(),
             down_nodes=frozenset(), max_hops=32):
    """Walk the plan like FF buckets would; return (status, path_of_dpids).

    status in {DELIVERED, LOOP, BLACKHOLE, NO_RULE, MAXHOPS}.
    At each hop use primary unless its link is failed, then backup.
    """
    portmap = model.dpid_view()["portmap"]
    node = src_dpid
    visited = []
    for _ in range(max_hops):
        if node == dest_dpid:
            return "DELIVERED", visited + [node]
        if node in visited:
            return "LOOP", visited + [node]
        visited.append(node)
        pl = plan.get(node)
        if pl is None:
            return "NO_RULE", visited
        chosen = None
        for port in (pl["primary"], pl["backup"]):
            if port is None:
                continue
            entry = portmap[node].get(port)
            if not entry or entry[0] != "sw":
                continue
            peer = entry[1]
            if peer in down_nodes or frozenset((node, peer)) in failed_edges:
                continue
            chosen = peer
            break
        if chosen is None:
            return "BLACKHOLE", visited
        node = chosen
    return "MAXHOPS", visited
