"""Render the REAL full 52-switch campus topology to a clean vector PDF
(docs/topology_full.pdf). Convert to PNG with:
    pdftoppm -png -r 200 docs/topology_full.pdf docs/topology_full
Run from the project root with a Python that has reportlab installed.
"""
import os
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import campus_model as cm

DV = "/usr/share/fonts/truetype/dejavu"
pdfmetrics.registerFont(TTFont("DV", DV + "/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DVB", DV + "/DejaVuSans-Bold.ttf"))

# ---- palette (matches the dashboard) ----
C_BG      = HexColor("#ffffff")
C_TITLE   = HexColor("#12324f")
C_CORE    = HexColor("#7f77dd")
C_DIST    = HexColor("#1d9e75")
C_ACCESS  = HexColor("#378add")
C_HOST    = HexColor("#6b7787")
C_CTRL    = HexColor("#22303f")
C_MESH    = HexColor("#9aa6e0")
C_DC      = HexColor("#b6c2cf")
C_PRIM    = HexColor("#1d9e75")
C_BACK    = HexColor("#67c3a3")
C_HL      = HexColor("#c6cdd5")
C_TEXT    = HexColor("#1b2430")
C_MUTE    = HexColor("#6b7787")

W, H = 1500, 470
m = cm.build()                                   # full model
v = m.dpid_view()
PODS, APP = m.pods, m.access_per_pod

x0, usable = 40, W - 80
pod_w = usable / PODS
Y_CTRL, Y_CORE, Y_DIST, Y_ACC, Y_HOST = 30, 112, 200, 300, 360


def Y(td):                                        # top-down -> reportlab bottom-up
    return H - td


pos = {}                                          # name -> (x, y_topdown)

# cores spread across full width
cores = [m.core_name(i) for i in range(1, m.cores + 1)]
for j, cname in enumerate(cores):
    pos[cname] = (x0 + (j + 0.5) * (usable / m.cores), Y_CORE)

# pods: distribution, access, hosts
for i in range(PODS):
    p = i + 1
    pod_left = x0 + i * pod_w
    pc = pod_left + pod_w / 2
    pos[m.dist_name(p, cm.PLANE_A)] = (pc - 46, Y_DIST)
    pos[m.dist_name(p, cm.PLANE_B)] = (pc + 46, Y_DIST)
    for k in range(APP):
        a = m.access_name(p, k + 1)
        ax = pod_left + (k + 0.5) * (pod_w / APP)
        pos[a] = (ax, Y_ACC)
        for n in range(m.hosts_per_access):
            hx = ax + (n - (m.hosts_per_access - 1) / 2) * 11
            pos[m.host_name(p, k + 1, n + 1)] = (hx, Y_HOST)

c = canvas.Canvas("docs/topology_full.pdf", pagesize=(W, H))
c.setFillColor(C_BG)
c.rect(0, 0, W, H, fill=1, stroke=0)


def line(a, b, col, w=0.6, dash=None):
    x1, y1 = pos[a]; x2, y2 = pos[b]
    c.setStrokeColor(col); c.setLineWidth(w)
    c.setDash(dash or [])
    c.line(x1, Y(y1), x2, Y(y2))
    c.setDash([])


# ---- links (draw beneath nodes) ----
name2 = v["name2dpid"]
half = m.cores // 2
planeA_cores = cores[:half]
planeB_cores = cores[half:]

# host -> access
for lk in m.links:
    if lk["kind"] == cm.K_HOST_ACCESS:
        line(lk["a"], lk["b"], C_HL, 0.4)

# access -> dist (primary plane A solid, backup plane B dashed)
for i in range(PODS):
    p = i + 1
    da, db = m.dist_name(p, cm.PLANE_A), m.dist_name(p, cm.PLANE_B)
    for k in range(APP):
        a = m.access_name(p, k + 1)
        line(a, da, C_PRIM, 0.7)
        line(a, db, C_BACK, 0.6, dash=[2, 2])

# dist -> core
for i in range(PODS):
    p = i + 1
    for cc in planeA_cores:
        line(m.dist_name(p, cm.PLANE_A), cc, C_DC, 0.6)
    for cc in planeB_cores:
        line(m.dist_name(p, cm.PLANE_B), cc, C_DC, 0.6)

# core mesh (arcs above the core row)
c.setStrokeColor(C_MESH); c.setLineWidth(1.1)
for a_i in range(len(cores)):
    for b_i in range(a_i + 1, len(cores)):
        x1, y1 = pos[cores[a_i]]; x2, y2 = pos[cores[b_i]]
        lift = 12 + 11 * (b_i - a_i)
        c.bezier(x1, Y(y1), x1, Y(y1 - lift), x2, Y(y2 - lift), x2, Y(y2))

