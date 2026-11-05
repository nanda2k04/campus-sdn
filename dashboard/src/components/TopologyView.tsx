import { useMemo, useState } from "react";
import { api, Link, Switch, Topology } from "../api";

interface Pos { x: number; y: number; tier: string; }

const ROW = { core: 70, dist: 210, access: 350, host: 470 };
const PAD = 60;

function layout(topo: Topology): { pos: Record<string, Pos>; width: number; height: number } {
  const pos: Record<string, Pos> = {};
  const cores = topo.switches.filter((s) => s.tier === "core").sort((a, b) => a.dpid - b.dpid);
  const pods = Array.from(new Set(topo.switches.filter((s) => s.pod).map((s) => s.pod))).sort(
    (a, b) => (a as number) - (b as number),
  ) as number[];

  const accPerPod = Math.max(
    1,
    ...pods.map((p) => topo.switches.filter((s) => s.tier === "access" && s.pod === p).length),
  );
  const podW = Math.max(accPerPod * 66 + 40, 200);
  const width = Math.max(pods.length * podW + PAD * 2, cores.length * 140 + PAD * 2);
  const height = ROW.host + 90;

  cores.forEach((c, i) => {
    pos[c.name] = { x: PAD + ((i + 0.5) * (width - 2 * PAD)) / cores.length, y: ROW.core, tier: "core" };
  });

  pods.forEach((p, pi) => {
    const left = PAD + pi * ((width - 2 * PAD) / pods.length);
    const w = (width - 2 * PAD) / pods.length;
    const dists = topo.switches.filter((s) => s.tier === "dist" && s.pod === p)
      .sort((a, b) => (a.plane! < b.plane! ? -1 : 1));
    dists.forEach((d, di) => {
      pos[d.name] = { x: left + w * (dists.length === 1 ? 0.5 : 0.32 + di * 0.36), y: ROW.dist, tier: "dist" };
    });
    const acc = topo.switches.filter((s) => s.tier === "access" && s.pod === p)
      .sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
    acc.forEach((a, ai) => {
      const x = left + w * ((ai + 0.5) / acc.length);
      pos[a.name] = { x, y: ROW.access, tier: "access" };
      const hs = topo.hosts.filter((h) => h.access === a.name);
      hs.forEach((h, hi) => {
        pos[h.name] = { x: x + (hi - (hs.length - 1) / 2) * 26, y: ROW.host, tier: "host" };
      });
    });
  });
  return { pos, width, height };
}

const NODE_FILL: Record<string, string> = {
  core: "#bf5af2", dist: "#30d1c0", access: "#0a84ff", host: "#8a8a8e",
};
// link colors (aligned with the Apple-palette tokens in styles.css)
const C_DOWN = "#ff453a", C_TRACE = "#ffd60a", C_BACKUP = "#ff9f0a",
      C_PRIMARY = "#2f9e6a", C_BACKUP_HIDDEN = "#3a5570", C_HOST = "#2a3850";
const TIER_RANK: Record<string, number> = { core: 0, dist: 1, access: 2, host: 3 };

const key = (a: string, b: string) => [a, b].sort().join("|");
// Any host link (host-access OR host-dc server link). Inter-switch kinds
// (core-mesh, dist-core, access-dist, dist-peer, dc-core) never start with "host",
// so this cleanly separates host endpoints (which are NOT in the switch graph).
const isHostLink = (kind: string) => kind.startsWith("host");

// link-load heatmap: colour by live Mbit/s
function heatColor(mbps: number): string {
  if (mbps < 0.05) return "#33425a";   // idle
  if (mbps < 1) return "#2f7d55";      // light (green)
  if (mbps < 5) return "#9bbf2e";      // moderate (yellow-green)
  if (mbps < 20) return "#e8a21a";     // busy (amber)
  return "#e24b4a";                    // hot (red)
}

