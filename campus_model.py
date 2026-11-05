"""Single source of truth for the hybrid-campus SDN topology.

This module is intentionally pure Python (standard library only) so the *same*
topology definition can be imported by three very different runtimes without
dragging in each other's dependencies:

  * ``topology.py``        -> the Mininet builder      (system python3.12, root)
  * ``validate_topology.py`` -> the validator          (system python3.x)
  * the Ryu controller     -> path / plane awareness   (ryu-venv python3.9)

Architecture (approved in Phase 1): a hybrid campus network.

    Ryu controller
        |
    Core  : ``cores`` switches in a FULL MESH               (default 4)
        |    plane-A distribution uplink to cores[0:half]
        |    plane-B distribution uplink to cores[half:]
    Dist  : 2 per pod  -> plane A (primary) + plane B (backup)
        |    every access switch is DUAL-HOMED to both dist switches in its pod
    Access: ``access_per_pod`` per pod
        |
    Hosts : ``hosts_per_access`` per access switch (single-homed, by design)

Every physical link is created with an explicit, deterministic port number on
BOTH ends (see the ``*_PORT`` / ``*_BASE`` constants), so the controller can
reason about ports without discovery ambiguity.
"""

from __future__ import annotations

import json
from collections import deque, defaultdict

# --------------------------------------------------------------------------- #
# Tier / plane vocabulary
# --------------------------------------------------------------------------- #
CORE = "core"
DIST = "dist"
ACCESS = "access"
DC = "dc"              # dedicated data-centre / server-aggregation switch
PLANE_A = "A"          # primary plane
PLANE_B = "B"          # backup plane

# Link kinds (used for colouring in the dashboard and for scenario selection)
K_CORE_MESH = "core-mesh"
K_DIST_CORE = "dist-core"
K_ACCESS_DIST = "access-dist"
K_HOST_ACCESS = "host-access"
K_DIST_PEER = "dist-peer"      # MLAG lateral link between a pod's two dist switches
K_DC_CORE = "dc-core"          # data-centre switch uplink to the cores
K_HOST_DC = "host-dc"          # server to the data-centre switch

# Departments (key, label, qos_class) with qos_class 2=high / 1=med / 0=low.
# Pods are mapped onto this rotation so every access switch/host inherits a
# department identity and a QoS service class (used by app-aware queueing).
DEPARTMENTS = [
    ("cs", "CS Lab", 1), ("elec", "Electronics", 1), ("lib", "Library", 0),
    ("class", "Classroom", 0), ("fac", "Faculty", 2), ("res", "Research", 2),
]
QOS_CLASS_NAME = {2: "high", 1: "med", 0: "low"}

# --------------------------------------------------------------------------- #
# Deterministic port conventions.  These are contractual: the Mininet builder
# and the controller both rely on them, so they must never drift.
# --------------------------------------------------------------------------- #
ACCESS_UPLINK_A_PORT = 1     # access -> dist plane A  (FF primary bucket)
ACCESS_UPLINK_B_PORT = 2     # access -> dist plane B  (FF backup bucket)
ACCESS_HOST_BASE = 10        # access -> host k  == ACCESS_HOST_BASE + (k-1)

DIST_UPLINK_BASE = 1         # dist -> core[first]  = port 1 (primary)
                             # dist -> core[second] = port 2 (backup)
DIST_PEER_PORT = 3           # dist -> twin dist (MLAG lateral), port 3 both ends
DIST_DOWNLINK_BASE = 10      # dist -> access a     = base + (a-1)

CORE_MESH_BASE = 1           # core -> peer core (ports 1..cores-1, by peer index)
CORE_DOWNLINK_BASE = 10      # core -> dist(pod p) = base + (p-1)
                             # core -> dc_srv      = base + pods (one past the pods)

DC_UPLINK_A_PORT = 1         # dc_srv -> plane-A core (primary)
DC_UPLINK_B_PORT = 2         # dc_srv -> plane-B core (backup)
DC_HOST_BASE = 10            # dc_srv -> server k   = base + (k-1)
DC_DPID = 50                 # data-centre switch dpid (distinct from all tiers)
DC_POD = 200                 # synthetic pod id for the DC (MAC/prefix aggregation)

