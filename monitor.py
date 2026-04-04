#!/usr/bin/env python3
"""Mac System Monitor — graph-paper B&W + htmx + audible score charts."""

import collections, time, os, platform, socket, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

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


def _hover_rects(buf, pl, pr, pt, pb, w, h, ymax):
    vs = list(buf); n = len(vs)
    iw = w - pl - pr; ih = h - pt - pb
    cw = max(1, iw // n)
    out = ""
    for i, v in enumerate(vs):
        freq = int(200 + min(v, ymax) / ymax * 1000)
        det  = (30 if i > 0 and v > vs[i-1] else
               -30 if i > 0 and v < vs[i-1] else 0)
        x = pl + int(i / (n - 1) * iw)
        out += (f'<rect x="{x}" y="{pt}" width="{cw+1}" height="{ih}" '
                f'fill="transparent" style="cursor:crosshair" '
                f'onmouseover="window._htmxBeep&&window._htmxBeep({freq},{det})"/>')
    return out


def _grid(pl, pr, pt, pb, w, h, ymax, pcts, fmt_fn):
    ih  = h - pt - pb
    out = ""
    for pct in pcts:
        y  = pt + ih - int(pct / 100 * ih)
        yv = ymax * pct / 100
        out += (f'<line x1="{pl}" y1="{y}" x2="{w-pr}" y2="{y}" '
                f'stroke="#aaa" stroke-width="0.4" stroke-dasharray="2,3"/>'
                f'<text x="{pl-3}" y="{y+3}" font-size="7" text-anchor="end" '
                f'fill="#000" font-family="Courier,monospace">{fmt_fn(yv)}</text>')
    return out


def _axes(pl, pr, pt, pb, w, h):
    ih = h - pt - pb
    return (f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{pt+ih}" '
            f'stroke="#000" stroke-width="1.5"/>'
            f'<line x1="{pl}" y1="{pt+ih}" x2="{w-pr}" y2="{pt+ih}" '
            f'stroke="#000" stroke-width="1.5"/>')


def _stamp(w, h):
    return (f'<text x="{w-8}" y="{h}" text-anchor="end" font-size="8" '
            f'fill="#666" font-family="Courier,monospace">'
            f'upd {time.strftime("%H:%M:%S")}</text>')


# ── charts ────────────────────────────────────────────────────────────────────

def svg_cpu_score(*, w=720, h=270):
    pl, pr, pt, pb = 32, 36, 12, 22
    iw = w - pl - pr; ih = h - pt - pb; n = BUF

    grid = _grid(pl, pr, pt, pb, w, h, 100,
                 (20, 40, 60, 80, 100), lambda v: f"{int(v)}%")

    # column hover zones — avg of all cores, sweep to hear history
    avgs = [sum(list(b)[i] for b in CPU_BUFS) / _nc for i in range(n)]
    cw   = max(1, iw // n)
    hov  = ""
    for i, avg in enumerate(avgs):
        freq = int(200 + avg / 100 * 1000)
        det  = (30 if i > 0 and avg > avgs[i-1] else
               -30 if i > 0 and avg < avgs[i-1] else 0)
        x = pl + int(i / (n - 1) * iw)
        hov += (f'<rect x="{x}" y="{pt}" width="{cw+1}" height="{ih}" '
                f'fill="transparent" style="cursor:crosshair" '
                f'onmouseover="window._htmxBeep&&window._htmxBeep({freq},{det})"/>')

    lines = ""
    for ci, buf in enumerate(CPU_BUFS):
        dash = DASH[ci % len(DASH)]
        sw   = 1.5 if ci % 3 == 0 else (0.9 if ci % 3 == 1 else 1.2)
        pts  = _make_pts(buf, pl, pr, pt, pb, w, h, 100)
        d    = (f"M{pts[0][0]},{pts[0][1]}" +
                "".join(f" L{x},{y}" for x, y in pts[1:]))
        da   = f'stroke-dasharray="{dash}"' if dash else ""
        lines += (f'<path d="{d}" fill="none" stroke="#000" '
                  f'stroke-width="{sw}" {da} opacity="0.62"/>')
        lx, ly = pts[-1]
        lines += (f'<text x="{lx+3}" y="{min(ly+3, h-pb-2)}" font-size="7" '
                  f'fill="#000" font-family="Courier,monospace">C{ci}</text>')

    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}{lines}{_axes(pl,pr,pt,pb,w,h)}{hov}{_stamp(w,h)}</svg>')


def svg_dual(buf_a, buf_b, la, lb, *, w=460, h=160):
    pl, pr, pt, pb = 56, 8, 12, 22
    all_v = [v for v in list(buf_a) + list(buf_b) if v > 0]
    ymax  = max(max(all_v) * 1.15, 1) if all_v else 1

    grid = _grid(pl, pr, pt, pb, w, h, ymax,
                 (25, 50, 75, 100), fmt_bytes)
    hovA = _hover_rects(buf_a, pl, pr, pt, pb, w, h, ymax)
    hovB = _hover_rects(buf_b, pl, pr, pt, pb, w, h, ymax)

    def path(buf, dash, label):
        pts = _make_pts(buf, pl, pr, pt, pb, w, h, ymax)
        vs  = list(buf)
        d   = (f"M{pts[0][0]},{pts[0][1]}" +
               "".join(f" L{x},{y}" for x, y in pts[1:]))
        da  = f'stroke-dasharray="{dash}"' if dash else ""
        lx, ly = pts[-1]
        tag = (f'<text x="{lx-2}" y="{max(ly-5, pt+10)}" text-anchor="end" '
               f'font-size="8" fill="#000" font-family="Courier,monospace">'
               f'{label}:{fmt_bytes(vs[-1])}</text>')
        return (f'<path d="{d}" fill="none" stroke="#000" '
                f'stroke-width="1.2" {da} opacity="0.82"/>{tag}')

    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}{path(buf_a,"",la)}{path(buf_b,"6,3",lb)}'
            f'{_axes(pl,pr,pt,pb,w,h)}{hovA}{hovB}{_stamp(w,h)}</svg>')