export function TopologyView({ topo, flash }: { topo: Topology | null; flash: (m: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [showBackups, setShowBackups] = useState(false);
  const [showHeat, setShowHeat] = useState(false);
  const [src, setSrc] = useState("");
  const [dst, setDst] = useState("");
  const [pathInfo, setPathInfo] = useState<{ links: Set<string>; text: string; rerouted: boolean } | null>(null);

  const L = useMemo(() => (topo ? layout(topo) : null), [topo]);

  // adjacency over UP inter-switch links (+ a full-graph one to detect reroute)
  const graphs = useMemo(() => {
    if (!topo) return null;
    const dpid: Record<string, number> = {};
    const tier: Record<string, string> = {};
    topo.switches.forEach((s) => { dpid[s.name] = s.dpid; tier[s.name] = s.tier; });
    const up: Record<string, string[]> = {};
    const all: Record<string, string[]> = {};
    topo.switches.forEach((s) => { up[s.name] = []; all[s.name] = []; });
    topo.links.forEach((l) => {
      if (isHostLink(l.kind)) return;
      all[l.src].push(l.dst); all[l.dst].push(l.src);
      if (l.status === "up") { up[l.src].push(l.dst); up[l.dst].push(l.src); }
    });
    const sortByDpid = (g: Record<string, string[]>) =>
      Object.values(g).forEach((a) => a.sort((x, y) => (dpid[x] ?? 9e9) - (dpid[y] ?? 9e9)));
    sortByDpid(up); sortByDpid(all);
    return { up, all, tier, dpid };
  }, [topo]);

  // AUTO backup detection: for every DOWN primary (spanning-tree) link, find the
  // route the downstream switch now uses to climb back to a core, over live links.
  const failover = useMemo(() => {
    if (!topo || !graphs) return { links: new Set<string>(), notes: [] as string[], coreNotes: [] as string[] };
    const links = new Set<string>();
    const notes: string[] = [];
    const coreNotes: string[] = [];
    const bfsToCore = (start: string): string[] | null => {
      const seen = new Set([start]);
      const q: string[][] = [[start]];
      while (q.length) {
        const path = q.shift()!;
        const node = path[path.length - 1];
        if (node !== start && graphs.tier[node] === "core") return path;
        for (const n of graphs.up[node] ?? []) {
          if (seen.has(n)) continue;
          seen.add(n); q.push([...path, n]);
        }
      }
      return null;
    };
    topo.links.forEach((l) => {
      // ANY down inter-switch link triggers a backup: the controller reroutes on
      // a link-down regardless of spanning-tree membership (the tree is only the
      // BROADCAST tree; unicast forwarding is destination-rooted). So we must not
      // gate on `on_spanning_tree` -- a dist's primary core uplink can be off the
      // tree yet still be the active unicast primary (e.g. d1a→c1).
      if (l.status !== "down" || isHostLink(l.kind)) return;
      // A backup path is only meaningful when the failed link has a clear host-ward
      // "downstream" side that must climb back to a core (access↔dist, dist↔core).
      // A core↔core (same-tier) link has cores on both ends -- the controller
      // reroutes at the core layer, but there is no single host-ward path to draw,
      // so don't fabricate one.
      if (graphs.tier[l.src] === graphs.tier[l.dst]) {
        coreNotes.push(`${l.src}↔${l.dst} down — core layer still connected via the remaining mesh; controller reinstalled affected flows`);
        return;
      }
      // downstream = the endpoint further from the core (higher tier rank)
      const down = (TIER_RANK[graphs.tier[l.src]] ?? 9) >= (TIER_RANK[graphs.tier[l.dst]] ?? 9)
        ? l.src : l.dst;
      const p = bfsToCore(down);
      if (!p || p.length < 2) return;
      for (let i = 0; i < p.length - 1; i++) links.add(key(p[i], p[i + 1]));
      notes.push(`${down} → ${p.slice(1).join(" → ")} (primary ${l.src}↔${l.dst} down)`);
    });
    return { links, notes, coreNotes };
  }, [topo, graphs]);

  if (!topo || !L) return <div className="panel muted">No topology yet.</div>;

  function bfs(g: Record<string, string[]>, a: string, b: string): string[] | null {
    if (a === b) return [a];
    const seen = new Set([a]);
    const q: string[][] = [[a]];
    while (q.length) {
      const path = q.shift()!;
      const node = path[path.length - 1];
      for (const n of g[node] ?? []) {
        if (seen.has(n)) continue;
        if (n === b) return [...path, n];
        seen.add(n); q.push([...path, n]);
      }
    }
    return null;
  }

  function showPath() {
    if (!topo || !graphs || !src || !dst || src === dst) return;
    const sa = topo.hosts.find((h) => h.name === src)?.access;
    const da = topo.hosts.find((h) => h.name === dst)?.access;
    if (!sa || !da) return;
    const p = bfs(graphs.up, sa, da);
    if (!p) { setPathInfo({ links: new Set(), text: "no path (destination unreachable)", rerouted: false }); return; }
    const links = new Set<string>();
    links.add(key(src, sa));
    for (let i = 0; i < p.length - 1; i++) links.add(key(p[i], p[i + 1]));
    links.add(key(da, dst));
    const normal = bfs(graphs.all, sa, da);          // path if nothing were down
    const rerouted = !!normal && normal.join(">") !== p.join(">");
    setPathInfo({ links, text: [src, ...p, dst].join(" → "), rerouted });
  }

  async function clickLink(lk: Link) {
    if (busy || isHostLink(lk.kind)) return;
    setBusy(true);
    const down = lk.status === "up";
    const r = down ? await api.failLink(lk.src, lk.dst) : await api.recoverLink(lk.src, lk.dst);
    flash(`${down ? "Failed" : "Recovered"} ${lk.src}↔${lk.dst}: ${r.ok ? "ok" : r.msg}`);
    setBusy(false);
  }
  async function clickSwitch(s: Switch) {
    if (busy) return;
    setBusy(true);
    const down = s.status === "up";
    const r = down ? await api.failSwitch(s.name) : await api.recoverSwitch(s.name);
    flash(`${down ? "Isolated" : "Restored"} switch ${s.name}: ${r.ok ? "ok" : r.msg}`);
    setBusy(false);
  }

  const hostNames = topo.hosts.map((h) => h.name);
  const traced = pathInfo?.links ?? new Set<string>();
  const backupActive = failover.links;

  // order cores left->right so core-mesh links can be drawn as nested arcs
  // (further-apart pairs arc higher) -- this fans them out from each core node
  // so overlapping lines no longer make a single link impossible to click.
  const coreRank: Record<string, number> = {};
  topo.switches.filter((s) => s.tier === "core").sort((a, b) => a.dpid - b.dpid)
    .forEach((s, i) => { coreRank[s.name] = i; });

  return (
    <>
      <div className="panel" style={{ marginBottom: 12, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ display: "flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
          <input type="checkbox" checked={showBackups} onChange={(e) => setShowBackups(e.target.checked)} />
          <strong>Show backup paths</strong>
        </label>
        <label style={{ display: "flex", gap: 6, alignItems: "center", cursor: "pointer", marginLeft: 14 }}>
          <input type="checkbox" checked={showHeat} onChange={(e) => setShowHeat(e.target.checked)} />
          <strong>Link-load heatmap</strong>
        </label>
        <span className="muted" style={{ fontSize: 12 }}>
          Default view shows only the active primary links. The heatmap colours every link by live throughput.
        </span>
      </div>

      <div className="panel" style={{ marginBottom: 12, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <strong>Trace path:</strong>
        <select className="act" value={src} onChange={(e) => setSrc(e.target.value)}>
          <option value="">source host…</option>
          {hostNames.map((h) => <option key={h} value={h}>{h}</option>)}
        </select>
        <span className="muted">→</span>
        <select className="act" value={dst} onChange={(e) => setDst(e.target.value)}>
          <option value="">dest host…</option>
          {hostNames.map((h) => <option key={h} value={h}>{h}</option>)}
        </select>
        <button className="act good" onClick={showPath}>Show active path</button>
        <button className="act" onClick={() => setPathInfo(null)}>Clear</button>
        {pathInfo && (
          <span className="pathline" style={{ marginLeft: 8 }}>
            {pathInfo.rerouted && <span className="badge down" style={{ marginRight: 8 }}>REROUTED via backup</span>}
            <span className="muted">{pathInfo.text}</span>
          </span>
        )}
      </div>

      {failover.notes.length > 0 && (
        <div className="panel" style={{ marginBottom: 12, borderLeft: `3px solid ${C_BACKUP}` }}>
          <strong style={{ color: C_BACKUP }}>● Backup path active</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {failover.notes.map((n, i) => <li key={i} className="muted" style={{ fontSize: 13 }}>{n}</li>)}
          </ul>
        </div>
      )}

      {failover.coreNotes.length > 0 && (
        <div className="panel" style={{ marginBottom: 12, borderLeft: `3px solid ${NODE_FILL.core}` }}>
          <strong style={{ color: NODE_FILL.core }}>Core-mesh link down</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {failover.coreNotes.map((n, i) => <li key={i} className="muted" style={{ fontSize: 13 }}>{n}</li>)}
          </ul>
        </div>
      )}

      <div className="legend">
        <span><i style={{ background: NODE_FILL.core }} /> core</span>
        <span><i style={{ background: NODE_FILL.dist }} /> distribution</span>
        <span><i style={{ background: NODE_FILL.access }} /> access</span>
        <span><i style={{ background: NODE_FILL.host }} /> host</span>
        <span><i style={{ background: C_PRIMARY }} /> primary link</span>
        <span><i className="topo-flow" style={{ background: C_BACKUP, height: "4px" }} /> backup active</span>
        <span><i style={{ background: C_DOWN }} /> link down</span>
        <span><i style={{ background: C_TRACE, height: "4px" }} /> traced path</span>
        <span className="muted">— click a link to fail/recover, a switch to isolate/restore —</span>
      </div>

      {showHeat && (
        <div className="legend" style={{ marginTop: -6 }}>
          <span className="muted">load:</span>
          <span><i style={{ background: "#33425a" }} /> idle</span>
          <span><i style={{ background: "#2f7d55" }} /> &lt;1 Mbps</span>
          <span><i style={{ background: "#9bbf2e" }} /> 1–5</span>
          <span><i style={{ background: "#e8a21a" }} /> 5–20</span>
          <span><i style={{ background: "#e24b4a" }} /> &gt;20 Mbps</span>
          <span className="muted">— hover a link for its exact Mbps —</span>
        </div>
      )}

      <div className="topo-wrap">
        <svg width={L.width} height={L.height} style={{ display: "block", minWidth: "100%" }}>
          {topo.links.map((lk, i) => {
            const a = L.pos[lk.src], b = L.pos[lk.dst];
            if (!a || !b) return null;
            const down = lk.status === "down";
            const host = isHostLink(lk.kind);
            const primary = host || lk.on_spanning_tree;      // host links are the only path too
            const k = key(lk.src, lk.dst);
            const onTrace = traced.has(k);
            const onBackup = backupActive.has(k);

            // visibility: always show primary, down, or anything currently lit;
            // backups only when a toggle is on. Heatmap shows every link.
            const visible = primary || down || onBackup || onTrace || showBackups || (showHeat && !host);
            if (!visible) return null;

            const mbps = lk.mbps ?? 0;
            let stroke: string;
            let w: number;
            let dash: string | undefined;
            let flow = false;                                   // animated "traffic flowing" dash
            if (down) { stroke = C_DOWN; w = 3; dash = "5 4"; }
            else if (onTrace) { stroke = C_TRACE; w = 4; }
            else if (onBackup) { stroke = C_BACKUP; w = 4.5; dash = "9 6"; flow = true; }  // auto backup: glowing orange flow
            else if (showHeat && !host) { stroke = heatColor(mbps); w = 2 + Math.min(4, mbps / 5); }
            else if (host) { stroke = C_HOST; w = 1; }
            else if (primary) { stroke = C_PRIMARY; w = 2; }
            else { stroke = C_BACKUP_HIDDEN; w = 1.5; dash = "4 4"; }  // revealed backup (toggle on)

            const titleTxt = `${lk.src}:${lk.src_port} ↔ ${lk.dst}:${lk.dst_port} (${lk.kind}, ${lk.status}${lk.on_spanning_tree ? ", primary" : ", backup"}) · ${mbps.toFixed(2)} Mbps`;

            // core<->core: draw as a nested arc above the core row, with a fat
            // invisible hit-path so each one is easy to click without hitting a peer.
            if (lk.kind === "core-mesh") {
              const span = Math.abs((coreRank[lk.src] ?? 0) - (coreRank[lk.dst] ?? 0));
              const lift = 18 + 15 * span;
              const my = Math.min(a.y, b.y) - lift;
              const d = `M ${a.x} ${a.y} Q ${(a.x + b.x) / 2} ${my} ${b.x} ${b.y}`;
              return (
                <g key={i}>
                  <path d={d} stroke="transparent" strokeWidth={14} fill="none"
                    style={{ cursor: "pointer" }} onClick={() => clickLink(lk)}>
                    <title>{titleTxt}</title>
                  </path>
                  <path d={d} stroke={stroke} strokeWidth={w} strokeDasharray={dash}
                    className={flow ? "topo-flow" : undefined}
                    fill="none" style={{ pointerEvents: "none" }} />
                </g>
              );
            }

            return (
              <line
                key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={stroke} strokeWidth={w}
                strokeDasharray={dash}
                className={flow ? "topo-flow" : undefined}
                style={{ cursor: host ? "default" : "pointer" }}
                onClick={() => clickLink(lk)}
              >
                <title>{titleTxt}</title>
              </line>
            );
          })}
          {topo.switches.map((s) => {
            const p = L.pos[s.name]; if (!p) return null;
            const down = s.status === "down" || !s.connected;
            return (
              <g key={s.name} style={{ cursor: "pointer" }} onClick={() => clickSwitch(s)}>
                <circle cx={p.x} cy={p.y} r={13}
                  fill={NODE_FILL[s.tier]} stroke={down ? C_DOWN : "#0a0b0e"}
                  strokeWidth={down ? 3 : 1.5} opacity={down ? 0.55 : 1}>
                  <title>{`${s.name} (dpid ${s.dpid}, ${s.tier})\n${s.connected ? "connected" : "disconnected"} · ${s.groups} groups`}</title>
                </circle>
                <text className="node-label" x={p.x} y={p.y - 18} textAnchor="middle">{s.name}</text>
              </g>
            );
          })}
          {topo.hosts.map((h) => {
            const p = L.pos[h.name]; if (!p) return null;
            const endpoint = h.name === src || h.name === dst;
            return (
              <g key={h.name}>
                <rect x={p.x - 8} y={p.y - 8} width={16} height={16} rx={3}
                  fill={endpoint && traced.size ? C_TRACE : NODE_FILL.host}
                  opacity={h.learned || endpoint ? 1 : 0.5}>
                  <title>{`${h.name} ${h.ip} (${h.mac})${h.learned ? " · learned" : ""}`}</title>
                </rect>
              </g>
            );
          })}
        </svg>
      </div>
    </>
  );
}
