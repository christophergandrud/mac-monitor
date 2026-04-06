#!/usr/bin/env python3
"""Mac System Monitor — htmx + audible score charts."""

import collections, time, os, platform, socket, random, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import theme as _theme
_T  = _theme.load()   # active theme dict — reloaded on /theme poll

try:
    import claude_monitor as _cm
    HAS_CM = True
except ImportError:
    HAS_CM = False

def _reload_theme():
    global _T
    _T = _theme.load()
    return _theme.css_vars(_T)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("  psutil not found — pip install psutil  (running with simulation)")

# ── constants ─────────────────────────────────────────────────────────────────
BUF  = 60
PORT = 8787
DASH = ["", "6,3", "2,3", "8,2,2,2", "1,4", "5,2,1,2",
        "4,2", "3,1,1,1", "10,3", "5,1,2,1", "4,1", "2,2"]


# ── rolling buffers ───────────────────────────────────────────────────────────
_nc      = (psutil.cpu_count(logical=True) or 4) if HAS_PSUTIL else 4
CPU_BUFS = [collections.deque([random.uniform(5, 35)] * BUF, maxlen=BUF)
            for _ in range(_nc)]
MEM_BUF  = collections.deque([random.uniform(40, 60)] * BUF, maxlen=BUF)
SWAP_BUF = collections.deque([random.uniform(2,  10)] * BUF, maxlen=BUF)
NET_TX   = collections.deque([0] * BUF, maxlen=BUF)
NET_RX   = collections.deque([0] * BUF, maxlen=BUF)
DISK_R   = collections.deque([0] * BUF, maxlen=BUF)
DISK_W   = collections.deque([0] * BUF, maxlen=BUF)

_p_net = _p_disk = None
_p_t   = time.time()
_last_collect = 0.0

# ── Claude context-fill history ───────────────────────────────────────────────
# pid → deque of context_pct floats; updated each time /metrics/claude is polled
_CTX_BUF = 40   # ~2 min at 3s poll interval
_ctx_history: dict = {}  # pid → collections.deque

# ── data collection ───────────────────────────────────────────────────────────

def maybe_collect():
    global _p_net, _p_disk, _p_t, _last_collect
    now = time.time()
    if now - _last_collect < 0.85:
        return
    _last_collect = now

    if not HAS_PSUTIL:
        for b in CPU_BUFS:
            b.append(max(0, min(100, b[-1] + random.gauss(0, 3))))
        MEM_BUF.append(max(0, min(100, MEM_BUF[-1]  + random.gauss(0, 0.8))))
        SWAP_BUF.append(max(0, min(100, SWAP_BUF[-1] + random.gauss(0, 0.3))))
        NET_TX.append(abs(NET_TX[-1] + random.gauss(0, 200_000)))
        NET_RX.append(abs(NET_RX[-1] + random.gauss(0, 500_000)))
        DISK_R.append(abs(DISK_R[-1] + random.gauss(0, 2_000_000)))
        DISK_W.append(abs(DISK_W[-1] + random.gauss(0, 500_000)))
        return

    for b, v in zip(CPU_BUFS, psutil.cpu_percent(percpu=True)):
        b.append(v)

    m = psutil.virtual_memory()
    MEM_BUF.append(m.percent)
    SWAP_BUF.append(psutil.swap_memory().percent)

    net = psutil.net_io_counters()
    dt  = max(now - _p_t, 0.01)
    if _p_net:
        NET_TX.append(max(0, (net.bytes_sent - _p_net.bytes_sent) / dt))
        NET_RX.append(max(0, (net.bytes_recv - _p_net.bytes_recv) / dt))
    _p_net = net

    try:
        dk = psutil.disk_io_counters()
        if dk and _p_disk:
            DISK_R.append(max(0, (dk.read_bytes  - _p_disk.read_bytes)  / dt))
            DISK_W.append(max(0, (dk.write_bytes - _p_disk.write_bytes) / dt))
        _p_disk = dk
    except Exception:
        pass
    _p_t = now


def _val_to_hz(v, ymax, midi_lo=52, midi_hi=84):
    """Map value → Hz via MIDI scale (perceptually equal semitone steps)."""
    ratio = min(max(v, 0), ymax) / max(ymax, 0.001)
    midi  = midi_lo + ratio * (midi_hi - midi_lo)
    return int(round(440 * 2 ** ((midi - 69) / 12)))


def _slope_cents(vs, i, ymax):
    """Proportional slope → detune cents, clamped to ±80."""
    if i == 0:
        return 0
    raw = (vs[i] - vs[i - 1]) / max(ymax, 0.001) * 400
    return int(max(-80, min(80, raw)))


def fmt_bytes(b):
    b = max(0, b)
    if b < 1024:  return f"{b:.0f}B/s"
    if b < 1<<20: return f"{b/1024:.1f}K/s"
    return f"{b/(1<<20):.1f}M/s"


def get_sysinfo():
    if not HAS_PSUTIL:
        return dict(host=socket.gethostname(), os_ver=platform.system(),
                    up="N/A", load="N/A", cores=_nc, ram="N/A")
    up  = int(time.time() - psutil.boot_time())
    mem = psutil.virtual_memory()
    try:    load = " / ".join(f"{x:.2f}" for x in os.getloadavg())
    except: load = "N/A"
    return dict(
        host   = socket.gethostname(),
        os_ver = platform.mac_ver()[0] or platform.system(),
        up     = f"{up//3600}h {(up%3600)//60}m",
        load   = load,
        cores  = _nc,
        ram    = f"{mem.total/(1<<30):.1f} GB",
    )


