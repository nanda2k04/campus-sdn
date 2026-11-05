"""Phase 3 -- topology-aware L2 controller for the hybrid campus (Ryu, OF1.3).

Design goals (Phase 1 sections 5, 6, 11, 12, 13):
  * The controller holds network-wide state from ``campus_model`` (the single
    source of truth) -- NOT independent per-switch learning.
  * Unicast is forwarded along controller-computed SHORTEST PATHS
    (installed as exact eth_dst flows), never flooded.
  * Broadcast/ARP is made loop-free WITHOUT spanning-tree protocol chatter:
    we compute one spanning tree from the model and install explicit
    per-in_port broadcast flows that output only to that switch's tree/host
    ports minus the ingress (OF1.3 has no per-port NO_FLOOD flag).
  * MAC/host learning is real: access switches copy broadcast frames to the
    controller, which records host_mac -> (dpid, port) and logs HOST_LEARNED.

This phase deliberately contains NO failover logic yet -- baseline connectivity
must be proven first (the section 12 gate).  Flows are installed permanently
(no timeout); Phase 7 will delete/recompute them on failure.

Run (from the project dir, with the ryu venv):
    ./ryu-venv/bin/ryu-manager --ofp-tcp-listen-port 6653 controller.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections import defaultdict, deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER,
                                     DEAD_DISPATCHER, set_ev_cls)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub
from ryu.topology import event as topo_event
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response

import campus_model as cm
import paths
import qos

REST_INSTANCE = "campus_app"

PRIO_MISS = 0
PRIO_IPV6_DROP = 5
PRIO_BROADCAST = 10
PRIO_UNICAST = 20         # destination /40 prefix (best-effort) forwarding
PRIO_QOS_EF = 30          # per-access EF (critical) plane-pin, above best-effort
PRIO_DPI = 35             # app-aware L4 classification, above EF, below local
PRIO_LOCAL = 40           # exact-MAC local host delivery, above the EF plane-pin


class CampusController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(CampusController, self).__init__(*args, **kwargs)

        # --- network-wide state, seeded from the single source of truth ---
        # Scale MUST match the running Mininet topology; the runner exports these.
        params = dict(
            pods=int(os.environ.get("CAMPUS_PODS", cm.DEFAULT_PARAMS["pods"])),
            access_per_pod=int(os.environ.get("CAMPUS_ACCESS_PER_POD",
                                              cm.DEFAULT_PARAMS["access_per_pod"])),
            hosts_per_access=int(os.environ.get("CAMPUS_HOSTS_PER_ACCESS",
                                                cm.DEFAULT_PARAMS["hosts_per_access"])),
            cores=int(os.environ.get("CAMPUS_CORES", cm.DEFAULT_PARAMS["cores"])),
            # Superset features MUST match topology.py's env reads, or the
            # controller's world-model diverges from the live datapath: the DC
            # switch (dpid 50) and MLAG peer links would be unknown to the
            # controller, so the DC gets no dest group and servers are
            # unreachable. Keep these three lines in lock-step with topology.py.
            mlag=os.environ.get("CAMPUS_MLAG") == "1",
            dc=os.environ.get("CAMPUS_DC") == "1",
            servers=int(os.environ.get("CAMPUS_SERVERS", "3")),
        )
        self.model = cm.build(**params)
        self.logger.info("controller model params: %s", params)
        v = self.model.dpid_view()
        self.adj = v["adj"]                  # {dpid: {peer_dpid: out_port}}
        self.portmap = v["portmap"]          # {dpid: {port: (...)}}
        self.host_by_mac = v["host_by_mac"]  # authoritative host locations
        self.dpid2name = v["dpid2name"]
        self.dpid2tier = v["dpid2tier"]
        self.tree_edges = self.model.spanning_tree()   # loop-free broadcast tree

        # Per-switch broadcast flood set = spanning-tree ports + host ports.
        # OF1.3 has no per-port NO_FLOOD flag, so instead of OFPP_FLOOD we
        # install explicit per-in_port flows that output to (flood_ports - in_port),
        # which keeps broadcast strictly on the acyclic tree (no loops, no storm).
        self.flood_ports = {}                # dpid -> sorted list of ports
        for dpid, ports in self.portmap.items():
            fp = []
            for port, entry in ports.items():
                if entry[0] == "host":
                    fp.append(port)
                elif entry[0] == "sw" and frozenset((dpid, entry[1])) in self.tree_edges:
                    fp.append(port)
            self.flood_ports[dpid] = sorted(fp)

        self.datapaths = {}                  # dpid -> datapath (connected)
        self.mac_to_loc = {}                 # LEARNED mac -> (dpid, in_port)
        self.installed_pairs = set()         # (src_mac, dst_mac) already pathed

        # --- Phase 5/6: path computation + fast-failover protected forwarding ---
        # CAMPUS_FF=0 falls back to plain shortest-path (Phase 3 behaviour).
        self.enable_ff = os.environ.get("CAMPUS_FF", "1") != "0"
        # CAMPUS_LB=1: spread traffic across BOTH disjoint planes with SELECT
        # groups (ECMP) instead of primary/backup FF groups -- uses every link.
        self.enable_lb = os.environ.get("CAMPUS_LB") == "1"
        # CAMPUS_QOS=1: load- & QoS-aware plane selection (gaps F1-F4).
        #   * EF/critical flows (DSCP 46) are pinned to the least-loaded plane,
        #   * best-effort SELECT buckets are WCMP-weighted by inverse plane load,
        #   * reroutes check target-plane headroom (CAMPUS_BW = per-link cap).
        self.enable_qos = os.environ.get("CAMPUS_QOS") == "1"
        # CAMPUS_DPI=1: app-aware classification (L4 5-tuple) steers recognised
        # apps to the preferred plane; CAMPUS_QOS_QUEUES=1 additionally marks an
        # OVS priority queue (needs queues configured on ports by topology.py).
        self.enable_dpi = os.environ.get("CAMPUS_DPI") == "1"
        self.enable_queues = os.environ.get("CAMPUS_QOS_QUEUES") == "1"
        self.link_cap = float(os.environ.get("CAMPUS_BW", "0") or 0)
        self.balancer = qos.PlaneBalancer(self.model)
        # per access switch: which uplink port reaches each plane, and the dist
        # dpid on the far end (to tell if that plane's uplink is currently down).
        self.access_uplinks = {}             # acc_dpid -> {plane: (port, dist_dpid)}
        for dpid, ports in self.portmap.items():
            # ACCESS uses ports 1/2 for plane-A/B dist uplinks; the DC switch uses
            # ports 1/2 for its plane-A/B core uplinks -- same plane semantics, so
            # EF plane-pinning works for both.
            if not self.model.is_dest_tier(self.dpid2tier.get(dpid, "")):
                continue
            up = {}
            pa = ports.get(cm.ACCESS_UPLINK_A_PORT)
            pb = ports.get(cm.ACCESS_UPLINK_B_PORT)
            if pa and pa[0] == "sw":
                up[cm.PLANE_A] = (cm.ACCESS_UPLINK_A_PORT, pa[1])
            if pb and pb[0] == "sw":
                up[cm.PLANE_B] = (cm.ACCESS_UPLINK_B_PORT, pb[1])
            self.access_uplinks[dpid] = up
        self.qos_pin = {}                    # acc_dpid -> plane currently pinned
        self.pc = paths.PathComputer(self.adj)
        self.installed_dests = set()         # access dpids with protected fwding
        self.dest_plans = {}                 # access dpid -> per-switch plan
        self.installed_groups = set()        # (dpid, group_id) already added
        # Phase 7: live failure state -> drives controller rerouting
        self.down_edges = set()              # frozenset({dpid_a, dpid_b})
        self.down_nodes = set()              # dpid
        # deterministic group_id per destination access; masked MAC per access
        self.access_gid = {}
        self.access_prefix = {}
        self.hosts_on_access = defaultdict(list)
        gid = 1
        for name, attr in sorted(self.model.switches.items(),
                                 key=lambda kv: kv[1]["dpid"]):
            # ACCESS and DC switches both anchor destinations (hosts/servers),
            # so both get a /40 group id + MAC prefix for aggregated forwarding.
            if self.model.is_dest_tier(attr["tier"]):
                self.access_gid[attr["dpid"]] = gid
                self.access_prefix[attr["dpid"]] = (
                    "00:00:00:%02x:%02x:00" % (attr["pod"], attr["index"]),
                    "ff:ff:ff:ff:ff:00")
                gid += 1
        for hname, h in self.model.hosts.items():
            adpid = self.model.switches[h["access"]]["dpid"]
            self.hosts_on_access[adpid].append((h["mac"], h["access_port"]))

        # --- Phase 4: dynamic (LLDP) discovery + reconciliation vs the model ---
        # expected_links: directed inter-switch links the model says SHOULD exist
        self.expected_links = self._expected_directed_links()
        # observed_links: what LLDP actually discovers, (src_dpid,src_port)->(dst_dpid,dst_port)
        self.observed_links = {}
        self._last_reconcile = None
        hub.spawn(self._reconcile_loop)

        # --- live link-utilisation (port-stats polling) for the heatmap ---
        self.port_bytes = {}                 # (dpid, port) -> (bytes, ts)
        self.port_bps = {}                   # (dpid, port) -> bits/sec (recent)
        self.stats_interval = float(os.environ.get("CAMPUS_STATS_S", "2"))
        hub.spawn(self._stats_loop)
        if self.enable_qos or self.enable_dpi:
            hub.spawn(self._qos_loop)

        # --- R1: controller high-availability (removes the single-controller SPOF)
        # Two controllers run the same app; every switch is connected to BOTH
        # (topology.py --ha).  The primary is active; the backup stays passive
        # (connected but does not write flows) until it detects the primary is
        # gone, then promotes itself and takes over rerouting.  CAMPUS_HA=1 arms
        # this; without it the controller behaves exactly as before (role=single).
        self.ha_enabled = os.environ.get("CAMPUS_HA") == "1"
        self.ha_role = os.environ.get("CAMPUS_ROLE", "primary") if self.ha_enabled \
            else "single"
        self.ha_active = (self.ha_role != "backup")   # backup starts passive
        self.ha_peer_api = os.environ.get("CAMPUS_PEER_API", "")
        self.ha_hb_s = float(os.environ.get("CAMPUS_HB_MS", "700")) / 1000.0
        self.ha_misses_max = int(os.environ.get("CAMPUS_HB_MISSES", "3"))
        self._ha_misses = 0
        if self.ha_enabled:
            hub.spawn(self._ha_loop)

        # CAMPUS_PROACTIVE=1: install forwarding for EVERY destination once all
        # switches connect, instead of waiting for the first packet. Essential at
        # scale, where a full pingall (O(hosts^2)) is impractical -- this is O(1)
        # per destination and makes all FF/SELECT groups appear immediately.
        self.enable_proactive = os.environ.get("CAMPUS_PROACTIVE") == "1"
        if self.enable_proactive:
            hub.spawn(self._proactive_loop)

        # --- Phase 9: REST API state ---
        self.started_at = time.time()
        self.event_log = deque(maxlen=2000)   # ring buffer served by /api/events
        self._event_seq = 0
        self.port_hw = {}                     # (dpid, port) -> hw_addr, for port-mod
        wsgi = kwargs["wsgi"]
        wsgi.register(CampusRestController, {REST_INSTANCE: self})

        self.logger.info("controller init: %s | tree_edges=%d | expected_links=%d",
                          self.model.summary(), len(self.tree_edges),
                          len(self.expected_links))

    # ------------------------------------------- Phase 4: discovery helpers
    def _expected_directed_links(self):
        """Directed inter-switch links from the model: {(a,ap,b,bp)}."""
        exp = set()
        for dpid, ports in self.portmap.items():
            for port, entry in ports.items():
                if entry[0] == "sw":
                    exp.add((dpid, port, entry[1], entry[2]))
        return exp

    def _link_matches_model(self, a, ap, b, bp):
        return (a, ap, b, bp) in self.expected_links

    # ------------------------------------------------------------- events
    def ev(self, etype, **kw):
        """Structured event log (feeds the Phase 9 API / Phase 10 dashboard)."""
        self._event_seq += 1
        rec = dict(seq=self._event_seq, ts=round(time.time(), 4), event=etype)
        rec.update(kw)
        self.logger.info("EVENT %s", json.dumps(rec))
        self.event_log.append(rec)

    def sw_name(self, dpid):
        return self.dpid2name.get(dpid, "dpid:%d" % dpid)

    # ----------------------------------------------------- datapath registry
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER and dp.id is not None:
            self.datapaths[dp.id] = dp
            if dp.id in self.down_nodes:      # a failed switch came back
                self._handle_switch_recovered(dp.id)
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            # forget this switch's installed groups so a reconnect re-adds them
            self.installed_groups = {k for k in self.installed_groups
                                     if k[0] != dp.id}
            self.ev("SWITCH_DISCONNECTED", dpid=dp.id, name=self.sw_name(dp.id))
            if dp.id not in self.down_nodes:
                self.down_nodes.add(dp.id)
                self.ev("FAILOVER_TRIGGERED", kind="switch",
                        switch=self.sw_name(dp.id))
                self._reroute("SWITCH_DOWN")

    def _handle_switch_recovered(self, dpid):
        """A previously-dead switch reconnected: it lost all flows, so force its
        entries to be reinstalled by dropping them from every cached plan."""
        self.down_nodes.discard(dpid)
        for access in self.installed_dests:
            self.dest_plans.get(access, {}).pop(dpid, None)
        self.ev("SWITCH_RECOVERED", dpid=dpid, name=self.sw_name(dpid))
        self._reroute("SWITCH_RECOVERED")

    # ---------------------------------- Phase 4: LLDP topology discovery
    # These events are produced by ryu.topology.switches, which is activated by
    # running ryu-manager with --observe-links.  LLDP links are DIRECTED (one
    # event per direction), so a physical link yields two.
    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        s, d = ev.link.src, ev.link.dst
        edge = frozenset((s.dpid, d.dpid))
        recovered = edge in self.down_edges
        if recovered:
            self.down_edges.discard(edge)

        key = (s.dpid, s.port_no)
        if self.observed_links.get(key) != (d.dpid, d.port_no):
            self.observed_links[key] = (d.dpid, d.port_no)
            match = "expected" if self._link_matches_model(
                s.dpid, s.port_no, d.dpid, d.port_no) else "UNEXPECTED"
            self.ev("LINK_DISCOVERED", src=self.sw_name(s.dpid), src_port=s.port_no,
                    dst=self.sw_name(d.dpid), dst_port=d.port_no, match=match)

        if recovered:                          # only fires for a link we had marked down
            self.ev("LINK_RECOVERED", src=self.sw_name(s.dpid),
                    dst=self.sw_name(d.dpid))
            self._reroute("LINK_RECOVERED")

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete(self, ev):
        s, d = ev.link.src, ev.link.dst
        self.observed_links.pop((s.dpid, s.port_no), None)
        self.ev("LINK_DOWN", src=self.sw_name(s.dpid), src_port=s.port_no,
                dst=self.sw_name(d.dpid), dst_port=d.port_no)
        # event-driven controller reaction (dedup across the two directions)
        edge = frozenset((s.dpid, d.dpid))
        if edge not in self.down_edges:
            self.down_edges.add(edge)
            self.ev("FAILOVER_TRIGGERED", kind="link",
                    link=[self.sw_name(s.dpid), self.sw_name(d.dpid)])
            self._reroute("LINK_DOWN")

    @set_ev_cls(topo_event.EventPortModify)
    def _port_modify(self, ev):
        p = ev.port
        entry = self.portmap.get(p.dpid, {}).get(p.port_no)
        peer = self.sw_name(entry[1]) if entry and entry[0] == "sw" else None
        self.ev("PORT_STATE", dpid=p.dpid, name=self.sw_name(p.dpid),
                port=p.port_no, peer=peer, live=(not p.is_down()))

    def _reconcile_loop(self):
        """Periodically compare discovered topology against the model."""
        while True:
            hub.sleep(10)
            obs = set((a, ap, b, bp)
                      for (a, ap), (b, bp) in self.observed_links.items())
            matched = obs & self.expected_links
            missing = self.expected_links - obs
            extra = obs - self.expected_links
            summary = dict(expected=len(self.expected_links), observed=len(obs),
                           matched=len(matched), missing=len(missing),
                           extra=len(extra))
            if summary == self._last_reconcile:
                continue                     # only log when something changed
            self._last_reconcile = summary
            self.ev("RECONCILE", converged=(not missing and not extra),
                    missing_sample=[(self.sw_name(a), ap, self.sw_name(b), bp)
                                    for (a, ap, b, bp) in list(missing)[:4]],
                    extra_sample=[(self.sw_name(a), ap, self.sw_name(b), bp)
                                  for (a, ap, b, bp) in list(extra)[:4]],
                    **summary)

    # --------------------------------------------------------- switch setup
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser

        # table-miss -> controller
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                              ofp.OFPCML_NO_BUFFER)])

        # drop IPv6 entirely (this is an IPv4 project) -- kills the host boot-time
        # multicast/DAD chatter so it can neither flood nor overload the controller
        self.add_flow(dp, PRIO_IPV6_DROP,
                      parser.OFPMatch(eth_type=0x86dd), [])

        # explicit, loop-free broadcast: one flow per possible ingress port,
        # each outputting to the switch's tree/host ports minus that ingress.
        self._install_broadcast_tree(dp)

        # fetch port descriptions to cache hw_addr for OFPPortMod (API failures)
        dp.send_msg(parser.OFPPortDescStatsRequest(dp, 0))

        # QoS: pin this access switch's EF (critical) traffic to the least-loaded
        # plane (one rule per access switch; repinned by _qos_loop on load change).
        if (self.enable_qos or self.enable_dpi) and dp.id in self.access_uplinks:
            self._install_qos_plane(dp)

        self.ev("SWITCH_CONNECTED", dpid=dp.id, name=self.sw_name(dp.id),
                tier=self.dpid2tier.get(dp.id))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_cache(self, ev):
        """Cache each port's hw_addr so the API can send OFPPortMod (PORT_DOWN)."""
        dp = ev.msg.datapath
        for p in ev.msg.body:
            self.port_hw[(dp.id, p.port_no)] = p.hw_addr

    # -------------------------------------------- Phase 9: failure injection
    def api_set_link(self, a_name, b_name, down):
        """Administratively down/up the a->b port via OpenFlow (real failure)."""
        if a_name not in self.model.switches or b_name not in self.model.switches:
            return False, "unknown switch"
        da = self.model.switches[a_name]["dpid"]
        db = self.model.switches[b_name]["dpid"]
        port = self.adj.get(da, {}).get(db)
        if port is None:
            return False, "no link %s-%s" % (a_name, b_name)
        ok = self._port_mod(da, port, down)
        if ok:
            self.ev("API_LINK_DOWN" if down else "API_LINK_UP",
                    link=[a_name, b_name], via_dpid=da, port=port)
        return ok, "ok" if ok else "switch/port not ready"

    def api_set_switch(self, name, down):
        """Isolate/restore a switch by port-modding all its inter-switch ports."""
        if name not in self.model.switches:
            return False, "unknown switch"
        d = self.model.switches[name]["dpid"]
        n = 0
        for port, entry in self.portmap.get(d, {}).items():
            if entry[0] == "sw" and self._port_mod(d, port, down):
                n += 1
        self.ev("API_SWITCH_DOWN" if down else "API_SWITCH_UP",
                switch=name, ports=n)
        return (n > 0), ("ok" if n else "switch not ready")

    def api_install_all(self):
        """Proactively install forwarding toward every destination (so all
        FF/SELECT groups exist without sending traffic).  Safe to call repeatedly."""
        before = len(self.installed_dests)
        self._install_all_dests()
        self.ev("PROACTIVE_INSTALL", dests=len(self.installed_dests),
                groups=len(self.installed_groups), via="api")
        return True, ("installed %d dests (%d new), %d groups"
                      % (len(self.installed_dests),
                         len(self.installed_dests) - before,
                         len(self.installed_groups)))

    def api_reset(self):
        """Restore the whole fabric and clear the event log -- a one-click 'start
        fresh' so the demo need not be restarted from the terminal. Brings every
        downed link/switch back up (real OpenFlow port-mod up), clears the down
        sets, reinstalls clean forwarding, and empties the event ring buffer.
        Learned-host state is kept (connectivity stays live)."""
        restored_links = 0
        for edge in list(self.down_edges):
            a, b = tuple(edge)
            for u, w in ((a, b), (b, a)):
                port = self.adj.get(u, {}).get(w)
                if port is not None and self._port_mod(u, port, False):
                    restored_links += 1
        restored_sw = 0
        for d in list(self.down_nodes):
            for port, entry in self.portmap.get(d, {}).items():
                if entry[0] == "sw" and self._port_mod(d, port, False):
                    restored_sw += 1
        self.down_edges.clear()
        self.down_nodes.clear()
        self._reroute("NETWORK_RESET")       # reinstall clean flows (logs, then cleared)
        self.event_log.clear()               # start the log fresh
        self.ev("NETWORK_RESET",
                note="all links/switches restored, event log cleared")
        return True, "reset"

    def _port_mod(self, dpid, port, down):
        dp = self.datapaths.get(dpid)
        hw = self.port_hw.get((dpid, port))
        if dp is None or hw is None:
            return False
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPortMod(
            datapath=dp, port_no=port, hw_addr=hw,
            config=(ofp.OFPPC_PORT_DOWN if down else 0),
            mask=ofp.OFPPC_PORT_DOWN, advertise=0))
        return True

    def _install_broadcast_tree(self, dp):
        """Install per-in_port broadcast flows confined to the spanning tree."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        fports = self.flood_ports.get(dp.id, [])
        pm = self.portmap.get(dp.id, {})
        for in_port in fports:
            actions = []
            # copy to controller ONLY when the broadcast originates locally
            # (ingress is a host port) -- copying transit broadcasts arriving
            # on an uplink would relearn the sender at the wrong port (flapping).
            if pm.get(in_port, (None,))[0] == "host":
                actions.append(parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                                       ofp.OFPCML_NO_BUFFER))
            actions += [parser.OFPActionOutput(p) for p in fports if p != in_port]
            self.add_flow(dp, PRIO_BROADCAST,
                          parser.OFPMatch(in_port=in_port,
                                          eth_dst="ff:ff:ff:ff:ff:ff"),
                          actions)
        self.ev("BROADCAST_TREE_APPLIED", dpid=dp.id, name=self.sw_name(dp.id),
                flood_ports=fports)

    # ------------------------------------------------------------- flow util
    def add_flow(self, dp, priority, match, actions, idle=0, hard=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst,
                                      idle_timeout=idle, hard_timeout=hard))

    def _install_path(self, path, dst_mac, final_port):
        """Install eth_dst=dst_mac -> next-hop-port on every switch in path."""
        for i, node in enumerate(path):
            dp = self.datapaths.get(node)
            if dp is None:
                return False
            if node == path[-1]:
                out_port = final_port
            else:
                out_port = self.adj[node][path[i + 1]]
            parser = dp.ofproto_parser
            self.add_flow(dp, PRIO_UNICAST,
                          parser.OFPMatch(eth_dst=dst_mac),
                          [parser.OFPActionOutput(out_port)])
        return True

    # ------------------------------------------------------------ packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # HA: a passive backup stays connected but installs no forwarding -- only
        # the active controller writes per-flow state (avoids two controllers
        # fighting over the same switches).  Static infra is installed by both.
        if not self.ha_active:
            return

        src, dst = eth.src, eth.dst
        dpid = dp.id

        # --- learn source location (real MAC learning) ---
        # ONLY trust a host-port ingress: a host MAC seen on an inter-switch port
        # is transit (e.g. a table-miss at a distribution switch before forwarding
        # is installed) and must NOT be learned there -- otherwise mac_to_loc maps
        # the host to a non-access switch and downstream lookups break.
        ingress = self.portmap.get(dpid, {}).get(in_port)
        host_ingress = ingress is not None and ingress[0] == "host"
        if (host_ingress and not _is_multicast(src)
                and self.mac_to_loc.get(src) != (dpid, in_port)):
            first = src not in self.mac_to_loc
            self.mac_to_loc[src] = (dpid, in_port)
            if first:
                self.ev("HOST_LEARNED", mac=src, dpid=dpid,
                        name=self.sw_name(dpid), port=in_port)

        # --- broadcast: already flooded on the tree by the proactive flow ---
        if dst == "ff:ff:ff:ff:ff:ff":
            return
        if _is_multicast(dst):
            return                          # no IPv4 dependence on multicast here

        # --- unicast ---
        loc = self.mac_to_loc.get(dst) or self.host_by_mac_loc(dst)
        if loc is None:
            # host_by_mac is seeded from the model (all hosts known), so this is
            # unexpected. Drop rather than OFPP_FLOOD (which in OF1.3 has no
            # NO_FLOOD guard and would loop on the redundant fabric).
            self.ev("UNKNOWN_DEST", dst_mac=dst, at=self.sw_name(dpid))
            return
        dst_dpid, dst_port = loc

        if self.enable_ff:
            self._forward_ff(dp, in_port, msg, src, dst, dpid, dst_dpid, dst_port)
        else:
            self._forward_plain(dp, in_port, msg, src, dst, dpid, dst_dpid, dst_port)

    # -------------------------------------------------- forwarding strategies
    def _forward_plain(self, dp, in_port, msg, src, dst, dpid, dst_dpid, dst_port):
        """Phase 3 fallback: single shortest path, plain output, no protection."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        path = self.model.shortest_path_dpids(dpid, dst_dpid)
        if not path:
            self.ev("NO_PATH", src=self.sw_name(dpid), dst=self.sw_name(dst_dpid))
            return
        pair = (src, dst)
        if pair not in self.installed_pairs:
            ok = self._install_path(path, dst, dst_port)
            src_loc = self.mac_to_loc.get(src)
            if ok and src_loc:
                self._install_path(list(reversed(path)), src, src_loc[1])
            if ok:
                self.installed_pairs.add(pair)
                self.ev("PATH_INSTALLED", src_mac=src, dst_mac=dst,
                        path=[self.sw_name(d) for d in path], hops=len(path))
        out_port = dst_port if len(path) == 1 else self.adj[dpid][path[1]]
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)], data=data))

    def _forward_ff(self, dp, in_port, msg, src, dst, dpid, dst_dpid, dst_port):
        """Phase 6: protected destination-rooted forwarding via FF groups."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # install protected forwarding toward the destination (and back to src)
        self._install_protected_dest(dst_dpid)
        src_loc = self.mac_to_loc.get(src) or self.host_by_mac_loc(src)
        if src_loc:
            self._install_protected_dest(src_loc[0])

        pair = (src, dst)
        if pair not in self.installed_pairs:
            pri, bak = self.pc.disjoint_pair(dpid, dst_dpid)
            self.ev("PRIMARY_PATH", src_mac=src, dst_mac=dst,
                    path=[self.sw_name(x) for x in (pri or [])])
            if bak:
                self.ev("BACKUP_PATH", src_mac=src, dst_mac=dst,
                        path=[self.sw_name(x) for x in bak])
            self.installed_pairs.add(pair)

        # forward the triggering packet toward the destination now
        if dpid == dst_dpid:
            out_port = dst_port
        else:
            plan = self.dest_plans.get(dst_dpid) or {}
            entry = plan.get(dpid)
            out_port = entry["primary"] if entry else None
        if out_port is None:
            return
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)], data=data))

    def _set_ff_group(self, dp, gid, primary, backup):
        """Add or MODIFY an OFPGT_FF group: bucket0=primary, bucket1=backup.

        The datapath uses bucket0 while its watch_port is live, else bucket1 --
        local fast-failover with no controller in the loop.  On reroute the
        buckets change, so we MODIFY an already-present group.
        """
        ofp, parser = dp.ofproto, dp.ofproto_parser
        buckets = [parser.OFPBucket(
            watch_port=primary, watch_group=ofp.OFPG_ANY,
            actions=[parser.OFPActionOutput(primary)])]
        if backup is not None:
            buckets.append(parser.OFPBucket(
                watch_port=backup, watch_group=ofp.OFPG_ANY,
                actions=[parser.OFPActionOutput(backup)]))
        key = (dp.id, gid)
        cmd = ofp.OFPGC_MODIFY if key in self.installed_groups else ofp.OFPGC_ADD
        dp.send_msg(parser.OFPGroupMod(
            datapath=dp, command=cmd, type_=ofp.OFPGT_FF,
            group_id=gid, buckets=buckets))
        self.installed_groups.add(key)

    def _set_select_group(self, dp, gid, ports, weights=None):
        """Add/MODIFY an OFPGT_SELECT group.  OVS hashes each flow onto a bucket,
        so flows spread across both disjoint planes (ECMP) while a single flow
        stays on one path.  With ``weights`` (a {port: weight} map) the buckets
        are WCMP-weighted -- the quieter plane gets more flows (gap F4)."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        buckets = [parser.OFPBucket(
            weight=(weights.get(p, 1) if weights else 1),
            actions=[parser.OFPActionOutput(p)]) for p in ports]
        key = (dp.id, gid)
        cmd = ofp.OFPGC_MODIFY if key in self.installed_groups else ofp.OFPGC_ADD
        dp.send_msg(parser.OFPGroupMod(
            datapath=dp, command=cmd, type_=ofp.OFPGT_SELECT,
            group_id=gid, buckets=buckets))
        self.installed_groups.add(key)

    def _install_node_entry(self, dp, gid, prefix, mask, pf):
        """Install one switch's forwarding toward a destination access switch.

        ECMP (CAMPUS_LB) splits traffic ONLY at the source access switch, across
        its two plane uplinks.  Each plane is a node-disjoint single-path tree to
        the destination, so this is loop-free.  Splitting at every node instead
        would loop, because plan_dest_tree's `backup` is a failover alternate
        (safe only when the primary is down), not a concurrent equal-cost path.
        """
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_dst=(prefix, mask))
        if pf["backup"] is not None:
            if self.enable_lb and dp.id in self.access_gid:
                # access switch: hash flows across plane-A vs plane-B uplink.
                # With QoS on, weight the two buckets by inverse plane load (WCMP)
                # so the quieter plane takes more best-effort flows.
                weights = None
                if self.enable_qos:
                    pbp = {cm.PLANE_A: pf["primary"], cm.PLANE_B: pf["backup"]}
                    weights = dict(self.balancer.wcmp_weights(pbp))
                self._set_select_group(dp, gid, [pf["primary"], pf["backup"]],
                                       weights=weights)
                self.add_flow(dp, PRIO_UNICAST, match, [parser.OFPActionGroup(gid)])
                return "select"
            if not self.enable_lb:
                self._set_ff_group(dp, gid, pf["primary"], pf["backup"])
                self.add_flow(dp, PRIO_UNICAST, match, [parser.OFPActionGroup(gid)])
                return "ff"
        # single primary output: all non-access nodes in LB mode, or no backup
        self.add_flow(dp, PRIO_UNICAST, match,
                      [parser.OFPActionOutput(pf["primary"])])
        return "plain"

    def _del_dest_flow(self, dp, prefix, mask):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            match=parser.OFPMatch(eth_dst=(prefix, mask))))

    def _current_plan(self, access_dpid):
        """Failure-aware protected plan over the LIVE graph (Phase 5/7)."""
        return paths.plan_dest_tree(self.model, access_dpid,
                                    frozenset(self.down_edges),
                                    frozenset(self.down_nodes))

    def _install_protected_dest(self, access_dpid):
        """Install destination-rooted protected forwarding toward one access sw."""
        if access_dpid in self.installed_dests:
            return
        if access_dpid not in self.access_gid:
            return                            # only access switches host destinations
        plan = self._current_plan(access_dpid)
        self.dest_plans[access_dpid] = plan
        gid = self.access_gid[access_dpid]
        prefix, mask = self.access_prefix[access_dpid]

        ff = plain = 0
        for node, pf in plan.items():
            d = self.datapaths.get(node)
            if d is None:
                continue
            if self._install_node_entry(d, gid, prefix, mask, pf) == "ff":
                ff += 1
            else:
                plain += 1

        # deliver to local hosts at the destination access switch (exact MAC)
        da = self.datapaths.get(access_dpid)
        if da is not None:
            parser = da.ofproto_parser
            for mac, port in self.hosts_on_access.get(access_dpid, []):
                # PRIO_LOCAL > PRIO_QOS_EF so EF traffic destined to a LOCAL host
                # is delivered locally instead of being swept up the EF plane-pin.
                self.add_flow(da, PRIO_LOCAL, parser.OFPMatch(eth_dst=mac),
                              [parser.OFPActionOutput(port)])

        self.installed_dests.add(access_dpid)
        self.ev("PROTECTED_DEST_INSTALLED", access=self.sw_name(access_dpid),
                gid=gid, ff_groups=ff, plain_flows=plain)

    # ------------------------------------------------ Phase 7: rerouting
    def _reroute(self, reason):
        """Recompute every installed destination over the live graph and
        reinstall only the switches whose forwarding changed."""
        if not self.ha_active:               # passive backup does not reroute
            return
        gid_of = self.access_gid
        for access in list(self.installed_dests):
            new = self._current_plan(access)
            old = self.dest_plans.get(access, {})
            changed = [n for n in (set(new) | set(old)) if old.get(n) != new.get(n)]
            if not changed:
                continue
            self.dest_plans[access] = new
            prefix, mask = self.access_prefix[access]
            updated = removed = 0
            for node in changed:
                dp = self.datapaths.get(node)
                if dp is None:
                    continue
                pf = new.get(node)
                if pf is None:
                    self._del_dest_flow(dp, prefix, mask)
                    removed += 1
                else:
                    self._install_node_entry(dp, gid_of[access], prefix, mask, pf)
                    updated += 1
            self.ev("FLOW_REINSTALLED", dest=self.sw_name(access), reason=reason,
                    updated=updated, removed=removed)
            # R2: under MULTIPLE simultaneous failures a destination can become
            # truly partitioned (both node-disjoint planes cut).  Flag the access
            # switches that can no longer reach it so the operator/dashboard sees
            # an unrecoverable partition rather than a silent black-hole.
            cut = [self.sw_name(a) for a in self.access_gid
                   if a != access and a not in self.down_nodes and a not in new]
            if cut:
                self.ev("DEST_UNREACHABLE", dest=self.sw_name(access),
                        reason=reason, cut_sources=len(cut),
                        sample=cut[:4])
        # a failure may have taken down the plane EF was pinned to -> repin now
        if self.enable_qos or self.enable_dpi:
            self._repin_qos(reason)

    # --------------------------------------- Phase 12: load- & QoS-aware routing
    def _choose_qos_plane(self, acc_dpid):
        """Pick the plane EF traffic should leave this access switch on: the
        balancer's preferred (least-loaded) plane, unless that plane's uplink is
        currently down, in which case the other plane."""
        up = self.access_uplinks.get(acc_dpid, {})
        if not up:
            return None, None
        pref = self.balancer.preferred
        order = [pref] + [p for p in (cm.PLANE_A, cm.PLANE_B) if p != pref]
        for plane in order:
            if plane in up:
                port, dist_dpid = up[plane]
                if not self._link_down(acc_dpid, dist_dpid):
                    return plane, port
        # both uplinks down (or unknown) -> fall back to any we have
        for plane, (port, _d) in up.items():
            return plane, port
        return None, None

    def _qos_actions(self, parser, queue, port):
        """SetQueue (when OVS queues are configured) then output to the plane."""
        acts = []
        if self.enable_queues:
            acts.append(parser.OFPActionSetQueue(queue))
        acts.append(parser.OFPActionOutput(port))
        return acts

    def _install_qos_plane(self, dp):
        """Per dest-tier switch: steer prioritised traffic onto the least-loaded
        plane uplink.  EF (DSCP 46) when CAMPUS_QOS; app-classified L4 flows when
        CAMPUS_DPI; both optionally mark an OVS priority queue (CAMPUS_QOS_QUEUES).
        Traffic to a LOCAL host is caught first by the PRIO_LOCAL exact-MAC rules."""
        plane, port = self._choose_qos_plane(dp.id)
        if port is None:
            return
        parser = dp.ofproto_parser
        # EF (DSCP-marked) critical traffic -> high queue, preferred plane
        if self.enable_qos:
            self.add_flow(dp, PRIO_QOS_EF,
                          parser.OFPMatch(eth_type=0x0800, ip_dscp=qos.DSCP_EF),
                          self._qos_actions(parser, qos.QUEUE_HIGH, port))
        # app-aware DPI: classify by L4 5-tuple -> its queue, preferred plane
        if self.enable_dpi:
            for proto, l4port, queue, _label in qos.APP_SIGS:
                m = dict(eth_type=0x0800, ip_proto=proto)
                if proto == qos.IPPROTO_TCP:
                    m["tcp_dst"] = l4port
                else:
                    m["udp_dst"] = l4port
                self.add_flow(dp, PRIO_DPI, parser.OFPMatch(**m),
                              self._qos_actions(parser, queue, port))
        self.qos_pin[dp.id] = plane

    def _repin_qos(self, reason):
        """Re-point every dest-tier switch's EF/app rules at the current best plane."""
        if not (self.enable_qos or self.enable_dpi):
            return
        n = 0
        for dpid in self.access_uplinks:
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            old = self.qos_pin.get(dpid)
            self._install_qos_plane(dp)
            if self.qos_pin.get(dpid) != old:
                n += 1
        if n:
            self.ev("QOS_REBALANCE", reason=reason, repinned=n,
                    **self.balancer.snapshot())

    def _qos_loop(self):
        """Feed live link load back into the plane decision (gaps F2/F3)."""
        while True:
            hub.sleep(self.stats_interval)
            try:
                changed = self.balancer.update(self.link_mbps)
            except Exception:
                continue
            if self.link_cap:
                a_ok = self.balancer.has_headroom(cm.PLANE_A, self.link_cap)
                b_ok = self.balancer.has_headroom(cm.PLANE_B, self.link_cap)
                if not a_ok and not b_ok:
                    self.ev("CONGESTION_WARNING", cap_mbps=self.link_cap,
                            **self.balancer.snapshot())
            if changed:
                self._repin_qos("load-change")

    # ------------------------------------------------ R1: controller HA/failover
    def _install_all_dests(self):
        """Proactively install protected forwarding toward EVERY access switch.
        Used on promotion so the newly-active backup holds the full plan state
        (and correct flows) regardless of what the dead primary left behind."""
        for access in list(self.access_gid):
            if self.datapaths.get(access) is not None:
                self._install_protected_dest(access)

    def _peer_alive(self):
        """True iff the peer controller answers its REST API AND is the active
        owner.  Used by the backup to decide whether the primary is still up."""
        if not self.ha_peer_api:
            return False
        try:
            url = self.ha_peer_api.rstrip("/") + "/api/controller"
            with urllib.request.urlopen(url, timeout=0.5) as r:
                d = json.load(r)
            return d.get("status") == "up" and d.get("ha_active", True)
        except Exception:
            return False

    def _ha_promote(self):
        """Backup takes over: wipe the dead primary's groups, reinstall the full
        protected forwarding over the live graph, and start acting on events."""
        self.ha_active = True
        self.ev("CONTROLLER_PROMOTED", role=self.ha_role,
                note="peer controller unreachable -- taking over")
        self.installed_dests.clear()
        self.dest_plans.clear()
        self.installed_groups.clear()
        self.installed_pairs.clear()
        for dp in list(self.datapaths.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            # clear the groups the former primary installed so our ADDs are clean
            dp.send_msg(parser.OFPGroupMod(
                datapath=dp, command=ofp.OFPGC_DELETE, type_=ofp.OFPGT_ALL,
                group_id=ofp.OFPG_ALL, buckets=[]))
        self._install_all_dests()
        if self.enable_qos or self.enable_dpi:
            self._repin_qos("ha-promote")

    def _ha_demote(self):
        """Primary came back -> step down to passive (it reasserts ownership)."""
        self.ha_active = False
        self._ha_misses = 0
        self.ev("CONTROLLER_DEMOTED", role=self.ha_role,
                note="peer controller back -- stepping down to standby")

    def _proactive_loop(self):
        """Wait for the fabric to connect, then install forwarding for every
        destination up front (no traffic needed).  O(1) per destination, so it
        scales where a full pingall cannot."""
        total = len(self.model.switches)
        for _ in range(240):                  # wait up to ~2 min for all switches
            if len(self.datapaths) >= total:
                break
            hub.sleep(0.5)
        hub.sleep(2)                          # let the last features settle
        if self.ha_active:
            self._install_all_dests()
            self.ev("PROACTIVE_INSTALL", dests=len(self.installed_dests),
                    groups=len(self.installed_groups))

    def _ha_loop(self):
        """Heartbeat the peer; the backup promotes on primary loss, and steps
        back down (failback) if the primary later returns."""
        self.ev("HA_ARMED", role=self.ha_role, active=self.ha_active,
                peer=self.ha_peer_api or None)
        while True:
            hub.sleep(self.ha_hb_s)
            if self.ha_role != "backup":
                continue                      # the primary simply stays active
            owner = self._peer_alive()
            if owner:
                self._ha_misses = 0
                if self.ha_active:            # primary returned -> fail back
                    self._ha_demote()
            else:
                self._ha_misses += 1
                if self._ha_misses >= self.ha_misses_max and not self.ha_active:
                    self._ha_promote()

    # ------------------------------------------- live link utilisation (heatmap)
    def _stats_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                try:
                    ofp, parser = dp.ofproto, dp.ofproto_parser
                    dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                except Exception:
                    pass
            hub.sleep(self.stats_interval)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for st in ev.msg.body:
            port = st.port_no
            if port > 0xff00:                 # skip LOCAL / reserved ports
                continue
            key = (dpid, port)
            total = st.tx_bytes + st.rx_bytes
            prev = self.port_bytes.get(key)
            self.port_bytes[key] = (total, now)
            if prev:
                dt = now - prev[1]
                if dt > 0:
                    self.port_bps[key] = max(0.0, (total - prev[0]) * 8.0 / dt)

    def link_mbps(self, da, db):
        """Recent throughput on the a->b link, in Mbit/s (max of the two ports)."""
        pa = self.adj.get(da, {}).get(db)
        pb = self.adj.get(db, {}).get(da)
        bps = max(self.port_bps.get((da, pa), 0.0) if pa else 0.0,
                  self.port_bps.get((db, pb), 0.0) if pb else 0.0)
        return round(bps / 1e6, 3)

    # ------------------------------------------------------------- helpers
    def host_by_mac_loc(self, mac):
        return self.host_by_mac.get(mac)

    # ---------------------------------------------- Phase 9: state as JSON
    def _link_down(self, da, db):
        return (frozenset((da, db)) in self.down_edges
                or da in self.down_nodes or db in self.down_nodes)

    def api_controller(self):
        inter = sum(1 for l in self.model.links
                    if l["kind"] not in (cm.K_HOST_ACCESS, cm.K_HOST_DC))
        return dict(
            status="up", of_version="OpenFlow13",
            uptime_s=round(time.time() - self.started_at, 1),
            ff_enabled=self.enable_ff, lb_enabled=self.enable_lb,
            qos_enabled=self.enable_qos, dpi_enabled=self.enable_dpi,
            queues_enabled=self.enable_queues,
            mlag=getattr(self.model, "mlag", False),
            dc_enabled=getattr(self.model, "dc", False),
            role=getattr(self, "ha_role", "single"),
            ha_active=getattr(self, "ha_active", True),
            switches_total=len(self.model.switches),
            switches_connected=len(self.datapaths),
            switches_down=len(self.down_nodes),
            hosts_total=len(self.model.hosts),
            hosts_learned=len(self.mac_to_loc),
            links_total=inter, links_down=len(self.down_edges),
            protected_dests=len(self.installed_dests),
            ff_groups=len(self.installed_groups),
            reconcile=self._last_reconcile)

    def api_qos(self):
        """Load- & QoS-aware routing state (gaps F1-F4)."""
        snap = self.balancer.snapshot()
        cap = self.link_cap
        return dict(
            enabled=self.enable_qos,
            preferred_plane=snap["preferred"],
            bottleneck_mbps=snap["bottleneck_mbps"],
            total_mbps=snap["total_mbps"],
            link_cap_mbps=cap,
            headroom={p: self.balancer.has_headroom(p, cap)
                      for p in (cm.PLANE_A, cm.PLANE_B)} if cap else {},
            ef_pins={self.sw_name(d): p for d, p in self.qos_pin.items()},
            ef_dscp=qos.DSCP_EF)

    def api_switches(self):
        out = []
        for name, a in self.model.switches.items():
            d = a["dpid"]
            out.append(dict(
                name=name, dpid=d, tier=a["tier"], pod=a["pod"],
                plane=a["plane"], index=a["index"],
                connected=(d in self.datapaths),
                status=("down" if d in self.down_nodes else "up"),
                ports=len(self.portmap.get(d, {})),
                groups=sum(1 for (dd, _g) in self.installed_groups if dd == d)))
        return out

    def api_links(self, include_hosts=False):
        out = []
        host_kinds = (cm.K_HOST_ACCESS, cm.K_HOST_DC)
        for lk in self.model.links:
            if lk["kind"] in host_kinds and not include_hosts:
                continue
            if lk["kind"] in host_kinds:
                out.append(dict(src=lk["a"], src_port=lk["a_port"],
                                dst=lk["b"], dst_port=lk["b_port"],
                                kind=lk["kind"], status="up",
                                on_spanning_tree=False))
                continue
            da = self.model.switches[lk["a"]]["dpid"]
            db = self.model.switches[lk["b"]]["dpid"]
            out.append(dict(
                src=lk["a"], src_port=lk["a_port"],
                dst=lk["b"], dst_port=lk["b_port"], kind=lk["kind"],
                status=("down" if self._link_down(da, db) else "up"),
                on_spanning_tree=(frozenset((da, db)) in self.tree_edges),
                mbps=self.link_mbps(da, db)))
        return out

    def api_hosts(self):
        return [dict(name=n, mac=h["mac"], ip=h["ip"], access=h["access"],
                     port=h["access_port"], learned=(h["mac"] in self.mac_to_loc))
                for n, h in self.model.hosts.items()]

    def api_topology(self):
        return dict(controller=self.api_controller(),
                    switches=self.api_switches(),
                    hosts=self.api_hosts(),
                    links=self.api_links(include_hosts=True))

    def api_events(self, since=None, limit=200):
        evs = list(self.event_log)
        if since is not None:
            evs = [e for e in evs if e["seq"] > since]
        return evs[-limit:]

    def api_metrics(self):
        try:
            with open("results/summary.json") as fh:
                experiments = json.load(fh)
        except (OSError, ValueError):
            experiments = []
        return dict(experiments=experiments,
                    live=dict(links_down=len(self.down_edges),
                              switches_down=len(self.down_nodes)))

    def api_eval(self):
        """Phase 11 statistical evaluation + RSTP baseline, for the dashboard
        Analysis tab.  Returns None sections when a run has not been done yet."""
        def _load(path):
            try:
                with open(path) as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return None
        return dict(eval=_load("results/eval/summary_stats.json"),
                    rstp=_load("results/baseline_rstp/summary_stats.json"))


def _is_multicast(mac):
    """True for broadcast/multicast (I/G bit of first octet set)."""
    try:
        return bool(int(mac.split(":")[0], 16) & 1)
    except (ValueError, IndexError):
        return False


class CampusRestController(ControllerBase):
    """Phase 9 REST API -- serves the controller's REAL state and injects REAL
    failures via OpenFlow port-mod.  Runs in the Ryu WSGI server (default :8080).
    """

    def __init__(self, req, link, data, **config):
        super(CampusRestController, self).__init__(req, link, data, **config)
        self.app = data[REST_INSTANCE]

    def _json(self, obj, status=200):
        return Response(content_type="application/json", status=status,
                        body=json.dumps(obj).encode("utf-8"),
                        headers={"Access-Control-Allow-Origin": "*"})

    def _body(self, req):
        try:
            return json.loads(req.body or b"{}")
        except ValueError:
            return {}

    # --------- read-only state ---------
    @route("campus", "/api/controller", methods=["GET"])
    def controller(self, req, **_):
        return self._json(self.app.api_controller())

    @route("campus", "/api/topology", methods=["GET"])
    def topology(self, req, **_):
        return self._json(self.app.api_topology())

    @route("campus", "/api/switches", methods=["GET"])
    def switches(self, req, **_):
        return self._json(self.app.api_switches())

    @route("campus", "/api/links", methods=["GET"])
    def links(self, req, **_):
        return self._json(self.app.api_links())

    @route("campus", "/api/qos", methods=["GET"])
    def qos(self, req, **_):
        return self._json(self.app.api_qos())

    @route("campus", "/api/hosts", methods=["GET"])
    def hosts(self, req, **_):
        return self._json(self.app.api_hosts())

    @route("campus", "/api/events", methods=["GET"])
    def events(self, req, **_):
        since = req.GET.get("since")
        limit = req.GET.get("limit")
        return self._json(self.app.api_events(
            since=int(since) if since else None,
            limit=int(limit) if limit else 200))

    @route("campus", "/api/metrics", methods=["GET"])
    def metrics(self, req, **_):
        return self._json(self.app.api_metrics())

    @route("campus", "/api/eval", methods=["GET"])
    def eval(self, req, **_):
        return self._json(self.app.api_eval())

    # --------- failure injection (real, via OpenFlow) ---------
    @route("campus", "/api/failures/link", methods=["POST"])
    def fail_link(self, req, **_):
        b = self._body(req)
        ok, msg = self.app.api_set_link(b.get("a"), b.get("b"), True)
        return self._json(dict(ok=ok, msg=msg), status=200 if ok else 400)

    @route("campus", "/api/recovery/link", methods=["POST"])
    def recover_link(self, req, **_):
        b = self._body(req)
        ok, msg = self.app.api_set_link(b.get("a"), b.get("b"), False)
        return self._json(dict(ok=ok, msg=msg), status=200 if ok else 400)

    @route("campus", "/api/failures/switch", methods=["POST"])
    def fail_switch(self, req, **_):
        b = self._body(req)
        ok, msg = self.app.api_set_switch(b.get("name"), True)
        return self._json(dict(ok=ok, msg=msg), status=200 if ok else 400)

    @route("campus", "/api/recovery/switch", methods=["POST"])
    def recover_switch(self, req, **_):
        b = self._body(req)
        ok, msg = self.app.api_set_switch(b.get("name"), False)
        return self._json(dict(ok=ok, msg=msg), status=200 if ok else 400)

    @route("campus", "/api/install_all", methods=["POST"])
    def install_all(self, req, **_):
        ok, msg = self.app.api_install_all()
        return self._json({"ok": ok, "msg": msg})

    @route("campus", "/api/reset", methods=["POST"])
    def reset(self, req, **_):
        ok, msg = self.app.api_reset()
        return self._json(dict(ok=ok, msg=msg), status=200 if ok else 400)