def svg_mem(*, w=460, h=150):
    pl, pr, pt, pb = 32, 8, 12, 22
    hov_m = _hover_rects(MEM_BUF,  pl, pr, pt, pb, w, h, 100)
    hov_s = _hover_rects(SWAP_BUF, pl, pr, pt, pb, w, h, 100)
    grid  = _grid(pl, pr, pt, pb, w, h, 100,
                  (25, 50, 75, 100), lambda v: f"{int(v)}%")

    def path(buf, dash, label, op):
        pts = _make_pts(buf, pl, pr, pt, pb, w, h, 100)
        vs  = list(buf)
        d   = (f"M{pts[0][0]},{pts[0][1]}" +
               "".join(f" L{x},{y}" for x, y in pts[1:]))
        da  = f'stroke-dasharray="{dash}"' if dash else ""
        lx, ly = pts[-1]
        tag = (f'<text x="{lx-2}" y="{max(ly-5, pt+10)}" text-anchor="end" '
               f'font-size="8" fill="#000" font-family="Courier,monospace">'
               f'{label}:{vs[-1]:.1f}%</text>')
        return (f'<path d="{d}" fill="none" stroke="#000" '
                f'stroke-width="1.2" {da} opacity="{op}"/>{tag}')

    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">'
            f'{grid}'
            f'{path(MEM_BUF,  "", "MEM", "0.85")}'
            f'{path(SWAP_BUF, "5,3", "SWAP", "0.5")}'
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
        freq = int(200 + pct / 100 * 1000)
        rows += (
            f'<div style="margin:.5rem 0">'
            f'<div style="display:flex;justify-content:space-between;'
            f'font-size:11px;font-weight:bold;font-family:Courier,monospace">'
            f'<span>{label}</span><span>{pct:.1f}%</span></div>'
            f'<div style="background:#fff;border:1px solid #000;height:10px;'
            f'overflow:hidden;cursor:crosshair" '
            f'onmouseover="window._htmxBeep&&window._htmxBeep({freq})">'
            f'<div style="background:#000;width:{pct:.1f}%;height:100%"></div>'
            f'</div></div>'
        )
    rows += (f'<p style="font-size:9px;font-family:Courier,monospace;margin-top:.5rem">'
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


def _sort_arrow(col, active):
    return " &#x25BC;" if col == active else ""


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
            f'<td style="font-family:Courier,monospace;font-size:11px">{p["pid"]}</td>'
            f'<td style="font-family:Courier,monospace;font-size:11px">{p["name"][:32]}</td>'
            f'<td style="font-family:Courier,monospace;font-size:11px">'
            f'<div style="display:flex;align-items:center;gap:.3rem">'
            f'<div style="background:#fff;border:1px solid #000;'
            f'width:48px;height:6px;flex-shrink:0">'
            f'<div style="background:#000;width:{bw}%;height:100%"></div></div>'
            f'{p["cpu"]:.1f}%</div></td>'
            f'<td style="font-family:Courier,monospace;font-size:11px">{p["mem"]:.1f}%</td>'
            f'<td style="font-family:Courier,monospace;font-size:11px">{p["status"]}</td>'
            f'</tr>'
        )
    return rows