def get_procs(q="", sort="cpu"):
    if not HAS_PSUTIL:
        names = ["kernel_task","python3","bash","Finder","Safari",
                 "node","redis-server","postgres","vim","tmux"]
        rows  = [dict(pid=i*11, name=names[i % len(names)],
                      cpu=random.uniform(0, 25), mem=random.uniform(0, 6),
                      status="running") for i in range(1, 21)]
    else:
        rows = []
        for p in psutil.process_iter(
                ['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
            try:
                i = p.info
                rows.append(dict(pid=i['pid'], name=i['name'] or "",
                                 cpu=i['cpu_percent']    or 0.0,
                                 mem=i['memory_percent'] or 0.0,
                                 status=i['status'] or ""))
            except Exception:
                pass

    if q:
        rows = [r for r in rows if q.lower() in r['name'].lower()]

    key_fns = {
        "cpu":  (lambda r: r['cpu'],  True),
        "mem":  (lambda r: r['mem'],  True),
        "name": (lambda r: r['name'], False),
        "pid":  (lambda r: r['pid'],  False),
    }
    fn, rev = key_fns.get(sort, key_fns["cpu"])
    return sorted(rows, key=fn, reverse=rev)[:30]


# ── SVG helpers ───────────────────────────────────────────────────────────────

def _make_pts(buf, pl, pr, pt, pb, w, h, ymax):
    vs = list(buf); n = len(vs)
    iw = w - pl - pr; ih = h - pt - pb
    return [(pl + int(i / (n - 1) * iw),
             pt + ih - int(min(max(vs[i], 0), ymax) / ymax * ih))
            for i in range(n)]


def _hover_rects(buf, pl, pr, pt, pb, w, h, ymax, voice=0, midi_lo=52, midi_hi=84,
                 voice_fn=None):
    """voice_fn(i) overrides the fixed voice per column (used by CPU chart)."""
    vs = list(buf); n = len(vs)
    iw = w - pl - pr; ih = h - pt - pb
    cw = max(1, iw // n)
    out = ""
    for i, v in enumerate(vs):
        freq = _val_to_hz(v, ymax, midi_lo, midi_hi)
        det  = _slope_cents(vs, i, ymax)
        vi   = voice_fn(i) if voice_fn else voice
        x    = pl + int(i / (n - 1) * iw)
        out += (f'<rect x="{x}" y="{pt}" width="{cw+1}" height="{ih}" '
                f'fill="transparent" style="cursor:crosshair" '
                f'onmouseover="window._htmxBeep&&window._htmxBeep({freq},{det},{vi})"/>')
    return out


def _grid(pl, pr, pt, pb, w, h, ymax, pcts, fmt_fn):
    ih  = h - pt - pb
    out = ""
    for pct in pcts:
        y  = pt + ih - int(pct / 100 * ih)
        yv = ymax * pct / 100
        out += (f'<line x1="{pl}" y1="{y}" x2="{w-pr}" y2="{y}" '
                f'stroke="var(--t-border)" stroke-width="0.5" stroke-dasharray="2,3"/>'
                f'<text x="{pl-3}" y="{y+3}" font-size="7" text-anchor="end" '
                f'fill="var(--t-muted)" font-family="Courier,monospace">{fmt_fn(yv)}</text>')
    return out


def _axes(pl, pr, pt, pb, w, h):
    ih = h - pt - pb
    return (f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{pt+ih}" '
            f'stroke="var(--t-border)" stroke-width="1.5"/>'
            f'<line x1="{pl}" y1="{pt+ih}" x2="{w-pr}" y2="{pt+ih}" '
            f'stroke="var(--t-border)" stroke-width="1.5"/>')


def _svg_line(buf, pl, pr, pt, pb, w, h, ymax, dash, label, color,
              opacity=0.82, label_fn=None):
    """Render one polyline with an end-label. label_fn(vs) formats the current value."""
    if label_fn is None:
        label_fn = lambda vs: fmt_bytes(vs[-1])
    pts = _make_pts(buf, pl, pr, pt, pb, w, h, ymax)
    vs  = list(buf)
    d   = _pts_to_path(pts)
    da  = f'stroke-dasharray="{dash}"' if dash else ""
    lx, ly = pts[-1]
    tag = (f'<text x="{lx-2}" y="{max(ly-5, pt+10)}" text-anchor="end" '
           f'font-size="8" fill="{color}" font-family="Courier,monospace">'
           f'{label}:{label_fn(vs)}</text>')
    return (f'<path d="{d}" fill="none" stroke="{color}" '
            f'stroke-width="1.2" {da} opacity="{opacity}"/>{tag}')


def _pts_to_path(pts):
    return f"M{pts[0][0]},{pts[0][1]}" + "".join(f" L{x},{y}" for x, y in pts[1:])


def _stamp(w, h):
    return (f'<text x="{w-8}" y="{h}" text-anchor="end" font-size="8" '
            f'fill="var(--t-muted)" font-family="Courier,monospace">'
            f'upd {time.strftime("%H:%M:%S")}</text>')


# ── charts ────────────────────────────────────────────────────────────────────

def svg_cpu_score(*, w=720, h=270):
    pl, pr, pt, pb = 32, 36, 12, 22
    iw = w - pl - pr; ih = h - pt - pb; n = BUF

    grid = _grid(pl, pr, pt, pb, w, h, 100,
                 (20, 40, 60, 80, 100), lambda v: f"{int(v)}%")

    snap = [list(b) for b in CPU_BUFS]
    avgs = [sum(snap[c][i] for c in range(_nc)) / _nc for i in range(n)]
    # Voice per column = dominant core's index mod 3 — timbre shifts as load migrates
    hov = _hover_rects(
        collections.deque(avgs), pl, pr, pt, pb, w, h, 100,
        midi_lo=52, midi_hi=76,
        voice_fn=lambda i: max(range(_nc), key=lambda c: snap[c][i]) % 3,
    )

    lines = ""
    for ci, buf in enumerate(CPU_BUFS):
        dash  = DASH[ci % len(DASH)]
        color = f"var(--t-c{ci % 3})"
        sw    = 1.5 if ci % 3 == 0 else (0.9 if ci % 3 == 1 else 1.2)
        pts   = _make_pts(buf, pl, pr, pt, pb, w, h, 100)
        d     = _pts_to_path(pts)
        da    = f'stroke-dasharray="{dash}"' if dash else ""
        lines += (f'<path d="{d}" fill="none" stroke="{color}" '
                  f'stroke-width="{sw}" {da} opacity="0.72"/>')
        lx, ly = pts[-1]
        lines += (f'<text x="{lx+3}" y="{min(ly+3, h-pb-2)}" font-size="7" '
                  f'fill="{color}" font-family="Courier,monospace">C{ci}</text>')

    # Each time step = [avg-voice0, max-core-voice1, min-core-voice2] played as a chord
    freqs_data = json.dumps([
        [
            [_val_to_hz(avgs[i], 100, 52, 76), _slope_cents(avgs, i, 100), 0],
            [_val_to_hz(max(snap[c][i] for c in range(_nc)), 100, 52, 76), _slope_cents(avgs, i, 100), 1],
            [_val_to_hz(min(snap[c][i] for c in range(_nc)), 100, 52, 76), _slope_cents(avgs, i, 100), 2],
        ]
        for i, a in enumerate(avgs)
    ])
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'data-freqs=\'{freqs_data}\' data-pl="{pl}" data-iw="{w-pl-pr}" data-w="{w}" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}{lines}{_axes(pl,pr,pt,pb,w,h)}{hov}{_stamp(w,h)}</svg>')


def svg_dual(buf_a, buf_b, la, lb, *, w=460, h=160):
    pl, pr, pt, pb = 56, 8, 12, 22
    all_v = [v for v in list(buf_a) + list(buf_b) if v > 0]
    ymax  = max(max(all_v) * 1.15, 1) if all_v else 1

    grid = _grid(pl, pr, pt, pb, w, h, ymax,
                 (25, 50, 75, 100), fmt_bytes)
    hovA = _hover_rects(buf_a, pl, pr, pt, pb, w, h, ymax, voice=0, midi_lo=56, midi_hi=80)
    hovB = _hover_rects(buf_b, pl, pr, pt, pb, w, h, ymax, voice=1, midi_lo=56, midi_hi=80)

    vs_a = list(buf_a); vs_b = list(buf_b)
    freqs_data = json.dumps([
        [
            [_val_to_hz(vs_a[i], ymax, 56, 80), _slope_cents(vs_a, i, ymax), 0],
            [_val_to_hz(vs_b[i], ymax, 56, 80), _slope_cents(vs_b, i, ymax), 1],
        ]
        for i in range(len(vs_a))
    ])
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'data-freqs=\'{freqs_data}\' data-pl="{pl}" data-iw="{w-pl-pr}" data-w="{w}" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}'
            f'{_svg_line(buf_a,pl,pr,pt,pb,w,h,ymax,"",la,"var(--t-c0)")}'
            f'{_svg_line(buf_b,pl,pr,pt,pb,w,h,ymax,"6,3",lb,"var(--t-c1)")}'
            f'{_axes(pl,pr,pt,pb,w,h)}{hovA}{hovB}{_stamp(w,h)}</svg>')


def svg_mem(*, w=460, h=150):
    pl, pr, pt, pb = 32, 8, 12, 22
    hov_m = _hover_rects(MEM_BUF,  pl, pr, pt, pb, w, h, 100, voice=0, midi_lo=60, midi_hi=84)
    hov_s = _hover_rects(SWAP_BUF, pl, pr, pt, pb, w, h, 100, voice=2, midi_lo=60, midi_hi=84)
    grid  = _grid(pl, pr, pt, pb, w, h, 100,
                  (25, 50, 75, 100), lambda v: f"{int(v)}%")

    pct_label = lambda vs: f"{vs[-1]:.1f}%"
    vs_m = list(MEM_BUF); vs_s = list(SWAP_BUF)
    freqs_data = json.dumps([
        [
            [_val_to_hz(vs_m[i], 100, 60, 84), _slope_cents(vs_m, i, 100), 0],
            [_val_to_hz(vs_s[i], 100, 60, 84), _slope_cents(vs_s, i, 100), 2],
        ]
        for i in range(len(vs_m))
    ])
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'data-freqs=\'{freqs_data}\' data-pl="{pl}" data-iw="{w-pl-pr}" data-w="{w}" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}'
            f'{_svg_line(MEM_BUF, pl,pr,pt,pb,w,h,100,"","MEM","var(--t-c0)",0.85,pct_label)}'
            f'{_svg_line(SWAP_BUF,pl,pr,pt,pb,w,h,100,"5,3","SWAP","var(--t-c2)",0.75,pct_label)}'
            f'{_axes(pl,pr,pt,pb,w,h)}{hov_m}{hov_s}{_stamp(w,h)}</svg>')


