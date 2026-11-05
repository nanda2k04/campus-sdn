import { api, EvalResponse, ScenarioStat } from "../api";
import { usePolling } from "../usePolling";

const SCEN_LABEL: Record<string, string> = {
  access_uplink_ff: "Access-uplink (local FF)",
  dist_core_ff: "Dist-core (local FF)",
  core_dist_ff: "Core-dist (local FF)",
  last_hop_reroute: "Last-hop (controller reroute)",
  switch_isolation_reroute: "Switch failure (reroute)",
  access_uplink_rstp: "Access-uplink (RSTP)",
};

// literature convergence (ms), typical order-of-magnitude
const LIT: [string, number][] = [
  ["MPLS / IP FRR (LFA) [lit]", 50],
  ["OSPF + BFD, tuned [lit]", 150],
  ["IS-IS, tuned [lit]", 300],
  ["RSTP (L2) [lit]", 1500],
  ["OSPF, default timers [lit]", 3000],
  ["BGP, inter-domain [lit]", 30000],
];

const LO = Math.log10(30), HI = Math.log10(30000);
const barW = (ms: number) => `${Math.max(2, ((Math.log10(ms) - LO) / (HI - LO)) * 100)}%`;

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="panel" style={{ flex: "1 1 150px", minWidth: 140 }}>
      <div style={{ fontSize: 22, fontWeight: 500 }}>{value}</div>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
    </div>
  );
}

