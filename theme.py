"""
theme.py — Warp-compatible YAML theme loader for Mac Monitor.

Drop a Warp theme file at ~/.mac-monitor/theme.yaml to restyle the app.
Community themes: https://github.com/warpdotdev/themes
"""

import pathlib, colorsys, subprocess, json

try:
    import yaml
    def _parse(text): return yaml.safe_load(text)
except ImportError:
    import json
    def _parse(text): return json.loads(text)

def _find_themes_dir() -> pathlib.Path:
    """Locate the themes directory, handling py2app bundles where __file__ is inside a zip."""
    candidate = pathlib.Path(__file__).parent / "themes"
    if candidate.is_dir():
        return candidate
    # py2app: themes are in Contents/Resources/themes inside the .app bundle
    import sys
    if getattr(sys, "frozen", False):
        resources = pathlib.Path(sys.executable).resolve().parent.parent / "Resources" / "themes"
        if resources.is_dir():
            return resources
    return candidate  # fallback

_HERE     = _find_themes_dir()
_USER     = pathlib.Path.home() / ".mac-monitor" / "theme.yaml"
_SETTINGS = pathlib.Path.home() / ".mac-monitor" / "settings.json"
_DEFAULT  = "spring-dark.yaml"

_SETTINGS_DEFAULTS = {
    "follow_system": False,
    "dark_theme":    "spring-dark",
    "light_theme":   "paper",
}

# ── colour helpers ─────────────────────────────────────────────────────────────

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))

def _rgb_to_hex(r, g, b):
    return "#{:02X}{:02X}{:02X}".format(int(r*255), int(g*255), int(b*255))

def _adjust_lightness(hex_color, delta):
    """Shift HSL lightness by delta (−1..+1). Returns hex string."""
    r, g, b = _hex_to_rgb(hex_color)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l + delta))
    return _rgb_to_hex(*colorsys.hls_to_rgb(h, l, s))

def _alpha_hex(hex_color, alpha_pct):
    """Return hex color with two-digit alpha suffix (e.g. #RRGGBBAA)."""
    aa = round(alpha_pct / 100 * 255)
    return hex_color.rstrip("#").replace("#", "#") + f"{aa:02X}"


# ── loader ─────────────────────────────────────────────────────────────────────

def _resolve(raw: dict) -> dict:
    """Merge raw YAML over defaults and derive panel/border tones."""
    tc = raw.get("terminal_colors", {})
    normal = tc.get("normal", {})
    bright = tc.get("bright", {})

    bg      = raw.get("background", "#0d0d0d")
    fg      = raw.get("foreground", "#f0f0f0")
    accent  = raw.get("accent",     "#C8FF47")
    cursor  = raw.get("cursor",     "#ffffff")
    details = raw.get("details",    "darker")

    # For dark backgrounds, panels must be lighter to be visible.
    # For light backgrounds, panels must be darker.
    # `details: darker` biases chrome toward dark; `lighter` toward light.
    r, g, b = _hex_to_rgb(bg)
    _, bg_l, _ = colorsys.rgb_to_hls(r, g, b)
    dark_bg = bg_l < 0.5
    # Always push panels away from bg so they're distinguishable
    base_shift = +0.07 if dark_bg else -0.07
    # `details` nudges the direction slightly (Warp semantics)
    if (details == "darker" and dark_bg) or (details == "lighter" and not dark_bg):
        base_shift *= 0.6   # same direction, subtler
    panel  = _adjust_lightness(bg, base_shift)
    panel2 = _adjust_lightness(bg, base_shift * 0.5)
    stripe = _adjust_lightness(bg, base_shift * 1.5)

    c0 = accent
    c1 = normal.get("red",     "#FF6BB5")
    c2 = normal.get("cyan",    "#4DDDFF")

    return {
        "name":    raw.get("name", "Custom"),
        "bg":      bg,
        "fg":      fg,
        "accent":  accent,
        "cursor":  cursor,
        "panel":   panel,
        "panel2":  panel2,
        "stripe":  stripe,
        "border":  _adjust_lightness(bg, base_shift * 2.5),
        "muted":   _adjust_lightness(fg, -0.35),
        "c0":      c0,
        "c1":      c1,
        "c2":      c2,
        # shadow tint for card box-shadow
        "c0_22":   c0 + "22",
        "c0_33":   c0 + "33",
        "c0_44":   c0 + "44",
        # raw terminal_colors preserved for future use
        "normal":  normal,
        "bright":  bright,
    }