# ── HTML fragments ────────────────────────────────────────────────────────────

def html_gauges():
    items = [
        ("CPU",  sum(b[-1] for b in CPU_BUFS) / _nc),
        ("MEM",  MEM_BUF[-1]),
        ("SWAP", SWAP_BUF[-1]),
    ]
    rows = ""
    for label, pct in items:
        pct  = min(max(pct, 0), 100)
        freq = _val_to_hz(pct, 100, 60, 84)
        rows += (
            f'<div style="margin:.5rem 0">'
            f'<div style="display:flex;justify-content:space-between;font-weight:bold">'
            f'<span>{label}</span><span>{pct:.1f}%</span></div>'
            f'<div style="background:var(--t-panel);border:1px solid var(--t-border);height:10px;'
            f'overflow:hidden;cursor:crosshair" '
            f'onmouseover="window._htmxBeep&&window._htmxBeep({freq})">'
            f'<div style="background:var(--t-c0);width:{pct:.1f}%;height:100%"></div>'
            f'</div></div>'
        )
    rows += (f'<p style="font-size:9px;margin-top:.5rem">'
             f'upd {time.strftime("%H:%M:%S")}</p>')
    return rows


def html_sysinfo():
    si = get_sysinfo()
    pairs = [
        ("HOST",  si["host"]),
        ("OS",    si["os_ver"]),
        ("UP",    si["up"]),
        ("LOAD",  si["load"]),
        ("CORES", str(si["cores"])),
        ("RAM",   si["ram"]),
        ("",      time.strftime("%H:%M:%S")),
    ]
    return "".join(
        f'<span style="margin-right:1rem;font-family:Courier,monospace;font-size:11px">'
        f'{"<b>"+k+"</b>&nbsp;" if k else ""}{v}</span>'
        for k, v in pairs
    )


def html_proc_rows(q="", sort="cpu"):
    procs = get_procs(q, sort)
    if not procs:
        return ("<tr><td colspan='5' "
                "style='text-align:center;font-family:Courier,monospace;font-size:11px'>"
                "-- NO MATCH --</td></tr>")
    rows = ""
    for p in procs:
        bw = min(int(p["cpu"]), 100)
        rows += (
            f'<tr>'
            f'<td>{p["pid"]}</td>'
            f'<td>{p["name"][:32]}</td>'
            f'<td><div style="display:flex;align-items:center;gap:.3rem">'
            f'<div style="background:var(--t-panel);border:1px solid var(--t-border);'
            f'width:48px;height:6px;flex-shrink:0">'
            f'<div style="background:var(--t-c1);width:{bw}%;height:100%"></div></div>'
            f'{p["cpu"]:.1f}%</div></td>'
            f'<td>{p["mem"]:.1f}%</td>'
            f'<td>{p["status"]}</td>'
            f'</tr>'
        )
    return rows


def html_search_results(q):
    if not q or len(q) < 2:
        return ""
    q_low = q.lower()
    maybe_collect()

    si = get_sysinfo()
    KEYWORDS = {
        "cpu":     ("CPU AVG",  f"{sum(b[-1] for b in CPU_BUFS) / _nc:.1f}%"),
        "mem":     ("MEMORY",   f"{MEM_BUF[-1]:.1f}%"),
        "memory":  ("MEMORY",   f"{MEM_BUF[-1]:.1f}%"),
        "swap":    ("SWAP",     f"{SWAP_BUF[-1]:.1f}%"),
        "net":     ("NETWORK",  f"TX {fmt_bytes(NET_TX[-1])}  RX {fmt_bytes(NET_RX[-1])}"),
        "network": ("NETWORK",  f"TX {fmt_bytes(NET_TX[-1])}  RX {fmt_bytes(NET_RX[-1])}"),
        "disk":    ("DISK",     f"R {fmt_bytes(DISK_R[-1])}  W {fmt_bytes(DISK_W[-1])}"),
        "load":    ("LOAD AVG", si["load"]),
        "uptime":  ("UPTIME",   si["up"]),
        "ram":     ("RAM",      si["ram"]),
        "cores":   ("CORES",    str(si["cores"])),
    }

    out = ""
    seen = set()
    for kw, (label, val) in KEYWORDS.items():
        if (q_low in kw or kw in q_low) and label not in seen:
            seen.add(label)
            out += (f'<div style="border-bottom:1px solid #ddd;padding:.2rem 0;'
                    f'font-family:Courier,monospace;font-size:11px">'
                    f'<b>{label}</b> &mdash; {val}</div>')

    procs = get_procs(q)[:6]
    if procs:
        out += ('<div style="font-family:Courier,monospace;font-size:10px;font-weight:bold;'
                'padding:.25rem 0;border-bottom:1px solid #000;margin-top:.2rem">'
                'PROCESSES</div>')
        for p in procs:
            out += (f'<div style="font-family:Courier,monospace;font-size:11px;padding:.1rem 0">'
                    f'{p["pid"]:>6}&nbsp;&nbsp;{p["name"][:22]:<22}&nbsp;&nbsp;'
                    f'cpu&nbsp;{p["cpu"]:5.1f}%&nbsp;&nbsp;mem&nbsp;{p["mem"]:4.1f}%</div>')

    if not out:
        out = ('<div style="font-family:Courier,monospace;font-size:11px;padding:.2rem 0">'
               '-- no results --</div>')
    return out



# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def send_html(self, body, status=200):
        b = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(b))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        print(f"  {self.command:6} {self.path}")

    def do_GET(self):
        p    = urlparse(self.path)
        qs   = parse_qs(p.query)
        path = p.path
        q    = qs.get("q",    [""])[0]
        sort = qs.get("sort", ["cpu"])[0]

        routes = {
            "/":                lambda: _build_index(),
            "/theme":           lambda: _reload_theme(),
            "/metrics/cpu":     lambda: (maybe_collect(), svg_cpu_score())[1],
            "/metrics/memory":  lambda: (maybe_collect(), svg_mem())[1],
            "/metrics/gauges":  lambda: (maybe_collect(), html_gauges())[1],
            "/metrics/network": lambda: (maybe_collect(), svg_dual(NET_TX, NET_RX, "TX", "RX"))[1],
            "/metrics/disk":    lambda: (maybe_collect(), svg_dual(DISK_R, DISK_W, "READ", "WRITE"))[1],
            "/metrics/sysinfo": lambda: html_sysinfo(),
            "/processes":       lambda: (maybe_collect(), html_proc_rows(q, sort))[1],
            "/search":          lambda: html_search_results(q),
            "/tab/system":      lambda: html_system_tab(),
            "/tab/claude":      lambda: html_claude_tab(),
            "/metrics/claude":  lambda: html_claude_instances(),
            "/metrics/daily":   lambda: html_daily_stats(),
        }
        fn = routes.get(path)
        if fn:
            self.send_html(fn())
        elif path.startswith("/claude/focus/"):
            try:
                pid = int(path.split("/")[-1])
                ok = HAS_CM and _cm.focus_terminal(pid)
                self.send_html("ok" if ok else "fail")
            except (ValueError, Exception):
                self.send_html("fail", 400)
        else:
            self.send_html("<p>not found</p>", 404)

    def do_POST(self):
        p    = urlparse(self.path)
        qs   = parse_qs(p.query)
        slug = qs.get("slug", [""])[0]
        if p.path == "/theme/set" and slug:
            global _T
            try:
                _T = _theme.set_theme(slug)
            except FileNotFoundError:
                self.send_html("<p>unknown theme</p>", 400)
                return
            self.send_html(_theme.css_vars(_T))
        elif p.path.startswith("/claude/focus/"):
            try:
                pid = int(p.path.split("/")[-1])
                ok = HAS_CM and _cm.focus_terminal(pid)
                self.send_html("ok" if ok else "fail")
            except (ValueError, Exception):
                self.send_html("fail", 400)
        else:
            self.send_html("<p>not found</p>", 404)


# ── tab content ───────────────────────────────────────────────────────────────

def html_system_tab() -> str:
    return """<div class="page">

  <!-- CPU Score -->
  <div class="group">CPU</div>
  <div class="card">
    <div class="card-head">
      <span class="card-title">Per-Core Activity &mdash; 60s rolling</span>
      <button onclick="playChart('cpu-chart',this)" class="play-btn">[ PLAY ]</button>
      <span class="badge">every 2s</span>
    </div>
    <div class="card-body">
      <div id="cpu-chart" hx-get="/metrics/cpu" hx-trigger="load, every 2s" hx-swap="innerHTML">
        <div style="height:270px"></div>
      </div>
    </div>
  </div>

  <!-- Memory -->
  <div class="group">Memory</div>
  <div class="grid-2">
    <div class="card">
      <div class="card-head">
        <span class="card-title">RAM &amp; Swap &mdash; 60s rolling</span>
        <button onclick="playChart('mem-chart',this)" class="play-btn">[ PLAY ]</button>
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div id="mem-chart" hx-get="/metrics/memory" hx-trigger="load, every 2s" hx-swap="innerHTML">
          <div style="height:150px"></div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">Utilisation Gauges</span>
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div hx-get="/metrics/gauges" hx-trigger="load, every 2s" hx-swap="innerHTML">
          <div style="min-height:100px"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- I/O -->
  <div class="group">I/O</div>
  <div class="grid-2">
    <div class="card">
      <div class="card-head">
        <span class="card-title">Network &mdash; TX / RX</span>
        <button onclick="playChart('net-chart',this)" class="play-btn">[ PLAY ]</button>
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div id="net-chart" hx-get="/metrics/network" hx-trigger="load, every 2s" hx-swap="innerHTML">
          <div style="height:160px"></div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">Disk &mdash; Read / Write</span>
        <button onclick="playChart('disk-chart',this)" class="play-btn">[ PLAY ]</button>
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div id="disk-chart" hx-get="/metrics/disk" hx-trigger="load, every 2s" hx-swap="innerHTML">
          <div style="height:160px"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Processes -->
  <div class="group">Processes</div>
  <div class="card">
    <div class="card-head">
      <span class="card-title">Top 30 Processes</span>
      <span class="badge">every 3s</span>
    </div>
    <div class="card-body">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.6rem">
        <input type="text" id="proc-q" name="q"
               placeholder="filter by name..."
               style="width:220px"
               hx-get="/processes"
               hx-trigger="keyup changed delay:300ms"
               hx-target="#proc-tbody"
               hx-include="#proc-q,#proc-sort">
        <input type="hidden" id="proc-sort" name="sort" value="cpu">
        <span class="htmx-indicator">[ searching... ]</span>
      </div>
      <table>
        <thead>
          <tr>
            <th hx-get="/processes?sort=pid"
                hx-target="#proc-tbody" hx-include="#proc-q"
                onclick="document.getElementById('proc-sort').value='pid'">PID</th>
            <th hx-get="/processes?sort=name"
                hx-target="#proc-tbody" hx-include="#proc-q"
                onclick="document.getElementById('proc-sort').value='name'">Name</th>
            <th hx-get="/processes?sort=cpu"
                hx-target="#proc-tbody" hx-include="#proc-q"
                onclick="document.getElementById('proc-sort').value='cpu'">CPU %</th>
            <th hx-get="/processes?sort=mem"
                hx-target="#proc-tbody" hx-include="#proc-q"
                onclick="document.getElementById('proc-sort').value='mem'">MEM %</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="proc-tbody"
               hx-get="/processes" hx-trigger="load, every 3s"
               hx-swap="innerHTML"></tbody>
      </table>
    </div>
  </div>

</div>"""


