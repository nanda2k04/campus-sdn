// Types mirror the Ryu controller's REST responses (controller.py, Phase 9).

export interface Reconcile {
  expected: number; observed: number; matched: number;
  missing: number; extra: number;
}
export interface ControllerStatus {
  status: string; of_version: string; uptime_s: number; ff_enabled: boolean;
  lb_enabled?: boolean;
  qos_enabled?: boolean;                 // Phase 12: load-/QoS-aware routing
  dpi_enabled?: boolean;                 // app-aware L4 classification
  queues_enabled?: boolean;             // OVS priority queues (SetQueue)
  mlag?: boolean;                        // MLAG lateral dist-peer links
  dc_enabled?: boolean;                  // dedicated data-centre tier
  role?: string;                         // "single" | "primary" | "backup"
  ha_active?: boolean;                   // this controller is the active owner
  switches_total: number; switches_connected: number; switches_down: number;
  hosts_total: number; hosts_learned: number;
  links_total: number; links_down: number;
  protected_dests: number; ff_groups: number;
  reconcile: Reconcile | null;
}
// Phase 12: /api/qos — live plane-load balance + EF (critical) pins.
export interface QosState {
  enabled: boolean;
  preferred_plane: string;
  bottleneck_mbps: Record<string, number>;
  total_mbps: Record<string, number>;
  link_cap_mbps: number;
  headroom: Record<string, boolean>;
  ef_pins: Record<string, string>;
  ef_dscp: number;
}
export interface Switch {
  name: string; dpid: number; tier: "core" | "dist" | "access";
  pod: number | null; plane: string | null; index: number | null;
  connected: boolean; status: "up" | "down"; ports: number; groups: number;
}
export interface Link {
  src: string; src_port: number; dst: string; dst_port: number;
  kind: string; status: "up" | "down"; on_spanning_tree: boolean;
  mbps?: number;
}
export interface Host {
  name: string; mac: string; ip: string; access: string;
  port: number; learned: boolean;
}
export interface Topology {
  controller: ControllerStatus; switches: Switch[]; hosts: Host[]; links: Link[];
}
export interface EventRec {
  seq: number; ts: number; event: string; [k: string]: unknown;
}
export interface Metrics {
  experiments: ExperimentResult[];
  live: { links_down: number; switches_down: number };
}
export interface ExperimentResult {
  scenario: string; mechanism: string; src: string; dst: string;
  failed_component: string | null;
  path_before: string[] | null; path_after: string[] | null;
  packets_tx: number; packets_rx: number; packet_loss: number; loss_pct: number;
  outage_ms: number;
  detection_ms: number | null; reroute_ms: number | null;
  affected_flows: number | null;
  baseline_rtt_ms: { min: number; avg: number; max: number } | null;
}

export interface Stat {
  n: number; mean?: number; median?: number; p95?: number;
  std?: number; ci95?: number; min?: number; max?: number;
}
export interface ScenarioStat {
  mechanism: string; trials: number;
  outage_ms: Stat; reroute_ms: Stat; packet_loss: Stat;
  recovery_success_rate?: number; rerouted_pct?: number;
}
export interface EvalSummary {
  trials_per_scenario: number; total_trials: number;
  controller: { cpu_avg?: number; cpu_max?: number; rss_avg_mb?: number;
    rss_max_mb?: number; samples?: number; found?: boolean };
  flows_total?: number;
  oneway_forward_outage_ms?: Record<string, Stat>;
  scenarios: Record<string, ScenarioStat>;
}
export interface EvalResponse {
  eval: EvalSummary | null;
  rstp: { scenarios: Record<string, ScenarioStat> } | null;
}

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json() as Promise<T>;
}
async function postJSON(path: string, body: unknown): Promise<{ ok: boolean; msg: string }> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export const api = {
  controller: () => getJSON<ControllerStatus>("/api/controller"),
  topology: () => getJSON<Topology>("/api/topology"),
  switches: () => getJSON<Switch[]>("/api/switches"),
  links: () => getJSON<Link[]>("/api/links"),
  qos: () => getJSON<QosState>("/api/qos"),
  hosts: () => getJSON<Host[]>("/api/hosts"),
  events: (since?: number) =>
    getJSON<EventRec[]>(`/api/events?limit=300${since ? `&since=${since}` : ""}`),
  metrics: () => getJSON<Metrics>("/api/metrics"),
  eval: () => getJSON<EvalResponse>("/api/eval"),
  failLink: (a: string, b: string) => postJSON("/api/failures/link", { a, b }),
  recoverLink: (a: string, b: string) => postJSON("/api/recovery/link", { a, b }),
  failSwitch: (name: string) => postJSON("/api/failures/switch", { name }),
  recoverSwitch: (name: string) => postJSON("/api/recovery/switch", { name }),
  reset: () => postJSON("/api/reset", {}),
  installAll: () => postJSON("/api/install_all", {}),
};