def load() -> dict:
    """Load active theme: user override → built-in default."""
    if _USER.exists():
        raw = _parse(_USER.read_text())
    else:
        raw = _parse((_HERE / _DEFAULT).read_text())
    return _resolve(raw)


def _is_dark_theme(raw: dict) -> bool:
    """Determine if a theme is dark based on its background luminance."""
    bg = raw.get("background", "#000000")
    r, g, b = _hex_to_rgb(bg)
    _, l, _ = colorsys.rgb_to_hls(r, g, b)
    return l < 0.5


def list_themes() -> list[dict]:
    """Return all built-in themes as list of {slug, name, dark} dicts.

    Sorted by group (dark first, then light), then by name within each group.
    """
    themes = []
    for f in sorted(_HERE.glob("*.yaml")):
        raw = _parse(f.read_text())
        themes.append({
            "slug": f.stem,
            "name": raw.get("name", f.stem),
            "dark": _is_dark_theme(raw),
        })
    # Dark themes first, then light; alphabetical within each group
    themes.sort(key=lambda t: (not t["dark"], t["name"]))
    return themes


def set_theme(slug: str) -> dict:
    """Write built-in theme <slug> to the user override path and return resolved dict."""
    src = _HERE / f"{slug}.yaml"
    if not src.exists():
        raise FileNotFoundError(slug)
    _USER.parent.mkdir(parents=True, exist_ok=True)
    _USER.write_text(src.read_text())
    return _resolve(_parse(src.read_text()))


def load_settings() -> dict:
    if _SETTINGS.exists():
        try:
            return {**_SETTINGS_DEFAULTS, **json.loads(_SETTINGS.read_text())}
        except Exception:
            pass
    return dict(_SETTINGS_DEFAULTS)


def save_settings(d: dict) -> None:
    _SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS.write_text(json.dumps(d, indent=2))


def system_is_dark() -> bool:
    """Return True if macOS is currently in Dark Mode."""
    try:
        r = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True
        )
        return r.returncode == 0 and "dark" in r.stdout.strip().lower()
    except Exception:
        return False


def theme_for_system() -> dict:
    """Return the correct theme dict based on current system appearance and settings."""
    s = load_settings()
    slug = s["dark_theme"] if system_is_dark() else s["light_theme"]
    src = _HERE / f"{slug}.yaml"
    if not src.exists():
        src = _HERE / _DEFAULT
    return _resolve(_parse(src.read_text()))


def css_vars(t: dict) -> str:
    """Return a <style> block with CSS custom properties for theme t."""
    return (
        '<style id="theme-style">\n'
        ':root{\n'
        f'  --t-bg:      {t["bg"]};\n'
        f'  --t-panel:   {t["panel"]};\n'
        f'  --t-panel2:  {t["panel2"]};\n'
        f'  --t-stripe:  {t["stripe"]};\n'
        f'  --t-border:  {t["border"]};\n'
        f'  --t-fg:      {t["fg"]};\n'
        f'  --t-muted:   {t["muted"]};\n'
        f'  --t-accent:  {t["accent"]};\n'
        f'  --t-cursor:  {t["cursor"]};\n'
        f'  --t-c0:      {t["c0"]};\n'
        f'  --t-c1:      {t["c1"]};\n'
        f'  --t-c2:      {t["c2"]};\n'
        f'  --t-c0-22:   {t["c0_22"]};\n'
        f'  --t-c0-33:   {t["c0_33"]};\n'
        f'  --t-c0-44:   {t["c0_44"]};\n'
        '}\n'
        '</style>\n'
    )