def html_claude_tab() -> str:
    """Shell for the Claude tab — instances and daily stats poll independently."""
    return """<div class="page">
  <div class="group">Active Instances</div>
  <div id="claude-instances"
       hx-get="/metrics/claude"
       hx-trigger="load, every 3s"
       hx-swap="innerHTML">
    <div style="padding:1rem;color:var(--t-muted);font-size:11px">scanning...</div>
  </div>

  <div class="group">Today &amp; 7-Day Activity</div>
  <div id="claude-daily"
       hx-get="/metrics/daily"
       hx-trigger="load, every 10s"
       hx-swap="innerHTML">
    <div style="padding:.5rem;color:var(--t-muted);font-size:11px">loading...</div>
  </div>
</div>"""


def _uptime_str(s: int) -> str:
    if s < 60:     return f"{s}s"
    if s < 3600:   return f"{s//60}m {s%60:02d}s"
    return f"{s//3600}h {(s%3600)//60:02d}m"


def _ctx_record(pid: int, pct: float) -> list:
    """Append context pct to rolling buffer for pid; return the buffer as a list."""
    if pid not in _ctx_history:
        _ctx_history[pid] = collections.deque(maxlen=_CTX_BUF)
    _ctx_history[pid].append(pct)
    return list(_ctx_history[pid])


def _ctx_sparkline(vals: list) -> str:
    """Tiny 100×20 SVG polyline of context-fill history."""
    if len(vals) < 2:
        return ""
    w, h = 100, 20
    n = len(vals)
    pts = " ".join(
        f"{i/(n-1)*w:.1f},{h - v/100*h:.1f}"
        for i, v in enumerate(vals)
    )
    latest = vals[-1]
    color = ("#c0392b" if latest >= 90
             else ("var(--t-c1)" if latest >= 75
                   else "var(--t-c0)"))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'style="display:inline-block;vertical-align:middle;margin-left:5px;opacity:.85">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def html_claude_instances() -> str:
    if not HAS_CM:
        return '<div style="padding:1rem;color:var(--t-muted);font-size:11px">claude_monitor not available</div>'

    instances = _cm.find_instances()

    if not instances:
        desktop = _cm.claude_desktop_process()
        if desktop:
            try:
                mem = desktop.memory_info().rss / (1024*1024)
            except Exception:
                mem = 0
            return (f'<div class="claude-instance"><div class="claude-head">'
                    f'<strong>Claude Desktop</strong>'
                    f'<span style="font-size:10px;color:var(--t-muted)">pid {desktop.pid} &mdash; {mem:.0f} MB</span>'
                    f'</div></div>')
        return '<div style="padding:1rem;color:var(--t-muted);font-size:11px">[ no claude code instances running ]</div>'

    parts = []
    for inst in instances:
        # ── attention flags ───────────────────────────────────────────────────
        flags_html = ""
        if inst.attention:
            flag_parts = []
            for f in inst.attention:
                css = "attention-flag critical" if f.kind in ("context", "ratelimit") else "attention-flag"
                flag_parts.append(f'<span class="{css}" title="{_esc(f.message)}">{_esc(f.kind)}</span>')
            flags_html = f'<div class="attention-flags">{"".join(flag_parts)}</div>'

        # ── context bar + sparkline ───────────────────────────────────────────
        ctx_html = "—"
        if inst.tokens:
            pct      = inst.tokens.context_pct
            history  = _ctx_record(inst.pid, pct)
            fill_css = "ctx-fill crit" if pct >= 90 else ("ctx-fill warn" if pct >= 75 else "ctx-fill")
            ctx_html = (f'<span>{pct:.0f}%</span>'
                        f'<span class="ctx-bar"><span class="{fill_css}" style="width:{min(pct,100):.0f}%"></span></span>'
                        f' {inst.tokens.context_used:,}&thinsp;/&thinsp;{inst.tokens.context_max:,} tok'
                        f'{_ctx_sparkline(history)}')

        # ── cost ──────────────────────────────────────────────────────────────
        cost_html = "—"
        if inst.tokens and inst.tokens.session_cost > 0:
            cost_html = f'<span>${inst.tokens.session_cost:.3f}</span> <span style="color:var(--t-muted);font-size:10px">API equiv.</span>'

        # ── current tool ─────────────────────────────────────────────────────
        tool_html = "—"
        if inst.current_tool:
            tc = inst.current_tool
            status_col = "var(--t-accent)" if tc.status == "active" else "var(--t-muted)"
            tool_html = f'<span style="color:{status_col}">{_esc(tc.name)}</span> {_esc(tc.summary)}'

        # ── model ─────────────────────────────────────────────────────────────
        model_short = ""
        if inst.tokens and inst.tokens.model:
            m = inst.tokens.model
            if "opus" in m:      model_short = "opus"
            elif "sonnet" in m:  model_short = "sonnet"
            elif "haiku" in m:   model_short = "haiku"
            else:                model_short = m.split("-")[1] if "-" in m else m

        # ── agents ───────────────────────────────────────────────────────────
        agents_html = ""
        if inst.agents:
            rows = []
            for ag in inst.agents[:4]:
                st_sym = "&#9679;" if ag.status == "active" else "&#9675;"
                rows.append(f'<div class="agent-row">{st_sym} <span>{_esc(ag.summary[:40])}</span>'
                            f' &mdash; {ag.tool_count} tools</div>')
            agents_html = f'<div class="agents-list">{"".join(rows)}</div>'

        # ── focus button ─────────────────────────────────────────────────────
        focus_btn = ""
        if inst.terminal_app:
            focus_btn = (f'<button class="focus-btn" '
                         f'hx-post="/claude/focus/{inst.pid}" hx-swap="none" '
                         f'title="Focus {inst.terminal_app}">&#8594; {_esc(inst.terminal_app)}</button>')

        # ── branch/version badge ──────────────────────────────────────────────
        meta = []
        if inst.git_branch:   meta.append(_esc(inst.git_branch))
        if inst.version:      meta.append(f'v{_esc(inst.version)}')
        if model_short:       meta.append(model_short)
        meta_html = ' &middot; '.join(meta)

        parts.append(f'''<div class="claude-instance">
  <div class="claude-head">
    <strong style="font-size:12px">{_esc(inst.project_name)}</strong>
    <span style="font-size:10px;color:var(--t-muted)">{_esc(inst.cwd)}</span>
    <span style="font-size:10px;color:var(--t-muted);margin-left:auto">{meta_html}</span>
    {focus_btn}
  </div>
  <div class="claude-body">
    <div class="claude-stat">pid <span>{inst.pid}</span> &middot; up <span>{_uptime_str(inst.uptime_s)}</span> &middot; cpu <span>{inst.cpu:.1f}%</span> &middot; mem <span>{inst.mem_mb:.0f}&thinsp;MB</span></div>
    <div class="claude-stat">context: {ctx_html}</div>
    <div class="claude-stat">tool: {tool_html}</div>
    <div class="claude-stat">cost: {cost_html}</div>
  </div>
  {agents_html}
  {flags_html}
</div>''')

    return "\n".join(parts)


