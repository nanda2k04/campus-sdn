#!/usr/bin/env python3
"""Render the REAL current campus topology as a clean dark SVG for slides
(black + AirPods-green theme, matching the deck). Reads campus_model so the
structure is exactly what the controller runs.

Layout is designed for legibility: Plane-A cores on the LEFT, Plane-B cores on
the RIGHT, so the dist->core uplinks form two clean colour-coded fans (green =
Plane A, blue = Plane B) instead of a cross-hatch. The DC sits in the central
gap, dual-homed one core per plane. Hosts are summarised as one bar per access.

Run:  PYTHONPATH=. ./ryu-venv/bin/python docs/gen_topo_image.py
Then rasterise the SVG to PNG.
"""
import campus_model as cm

PODS, ACC, HPA, CORES = 6, 4, 24, 4
m = cm.build(pods=PODS, access_per_pod=ACC, hosts_per_access=HPA,
             cores=CORES, mlag=True, dc=True, servers=3)

W, H = 1800, 960
MX = 80
Y_CORE, Y_DIST, Y_ACC, Y_HOST = 170, 420, 620, 760

BG = "#000000"
A = "#A8E85C"      # Plane A (green)
B = "#3b9bff"      # Plane B (blue)
MESH = "#44444c"   # core mesh (faint)
MLAGC = "#f5a623"  # MLAG peer (amber)
DCU = "#2fd4be"    # DC uplinks (teal)
HOSTC = "#2c3442"
TXT = "#ffffff"
DIM = "#8a8a94"

sw = m.switches
cores = sorted([n for n, a in sw.items() if a["tier"] == cm.CORE], key=lambda n: sw[n]["dpid"])
nA = len(cores) // 2
corePlane = {c: ("A" if i < nA else "B") for i, c in enumerate(cores)}

pos = {}

# Plane-A cores in left band, Plane-B cores in right band
A_cores = [c for c in cores if corePlane[c] == "A"]
B_cores = [c for c in cores if corePlane[c] == "B"]
def band(names, x0, x1, y):
    for i, n in enumerate(names):
        frac = (i + 0.5) / max(1, len(names))
        pos[n] = (W * x0 + (W * x1 - W * x0) * frac, y)
band(A_cores, 0.14, 0.34, Y_CORE)
band(B_cores, 0.66, 0.86, Y_CORE)

pods = sorted({a["pod"] for n, a in sw.items() if a["tier"] == cm.DIST})
podW = (W - 2 * MX) / len(pods)
for pi, p in enumerate(pods):
    left = MX + pi * podW
    for d in [n for n, a in sw.items() if a["tier"] == cm.DIST and a["pod"] == p]:
        plane = sw[d].get("plane")
        dx = left + podW * (0.32 if plane == "A" else 0.68)
        pos[d] = (dx, Y_DIST)
    accs = sorted([n for n, a in sw.items() if a["tier"] == cm.ACCESS and a["pod"] == p],
                  key=lambda n: sw[n]["index"] or 0)
    for ai, acn in enumerate(accs):
        pos[acn] = (left + podW * (ai + 0.5) / len(accs), Y_ACC)

# DC: central gap at core row, dual-homed one core per plane
dc = [n for n, a in sw.items() if a["tier"] == cm.DC]
if dc:
    pos[dc[0]] = (W * 0.5, Y_CORE + 60)

def pcolor(name):
    a = sw.get(name, {})
    pl = a.get("plane")
    if pl == "A":
        return A
    if pl == "B":
        return B
    if a.get("tier") == cm.CORE:
        return A if corePlane.get(name) == "A" else B
    return DIM

o = []
o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
o.append(f'<rect width="{W}" height="{H}" fill="{BG}"/>')
o.append(f'<text x="{MX}" y="60" fill="{TXT}" font-family="Calibri,Arial" font-size="36" font-weight="700">Campus SDN — Dual-Plane Fabric</text>')
o.append(f'<text x="{MX}" y="94" fill="{DIM}" font-family="Calibri,Arial" font-size="18">3-tier · two node-disjoint planes · OpenFlow 1.3 — {PODS} pods · {len(sw)} switches · {len(m.hosts)} hosts</text>')

def line(x1, y1, x2, y2, color, wdt, dash=None, op=1.0):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    o.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{wdt}"{d} stroke-opacity="{op}"/>')

