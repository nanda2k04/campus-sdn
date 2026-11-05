import type { ReactNode } from "react";
import { api, ControllerStatus, EventRec, QosState } from "../api";
import { usePolling } from "../usePolling";

function Tile({ k, v, u, small }: { k: string; v: ReactNode; u?: string; small?: boolean }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={`v ${small ? "small" : ""}`}>
        {v}{u && <span className="u">{u}</span>}
      </div>
    </div>
  );
}

export function Overview({ ctrl, events }: { ctrl: ControllerStatus | null; events: EventRec[] }) {
  // Phase 12: poll the QoS/plane-balance endpoint (null until CAMPUS_QOS=1).
  const qos = usePolling<QosState>(api.qos, 1500).data;

  if (!ctrl) return <div className="panel muted">No controller data yet.</div>;
  const r = ctrl.reconcile;
  const failoverEvents = events.filter((e) =>
    ["FAILOVER_TRIGGERED", "LINK_DOWN", "FLOW_REINSTALLED", "LINK_RECOVERED",
     "QOS_REBALANCE", "CONGESTION_WARNING", "CONTROLLER_PROMOTED", "DEST_UNREACHABLE"]
      .includes(e.event),
  ).slice(-8).reverse();

  return (
    <>
      <div className="tiles">
        <Tile k="Controller" v={ctrl.status.toUpperCase()} />
        <Tile k="Switches connected" v={`${ctrl.switches_connected}/${ctrl.switches_total}`} />
        <Tile k="Links up" v={`${ctrl.links_total - ctrl.links_down}/${ctrl.links_total}`} />
        <Tile k="Links down" v={ctrl.links_down} />
        <Tile k="Hosts learned" v={`${ctrl.hosts_learned}/${ctrl.hosts_total}`} />
        <Tile k="FF groups" v={ctrl.ff_groups} />
        <Tile k="Protected dests" v={ctrl.protected_dests} />
        <Tile k="Uptime" v={Math.round(ctrl.uptime_s)} u="s" small />
        <Tile k="OpenFlow" v={ctrl.of_version} small />
        <Tile k="Fast failover" v={ctrl.ff_enabled ? "ON" : "OFF"} small />
        {ctrl.lb_enabled && <Tile k="Load balancing" v="ON" small />}
        {ctrl.qos_enabled && qos && <Tile k="Preferred plane" v={qos.preferred_plane} small />}
        {ctrl.role && ctrl.role !== "single" && (
          <Tile k="HA role" v={`${ctrl.role}${ctrl.ha_active ? " ·active" : " ·standby"}`} small />
        )}
      </div>

      {ctrl.qos_enabled && qos && <PlaneBalance qos={qos} />}

      <div className="panel" style={{ marginTop: 16 }}>
        <h2>Topology reconciliation <span className="muted">· discovered vs. model</span></h2>
        {r ? (
          <p style={{ margin: 0 }}>
            {r.matched}/{r.expected} directed links matched{" "}
            <span className={r.missing + r.extra === 0 ? "badge up" : "badge down"} style={{ marginLeft: 6 }}>
              {r.missing + r.extra === 0 ? "converged" : `${r.missing} missing / ${r.extra} extra`}
            </span>
          </p>
        ) : (
          <p className="muted" style={{ margin: 0 }}>discovery still converging…</p>
        )}
      </div>

      <div className="panel">
        <h2>Recent failover activity</h2>
        {failoverEvents.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>No failures yet. Trigger one from the Topology or Links tab.</p>
        ) : (
          <div className="events">
            {failoverEvents.map((e) => (
              <div className="row" key={e.seq}>
                <span className={`ev ev-${e.event}`}>{e.event}</span>
                <span>{summarize(e)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

/** Live per-plane load balance (Phase 12). Shows which plane EF/critical traffic
 *  rides and how full each plane is relative to the per-link cap. */
function PlaneBalance({ qos }: { qos: QosState }) {
  const cap = qos.link_cap_mbps || 0;
  const planes = ["A", "B"];
  const efCount = Object.keys(qos.ef_pins || {}).length;
  const width = (mbps: number) => {
    if (cap > 0) return Math.min(100, (mbps / cap) * 100);
    const peak = Math.max(1, ...planes.map((p) => qos.bottleneck_mbps[p] ?? 0));
    return Math.min(100, (mbps / peak) * 100);
  };
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h2>
        QoS plane balance{" "}
        <span className="muted">· EF (DSCP {qos.ef_dscp}) steered to the least-loaded plane{cap ? ` · cap ${cap} Mb/s` : ""}</span>
      </h2>
      <div className="planes">
        {planes.map((p) => {
          const load = qos.bottleneck_mbps[p] ?? 0;
          const hot = qos.headroom && qos.headroom[p] === false;
          const pref = qos.preferred_plane === p;
          return (
            <div key={p} className={`plane ${pref ? "preferred" : ""}`}>
              <div className="ph">
                <span className="pname">Plane {p}</span>
                <span className={`badge ${pref ? "tier-access" : hot ? "down" : "up"}`}>
                  {pref ? "EF preferred" : hot ? "congested" : "headroom"}
                </span>
              </div>
              <div className={`meter ${hot ? "hot" : ""}`}><i style={{ width: `${width(load)}%` }} /></div>
              <div className="pv">{load.toFixed(1)}<span className="u"> Mb/s peak link</span></div>
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                total {(qos.total_mbps[p] ?? 0).toFixed(0)} Mb/s across plane links
              </div>
            </div>
          );
        })}
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>
        {efCount} access switch{efCount === 1 ? "" : "es"} pinning critical traffic to plane{" "}
        <strong style={{ color: "var(--text)" }}>{qos.preferred_plane}</strong>. Best-effort flows spread across both planes (WCMP).
      </div>
    </div>
  );
}

function summarize(e: EventRec): string {
  if (e.link) return `${(e.link as string[]).join(" ↔ ")}`;
  if (e.dest) return `dest ${e.dest} · ${e.updated ?? 0} flows`;
  if (e.preferred) return `preferred plane → ${e.preferred}${e.repinned ? ` · ${e.repinned} repinned` : ""}`;
  if (e.src && e.dst) return `${e.src} → ${e.dst}`;
  if (e.note) return String(e.note);
  return JSON.stringify(e).slice(0, 80);
}