def html_daily_stats() -> str:
    if not HAS_CM:
        return ""
    try:
        ds = _cm.daily_stats()
    except Exception:
        return '<div style="font-size:11px;color:var(--t-muted)">stats unavailable</div>'

    # 7-day message-count sparkline (proxy for activity; labeled clearly)
    _BLOCKS = "▁▂▃▄▅▆▇█"
    counts  = list(reversed(ds.cost_week))   # oldest → newest
    max_w   = max(counts) if counts and max(counts) > 0 else 1
    spark   = ""
    for v in counts:
        idx    = min(int(v / max_w * 8), 7)
        spark += f'<span style="color:var(--t-c0)">{_BLOCKS[idx]}</span>'

    total_msgs = int(sum(ds.cost_week))

    return (f'<div class="card"><div class="card-body" '
            f'style="display:flex;gap:2rem;flex-wrap:wrap;align-items:center">'
            f'<div class="claude-stat" style="font-size:13px">API equiv. today&nbsp; <span style="font-size:15px">${ds.cost_today:.2f}</span></div>'
            f'<div class="claude-stat">sessions today: <span>{ds.sessions_today}</span></div>'
            f'<div class="claude-stat" style="margin-left:auto;font-size:10px;color:var(--t-muted)">'
            f'7-day messages ({total_msgs:,} total)&nbsp; {spark}'
            f'</div>'
            f'</div></div>')


def _esc(s) -> str:
    """Minimal HTML escaping for untrusted strings."""
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── page ──────────────────────────────────────────────────────────────────────

def _build_index():
    return _INDEX_TMPL.replace("__THEME_STYLE__", _theme.css_vars(_T))

_INDEX_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Monitor</title>
__THEME_STYLE__
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<script>
// ── Voice presets ─────────────────────────────────────────────────────────────
// Spatially separated (pan L/R/C), distinct waveforms and chord voicings
var _VOICES=[
  {w:"sawtooth", r:[1,1.2599,1.4983,2], g:[0.55,0.30,0.30,0.18], Q:4, dur:0.55, pan:-0.55},
  {w:"square",   r:[1,1.1892,1.4983,2], g:[0.40,0.25,0.25,0.15], Q:2, dur:0.45, pan: 0.55},
  {w:"triangle", r:[1,1.3348,1.4983,2], g:[0.50,0.28,0.28,0.16], Q:7, dur:0.60, pan: 0.0},
];

// Snap Hz to nearest C-major pentatonic — guarantees consonance across streams
function snapPentatonic(hz){
  var midi=69+12*Math.log2(hz/440);
  var pent=[0,2,4,7,9];
  var oct=Math.floor((midi-60)/12);
  var deg=((midi-60)%12+12)%12;
  var nearest=pent.reduce(function(a,b){return Math.abs(b-deg)<Math.abs(a-deg)?b:a;});
  return 440*Math.pow(2,(60+oct*12+nearest-69)/12);
}

// ── Audio engine ──────────────────────────────────────────────────────────────
// Single shared AudioContext, compressor, and reverb.
// playNote(freq, detune, voiceIdx, atTime, killPrev)
//   atTime   — Web Audio timestamp; null = play immediately
//   killPrev — true for hover (cancels same voice); false for scheduled playback
var _audio=(function(){
  var ctx=null,rev=null,comp=null,_vOscs=[[],[],[]];
  function init(){
    if(ctx)return;
    ctx=new(window.AudioContext||window.webkitAudioContext)();
    comp=ctx.createDynamicsCompressor();
    comp.threshold.value=-24;comp.knee.value=8;
    comp.ratio.value=4;comp.attack.value=0.003;comp.release.value=0.15;
    comp.connect(ctx.destination);
    var sr=ctx.sampleRate,len=Math.floor(sr*1.8),ir=ctx.createBuffer(2,len,sr);
    for(var ch=0;ch<2;ch++){var d=ir.getChannelData(ch);
      for(var s=0;s<len;s++)d[s]=(Math.random()*2-1)*Math.pow(1-s/len,2.4);}
    rev=ctx.createConvolver();rev.buffer=ir;
    var rg=ctx.createGain();rg.gain.value=0.28;
    rev.connect(rg);rg.connect(comp);
  }
  function playNote(freq,detune,voiceIdx,atTime,killPrev,oscList){
    init();
    var vi=(voiceIdx||0)%_VOICES.length;
    var t=atTime||ctx.currentTime;
    if(killPrev){
      (_vOscs[vi]||[]).forEach(function(n){try{n.stop(t+0.025);}catch(e){}});
      _vOscs[vi]=[];
    }
    var v=_VOICES[vi];
    var pFreq=snapPentatonic(freq);
    var brightMod=detune?Math.max(0.4,Math.min(2.8,1+detune/80)):1;
    var fi=ctx.createBiquadFilter();fi.type="lowpass";
    fi.frequency.value=pFreq*4*brightMod;fi.Q.value=v.Q;
    var pan=ctx.createStereoPanner?ctx.createStereoPanner():ctx.createPanner();
    if(pan.pan)pan.pan.value=v.pan;
    var ma=ctx.createGain();ma.gain.value=0;
    fi.connect(pan);pan.connect(ma);ma.connect(comp);ma.connect(rev);
    v.r.forEach(function(r,i){
      var o=ctx.createOscillator(),g=ctx.createGain();
      o.type=v.w;o.frequency.value=pFreq*r;
      if(detune&&i===0)o.detune.value=detune;
      g.gain.value=v.g[i];
      o.connect(g);g.connect(fi);
      o.start(t);o.stop(t+v.dur+0.06);
      if(killPrev)_vOscs[vi].push(o);
      if(oscList)oscList.push(o);
    });
    ma.gain.setTargetAtTime(0.13,t,0.008);
    ma.gain.setTargetAtTime(0.0001,t+v.dur*0.45,0.04);
  }
  function scheduleToList(freq,det,vi,at,list){playNote(freq,det,vi,at,false,list);}
  return{
    getCtx:function(){init();return ctx;},
    hover:function(freq,det,vi){playNote(freq,det,vi,null,true,null);},
    schedule:function(freq,det,vi,at){playNote(freq,det,vi,at,false,null);},
    scheduleToList:scheduleToList
  };
})();

// Interactive hover — kills same-voice previous note
window._htmxBeep=function(freq,detune,voiceIdx){
  _audio.hover(freq,detune,voiceIdx);
};

// Schedule a chord (array of [freq,det,voice]) at a specific audio timestamp.
// oscList — optional array; scheduled oscillators are pushed into it for later stop().
function _scheduleChord(notes,atTime,oscList){
  if(!notes||!notes.length)return;
  notes.forEach(function(n){_audio.scheduleToList(n[0],n[1],n[2]||0,atTime,oscList||null);});
}

// ── Cursor overlay ────────────────────────────────────────────────────────────
// Appended as sibling of the htmx div so htmx innerHTML swaps don't remove it.
// Position is expressed as % of wrapper width, mapped from SVG viewBox coords.
function _getCursor(chartId){
  var id=chartId+'-cursor',c=document.getElementById(id);
  if(!c){
    var el=document.getElementById(chartId);if(!el)return null;
    var wrap=el.parentElement;
    if(getComputedStyle(wrap).position==='static')wrap.style.position='relative';
    c=document.createElement('div');c.id=id;
    c.style.cssText='display:none;position:absolute;top:0;bottom:0;width:1px;'+
      'background:#fff;opacity:0.5;pointer-events:none;z-index:10;';
    wrap.appendChild(c);
  }
  return c;
}

