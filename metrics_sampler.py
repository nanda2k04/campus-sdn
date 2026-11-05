"""Phase 11 -- measurement helpers: controller resource usage, flow counts, and
OpenFlow proof snapshots.  Runs in the system python3 (root), alongside Mininet.

  * ResourceSampler  -- background thread: Ryu process CPU%% + RSS (MB) over time
  * count_flows()    -- total installed flows across all OVS bridges
  * dump_groups()    -- raw `ovs-ofctl dump-groups` for a switch  (FF-group proof)
  * dump_flows()     -- raw `ovs-ofctl dump-flows`  for a switch  (dest-match proof)

Nothing here needs the controller's cooperation -- it reads the datapath and the
OS directly, so it is independent evidence, not a self-report from our own code.
"""
from __future__ import annotations

import csv
import os
import subprocess
import threading
import time

try:
    import psutil
except ImportError:                     # sampler degrades gracefully
    psutil = None

OF = "-O", "OpenFlow13"


def _find_ryu():
    """Return the psutil.Process running our Ryu controller, or None."""
    if psutil is None:
        return None
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(p.info.get("cmdline") or [])
        if "ryu-manager" in cmd or "controller.py" in cmd:
            return psutil.Process(p.info["pid"])
    return None


class ResourceSampler(threading.Thread):
    """Sample the Ryu process CPU%% and RSS on a fixed interval until stopped."""

    def __init__(self, interval=0.5, out_dir="results/eval"):
        super().__init__(daemon=True)
        self.interval = interval
        self.out_dir = out_dir
        self.rows = []                  # (t, cpu_pct, rss_mb)
        self._stopev = threading.Event()   # NOT self._stop (collides with Thread._stop)
        self._proc = _find_ryu()
        if self._proc is not None:
            try:
                self._proc.cpu_percent(None)   # prime the % counter
            except Exception:
                self._proc = None

    def run(self):
        t0 = time.time()
        while not self._stopev.is_set():
            if self._proc is not None:
                try:
                    cpu = self._proc.cpu_percent(None)
                    rss = self._proc.memory_info().rss / (1024 * 1024)
                    self.rows.append((round(time.time() - t0, 2),
                                      round(cpu, 1), round(rss, 1)))
                except Exception:
                    pass
            self._stopev.wait(self.interval)

    def stop(self):
        self._stopev.set()
        try:
            self.join(timeout=3)
        except Exception:
            pass
        return self.write()

    def write(self):
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, "resources.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t_s", "cpu_pct", "rss_mb"])
            w.writerows(self.rows)
        return path

    def summary(self):
        if not self.rows:
            return dict(samples=0, found=self._proc is not None)
        cpu = [r[1] for r in self.rows]
        rss = [r[2] for r in self.rows]
        return dict(samples=len(self.rows), found=True,
                    cpu_avg=round(sum(cpu) / len(cpu), 1), cpu_max=max(cpu),
                    rss_avg_mb=round(sum(rss) / len(rss), 1), rss_max_mb=max(rss))


def _sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=20).stdout
    except Exception as e:
        return "error: %s" % e


def list_bridges():
    out = _sh(["ovs-vsctl", "list-br"])
    return [b for b in out.split() if b]


def count_flows(bridges=None):
    """Total installed flows across all bridges (excludes the dump header line)."""
    bridges = bridges or list_bridges()
    total = 0
    per = {}
    for br in bridges:
        out = _sh(["ovs-ofctl", *OF, "dump-flows", br])
        n = sum(1 for ln in out.splitlines() if "cookie=" in ln)
        per[br] = n
        total += n
    return total, per


def dump_groups(switch):
    return _sh(["ovs-ofctl", *OF, "dump-groups", switch])


def dump_flows(switch):
    return _sh(["ovs-ofctl", *OF, "dump-flows", switch])


def dump_group_stats(switch):
    # per-bucket packet counters -- shows the backup bucket taking over on failure
    return _sh(["ovs-ofctl", *OF, "dump-group-stats", switch])


def dump_ports(switch):
    return _sh(["ovs-ofctl", *OF, "dump-ports", switch])


def save_proofs(switches, out_dir="results/eval/proofs", tag=""):
    """Save dump-groups + dump-flows for a few switches as raw evidence files."""
    os.makedirs(out_dir, exist_ok=True)
    suffix = ("_" + tag) if tag else ""
    written = []
    for sw in switches:
        for kind, fn in (("groups", dump_groups), ("flows", dump_flows)):
            path = os.path.join(out_dir, "%s_%s%s.txt" % (sw, kind, suffix))
            with open(path, "w") as fh:
                fh.write(fn(sw))
            written.append(path)
    return written
