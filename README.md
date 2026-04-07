# mac-monitor

A real-time macOS system monitor where charts make sound. Hover any chart to hear values as pitched tones, or press PLAY to sequence the history as chords. Also monitors running Claude Code instances.

## Quick start

```bash
# browser
pip install psutil
python3 monitor.py        # → http://localhost:8787

# native macOS app
pip install pywebview pyobjc-framework-Cocoa psutil
python app.py
```

Without `psutil` the monitor runs in simulation mode.

## Two tabs

**System** — per-core CPU, RAM/swap, network TX/RX, disk read/write, process list. Each chart has hover-to-beep and a PLAY button.

**Claude** — live Claude Code instances with context fill %, CPU, memory, current tool, sub-agents, and attention flags (permission blocks, rate limits, errors). Interactive charts with sound for each instance. Daily API cost and 7-day activity.

## Keyboard shortcuts

- **Space** — stop all playback
- **Cmd+[** / **Cmd+]** — switch tabs

## Sound design

Pitches are mapped to a MIDI range per chart and snapped to C-major pentatonic so simultaneous streams stay consonant. Three voice timbres (sawtooth/square/triangle) are panned left/right/center. Rising data detunes up and brightens the filter; falling data does the opposite.

## Themes

Drop any [Warp-compatible YAML theme](https://github.com/warpdotdev/themes) at `~/.mac-monitor/theme.yaml` — the app hot-reloads within 5 seconds. Six themes are bundled in `themes/`. Switch via the menu bar Settings > Appearance.

## Build .app

```bash
pip install py2app
python setup.py py2app      # release → dist/Mac Monitor.app
python setup.py py2app -A   # dev alias build (symlinked)
```

## Stack

Python stdlib server + `psutil` for metrics, htmx + SVG for the frontend, Web Audio API for sound, pywebview + PyObjC for the native app shell. No build step — `monitor.py` runs standalone on Python 3.8+.
