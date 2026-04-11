"""
charts.py — single source of truth for SVG charts and their audio mapping.

Every audible chart in Mac Monitor — System tab (CPU score, RAM/swap,
network, disk) and Claude tab (per-instance context/cpu/mem) — is built
by one function: `build_chart(polylines, audio=...)`. The SVG envelope,
hover-beep overlay, PLAY-button data attributes, freqs JSON for the
JavaScript player, gridlines, and end-of-line labels are all generated
from a list of `Series` objects.

The visual layer (polylines drawn) and the audio layer (chord scheduled
per time-step) are decoupled: by default the audio mirrors the visuals,
but the CPU score chart passes a synthesised audio list (avg/max/min
across cores) that doesn't match its 8 polyline series.

Public surface
--------------
- Series       — one polyline + audio voice config
- build_chart  — render an SVG with audio data
- play_button  — the [ PLAY ] button HTML used by every chart
- val_to_hz    — value → frequency (also used by gauge hover beeps)
- slope_cents  — slope → detune cents
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Match the inline font stack used elsewhere in the page so SVG text
# stays in the SF Mono / Menlo family.
_FONT = "SF Mono,Menlo,Monaco,Courier New,monospace"


# ── audio mapping ─────────────────────────────────────────────────────────────

def val_to_hz(v: float, ymax: float, midi_lo: int = 52, midi_hi: int = 84) -> int:
    """Map a value (0..ymax) to a frequency via a MIDI scale.

    Equal semitone steps mean perceptual change is linear in `v`.
    """
    ratio = min(max(v, 0), ymax) / max(ymax, 0.001)
    midi  = midi_lo + ratio * (midi_hi - midi_lo)
    return int(round(440 * 2 ** ((midi - 69) / 12)))


def slope_cents(vs, i: int, ymax: float) -> int:
    """Per-step delta as detune cents (clamped ±80) — gives notes a glide."""
    if i == 0:
        return 0
    raw = (vs[i] - vs[i - 1]) / max(ymax, 0.001) * 400
    return int(max(-80, min(80, raw)))


# ── series ────────────────────────────────────────────────────────────────────

@dataclass
class Series:
    """One polyline on a chart, with the audio voice it maps to.

    visual fields
    -------------
    buf       : iterable of numeric samples (deque or list).
    label     : optional end-of-line tag (e.g. "MEM", "C0").
    label_fn  : if set, label is rendered as "{label}:{label_fn(vs)}";
                if not, just "{label}" — used by per-core CPU labels.
    color     : CSS color string for the polyline.
    dash      : SVG `stroke-dasharray` value, e.g. "6,3" or "" for solid.
    sw        : stroke-width in user units (paired with non-scaling-stroke).
    opacity   : polyline opacity.

    audio fields (also used when this Series is in the `audio` list)
    ----------------------------------------------------------------
    voice     : voice index 0..2 routed to a Web Audio oscillator group.
    midi_lo/hi: MIDI range mapped from 0..ymax (controls pitch range).
    voice_fn  : optional voice override per index `i` — used by the CPU
                chart to switch voices based on which core is dominant.
    """
    buf:      Iterable[float]
    label:    str = ""
    color:    str = "var(--t-c0)"
    dash:     str = ""
    sw:       float = 1.2
    opacity:  float = 0.85
    voice:    int = 0
    midi_lo:  int = 56
    midi_hi:  int = 80
    label_fn: Callable[[list], str] | None = None
    voice_fn: Callable[[int], int] | None = None


# ── private SVG primitives ────────────────────────────────────────────────────

def _make_pts(vs, pl, pr, pt, pb, w, h, ymax):
    n  = len(vs)
    if n < 2:
        return []
    iw = w - pl - pr; ih = h - pt - pb
    return [(pl + int(i / (n - 1) * iw),
             pt + ih - int(min(max(vs[i], 0), ymax) / ymax * ih))
            for i in range(n)]


def _path_d(pts):
    return f"M{pts[0][0]},{pts[0][1]}" + "".join(f" L{x},{y}" for x, y in pts[1:])


def _grid(pl, pr, pt, pb, w, h, ymax, pcts, fmt_fn):
    if not pcts:
        return ""
    ih  = h - pt - pb
    out = ""
    for pct in pcts:
        y  = pt + ih - int(pct / 100 * ih)
        yv = ymax * pct / 100
        out += (f'<line x1="{pl}" y1="{y}" x2="{w-pr}" y2="{y}" '
                f'stroke="var(--t-border)" stroke-width="0.5" stroke-dasharray="2,3"/>'
                f'<text x="{pl-3}" y="{y+3}" font-size="7" text-anchor="end" '
                f'fill="var(--t-muted)" font-family="{_FONT}">{fmt_fn(yv)}</text>')
    return out


def _axes(pl, pr, pt, pb, w, h):
    ih = h - pt - pb
    return (f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{pt+ih}" '
            f'stroke="var(--t-border)" stroke-width="1.5"/>'
            f'<line x1="{pl}" y1="{pt+ih}" x2="{w-pr}" y2="{pt+ih}" '
            f'stroke="var(--t-border)" stroke-width="1.5"/>')


def _stamp(w, h):
    return (f'<text x="{w-8}" y="{h}" text-anchor="end" font-size="8" '
            f'fill="var(--t-muted)" font-family="{_FONT}">'
            f'upd {time.strftime("%H:%M:%S")}</text>')


def _polyline(s: Series, pts, pt, pb, h):
    """Path + optional end-of-line label for one Series."""
    out = (f'<path d="{_path_d(pts)}" fill="none" stroke="{s.color}" '
           f'stroke-width="{s.sw}" '
           f'{f"""stroke-dasharray="{s.dash}" """ if s.dash else ""}'
           f'opacity="{s.opacity}" stroke-linejoin="round" stroke-linecap="round" '
           f'vector-effect="non-scaling-stroke"/>')
    if not s.label:
        return out
    lx, ly = pts[-1]
    if s.label_fn is not None:
        # Right-anchored value tag above the line.
        text = f"{s.label}:{s.label_fn(list(s.buf))}"
        out += (f'<text x="{lx-2}" y="{max(ly-5, pt+10)}" text-anchor="end" '
                f'font-size="8" fill="{s.color}" font-family="{_FONT}">{text}</text>')
    else:
        # Plain end-tag (e.g. CPU "C0"…"C7") tucked next to the last point.
        out += (f'<text x="{lx+3}" y="{min(ly+3, h-pb-2)}" font-size="7" '
                f'fill="{s.color}" font-family="{_FONT}">{s.label}</text>')
    return out


def _hover_layer(audio: list[Series], pl, pr, pt, pb, w, h, ymax):
    """One transparent rect grid per audio Series — hovering fires its voice.

    Multiple audio series produce stacked layers, so hovering plays the
    full chord at that time-step (matches pre-refactor behaviour where
    each series rendered its own _hover_rects overlay).
    """
    if not audio:
        return ""
    iw = w - pl - pr; ih = h - pt - pb
    out = ""
    for s in audio:
        vs = list(s.buf)
        n  = len(vs)
        if n < 2:
            continue
        cw = max(1, iw // n)
        for i in range(n):
            freq = val_to_hz(vs[i], ymax, s.midi_lo, s.midi_hi)
            det  = slope_cents(vs, i, ymax)
            vi   = s.voice_fn(i) if s.voice_fn else s.voice
            x    = pl + int(i / (n - 1) * iw)
            out += (f'<rect x="{x}" y="{pt}" width="{cw+1}" height="{ih}" '
                    f'fill="transparent" style="cursor:crosshair" '
                    f'onmouseover="window._htmxBeep&&window._htmxBeep({freq},{det},{vi})"/>')
    return out


def _freqs_data(audio: list[Series], ymax) -> str:
    """One [hz, det, voice] chord per time index, JSON-encoded for the player."""
    if not audio:
        return "[]"
    n = max((len(list(s.buf)) for s in audio), default=0)
    if n < 2:
        return "[]"
    chords = []
    for i in range(n):
        chord = []
        for s in audio:
            vs = list(s.buf)
            j  = min(i, len(vs) - 1)
            chord.append([
                val_to_hz(vs[j], ymax, s.midi_lo, s.midi_hi),
                slope_cents(vs, j, ymax),
                s.voice_fn(i) if s.voice_fn else s.voice,
            ])
        chords.append(chord)
    return json.dumps(chords)


# ── public builder ────────────────────────────────────────────────────────────

def build_chart(
    polylines: list[Series],
    *,
    audio:          list[Series] | None = None,
    w:              int = 460,
    h:              int = 160,
    ymax:           float | None = None,
    pad:            tuple[int, int, int, int] = (32, 8, 12, 22),  # left, right, top, bottom
    grid_pcts:      tuple[int, ...] = (25, 50, 75, 100),
    grid_fmt:       Callable[[float], str] = lambda v: f"{v:.0f}",
    show_axes:      bool = True,
    show_stamp:     bool = True,
    preserve_aspect: str = "meet",   # "meet" keeps text undistorted; "none" stretches
    css_class:      str = "score-chart",
) -> str:
    """Render one chart SVG with everything wired up.

    polylines — what to draw.
    audio     — what to play (defaults to polylines). Decoupled because the
                CPU score chart visualises 8 cores but plays an avg/max/min
                chord.
    ymax      — y-axis upper bound. If None, auto-fits to series max + 15%.
    pad       — (left, right, top, bottom) inner padding in user units.
    grid_pcts — y-axis percentages to draw as gridlines, e.g. (25,50,75,100).
                Pass () to omit the grid (used by Claude tab mini-charts).
    preserve_aspect — "meet" (default) keeps the inner viewBox content
                undistorted at the cost of letterboxing on odd container
                shapes. "none" stretches non-uniformly — use this only when
                the chart has no SVG `<text>` elements that would distort.
    css_class — CSS class on the <svg>. Sizing (width/height) is left to
                CSS so the same chart works in any container.
    """
    if audio is None:
        audio = polylines

    pl, pr, pt, pb = pad

    if ymax is None:
        all_vs = [v for s in polylines for v in list(s.buf) if v > 0]
        ymax = max(max(all_vs) * 1.15, 1) if all_vs else 1

    grid = _grid(pl, pr, pt, pb, w, h, ymax, grid_pcts, grid_fmt)

    poly_svg = ""
    for s in polylines:
        pts = _make_pts(list(s.buf), pl, pr, pt, pb, w, h, ymax)
        if not pts:
            continue
        poly_svg += _polyline(s, pts, pt, pb, h)

    hov   = _hover_layer(audio, pl, pr, pt, pb, w, h, ymax)
    freqs = _freqs_data(audio, ymax)
    axes  = _axes(pl, pr, pt, pb, w, h) if show_axes else ""
    stamp = _stamp(w, h) if show_stamp else ""

    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'class="{css_class}" preserveAspectRatio="{preserve_aspect}" '
            f'data-freqs=\'{freqs}\' data-pl="{pl}" data-iw="{w-pl-pr}" data-w="{w}">'
            f'{grid}{poly_svg}{axes}{hov}{stamp}</svg>')


def empty_chart(
    *, w: int = 280, h: int = 80,
    css_class: str = "score-chart",
    preserve_aspect: str = "meet",
) -> str:
    """A flat baseline used by Claude charts when there are <2 samples yet."""
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'class="{css_class}" preserveAspectRatio="{preserve_aspect}">'
            f'<line x1="0" y1="{h//2}" x2="{w}" y2="{h//2}" '
            f'stroke="var(--t-border)" stroke-width="1"/></svg>')


def play_button(chart_id: str) -> str:
    """The PLAY-button markup used by every chart in every tab.

    Coupled to the JavaScript `playChart(chartId)` function in monitor.py;
    the matching `data-chart` attribute lets the post-htmx-swap handler
    re-find buttons after the chart's container is re-rendered.
    """
    return (f'<button onclick="playChart(\'{chart_id}\',this)" '
            f'class="play-btn" data-chart="{chart_id}">[ PLAY ]</button>')