def html_search_results(q):
    if not q or len(q) < 2:
        return ""
    q_low = q.lower()
    maybe_collect()

    KEYWORDS = {
        "cpu":     ("CPU AVG",  f"{sum(b[-1] for b in CPU_BUFS) / _nc:.1f}%"),
        "mem":     ("MEMORY",   f"{MEM_BUF[-1]:.1f}%"),
        "memory":  ("MEMORY",   f"{MEM_BUF[-1]:.1f}%"),
        "swap":    ("SWAP",     f"{SWAP_BUF[-1]:.1f}%"),
        "net":     ("NETWORK",  f"TX {fmt_bytes(NET_TX[-1])}  RX {fmt_bytes(NET_RX[-1])}"),
        "network": ("NETWORK",  f"TX {fmt_bytes(NET_TX[-1])}  RX {fmt_bytes(NET_RX[-1])}"),
        "disk":    ("DISK",     f"R {fmt_bytes(DISK_R[-1])}  W {fmt_bytes(DISK_W[-1])}"),
        "load":    ("LOAD AVG", get_sysinfo()["load"]),
        "uptime":  ("UPTIME",   get_sysinfo()["up"]),
        "ram":     ("RAM",      get_sysinfo()["ram"]),
        "cores":   ("CORES",    str(get_sysinfo()["cores"])),
    }

    out = ""
    for kw, (label, val) in KEYWORDS.items():
        if q_low in kw or kw in q_low:
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
            "/":                lambda: INDEX_HTML,
            "/metrics/cpu":     lambda: (maybe_collect(), svg_cpu_score())[1],
            "/metrics/memory":  lambda: (maybe_collect(), svg_mem())[1],
            "/metrics/gauges":  lambda: (maybe_collect(), html_gauges())[1],
            "/metrics/network": lambda: (maybe_collect(), svg_dual(NET_TX, NET_RX, "TX", "RX"))[1],
            "/metrics/disk":    lambda: (maybe_collect(), svg_dual(DISK_R, DISK_W, "READ", "WRITE"))[1],
            "/metrics/sysinfo": lambda: html_sysinfo(),
            "/processes":       lambda: (maybe_collect(), html_proc_rows(q, sort))[1],
            "/search":          lambda: html_search_results(q),
        }
        fn = routes.get(path)
        if fn:
            self.send_html(fn())
        else:
            self.send_html("<p>not found</p>", 404)


