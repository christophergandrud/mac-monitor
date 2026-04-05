# mac-monitor

A real-time macOS system monitor where the data makes sound. Charts are audible — hover to hear individual values, or press PLAY to sequence the full 60-second history as chords. Runs in the browser or as a native `.app`.

## Features

- **Live metrics** — CPU per-core, RAM, swap, network TX/RX, disk read/write, top processes
- **Audible graphs** — hover any chart to hear values as pitched sound; each chart has its own frequency register so CPU, memory, and I/O are immediately distinguishable by ear
- **Chord playback** — PLAY sequences the full history as simultaneous multi-voice chords (all lines fire at once, like piano keys); a white cursor tracks position in the chart
- **Adjustable BPM** — control playback speed in the menu bar (default 200)
- **Keyboard shortcut** — spacebar stops all active playback
- **Pentatonic quantisation** — all pitches snap to C-major pentatonic so simultaneous streams are always consonant regardless of data values
- **Sample-accurate timing** — notes pre-scheduled on the Web Audio clock; cursor driven by `requestAnimationFrame` + `audioCtx.currentTime`, no drift at any BPM
- **Search** — type in the menu bar to query metrics (`cpu`, `mem`, `network`, `disk`, `load`, `ram`, ...) or filter by process name

## Sound design

Each chart occupies a distinct MIDI register (logarithmic pitch mapping) and each data line has a distinct voice:

| Voice | Waveform | Chord | Pan | Used for |
|-------|----------|-------|-----|----------|
| 0 | Sawtooth | Major triad | Left −0.55 | CPU avg · Network TX · MEM |
| 1 | Square | Minor triad | Right +0.55 | CPU max core · Network RX |
| 2 | Triangle | Sus4 | Centre | CPU min core · SWAP |

| Chart | MIDI range | Hz range |
|-------|-----------|----------|
| CPU | 52 – 76 | 165 – 659 Hz |
| Network / Disk | 56 – 80 | 208 – 831 Hz |
| Memory | 60 – 84 | 262 – 1047 Hz |

- **Slope → detune + brightness** — rising data detunes up to +80 cents and opens the filter; falling detunes down and darkens the timbre
- **Reverb** — synthetic convolution reverb (exponential decay impulse response)
- **Master compressor** — DynamicsCompressor (−24 dB threshold, 4:1) keeps loud and quiet passages both audible

## Themes

Drop any [Warp-compatible YAML theme](https://github.com/warpdotdev/themes) at `~/.mac-monitor/theme.yaml` and the app reloads within 5 seconds — no restart needed.

```bash
cp themes/commodore-64.yaml ~/.mac-monitor/theme.yaml
```

Five themes are bundled:

| Theme | Inspired by |
|-------|-------------|
| `spring-dark.yaml` | Default — high-luminance spring palette |
| `apple-iie.yaml` | Apple IIe green phosphor monitor |
| `commodore-64.yaml` | Commodore 64 BASIC boot screen |
| `bbc-micro.yaml` | BBC Micro Mode 1 full-colour palette |
| `amber-phosphor.yaml` | Amber phosphor IBM terminal |

## Running in the browser

```bash
pip install psutil
python3 monitor.py
```

Open http://localhost:8787. Without `psutil` the monitor runs in simulation mode (random-walk data).

## Running as a native macOS app

Requires `pywebview` (WKWebView window) and `pyobjc-framework-Cocoa` (menu bar icon).

```bash
pip install pywebview pyobjc-framework-Cocoa psutil
python app.py
```

Opens the dashboard in a native window and adds a **◉ Mon** icon to the menu bar with Show Monitor / Quit.

### Build a distributable .app

```bash
pip install py2app
python setup.py py2app -A   # alias build — fast, symlinked, good for development
python setup.py py2app      # release build — self-contained → dist/Mac Monitor.app
open "dist/Mac Monitor.app"
```

## Stack

| Layer | Technology |
|-------|-----------|
| Server | Python stdlib — `http.server`, `collections.deque` |
| Metrics | `psutil` |
| Frontend | htmx 1.9 — SVG fragments polled every 2s |
| Audio | Web Audio API — OscillatorNode, BiquadFilter, StereoPanner, ConvolverNode, DynamicsCompressor |
| Desktop | pywebview (WKWebView) + PyObjC (NSStatusItem) + py2app |

No build step. `monitor.py` is a single file and runs anywhere Python 3.8+ is installed.