# links (under nodes)
for lk in m.links:
    k, a, b = lk["kind"], lk["a"], lk["b"]
    if a not in pos or b not in pos:
        continue
    x1, y1 = pos[a]; x2, y2 = pos[b]
    if k == cm.K_CORE_MESH:
        line(x1, y1 - 16, x2, y2 - 16, MESH, 1.3, op=0.8)
    elif k == cm.K_DIST_CORE:
        d = a if sw[a]["tier"] == cm.DIST else b
        line(x1, y1, x2, y2, pcolor(d), 1.4, op=0.55)
    elif k == cm.K_ACCESS_DIST:
        d = a if sw[a]["tier"] == cm.DIST else b
        line(x1, y1, x2, y2, pcolor(d), 1.6, op=0.85)
    elif k == cm.K_DIST_PEER:
        line(x1, y1, x2, y2, MLAGC, 2.2, dash="6 4", op=0.95)
    elif k == cm.K_DC_CORE:
        line(x1, y1, x2, y2, DCU, 2.2, op=0.95)

# host bars under access
for n, a in sw.items():
    if a["tier"] != cm.ACCESS:
        continue
    x, y = pos[n]
    line(x, y + 8, x, Y_HOST - 6, HOSTC, 1.1, op=0.8)
    o.append(f'<rect x="{x-20:.1f}" y="{Y_HOST-5}" width="40" height="12" rx="3" fill="{HOSTC}"/>')

# DC servers (below DC, in the clear central area)
if dc:
    dx, dy = pos[dc[0]]
    sy = dy + 66
    o.append(f'<line x1="{dx}" y1="{dy+18}" x2="{dx}" y2="{sy-8}" stroke="{DCU}" stroke-width="1.6"/>')
    o.append(f'<rect x="{dx-38:.1f}" y="{sy-8}" width="76" height="15" rx="3" fill="{HOSTC}"/>')
    o.append(f'<text x="{dx}" y="{sy+24}" fill="{DIM}" font-family="Calibri,Arial" font-size="14" text-anchor="middle">3 servers</text>')

def node(x, y, color, label, r=15, dy=-24, fs=13):
    o.append(f'<rect x="{x-r:.1f}" y="{y-r*0.72:.1f}" width="{2*r}" height="{r*1.44:.1f}" rx="5" fill="#0c0c0e" stroke="{color}" stroke-width="2.2"/>')
    o.append(f'<text x="{x:.1f}" y="{y+dy}" fill="{TXT}" font-family="Calibri,Arial" font-size="{fs}" font-weight="600" text-anchor="middle">{label}</text>')

for c in cores:
    x, y = pos[c]; node(x, y, pcolor(c), c, r=17, fs=14)
for n, a in sw.items():
    if a["tier"] == cm.DIST:
        x, y = pos[n]; node(x, y, pcolor(n), n, r=13, dy=-19, fs=12)
for n, a in sw.items():
    if a["tier"] == cm.ACCESS:
        x, y = pos[n]; node(x, y, DIM, n, r=12, dy=20, fs=11)
if dc:
    x, y = pos[dc[0]]; node(x, y, DCU, "DC", r=18, dy=-24, fs=14)

# plane captions over the core bands
o.append(f'<text x="{W*0.24:.0f}" y="{Y_CORE-46}" fill="{A}" font-family="Calibri,Arial" font-size="17" font-weight="700" text-anchor="middle">PLANE A</text>')
o.append(f'<text x="{W*0.76:.0f}" y="{Y_CORE-46}" fill="{B}" font-family="Calibri,Arial" font-size="17" font-weight="700" text-anchor="middle">PLANE B</text>')

for lbl, yy in [("CORE", Y_CORE), ("DISTRIBUTION", Y_DIST), ("ACCESS", Y_ACC), ("HOSTS", Y_HOST)]:
    o.append(f'<text x="{MX-24}" y="{yy+4}" fill="{DIM}" font-family="Calibri,Arial" font-size="12" font-weight="700" text-anchor="end">{lbl}</text>')

# legend
ly = H - 40
cx = MX
for col, t in [(A, "Plane A links"), (B, "Plane B links"), (MLAGC, "MLAG peer"), (DCU, "DC uplinks"), (MESH, "core mesh")]:
    o.append(f'<rect x="{cx}" y="{ly-11}" width="24" height="6" rx="3" fill="{col}"/>')
    o.append(f'<text x="{cx+32}" y="{ly-2}" fill="{DIM}" font-family="Calibri,Arial" font-size="14">{t}</text>')
    cx += 56 + len(t) * 8
o.append(f'<text x="{W-MX}" y="{ly-2}" fill="{DIM}" font-family="Calibri,Arial" font-size="14" text-anchor="end">each ACCESS bar = {HPA} hosts · access dual-homed to both planes</text>')
o.append('</svg>')

with open("docs/topology_dark.svg", "w") as fh:
    fh.write("\n".join(o))
print("wrote docs/topology_dark.svg  (%d switches, %d hosts)" % (len(sw), len(m.hosts)))
