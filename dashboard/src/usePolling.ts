import { useEffect, useRef, useState } from "react";

/** Poll an async function on an interval; returns latest data + error + online. */
export function usePolling<T>(fn: () => Promise<T>, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await fnRef.current();
        if (alive) { setData(d); setError(null); }
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { alive = false; clearInterval(id); };
  }, [intervalMs]);

  return { data, error, online: error === null && data !== null };
}