function _cursorPct(chartId,pct){
  // Re-read SVG layout attrs each frame — chart refreshes every 2s via htmx
  var svg=document.getElementById(chartId);
  svg=svg&&svg.querySelector('svg');
  if(!svg)return null;
  var pl=parseFloat(svg.getAttribute('data-pl')||32);
  var iw=parseFloat(svg.getAttribute('data-iw')||652);
  var sw=parseFloat(svg.getAttribute('data-w') ||720);
  return (pl+pct*iw)/sw*100;
}

// ── Play sessions ─────────────────────────────────────────────────────────────
// Each chart gets its own session keyed by chartId.
// Audio is pre-scheduled on the Web Audio timeline (sample-accurate, no drift).
// Cursor uses requestAnimationFrame + audioCtx.currentTime as ground truth.
var _sessions={};

function playChart(chartId,btn){
  var sess=_sessions[chartId];
  // Toggle off if same button pressed again
  if(sess){
    cancelAnimationFrame(sess.raf);
    var _now=_audio.getCtx().currentTime;
    (sess.oscs||[]).forEach(function(o){try{o.stop(_now);}catch(e){}});
    sess.btn.textContent="[ PLAY ]";
    var cc=_getCursor(chartId);if(cc)cc.style.display='none';
    delete _sessions[chartId];
    if(sess.btn===btn)return;
  }
  var el=document.getElementById(chartId);
  var svg=el&&el.querySelector('svg');
  if(!svg)return;
  var raw=svg.getAttribute('data-freqs');if(!raw)return;
  var freqs=JSON.parse(raw);
  var n=freqs.length;
  var bpm=parseInt((document.getElementById('bpm-input')||{}).value)||200;
  var stepSec=60/bpm;
  var totalSec=(n-1)*stepSec;

  // Pre-schedule all notes on the Web Audio clock — zero drift at any BPM
  var ctx=_audio.getCtx();
  var t0=ctx.currentTime+0.05;
  var sessionOscs=[];
  freqs.forEach(function(chord,i){_scheduleChord(chord,t0+i*stepSec,sessionOscs);});

  var cursor=_getCursor(chartId);
  btn.textContent="[ STOP ]";

  function tick(){
    var elapsed=ctx.currentTime-t0;
    var pct=elapsed/totalSec;
    if(pct>=1){
      delete _sessions[chartId];
      if(cursor)cursor.style.display='none';
      var loop=document.getElementById('loop-toggle');
      if(loop&&loop.checked){
        playChart(chartId,btn); // restart with fresh data
      } else {
        btn.textContent="[ PLAY ]";
      }
      return;
    }
    if(cursor&&pct>=0){
      var xp=_cursorPct(chartId,pct);
      if(xp!==null){cursor.style.left=xp+'%';cursor.style.display='block';}
    }
    _sessions[chartId].raf=requestAnimationFrame(tick);
  }
  _sessions[chartId]={raf:requestAnimationFrame(tick),btn:btn,oscs:sessionOscs};
}

// ── Tab system ────────────────────────────────────────────────────────────────
function switchTab(name,btn){
  localStorage.setItem('mac-monitor-tab',name);
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  htmx.ajax('GET','/tab/'+name,{target:'#tab-content',swap:'innerHTML'});
}
document.addEventListener('DOMContentLoaded',function(){
  var tab=localStorage.getItem('mac-monitor-tab')||'system';
  var btn=document.getElementById('tab-btn-'+tab);
  if(btn)switchTab(tab,btn);
});

// Spacebar stops all active playback sessions
document.addEventListener('keydown',function(e){
  if(e.code==='Space'&&e.target.tagName!=='INPUT'){
    e.preventDefault();
    Object.keys(_sessions).forEach(function(id){
      var sess=_sessions[id];
      cancelAnimationFrame(sess.raf);
      var now=_audio.getCtx().currentTime;
      (sess.oscs||[]).forEach(function(o){try{o.stop(now);}catch(e){}});
      sess.btn.textContent='[ PLAY ]';
      var cc=_getCursor(id);if(cc)cc.style.display='none';
      delete _sessions[id];
    });
  }
});
</script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

html,body{
  font-family:"Courier New",Courier,monospace;
  font-size:13px;color:var(--t-fg);min-height:100vh;
  background-color:var(--t-bg);
}

/* menubar */
.menubar{
  background:var(--t-bg);
  border-bottom:1px solid var(--t-border);
  padding:0 1rem;height:24px;
  display:flex;align-items:center;gap:.8rem;
  position:sticky;top:0;z-index:100;
}
.menubar-apple{font-size:15px;font-weight:bold}

/* (search styles now in .search-row block) */
#search-drop{
  display:none;position:absolute;top:100%;left:0;right:0;
  background:var(--t-panel2);border:1px solid var(--t-border);border-top:none;
  padding:.3rem .5rem;z-index:200;max-height:260px;overflow-y:auto;
  color:var(--t-fg);
}


/* section labels */
.group{
  font-size:10px;font-weight:bold;text-transform:uppercase;
  letter-spacing:.1em;margin:1.2rem 0 .4rem;
  border-bottom:1px solid var(--t-border);padding-bottom:1px;
  color:var(--t-muted);
}

/* mac window cards */
.card{
  background:var(--t-panel2);
  border:1px solid var(--t-border);
  box-shadow:3px 3px 0 var(--t-c0-22);
  margin-bottom:1rem;
}
.card-head{
  background:repeating-linear-gradient(
    180deg,var(--t-stripe) 0px,var(--t-stripe) 1px,var(--t-panel2) 1px,var(--t-panel2) 2px);
  border-bottom:1px solid var(--t-border);
  padding:.22rem .7rem;
  display:flex;align-items:center;gap:.6rem;
}
.card-title{font-size:11px;font-weight:bold;background:transparent;padding:0 .25rem;color:var(--t-fg)}
.badge{
  margin-left:auto;font-size:9px;font-weight:bold;
  background:var(--t-panel);color:var(--t-accent);border:1px solid var(--t-c0-44);padding:1px 4px;
}
.card-body{padding:.7rem .9rem}

/* layout */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:680px){.grid-2{grid-template-columns:1fr}}

/* tables */
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--t-border);padding:.25rem .5rem;text-align:left;color:var(--t-fg)}
th{
  background:var(--t-panel);color:var(--t-accent);font-size:10px;
  text-transform:uppercase;letter-spacing:.04em;
  cursor:pointer;user-select:none;
}
th:hover{background:var(--t-stripe);color:var(--t-fg)}
tr:nth-child(even) td{background:rgba(128,128,128,.05)}

/* inputs */
input[type=text]{
  border:1px solid var(--t-border);padding:.25rem .5rem;
  font-family:inherit;font-size:11px;background:var(--t-panel);color:var(--t-fg);outline:none;
}
input[type=text]:focus{outline:1px solid var(--t-accent);outline-offset:1px}
input[type=text]::placeholder{color:var(--t-muted)}