# ── page ──────────────────────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Monitor</title>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<script>
window._htmxBeep=(function(){
  var ctx=null,active=[],rev=null;
  function mkRev(c){
    var sr=c.sampleRate,len=Math.floor(sr*1.8),ir=c.createBuffer(2,len,sr);
    for(var ch=0;ch<2;ch++){var d=ir.getChannelData(ch);
      for(var s=0;s<len;s++)d[s]=(Math.random()*2-1)*Math.pow(1-s/len,2.4);}
    var rv=c.createConvolver();rv.buffer=ir;
    var rg=c.createGain();rg.gain.value=0.38;
    rv.connect(rg);rg.connect(c.destination);return rv;
  }
  return function(freq,detune){
    if(!ctx)ctx=new(window.AudioContext||window.webkitAudioContext)();
    if(!rev)rev=mkRev(ctx);
    active.forEach(function(n){try{n.stop(0);}catch(e){}});active=[];
    var fi=ctx.createBiquadFilter();fi.type="lowpass";
    fi.frequency.value=freq*5;fi.Q.value=4;
    var ma=ctx.createGain();
    fi.connect(ma);ma.connect(ctx.destination);ma.connect(rev);
    [1,1.2599,1.4983,2].forEach(function(r,i){
      var o=ctx.createOscillator(),g=ctx.createGain();
      o.type="sawtooth";o.frequency.value=freq*r;
      if(detune&&i===0)o.detune.value=detune;
      g.gain.value=[0.55,0.30,0.30,0.18][i];
      o.connect(g);g.connect(fi);
      o.start(ctx.currentTime);o.stop(ctx.currentTime+0.55);active.push(o);
    });
    ma.gain.setValueAtTime(0,ctx.currentTime);
    ma.gain.linearRampToValueAtTime(0.12,ctx.currentTime+0.018);
    ma.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+0.55);
  };
})();
</script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

html,body{
  font-family:"Courier New",Courier,monospace;
  font-size:13px;color:#000;min-height:100vh;
  background-color:#faf7ee;
  background-image:
    linear-gradient(rgba(160,90,20,.22) 1px,transparent 1px),
    linear-gradient(90deg,rgba(160,90,20,.22) 1px,transparent 1px),
    linear-gradient(rgba(160,90,20,.07) 1px,transparent 1px),
    linear-gradient(90deg,rgba(160,90,20,.07) 1px,transparent 1px);
  background-size:40px 40px,40px 40px,8px 8px,8px 8px;
}

/* menubar */
.menubar{
  background:rgba(250,247,238,.96);
  border-bottom:2px solid #000;
  padding:0 1rem;height:24px;
  display:flex;align-items:center;gap:.8rem;
  position:sticky;top:0;z-index:100;
}
.menubar-apple{font-size:15px;font-weight:bold}