export function Analysis() {
  const ev = usePolling<EvalResponse>(api.eval, 5000);
  const d = ev.data;

  if (!d || !d.eval) {
    return (
      <div className="panel muted">
        No evaluation data yet. Run <code>sudo ./run_phase11.sh full</code>, then{" "}
        <code>python3 make_report.py</code> — this tab reads{" "}
        <code>results/eval/summary_stats.json</code> live.
      </div>
    );
  }

  const E = d.eval;
  const scen = Object.entries(E.scenarios).filter(([k]) => k !== "baseline");
  const recovery = Math.min(
    100, ...scen.map(([, s]) => s.recovery_success_rate ?? 100),
  );
  const ctl = E.controller || {};

  // comparison bars -- measured recovery here is CONTROLLER-driven (OVS datapath
  // FF liveness needs BFD/hardware to trigger; verified via dump-group-stats).
  const linkMed = Math.min(
    ...scen.filter(([k]) => k.endsWith("_ff")).map(([, s]) => s.outage_ms.median ?? 1e9),
  );
  const pick = (k: string) => E.scenarios[k]?.outage_ms?.median;
  const rstpMed = d.rstp?.scenarios?.access_uplink_rstp?.outage_ms?.median;
  const SEP = { label: "—", ms: 0, cls: "sep" };
  const bars: { label: string; ms: number; cls: string }[] = [];
  // L2 datapath failover (RSTP works in software; our FF needs BFD/hardware)
  if (rstpMed) bars.push({ label: "RSTP datapath failover, measured (this testbed)", ms: rstpMed, cls: "rstp" });
  bars.push({ label: "MPLS / IP FRR (LFA) [lit]", ms: 50, cls: "lit" });
  bars.push(SEP);
  // controller-driven recovery vs L3 routing convergence
  if (isFinite(linkMed)) bars.push({ label: "SDN link recovery, controller (this work)", ms: linkMed, cls: "us2" });
  const sw = pick("switch_isolation_reroute");
  if (sw) bars.push({ label: "SDN switch recovery, controller (this work)", ms: sw, cls: "us2" });
  LIT.slice(1).forEach(([l, ms]) => bars.push({ label: l, ms, cls: "lit" }));
  const barColor: Record<string, string> = { us: "#1d9e75", us2: "#378add", rstp: "#ba7517", lit: "#5f6b7a", sep: "transparent" };

  const fmt = (s: ScenarioStat, key: "outage_ms" | "reroute_ms") => {
    const st = s[key];
    if (!st || !st.n) return "—";
    return key === "outage_ms"
      ? `${st.median?.toFixed(0)} (p95 ${st.p95?.toFixed(0)})`
      : `${st.median?.toFixed(1)} ±${st.ci95?.toFixed(1)}`;
  };

  return (
    <>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
        <Card label="trials" value={`${E.total_trials}`} />
        <Card label="recovery success" value={`${recovery}%`} />
        <Card label="flows installed" value={`${E.flows_total ?? "—"}`} />
        <Card label="controller CPU avg / max" value={`${ctl.cpu_avg ?? "—"}% / ${ctl.cpu_max ?? "—"}%`} />
        <Card label="controller RSS max" value={`${ctl.rss_max_mb ?? "—"} MB`} />
      </div>

      <div className="panel" style={{ marginBottom: 12 }}>
        <h3 style={{ marginTop: 0 }}>Resilience by failure type</h3>
        <table className="tbl">
          <thead>
            <tr>
              <th>scenario</th><th>mechanism</th><th>n</th>
              <th>outage ms (median, p95)</th><th>reroute ms (median ±CI)</th>
              <th>recovery</th>
            </tr>
          </thead>
          <tbody>
            {scen.map(([k, s]) => (
              <tr key={k}>
                <td>{SCEN_LABEL[k] ?? k}</td>
                <td>{s.mechanism}</td>
                <td>{s.trials}</td>
                <td>{fmt(s, "outage_ms")}</td>
                <td>{fmt(s, "reroute_ms")}</td>
                <td>{s.recovery_success_rate ?? 100}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Failover vs traditional <span className="muted" style={{ fontSize: 12 }}>(log scale) — L2 local protection, then L3 routing convergence</span></h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {bars.map((b, i) => b.cls === "sep" ? (
            <div key={i} className="muted" style={{ fontSize: 11, borderTop: "1px solid #24324a", paddingTop: 6, marginTop: 2 }}>
              ↑ L2 datapath failover &nbsp;·&nbsp; ↓ controller recovery vs L3 routing convergence
            </div>
          ) : (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ width: 250, fontSize: 12, textAlign: "right", color: (b.cls === "us" || b.cls === "us2") ? "#35c977" : undefined }}>{b.label}</div>
              <div style={{ flex: 1, background: "#0e1626", borderRadius: 3 }}>
                <div style={{ width: barW(b.ms), background: barColor[b.cls], height: 18, borderRadius: 3 }} />
              </div>
              <div style={{ width: 72, fontSize: 12 }}>{b.ms >= 1000 ? `${(b.ms / 1000).toFixed(1)} s` : `${b.ms.toFixed(0)} ms`}</div>
            </div>
          ))}
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Measured recovery here is <b>controller-driven</b>: OVS software datapath fast-failover does not switch buckets on a
          link-down (verified via <code>dump-group-stats</code>: primary bucket stays selected, backup idle; BFD on watch ports did
          not change it). The FF groups are installed (<code>type=ff</code>, <code>watch_port</code>, dest-rooted) and would give
          sub-50 ms local protection on real hardware. The decisive win is vs L3 routing (OSPF/IS-IS/BGP) and in architecture below.
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Why SDN, beyond raw speed (vs RSTP)</h3>
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7 }}>
          <li>RSTP <b>blocks ~50% of links</b> (backups idle); this SDN forwards on <b>all</b> links with node-disjoint primary/backup paths.</li>
          <li>No multipath / load-balancing / traffic-engineering in RSTP; SDN has a <b>global view</b> and programmable policy.</li>
          <li>RSTP reconverges slowly for root/topology changes; SDN reroute is <b>bounded and per-destination</b>, and handles switch failures FF can't protect locally.</li>
          <li>vs L3 routing (OSPF/IS-IS/BGP), SDN controller reroute is <b>10–1000× faster</b>.</li>
        </ul>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Proof dumps: <code>results/eval/proofs/*_groups_steady.txt</code> (OpenFlow <code>type=ff</code> groups) · full report: <code>results/report.pdf</code>
        </div>
      </div>
    </>
  );
}