/* buttons */
button{
  border:1px solid var(--t-border);padding:.25rem .75rem;
  font-family:inherit;font-size:11px;font-weight:bold;
  cursor:pointer;background:var(--t-panel);color:var(--t-accent);
  box-shadow:2px 2px 0 var(--t-c0-33);position:relative;
}
button:active{box-shadow:none;top:2px;left:2px}
button:hover{border-color:var(--t-accent);color:var(--t-fg);}

/* htmx */
.htmx-indicator{display:none;font-size:10px;font-weight:bold}
.htmx-request .htmx-indicator{display:inline}
.htmx-request.htmx-indicator{display:inline}

/* play button — overrides base button sizing */
.play-btn{margin-left:.5rem;font-size:9px;padding:0 5px;box-shadow:1px 1px 0 #000}

/* search row — own row below menubar */
.search-row{
  background:var(--t-bg);border-bottom:1px solid var(--t-border);
  padding:5px 1rem;position:sticky;top:24px;z-index:99;
}
.search-row-inner{position:relative;max-width:640px}
.search-row .search-input{
  width:100%;border:1px solid var(--t-accent);padding:3px 8px;
  font-family:inherit;font-size:12px;background:var(--t-panel);color:var(--t-fg);outline:none;
}
.search-row .search-input:focus{outline:2px solid var(--t-accent);outline-offset:1px}
.search-row .search-input::placeholder{color:var(--t-muted);font-weight:normal}
.search-row #search-drop{
  display:none;position:absolute;top:100%;left:0;right:0;
  background:var(--t-panel2);border:1px solid var(--t-border);border-top:none;
  padding:.3rem .5rem;z-index:200;max-height:260px;overflow-y:auto;color:var(--t-fg);
}

/* tab bar */
.tab-bar{
  display:flex;gap:0;padding:0 1rem;
  background:var(--t-bg);border-bottom:2px solid var(--t-border);
  position:sticky;top:58px;z-index:98;
}
.tab-btn{
  border:1px solid transparent;border-bottom:none;padding:4px 14px;
  font-size:10px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;
  cursor:pointer;background:transparent;color:var(--t-muted);
  box-shadow:none;position:relative;bottom:-2px;
}
.tab-btn:hover{color:var(--t-fg);border-color:var(--t-border)}
.tab-btn.active{
  background:var(--t-bg);color:var(--t-accent);
  border-color:var(--t-accent);border-bottom:2px solid var(--t-bg);
}

/* tab content */
#tab-content .page{max-width:1100px;margin:0 auto;padding:1rem 1rem 4rem}

/* claude tab */
.claude-instance{
  background:var(--t-panel2);border:1px solid var(--t-border);
  margin-bottom:1rem;
}
.claude-head{
  background:repeating-linear-gradient(
    180deg,var(--t-stripe) 0px,var(--t-stripe) 1px,var(--t-panel2) 1px,var(--t-panel2) 2px);
  border-bottom:1px solid var(--t-border);padding:.3rem .7rem;
  display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;
}
.claude-body{padding:.6rem .9rem;display:grid;grid-template-columns:1fr 1fr;gap:.5rem 1.5rem}
@media(max-width:680px){.claude-body{grid-template-columns:1fr}}
.claude-stat{font-size:11px}
.claude-stat span{color:var(--t-accent);font-weight:bold}
.ctx-bar{
  display:inline-block;height:6px;background:var(--t-border);
  width:80px;vertical-align:middle;margin-left:4px;position:relative;
}
.ctx-fill{
  display:block;height:100%;background:var(--t-c0);
  transition:width .3s;
}
.ctx-fill.warn{background:var(--t-c1)}
.ctx-fill.crit{background:#c0392b}
.attention-flags{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.5rem}
.attention-flag{
  font-size:9px;font-weight:bold;padding:1px 5px;
  border:1px solid var(--t-accent);color:var(--t-accent);text-transform:uppercase;
}
.attention-flag.critical{border-color:#c0392b;color:#c0392b}
.focus-btn{
  font-size:9px;padding:1px 6px;border:1px solid var(--t-border);
  background:var(--t-panel);color:var(--t-muted);cursor:pointer;
  font-family:inherit;font-weight:bold;box-shadow:none;
}
.focus-btn:hover{color:var(--t-accent);border-color:var(--t-accent)}
.sparkline-bar{display:inline-block;width:8px;background:var(--t-c0);margin-right:1px;vertical-align:bottom}
.daily-row{display:flex;align-items:flex-end;gap:.2rem;margin-top:.3rem;height:24px}
.agents-list{margin-top:.4rem;font-size:11px}
.agent-row{padding:.15rem 0;border-bottom:1px solid var(--t-border);color:var(--t-muted)}
.agent-row span{color:var(--t-fg)}

</style>
</head>
<body>
<span hx-get="/theme" hx-trigger="every 5s" hx-swap="outerHTML" hx-target="#theme-style" style="display:none"></span>

<div class="menubar">
  <div class="menubar-apple">&#63743;</div>
  <span style="font-size:10px;font-weight:bold;letter-spacing:.06em">MAC MONITOR</span>
  <span style="margin-left:auto;display:flex;align-items:center;gap:.5rem">
    <label style="font-size:10px;font-weight:bold">BPM</label>
    <input type="number" id="bpm-input" value="300" min="20" max="600"
           style="width:52px;border:1px solid var(--t-border);padding:0 4px;font-family:inherit;
                  font-size:11px;background:var(--t-panel);color:var(--t-accent);outline:none;height:16px">
    <label style="font-size:10px;font-weight:bold;display:flex;align-items:center;gap:3px;cursor:pointer">
      <input type="checkbox" id="loop-toggle" style="accent-color:var(--t-accent)">LOOP
    </label>
    <span hx-get="/metrics/sysinfo" hx-trigger="load, every 5s"
          hx-target="this" hx-swap="innerHTML"></span>
  </span>
</div>

<div class="search-row">
  <div class="search-row-inner">
    <input class="search-input" id="search-input" name="q" type="text"
           placeholder="search metrics &amp; processes..."
           hx-get="/search" hx-trigger="keyup changed delay:250ms"
           hx-target="#search-drop"
           onfocus="document.getElementById('search-drop').style.display='block'"
           onblur="setTimeout(function(){document.getElementById('search-drop').style.display=''},200)">
    <div id="search-drop"></div>
  </div>
</div>

<div class="tab-bar">
  <button class="tab-btn" id="tab-btn-system" onclick="switchTab('system',this)">[ System ]</button>
  <button class="tab-btn" id="tab-btn-claude" onclick="switchTab('claude',this)">[ Claude ]</button>
</div>

<div id="tab-content"></div>

</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    maybe_collect()   # prime psutil cpu_percent baseline
    print(f"  Mac System Monitor -> http://localhost:{PORT}")
    if not HAS_PSUTIL:
        print("  (simulation mode — pip install psutil for real metrics)")
    HTTPServer(("", PORT), Handler).serve_forever()
