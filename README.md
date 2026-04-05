# mac-monitor

A real-time macOS system monitor with audible charts. Built with htmx, plain Python (no framework), and the Web Audio API.

## Features

- **Live metrics** — CPU per-core, RAM, swap, network TX/RX, disk read/write, top processes
- **Audible graphs** — hover across any chart to hear the data as pitched sound; each metric occupies a distinct frequency register
- **Chord playback** — press PLAY on any chart to hear its full 60-second history as a multi-voice chord sequence (all lines play simultaneously, like piano keys)
- **Pentatonic quantisation** — all pitches snap to the C-major pentatonic scale so simultaneous streams are always consonant
- **Sample-accurate timing** — audio is pre-scheduled on the Web Audio timeline; the playhead cursor uses `requestAnimationFrame` + `audioCtx.currentTime` as ground truth, so there is no drift at any BPM
- **Search** — menubar search matches metric keywords (`cpu`, `mem`, `network`, `disk`, `load`, `ram`, ...) and process names live

## Sound design

| Voice | Waveform | Chord | Pan | Metric |
|-------|----------|-------|-----|--------|
| 0 | Sawtooth | Major triad | Left | CPU avg / Network TX / MEM |
| 1 | Square | Minor triad | Right | CPU max core / Network RX / SWAP |
| 2 | Triangle | Sus4 | Centre | CPU min core |

- **Frequency mapping** — values map through MIDI note numbers (logarithmic), not raw Hz, so pitch changes feel perceptually even
- **Slope → detune** — rising data = sharper (up to +80 cents) and brighter timbre; falling = flatter and darker
- **Reverb + compressor** — synthetic impulse response reverb; master DynamicsCompressor keeps loud and quiet passages both audible

## Running

```bash
pip install psutil
python3 monitor.py
# open http://localhost:8787
```

Without `psutil` the monitor runs in simulation mode (random-walk data).

## Stack

- **Python stdlib only** — `http.server`, `collections.deque`, no web framework
- **htmx 1.9** — all chart updates are server-side SVG fragments polled every 2s
- **Web Audio API** — oscillators, BiquadFilter, StereoPanner, ConvolverNode, DynamicsCompressor
- **No build step** — single file, runs anywhere Python 3.8+ is installed
