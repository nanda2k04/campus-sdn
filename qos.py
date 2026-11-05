"""Load- and QoS-aware plane selection for the hybrid campus.

Closes the high-severity routing gaps identified against the literature:

  F1  QoS-aware routing      -- classify flows by DSCP and route delay-sensitive
                                (EF) traffic onto the *least-loaded* plane instead
                                of treating every flow the same.
  F2  Dynamic load-aware     -- the controller already measures per-link Mbit/s
      path selection            for the heatmap; here we FEED THAT BACK into the
                                path decision (the "static vs dynamic LSI" gap).
  F3  Congestion-aware        -- before shifting best-effort traffic onto a plane,
      (post-)reroute            check that plane has headroom; keep EF on the
                                plane with room.
  F4  Weighted multipath      -- best-effort ECMP buckets are weighted by inverse
      (WCMP)                    plane load instead of equal-weight hashing.

Pure logic (stdlib only) so the controller (ryu-venv) and offline tests can both
import it.  The controller owns the OpenFlow install; this module owns the
*decisions* (classification, per-plane load, preferred plane, WCMP weights).

The whole topology is two node-disjoint planes (A, B).  A flow's plane is decided
at its SOURCE access switch: uplink port 1 -> plane A, port 2 -> plane B
(campus_model.ACCESS_UPLINK_A_PORT / _B_PORT).  A dist-B switch only connects to
plane-B cores, so once a packet enters a plane the destination-rooted forwarding
keeps it there -- which is why pinning the plane at the first hop is sufficient.
"""
from __future__ import annotations

import campus_model as cm

# ---- DiffServ classes (the three we route differently) --------------------- #
# DSCP values follow RFC 4594 defaults.  iperf can mark traffic with --tos
# (tos = dscp << 2), e.g. EF = dscp 46 -> --tos 184.
DSCP_EF = 46          # Expedited Forwarding: voice / interactive / critical
DSCP_AF = 26          # Assured Forwarding (AF31): important bulk
CLASS_EF = "EF"
CLASS_AF = "AF"
CLASS_BE = "BE"       # Best Effort: everything else


def classify_dscp(dscp):
    """Map an IP DSCP value to one of our three service classes."""
    if dscp is None:
        return CLASS_BE
    if dscp >= DSCP_EF:
        return CLASS_EF
    if dscp >= 18:                 # AF21/AF31 and up
        return CLASS_AF
    return CLASS_BE


# ---- app-aware DPI: L4 signature -> (service class, OVS queue id) ----------- #
# Classifies traffic by transport protocol + well-known port WITHOUT needing the
# sender to mark DSCP (deep-ish packet inspection on the 5-tuple), then maps it to
# a priority queue.  proto: 6=TCP, 17=UDP.  queue: 2=high, 1=med, 0=low.
IPPROTO_TCP, IPPROTO_UDP = 6, 17
APP_SIGS = [
    # (proto, l4_dst_port, queue, label)
    (IPPROTO_UDP, 5004, 2, "rtp-voice-video"),   # RTP media  -> high
    (IPPROTO_UDP, 5060, 2, "sip"),               # SIP        -> high
    (IPPROTO_TCP, 5061, 2, "sip-tls"),           # SIP-TLS    -> high
    (IPPROTO_TCP,  443, 2, "https"),             # HTTPS      -> high
    (IPPROTO_TCP,   80, 1, "http"),              # HTTP       -> med
    (IPPROTO_TCP,   21, 0, "ftp-bulk"),          # FTP bulk   -> low
    (IPPROTO_TCP,  873, 0, "rsync-bulk"),        # rsync bulk -> low
]
QUEUE_HIGH, QUEUE_MED, QUEUE_LOW = 2, 1, 0


def plane_of_dist(model, dist_name):
    return model.switches[dist_name]["plane"]


