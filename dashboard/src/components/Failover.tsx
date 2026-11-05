import { api, EventRec, Metrics } from "../api";
import { usePolling } from "../usePolling";

export function Failover({ events }: { events: EventRec[] }) {
  const m = usePolling<Metrics>(api.metrics, 4000).data;
  const exp = m?.experiments ?? [];

  const recent = events.filter((e) =>
    ["API_LINK_DOWN", "LINK_DOWN", "FAILOVER_TRIGGERED", "FLOW_REINSTALLED",
     "LINK_RECOVERED", "SWITCH_RECOVERED"].includes(e.event),
  ).slice(-14).reverse();

  return (
    <>
      <div className="panel">
        <h2>Measured resilience (Phase 8 experiments)</h2>
        {exp.length === 0 ? (
          <p className="muted">
            No results yet — run <code>sudo ./run_phase8.sh smoke</code> to generate
            <code> results/summary.json</code>.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>scenario</th><th>mechanism</th><th>loss</th><th>outage</th>
                <th>detection</th><th>reroute</th><th>flows</th>
                <th>path before → after</th>
              </tr>
            </thead>
            <tbody>
              {exp.map((e) => (
                <tr key={e.scenario}>
                  <td>{e.scenario}</td>
                  <td>
                    <span className={`badge ${e.mechanism === "local_ff" ? "tier-dist" : e.mechanism === "controller_reroute" ? "tier-access" : "up"}`}>
                      {e.mechanism}
                    </span>
                  </td>
                  <td>{e.packet_loss}/{e.packets_tx}</td>
                  <td>{e.outage_ms} ms</td>
                  <td>{e.detection_ms != null ? `${e.detection_ms} ms` : "—"}</td>
                  <td>{e.reroute_ms != null ? `${e.reroute_ms} ms` : "—"}</td>
                  <td>{e.affected_flows ?? "—"}</td>
                  <td className="pathline muted">
                    {e.path_before?.join("→") ?? "—"}
                    {e.path_after && e.path_after.join("→") !== e.path_before?.join("→")
                      ? `  ⇒  ${e.path_after.join("→")}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>Live failover timeline</h2>
        {recent.length === 0 ? (
          <p className="muted">No live failover events. Fail a link from the Topology tab.</p>
        ) : (
          <div className="events">
            {recent.map((e) => (
              <div className="row" key={e.seq}>
                <span className="muted">{new Date((e.ts as number) * 1000).toLocaleTimeString()}</span>
                <span className={`ev ev-${e.event}`}>{e.event}</span>
                <span>{e.link ? (e.link as string[]).join(" ↔ ")
                  : e.dest ? `dest ${e.dest} · ${e.updated ?? 0} flows`
                  : e.switch ? `switch ${e.switch}` : ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
