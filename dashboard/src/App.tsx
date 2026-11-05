import { useCallback, useEffect, useRef, useState } from "react";
import { api, ControllerStatus, EventRec, Topology } from "./api";
import { usePolling } from "./usePolling";
import { Overview } from "./components/Overview";
import { TopologyView } from "./components/TopologyView";
import { Failover } from "./components/Failover";
import { Tables } from "./components/Tables";
import { EventsLog } from "./components/EventsLog";
import { Analysis } from "./components/Analysis";

const TABS = ["Overview", "Topology", "Failover", "Analysis", "Switches", "Links", "Hosts", "Events"] as const;
type Tab = (typeof TABS)[number];

export function App() {
  const [tab, setTab] = useState<Tab>("Overview");
  const [toast, setToast] = useState<string | null>(null);

  const ctrl = usePolling<ControllerStatus>(api.controller, 1500);
  const topo = usePolling<Topology>(api.topology, 1500);

  // accumulate events across polls
  const [events, setEvents] = useState<EventRec[]>([]);
  const lastSeq = useRef(0);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const evs = await api.events(lastSeq.current || undefined);
        if (!alive || evs.length === 0) return;
        lastSeq.current = Math.max(lastSeq.current, ...evs.map((e) => e.seq));
        setEvents((prev) => [...prev, ...evs].slice(-500));
      } catch { /* backend not up yet */ }
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const flash = useCallback((m: string) => {
    setToast(m);
    setTimeout(() => setToast(null), 2600);
  }, []);

  const installAll = useCallback(async () => {
    const r = await api.installAll();
    flash(r.ok ? `Forwarding installed · ${r.msg}` : `Install failed: ${r.msg}`);
  }, [flash]);

  const resetNetwork = useCallback(async () => {
    if (!window.confirm(
      "Reset the network?\n\nThis restores ALL failed links and switches and clears the event log — a fresh start without restarting from the terminal.",
    )) return;
    const r = await api.reset();
    setEvents([]);                 // clear accumulated log client-side
    lastSeq.current = 0;           // next poll refetches the fresh (empty) log
    flash(r.ok
      ? "Network reset — all links/switches restored, event log cleared"
      : `Reset failed: ${r.msg}`);
  }, [flash]);

  const c = ctrl.data;
  return (
    <div className="app">
      <div className="topbar">
        <h1>Campus SDN · NOC</h1>
        <span className="subtitle">Fast Failover &amp; Resilience · OpenFlow 1.3</span>
        <div className="status">
          <span className={`chip ${ctrl.online ? "chip--good" : "chip--bad"}`}>
            <span className={`dot ${ctrl.online ? "up" : "down"}`} />
            {ctrl.online ? "Controller up" : "Unreachable"}
          </span>
          {c && <span className="chip"><strong>{c.switches_connected}</strong>/{c.switches_total} switches</span>}
          {c && (
            <span className={`chip ${c.links_down ? "chip--bad" : ""}`}>
              <strong>{c.links_down}</strong> link{c.links_down === 1 ? "" : "s"} down
            </span>
          )}
          {c && c.ff_enabled && <span className="chip">FF</span>}
          {c && c.lb_enabled && <span className="chip chip--on">LB</span>}
          {c && c.qos_enabled && <span className="chip chip--on">QoS</span>}
          {c && c.dpi_enabled && <span className="chip chip--on">DPI</span>}
          {c && c.queues_enabled && <span className="chip chip--on">Queues</span>}
          {c && c.mlag && <span className="chip chip--good">MLAG</span>}
          {c && c.dc_enabled && <span className="chip chip--good">DC</span>}
          {c && c.role && c.role !== "single" && (
            <span className={`chip ${c.ha_active ? "chip--good" : "chip--warn"}`}>
              HA · {c.role}{c.ha_active ? " (active)" : " (standby)"}
            </span>
          )}
          <button
            className="btn"
            onClick={installAll}
            disabled={!ctrl.online}
            title="Install forwarding for every destination now (all FF/SELECT groups, no pingall needed)"
          >
            ⚡ Install forwarding
          </button>
          <button
            className="btn btn--danger"
            onClick={resetNetwork}
            disabled={!ctrl.online}
            title="Restore all links/switches and clear the event log"
          >
            ⟲ Reset
          </button>
        </div>
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>

      <div className="content">
        {!ctrl.online && (
          <div className="panel">
            <h2>Waiting for the controller REST API…</h2>
            <p className="muted">
              Start it with <code>sudo ./run_phase9.sh smoke</code> (serves the API on
              <code> :8080</code>; this dashboard proxies <code>/api</code> to it).
            </p>
          </div>
        )}
        {tab === "Overview" && <Overview ctrl={c} events={events} />}
        {tab === "Topology" && <TopologyView topo={topo.data} flash={flash} />}
        {tab === "Failover" && <Failover events={events} />}
        {tab === "Analysis" && <Analysis />}
        {tab === "Switches" && <Tables kind="switches" topo={topo.data} flash={flash} />}
        {tab === "Links" && <Tables kind="links" topo={topo.data} flash={flash} />}
        {tab === "Hosts" && <Tables kind="hosts" topo={topo.data} flash={flash} />}
        {tab === "Events" && <EventsLog events={events} />}
      </div>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