/* search */
.search-wrap{position:relative;flex:1;max-width:380px}
.search-input{
  width:100%;border:1px solid #000;padding:1px 6px;
  font-family:inherit;font-size:11px;background:#fff;outline:none;
}
.search-input:focus{outline:1px solid #000;outline-offset:1px}
.search-input::placeholder{color:#888}
#search-drop{
  display:none;position:absolute;top:100%;left:0;right:0;
  background:rgba(250,247,238,.98);border:1px solid #000;border-top:none;
  padding:.3rem .5rem;z-index:200;max-height:260px;overflow-y:auto;
}

/* page */
.page{max-width:1100px;margin:0 auto;padding:1rem 1rem 4rem}

/* section labels */
.group{
  font-size:10px;font-weight:bold;text-transform:uppercase;
  letter-spacing:.1em;margin:1.2rem 0 .4rem;
  border-bottom:1px solid #000;padding-bottom:1px;
}

/* mac window cards */
.card{
  background:rgba(255,255,255,.84);
  border:2px solid #000;
  box-shadow:3px 3px 0 #000;
  margin-bottom:1rem;
}
.card-head{
  background:repeating-linear-gradient(
    180deg,#000 0px,#000 1px,#fff 1px,#fff 2px);
  border-bottom:2px solid #000;
  padding:.22rem .7rem;
  display:flex;align-items:center;gap:.6rem;
}
.card-head::before{
  content:"";display:inline-block;
  width:10px;height:10px;border:1px solid #000;background:#fff;flex-shrink:0;
}
.card-title{font-size:11px;font-weight:bold;background:#fff;padding:0 .25rem}
.badge{
  margin-left:auto;font-size:9px;font-weight:bold;
  background:#000;color:#fff;padding:1px 4px;
}
.card-body{padding:.7rem .9rem}

/* layout */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:680px){.grid-2{grid-template-columns:1fr}}

/* tables */
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #000;padding:.25rem .5rem;text-align:left}
th{
  background:#000;color:#fff;font-size:10px;
  text-transform:uppercase;letter-spacing:.04em;
  cursor:pointer;user-select:none;
}
th:hover{background:#333}
tr:nth-child(even) td{background:rgba(0,0,0,.04)}

/* inputs */
input[type=text]{
  border:2px solid #000;padding:.25rem .5rem;
  font-family:inherit;font-size:11px;background:#fff;outline:none;
}
input[type=text]:focus{outline:2px solid #000;outline-offset:1px}
input[type=text]::placeholder{color:#888}

/* buttons */
button{
  border:2px solid #000;padding:.25rem .75rem;
  font-family:inherit;font-size:11px;font-weight:bold;
  cursor:pointer;background:#fff;color:#000;
  box-shadow:2px 2px 0 #000;position:relative;
}
button:active{box-shadow:none;top:2px;left:2px}

/* htmx */
.htmx-indicator{display:none;font-size:10px;font-weight:bold}
.htmx-request .htmx-indicator{display:inline}
.htmx-request.htmx-indicator{display:inline}

code{
  font-family:inherit;font-size:11px;
  background:rgba(0,0,0,.08);border:1px solid #000;padding:0 .25rem;
}
</style>
</head>
<body>

<div class="menubar">
  <span class="menubar-apple">&#x2318;</span>
  <div class="search-wrap">
    <input class="search-input" id="search-input" type="text"
           placeholder="search metrics &amp; processes..."
           hx-get="/search" hx-trigger="keyup changed delay:250ms"
           hx-target="#search-drop"
           onfocus="document.getElementById('search-drop').style.display='block'"
           onblur="setTimeout(function(){document.getElementById('search-drop').style.display=''},200)">
    <div id="search-drop"></div>
  </div>
  <span style="margin-left:auto"
        hx-get="/metrics/sysinfo" hx-trigger="load, every 5s"
        hx-target="this" hx-swap="innerHTML">
  </span>
</div>

<div class="page">

  <!-- CPU Score -->
  <div class="group">CPU Score &mdash; sweep to hear history</div>
  <div class="card">
    <div class="card-head">
      <span class="card-title">Per-Core Activity &mdash; 60s rolling</span>
      <span class="badge">every 2s</span>
    </div>
    <div class="card-body">
      <div hx-get="/metrics/cpu" hx-trigger="load, every 2s" hx-swap="innerHTML">
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
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div hx-get="/metrics/memory" hx-trigger="load, every 2s" hx-swap="innerHTML">
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
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div hx-get="/metrics/network" hx-trigger="load, every 2s" hx-swap="innerHTML">
          <div style="height:160px"></div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">Disk &mdash; Read / Write</span>
        <span class="badge">every 2s</span>
      </div>
      <div class="card-body">
        <div hx-get="/metrics/disk" hx-trigger="load, every 2s" hx-swap="innerHTML">
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

</div>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    maybe_collect()   # prime psutil cpu_percent baseline
    print(f"  Mac System Monitor -> http://localhost:{PORT}")
    if not HAS_PSUTIL:
        print("  (simulation mode — pip install psutil for real metrics)")
    HTTPServer(("", PORT), Handler).serve_forever()