def plane_links(model):
    """{plane: [frozenset({dpid_a, dpid_b}), ...]} -- the access<->dist and
    dist<->core links that belong to each plane (by their distribution-switch
    endpoint).  Core-mesh links are cross-plane and excluded from the per-plane
    load metric.  Used to size each plane's live utilisation."""
    name2dpid = {n: a["dpid"] for n, a in model.switches.items()}
    out = {cm.PLANE_A: [], cm.PLANE_B: []}
    for lk in model.links:
        if lk["kind"] not in (cm.K_ACCESS_DIST, cm.K_DIST_CORE):
            continue
        # the distribution endpoint carries the plane label
        for end in (lk["a"], lk["b"]):
            attr = model.switches.get(end)
            if attr and attr["tier"] == cm.DIST:
                plane = attr["plane"]
                da, db = name2dpid[lk["a"]], name2dpid[lk["b"]]
                out[plane].append(frozenset((da, db)))
                break
    return out


class PlaneBalancer:
    """Turns live per-link Mbit/s into a plane decision, with hysteresis.

    The controller calls ``update(link_mbps_fn)`` on its stats cadence;
    ``preferred`` is the plane EF/critical traffic should ride, and
    ``weights`` gives WCMP bucket weights for best-effort splitting.
    """

    def __init__(self, model, hysteresis_mbps=2.0):
        self.model = model
        self._links = plane_links(model)
        self.hysteresis = hysteresis_mbps
        self.load = {cm.PLANE_A: 0.0, cm.PLANE_B: 0.0}     # bottleneck Mbit/s
        self.total = {cm.PLANE_A: 0.0, cm.PLANE_B: 0.0}    # summed Mbit/s
        self.preferred = cm.PLANE_A                        # start on A (status quo)

    def update(self, link_mbps_fn):
        """Recompute plane loads from a fn(dpid_a, dpid_b)->Mbit/s.

        Returns True if the preferred plane changed (so the caller can repin EF).
        """
        for plane, links in self._links.items():
            peak = tot = 0.0
            for edge in links:
                a, b = tuple(edge)
                m = link_mbps_fn(a, b)
                tot += m
                if m > peak:
                    peak = m
            self.load[plane] = round(peak, 3)
            self.total[plane] = round(tot, 3)

        # hysteresis: only flip when the other plane is clearly quieter, so a
        # near-tie does not cause EF flows to flap between planes.
        a, b = self.load[cm.PLANE_A], self.load[cm.PLANE_B]
        new = self.preferred
        if self.preferred == cm.PLANE_A and b + self.hysteresis < a:
            new = cm.PLANE_B
        elif self.preferred == cm.PLANE_B and a + self.hysteresis < b:
            new = cm.PLANE_A
        changed = new != self.preferred
        self.preferred = new
        return changed

    def has_headroom(self, plane, cap_mbps):
        """True if ``plane``'s bottleneck link is below ~80% of the cap (or cap
        unknown).  Used to decide whether a reroute would congest the target."""
        if not cap_mbps or cap_mbps <= 0:
            return True
        return self.load[plane] < 0.8 * cap_mbps

    def wcmp_weights(self, ports_by_plane):
        """Weighted-ECMP bucket weights (1..W) inversely proportional to plane
        load, for best-effort SELECT groups (closes F4).  ``ports_by_plane`` is
        {plane: out_port}; returns [(out_port, weight), ...].  Quieter plane gets
        the larger weight; equal load -> equal weight."""
        a, b = self.total[cm.PLANE_A], self.total[cm.PLANE_B]
        tot = a + b
        out = []
        for plane, port in ports_by_plane.items():
            other = b if plane == cm.PLANE_A else a
            if tot <= 0:
                w = 1
            else:
                # quieter plane (smaller own-load) gets more weight; clamp 1..16
                frac = other / tot            # share of load on the OTHER plane
                w = max(1, min(16, int(round(1 + 15 * frac))))
            out.append((port, w))
        return out

    def snapshot(self):
        return dict(
            preferred=self.preferred,
            bottleneck_mbps=dict(self.load),
            total_mbps=dict(self.total))