# --------------------------------------------------------------------------- #
# Default scale  (Phase 1 target: ~50 switches, ~72 hosts)
#   4 core + 6*2 dist (12) + 6*6 access (36) = 52 switches
#   36 access * 2 hosts = 72 hosts
# ``pods``, ``access_per_pod`` and ``hosts_per_access`` are the scalable knobs.
# ``cores`` and dist-per-pod=2 are design invariants of the two-plane model.
# --------------------------------------------------------------------------- #
DEFAULT_PARAMS = dict(pods=6, access_per_pod=6, hosts_per_access=2, cores=4)


class CampusTopology:
    """Builds and holds the whole topology as plain data structures."""

    def __init__(self, pods=6, access_per_pod=6, hosts_per_access=2, cores=4,
                 mlag=False, dc=False, servers=3):
        if cores < 4 or cores % 2 != 0:
            raise ValueError("cores must be an even number >= 4 (two equal planes)")
        if pods < 1 or access_per_pod < 1 or hosts_per_access < 1:
            raise ValueError("pods / access_per_pod / hosts_per_access must be >= 1")

        self.pods = pods
        self.access_per_pod = access_per_pod
        self.hosts_per_access = hosts_per_access
        self.cores = cores
        # Superset features (opt-in so the verified base topology is unchanged):
        #   mlag    -> lateral MLAG peer link between each pod's two dist switches,
        #              giving a horizontal failover path for downlink failures.
        #   dc      -> a dedicated data-centre switch (dual-homed to the cores)
        #              hosting `servers` servers.
        self.mlag = mlag
        self.dc = dc
        self.servers = servers if dc else 0

        # name -> attributes
        self.switches = {}   # {name: {dpid, tier, pod, plane, index}}
        self.hosts = {}      # {name: {ip, mac, access, access_port}}
        # every physical inter-device link, ports explicit on both ends
        self.links = []      # [{a, a_port, b, b_port, kind}]

        self._build()

    # ------------------------------------------------------------------ names
    def core_name(self, i):            # i in 1..cores
        return "c%d" % i

    def dist_name(self, pod, plane):   # plane in {A,B}
        return "d%d%s" % (pod, plane.lower())

    def access_name(self, pod, idx):   # idx in 1..access_per_pod
        return "a%d_%d" % (pod, idx)

    def host_name(self, pod, acc, n):  # n in 1..hosts_per_access
        return "h%d_%d_%d" % (pod, acc, n)

    def dc_name(self):
        return "dc_srv"

    def server_name(self, k):          # k in 1..servers
        return "srv%d" % k

    # --------------------------------------------------- department / QoS class
    def dept_of(self, pod):
        """(key, label, qos_class) for a pod, from the DEPARTMENTS rotation."""
        return DEPARTMENTS[(pod - 1) % len(DEPARTMENTS)]

    def qos_class_of(self, pod):
        return self.dept_of(pod)[2]

    def is_dest_tier(self, tier):
        """Tiers that host endpoints and therefore anchor destination forwarding."""
        return tier in (ACCESS, DC)

    # ------------------------------------------------------------------- dpid
    # Deterministic, human-readable DPIDs:
    #   core   c{i}       -> 100 + i          (101..)
    #   dist   d{p}{A|B}  -> 200 + p*10 + {1|2}
    #   access a{p}_{k}   -> 1000 + p*100 + k   (room for up to 99 access/pod)
    def core_dpid(self, i):
        return 100 + i

    def dist_dpid(self, pod, plane):
        return 200 + pod * 10 + (1 if plane == PLANE_A else 2)

    def access_dpid(self, pod, idx):
        return 1000 + pod * 100 + idx

    # ------------------------------------------------------------------ build
    def _add_switch(self, name, dpid, tier, pod=None, plane=None, index=None):
        if name in self.switches:
            raise ValueError("duplicate switch name %s" % name)
        self.switches[name] = dict(dpid=dpid, tier=tier, pod=pod,
                                   plane=plane, index=index)

    def _add_link(self, a, a_port, b, b_port, kind):
        self.links.append(dict(a=a, a_port=a_port, b=b, b_port=b_port, kind=kind))

    def _build(self):
        half = self.cores // 2
        plane_a_cores = list(range(1, half + 1))          # e.g. [1, 2]
        plane_b_cores = list(range(half + 1, self.cores + 1))  # e.g. [3, 4]

        # ---- core switches + full mesh ---------------------------------- #
        for i in range(1, self.cores + 1):
            self._add_switch(self.core_name(i), self.core_dpid(i), CORE, index=i)

        # full mesh; deterministic ports = rank of the peer among sorted peers
        peers = {i: [j for j in range(1, self.cores + 1) if j != i]
                 for i in range(1, self.cores + 1)}
        for i in range(1, self.cores + 1):
            for j in range(i + 1, self.cores + 1):
                ap = CORE_MESH_BASE + peers[i].index(j)
                bp = CORE_MESH_BASE + peers[j].index(i)
                self._add_link(self.core_name(i), ap,
                               self.core_name(j), bp, K_CORE_MESH)

        # ---- per-pod distribution + access + hosts ---------------------- #
        for p in range(1, self.pods + 1):
            da = self.dist_name(p, PLANE_A)
            db = self.dist_name(p, PLANE_B)
            dept_key, dept_label, qos_cls = self.dept_of(p)
            self._add_switch(da, self.dist_dpid(p, PLANE_A), DIST, pod=p, plane=PLANE_A)
            self._add_switch(db, self.dist_dpid(p, PLANE_B), DIST, pod=p, plane=PLANE_B)
            for d in (da, db):
                self.switches[d].update(dept=dept_key, dept_label=dept_label,
                                        qos_class=qos_cls)

            # MLAG lateral peer link between the pod's two dist switches (port 3
            # both ends). Gives a horizontal pivot: if a dist loses its downlink
            # to an access switch, it reaches it across this link via the twin.
            if self.mlag:
                self._add_link(da, DIST_PEER_PORT, db, DIST_PEER_PORT, K_DIST_PEER)

            # dist -> core uplinks (2 per dist: primary=port1, backup=port2)
            for local_port, core_i in enumerate(plane_a_cores, start=DIST_UPLINK_BASE):
                self._add_link(da, local_port,
                               self.core_name(core_i), CORE_DOWNLINK_BASE + (p - 1),
                               K_DIST_CORE)
            for local_port, core_i in enumerate(plane_b_cores, start=DIST_UPLINK_BASE):
                self._add_link(db, local_port,
                               self.core_name(core_i), CORE_DOWNLINK_BASE + (p - 1),
                               K_DIST_CORE)

            # access switches, dual-homed to both dist switches in the pod
            for a in range(1, self.access_per_pod + 1):
                acc = self.access_name(p, a)
                self._add_switch(acc, self.access_dpid(p, a), ACCESS, pod=p, index=a)
                self.switches[acc].update(dept=dept_key, dept_label=dept_label,
                                          qos_class=qos_cls)
                dist_dl_port = DIST_DOWNLINK_BASE + (a - 1)
                self._add_link(acc, ACCESS_UPLINK_A_PORT, da, dist_dl_port, K_ACCESS_DIST)
                self._add_link(acc, ACCESS_UPLINK_B_PORT, db, dist_dl_port, K_ACCESS_DIST)

                # hosts (single-homed)
                for n in range(1, self.hosts_per_access + 1):
                    h = self.host_name(p, a, n)
                    self.hosts[h] = dict(
                        ip="10.%d.%d.%d" % (p, a, n),
                        mac="00:00:00:%02x:%02x:%02x" % (p, a, n),
                        access=acc,
                        access_port=ACCESS_HOST_BASE + (n - 1),
                        dept=dept_key, qos_class=qos_cls,
                    )
                    self._add_link(h, 0, acc, ACCESS_HOST_BASE + (n - 1), K_HOST_ACCESS)

        # ---- optional dedicated data-centre / server tier ------------------ #
        if self.dc:
            self._build_dc(plane_a_cores, plane_b_cores)

    def _build_dc(self, plane_a_cores, plane_b_cores):
        """A data-centre switch dual-homed to one core per plane, hosting servers.
        Modelled as a dest-tier switch (like access) so the controller installs
        protected forwarding toward its servers via the same machinery."""
        dc = self.dc_name()
        self._add_switch(dc, DC_DPID, DC, pod=DC_POD, index=1,
                         )
        self.switches[dc].update(dept="dc", dept_label="Data Center", qos_class=2)
        # uplinks: plane-A first core (primary), plane-B first core (backup)
        ca = self.core_name(plane_a_cores[0])
        cb = self.core_name(plane_b_cores[0])
        dc_core_port = CORE_DOWNLINK_BASE + self.pods     # one past the pod downlinks
        self._add_link(dc, DC_UPLINK_A_PORT, ca, dc_core_port, K_DC_CORE)
        self._add_link(dc, DC_UPLINK_B_PORT, cb, dc_core_port, K_DC_CORE)
        # servers (single-homed to the DC switch)
        for k in range(1, self.servers + 1):
            s = self.server_name(k)
            self.hosts[s] = dict(
                ip="10.%d.1.%d" % (DC_POD, k),
                mac="00:00:00:%02x:01:%02x" % (DC_POD, k),
                access=dc,
                access_port=DC_HOST_BASE + (k - 1),
                dept="dc", qos_class=2,
            )
            self._add_link(s, 0, dc, DC_HOST_BASE + (k - 1), K_HOST_DC)

    # --------------------------------------------------------------- helpers
    def switch_graph(self):
        """Undirected adjacency over *switches only* (hosts excluded)."""
        g = defaultdict(set)
        for name in self.switches:
            g[name]  # ensure isolated nodes appear
        for lk in self.links:
            if lk["kind"] in (K_HOST_ACCESS, K_HOST_DC):
                continue
            g[lk["a"]].add(lk["b"])
            g[lk["b"]].add(lk["a"])
        return g

    def dpid_to_name(self):
        return {attr["dpid"]: name for name, attr in self.switches.items()}

    # BFS shortest path over switches, optionally with some nodes removed.
    def bfs_path(self, src, dst, blocked=frozenset()):
        if src == dst:
            return [src]
        g = self.switch_graph()
        if src in blocked or dst in blocked:
            return None
        seen = {src}
        q = deque([(src, [src])])
        while q:
            node, path = q.popleft()
            for nxt in sorted(g[node]):
                if nxt in seen or nxt in blocked:
                    continue
                if nxt == dst:
                    return path + [nxt]
                seen.add(nxt)
                q.append((nxt, path + [nxt]))
        return None

    def two_node_disjoint_paths(self, src, dst):
        """Return (p1, p2) sharing no *intermediate* switch, or None.

        Constructive check: find a shortest path, remove its internal nodes,
        find a second path.  Proves >=2 node-disjoint paths exist.
        """
        p1 = self.bfs_path(src, dst)
        if not p1:
            return None
        internal = set(p1[1:-1])
        p2 = self.bfs_path(src, dst, blocked=internal)
        if not p2:
            return None
        return p1, p2

    # ------------------------------------------------------ dpid-level views
    # The controller identifies switches by DPID (int), not by name, so it
    # needs a DPID-keyed view of the same topology.  These are derived, not
    # duplicated -- they come straight from ``self.links`` / ``self.switches``.
    def dpid_view(self):
        """Return DPID-keyed structures for the controller.

        adj         : {dpid: {peer_dpid: local_out_port}}
        portmap     : {dpid: {port: ('sw', peer_dpid, peer_port) | ('host', mac)}}
        edges       : set of frozenset({dpid_a, dpid_b}) inter-switch links
        host_by_mac : {mac: (access_dpid, access_port)}
        dpid2name / name2dpid / dpid2tier
        """
        name2dpid = {n: a["dpid"] for n, a in self.switches.items()}
        adj = defaultdict(dict)
        portmap = defaultdict(dict)
        edges = set()
        for lk in self.links:
            if lk["kind"] in (K_HOST_ACCESS, K_HOST_DC):
                mac = self.hosts[lk["a"]]["mac"]
                dp, port = name2dpid[lk["b"]], lk["b_port"]
                portmap[dp][port] = ("host", mac)
                continue
            da, db = name2dpid[lk["a"]], name2dpid[lk["b"]]
            adj[da][db] = lk["a_port"]
            adj[db][da] = lk["b_port"]
            portmap[da][lk["a_port"]] = ("sw", db, lk["b_port"])
            portmap[db][lk["b_port"]] = ("sw", da, lk["a_port"])
            edges.add(frozenset((da, db)))
        host_by_mac = {a["mac"]: (name2dpid[a["access"]], a["access_port"])
                       for a in self.hosts.values()}
        return dict(
            adj={k: dict(v) for k, v in adj.items()},
            portmap={k: dict(v) for k, v in portmap.items()},
            edges=edges,
            host_by_mac=host_by_mac,
            name2dpid=name2dpid,
            dpid2name={v: k for k, v in name2dpid.items()},
            dpid2tier={a["dpid"]: a["tier"] for a in self.switches.values()},
        )

    def _dpid_adj(self, blocked_edges=frozenset(), blocked_nodes=frozenset()):
        adj = defaultdict(set)
        for name, a in self.switches.items():
            if a["dpid"] not in blocked_nodes:
                adj[a["dpid"]]
        for e in self.dpid_view()["edges"]:
            u, v = tuple(e)
            if u in blocked_nodes or v in blocked_nodes or e in blocked_edges:
                continue
            adj[u].add(v)
            adj[v].add(u)
        return adj

    def spanning_tree(self, root_dpid=None):
        """BFS spanning tree over switches; returns set of frozenset tree edges.

        Used for loop-free broadcast: non-tree inter-switch ports are set
        NO_FLOOD so OFPP_FLOOD stays on the tree.
        """
        adj = self._dpid_adj()
        if not adj:
            return set()
        if root_dpid is None:
            root_dpid = min(adj)          # deterministic: lowest core dpid
        tree = set()
        seen = {root_dpid}
        q = deque([root_dpid])
        while q:
            n = q.popleft()
            for m in sorted(adj[n]):
                if m not in seen:
                    seen.add(m)
                    tree.add(frozenset((n, m)))
                    q.append(m)
        return tree

    def shortest_path_dpids(self, src_dpid, dst_dpid,
                            blocked_edges=frozenset(), blocked_nodes=frozenset()):
        """BFS shortest path as a list of DPIDs, or None."""
        if src_dpid == dst_dpid:
            return [src_dpid]
        adj = self._dpid_adj(blocked_edges, blocked_nodes)
        if src_dpid not in adj or dst_dpid not in adj:
            return None
        seen = {src_dpid}
        q = deque([(src_dpid, [src_dpid])])
        while q:
            node, path = q.popleft()
            for nxt in sorted(adj[node]):
                if nxt in seen:
                    continue
                if nxt == dst_dpid:
                    return path + [nxt]
                seen.add(nxt)
                q.append((nxt, path + [nxt]))
        return None

    # --------------------------------------------------------------- export
    def summary(self):
        tiers = defaultdict(int)
        for a in self.switches.values():
            tiers[a["tier"]] += 1
        host_kinds = (K_HOST_ACCESS, K_HOST_DC)
        return dict(
            switches=len(self.switches),
            core=tiers[CORE], dist=tiers[DIST], access=tiers[ACCESS], dc=tiers[DC],
            hosts=len(self.hosts),
            mlag=self.mlag, dc_enabled=self.dc, servers=self.servers,
            links=len(self.links),
            inter_switch_links=sum(1 for l in self.links if l["kind"] not in host_kinds),
        )

    def to_dict(self):
        return dict(
            params=dict(pods=self.pods, access_per_pod=self.access_per_pod,
                        hosts_per_access=self.hosts_per_access, cores=self.cores),
            summary=self.summary(),
            switches=self.switches,
            hosts=self.hosts,
            links=self.links,
        )

    def export_json(self, path):
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return path


def build(**params):
    """Convenience factory using DEFAULT_PARAMS merged with overrides."""
    merged = dict(DEFAULT_PARAMS)
    merged.update(params)
    return CampusTopology(**merged)


if __name__ == "__main__":
    topo = build()
    s = topo.summary()
    print("Campus topology summary:")
    for k, v in s.items():
        print("  %-20s %s" % (k, v))
    out = topo.export_json("topology.json")
    print("Exported model -> %s" % out)