# controller dashed control-plane links to cores
c.setStrokeColor(C_MUTE); c.setLineWidth(0.5); c.setDash([2, 3])
for cc in cores:
    cx, cy = pos[cc]
    c.line(W / 2, Y(Y_CTRL + 10), cx, Y(cy - 14))
c.setDash([])


def node_circle(name, col, r, label=None, fs=7):
    x, y = pos[name]
    c.setFillColor(col); c.setStrokeColor(HexColor("#ffffff")); c.setLineWidth(1)
    c.circle(x, Y(y), r, fill=1, stroke=1)
    if label:
        c.setFillColor(HexColor("#ffffff")); c.setFont("DVB", fs)
        c.drawCentredString(x, Y(y) - fs * 0.35, label)


# ---- nodes ----
# hosts
for name in m.hosts:
    x, y = pos[name]
    c.setFillColor(C_HOST); c.setStrokeColor(HexColor("#ffffff")); c.setLineWidth(0.6)
    c.rect(x - 3.2, Y(y) - 3.2, 6.4, 6.4, fill=1, stroke=1)

# access (labelled small, below node to avoid clutter)
for i in range(PODS):
    for k in range(APP):
        a = m.access_name(i + 1, k + 1)
        node_circle(a, C_ACCESS, 6)
        x, y = pos[a]
        c.setFillColor(C_MUTE); c.setFont("DV", 4.6)
        c.drawCentredString(x, Y(y) + 9, a)

# distribution
for i in range(PODS):
    for pl, tag in ((cm.PLANE_A, "a"), (cm.PLANE_B, "b")):
        d = m.dist_name(i + 1, pl)
        node_circle(d, C_DIST, 11, d, fs=6)

# cores
for cc in cores:
    node_circle(cc, C_CORE, 15, cc, fs=8)

# controller
c.setFillColor(C_CTRL); c.setStrokeColor(HexColor("#ffffff")); c.setLineWidth(1)
c.roundRect(W / 2 - 175, Y(Y_CTRL) - 13, 350, 26, 6, fill=1, stroke=1)
c.setFillColor(HexColor("#ffffff")); c.setFont("DVB", 9)
c.drawCentredString(W / 2, Y(Y_CTRL) - 3, "Ryu SDN Controller   ·   OpenFlow 1.3   ·   REST API")

# pod labels
for i in range(PODS):
    pc = x0 + i * pod_w + pod_w / 2
    c.setFillColor(C_TITLE); c.setFont("DVB", 8)
    c.drawCentredString(pc, Y(Y_DIST) + 30, "Pod %d" % (i + 1))

# ---- title & tier labels ----
c.setFillColor(C_TITLE); c.setFont("DVB", 15)
c.drawString(40, H - 24, "Hybrid Campus SDN Topology  —  52 switches, 72 hosts")
c.setFillColor(C_MUTE); c.setFont("DV", 8)
c.drawString(40, H - 36, "4 core (full mesh)  ·  12 distribution (6 pods × 2 planes)  "
                         "·  36 access (dual-homed)  ·  72 hosts")

c.setFillColor(C_MUTE); c.setFont("DVB", 7)
for lbl, yy in (("CORE", Y_CORE), ("DISTRIBUTION", Y_DIST), ("ACCESS", Y_ACC), ("HOSTS", Y_HOST)):
    c.drawString(6, Y(yy) - 3, "")   # keep left clear
    c.saveState()
    c.translate(14, Y(yy)); c.rotate(90)
    c.drawCentredString(0, 0, lbl)
    c.restoreState()

# ---- legend ----
lx, ly = 40, 30
items = [(C_CORE, "core"), (C_DIST, "distribution"), (C_ACCESS, "access"), (C_HOST, "host")]
c.setFont("DV", 7.5)
for col, lbl in items:
    c.setFillColor(col); c.circle(lx + 4, ly, 4, fill=1, stroke=0)
    c.setFillColor(C_TEXT); c.drawString(lx + 12, ly - 3, lbl)
    lx += 30 + c.stringWidth(lbl, "DV", 7.5)
# link legend
c.setStrokeColor(C_PRIM); c.setLineWidth(1)
c.line(lx + 8, ly, lx + 30, ly); c.setFillColor(C_TEXT); c.drawString(lx + 34, ly - 3, "primary (plane A)")
lx += 34 + c.stringWidth("primary (plane A)", "DV", 7.5) + 14
c.setStrokeColor(C_BACK); c.setDash([2, 2]); c.line(lx, ly, lx + 22, ly); c.setDash([])
c.setFillColor(C_TEXT); c.drawString(lx + 26, ly - 3, "backup (plane B)")

c.showPage()
c.save()
print("wrote docs/topology_full.pdf")
