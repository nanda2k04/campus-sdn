import { useState } from "react";
import { api, Topology } from "../api";

export function Tables({ kind, topo, flash }: {
  kind: "switches" | "links" | "hosts";
  topo: Topology | null;
  flash: (m: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  if (!topo) return <div className="panel muted">No data yet.</div>;

  async function act(fn: () => Promise<{ ok: boolean; msg: string }>, label: string) {
    if (busy) return;
    setBusy(true);
    const r = await fn();
    flash(`${label}: ${r.ok ? "ok" : r.msg}`);
    setBusy(false);
  }

  if (kind === "switches") {
    return (
      <div className="panel">
        <h2>Switches ({topo.switches.length})</h2>
        <table>
          <thead><tr>
            <th>name</th><th>dpid</th><th>tier</th><th>pod</th><th>plane</th>
            <th>connected</th><th>groups</th><th>status</th><th></th>
          </tr></thead>
          <tbody>
            {topo.switches.map((s) => (
              <tr key={s.name}>
                <td>{s.name}</td><td>{s.dpid}</td>
                <td><span className={`badge tier-${s.tier}`}>{s.tier}</span></td>
                <td>{s.pod ?? "—"}</td><td>{s.plane ?? "—"}</td>
                <td>{s.connected ? "yes" : "no"}</td><td>{s.groups}</td>
                <td><span className={`badge ${s.status}`}>{s.status}</span></td>
                <td>
                  {s.status === "up"
                    ? <button className="act danger" onClick={() => act(() => api.failSwitch(s.name), `isolate ${s.name}`)}>isolate</button>
                    : <button className="act good" onClick={() => act(() => api.recoverSwitch(s.name), `restore ${s.name}`)}>restore</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (kind === "links") {
    const links = topo.links.filter((l) => l.kind !== "host-access");
    return (
      <div className="panel">
        <h2>Inter-switch links ({links.length})</h2>
        <table>
          <thead><tr>
            <th>src</th><th>port</th><th>dst</th><th>port</th><th>kind</th>
            <th>tree</th><th>status</th><th></th>
          </tr></thead>
          <tbody>
            {links.map((l, i) => (
              <tr key={i}>
                <td>{l.src}</td><td>{l.src_port}</td><td>{l.dst}</td><td>{l.dst_port}</td>
                <td>{l.kind}</td><td>{l.on_spanning_tree ? "✓" : ""}</td>
                <td><span className={`badge ${l.status}`}>{l.status}</span></td>
                <td>
                  {l.status === "up"
                    ? <button className="act danger" onClick={() => act(() => api.failLink(l.src, l.dst), `fail ${l.src}-${l.dst}`)}>fail</button>
                    : <button className="act good" onClick={() => act(() => api.recoverLink(l.src, l.dst), `recover ${l.src}-${l.dst}`)}>recover</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2>Hosts ({topo.hosts.length})</h2>
      <table>
        <thead><tr><th>name</th><th>ip</th><th>mac</th><th>access</th><th>learned</th></tr></thead>
        <tbody>
          {topo.hosts.map((h) => (
            <tr key={h.name}>
              <td>{h.name}</td><td>{h.ip}</td><td>{h.mac}</td><td>{h.access}</td>
              <td>{h.learned ? "yes" : "no"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
