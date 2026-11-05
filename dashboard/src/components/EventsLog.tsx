import { useMemo, useState } from "react";
import { EventRec } from "../api";

export function EventsLog({ events }: { events: EventRec[] }) {
  const [filter, setFilter] = useState("");
  const types = useMemo(
    () => Array.from(new Set(events.map((e) => e.event))).sort(),
    [events],
  );
  const shown = events
    .filter((e) => !filter || e.event === filter)
    .slice(-300)
    .reverse();

  return (
    <div className="panel">
      <h2>Events ({events.length})</h2>
      <div style={{ marginBottom: 10 }}>
        <select className="act" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">all event types</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <div className="events">
        {shown.map((e) => {
          const { seq, ts, event, ...rest } = e;
          return (
            <div className="row" key={seq}>
              <span className="muted">#{seq}</span>
              <span className="muted">{new Date((ts as number) * 1000).toLocaleTimeString()}</span>
              <span className={`ev ev-${event}`}>{event}</span>
              <span className="muted">{JSON.stringify(rest)}</span>
            </div>
          );
        })}
        {shown.length === 0 && <p className="muted">No events yet.</p>}
      </div>
    </div>
  );
}
