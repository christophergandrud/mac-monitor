"""
app.py — macOS desktop entry point for Mac Monitor.

Threading model
  background thread : HTTPServer.serve_forever()
  main thread       : webview.start() — required by Cocoa/WKWebView
  NSStatusItem      : pywebview calls func= in a background thread, so we
                      dispatch back to the main thread via
                      performSelectorOnMainThread before touching AppKit.
"""

import threading
from http.server import HTTPServer

import webview
from AppKit import (
    NSStatusBar, NSVariableStatusItemLength,
    NSMenu, NSMenuItem, NSObject,
)
from Foundation import NSTimer, NSDistributedNotificationCenter
import objc

import monitor
import theme as _theme
from monitor import (Handler, PORT, maybe_collect,
                     CPU_BUFS, MEM_BUF, SWAP_BUF,
                     NET_TX, NET_RX, DISK_R, DISK_W, fmt_bytes, _nc, BUF)

_BLOCKS = "▁▂▃▄▅▆▇█"

def _cpu_spark(n=6):
    """Last n average-CPU samples as Unicode block characters."""
    avgs = [sum(b[i] for b in CPU_BUFS) / _nc
            for i in range(BUF - n, BUF)]
    return "".join(_BLOCKS[min(int(v / 100 * 8), 7)] for v in avgs)

URL = f"http://127.0.0.1:{PORT}"

# Module-level refs — ARC will collect these if they go out of scope.
_status_item = None
_delegate    = None


def _apply_theme(t: dict) -> None:
    """Push a resolved theme dict into the monitor module."""
    monitor._T = t


class _MenuDelegate(NSObject):
    """
    Owns the status item menu.
    - showMonitor_           : raise the webview window
    - updateTitle_           : NSTimer — refresh CPU sparkline in menu bar
    - menuWillOpen_          : NSMenuDelegate — refresh metric rows on open
    - selectTheme_           : theme menu item action
    - toggleFollowSystem_    : follow-system toggle action
    - systemAppearanceChanged_ : NSDistributedNotificationCenter callback
    """

    def initWithWindow_(self, win):
        self = objc.super(_MenuDelegate, self).init()
        if self is None:
            return None
        self._window           = win
        self._cpu_item         = None
        self._mem_item         = None
        self._net_item         = None
        self._dsk_item         = None
        self._theme_items      = []   # (NSMenuItem, slug) — for checkmark sync
        self._follow_item      = None
        return self

    # ── metrics ───────────────────────────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def showMonitor_(self, sender):
        self._window.show()

    @objc.typedSelector(b"v@:@")
    def updateTitle_(self, timer):
        cpu = sum(b[-1] for b in CPU_BUFS) / _nc
        _status_item.setTitle_(f"{_cpu_spark()} {cpu:.0f}%")

    def menuWillOpen_(self, menu):
        maybe_collect()
        cpu  = sum(b[-1] for b in CPU_BUFS) / _nc
        mem  = MEM_BUF[-1]
        swap = SWAP_BUF[-1]
        tx   = NET_TX[-1]
        rx   = NET_RX[-1]
        dr   = DISK_R[-1]
        dw   = DISK_W[-1]

        if self._cpu_item:
            self._cpu_item.setTitle_(f"CPU    {cpu:.1f}%")
        if self._mem_item:
            self._mem_item.setTitle_(f"MEM    {mem:.1f}%   SWAP {swap:.1f}%")
        if self._net_item:
            self._net_item.setTitle_(f"NET    ↑{fmt_bytes(tx)}  ↓{fmt_bytes(rx)}")
        if self._dsk_item:
            self._dsk_item.setTitle_(f"DISK   R {fmt_bytes(dr)}  W {fmt_bytes(dw)}")

    # ── appearance ────────────────────────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def selectTheme_(self, sender):
        slug = sender.representedObject()
        t    = _theme.set_theme(slug)
        _apply_theme(t)

        s = _theme.load_settings()
        # Update the user's dark/light preference to this slug
        if _theme.system_is_dark():
            s["dark_theme"] = slug
        else:
            s["light_theme"] = slug
        _theme.save_settings(s)

        self._sync_theme_checkmarks(slug)

    @objc.typedSelector(b"v@:@")
    def toggleFollowSystem_(self, sender):
        s = _theme.load_settings()
        s["follow_system"] = not s["follow_system"]
        _theme.save_settings(s)

        on = s["follow_system"]
        if self._follow_item:
            self._follow_item.setState_(1 if on else 0)

        if on:
            s2 = _theme.load_settings()
            slug = s2["dark_theme"] if _theme.system_is_dark() else s2["light_theme"]
            t = _theme.set_theme(slug)
            _apply_theme(t)
            self._sync_theme_checkmarks(slug)

    @objc.typedSelector(b"v@:@")
    def systemAppearanceChanged_(self, notification):
        s = _theme.load_settings()
        if not s.get("follow_system"):
            return
        slug = s["dark_theme"] if _theme.system_is_dark() else s["light_theme"]
        t = _theme.set_theme(slug)
        _apply_theme(t)
        self._sync_theme_checkmarks(slug)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _sync_theme_checkmarks(self, active_slug):
        for item, slug in self._theme_items:
            item.setState_(1 if slug == active_slug else 0)


class _StatusBarSetup(NSObject):
    """Trampoline — dispatched to the main thread to build the status item."""

    def initWithWindow_(self, win):
        self = objc.super(_StatusBarSetup, self).init()
        if self is None:
            return None
        self._window = win
        return self

    @objc.typedSelector(b"v@:@")
    def run_(self, _):
        global _status_item, _delegate

        _delegate    = _MenuDelegate.alloc().initWithWindow_(self._window)
        bar          = NSStatusBar.systemStatusBar()
        _status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)

        cpu = sum(b[-1] for b in CPU_BUFS) / _nc
        _status_item.setTitle_(f"{_cpu_spark()} {cpu:.0f}%")
        _status_item.setHighlightMode_(True)

        # ── main menu ─────────────────────────────────────────────────────────
        menu = NSMenu.alloc().init()
        menu.setDelegate_(_delegate)

        def static_item(title):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, "")
            item.setEnabled_(False)
            return item

        _delegate._cpu_item = static_item(f"CPU    {cpu:.0f}%")
        _delegate._mem_item = static_item("MEM    —")
        _delegate._net_item = static_item("NET    —")
        _delegate._dsk_item = static_item("DISK   —")

        for item in (_delegate._cpu_item, _delegate._mem_item,
                     _delegate._net_item, _delegate._dsk_item):
            menu.addItem_(item)

        menu.addItem_(NSMenuItem.separatorItem())

        show_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show Monitor", "showMonitor:", "")
        show_item.setTarget_(_delegate)
        menu.addItem_(show_item)

        menu.addItem_(NSMenuItem.separatorItem())

        # ── Settings > Appearance submenu ─────────────────────────────────────
        appearance_menu = NSMenu.alloc().init()

        active_name = monitor._T["name"]
        s           = _theme.load_settings()

        for t in _theme.list_themes():
            ti = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                t["name"], "selectTheme:", "")
            ti.setTarget_(_delegate)
            ti.setRepresentedObject_(t["slug"])
            ti.setState_(1 if t["name"] == active_name else 0)
            appearance_menu.addItem_(ti)
            _delegate._theme_items.append((ti, t["slug"]))

        appearance_menu.addItem_(NSMenuItem.separatorItem())

        follow_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Follow System", "toggleFollowSystem:", "")
        follow_item.setTarget_(_delegate)
        follow_item.setState_(1 if s.get("follow_system") else 0)
        appearance_menu.addItem_(follow_item)
        _delegate._follow_item = follow_item

        appearance_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Appearance", None, "")
        appearance_item.setSubmenu_(appearance_menu)

        settings_menu = NSMenu.alloc().initWithTitle_("Settings")
        settings_menu.addItem_(appearance_item)

        settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Settings", None, "")
        settings_item.setSubmenu_(settings_menu)
        menu.addItem_(settings_item)

        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "")
        menu.addItem_(quit_item)

        _status_item.setMenu_(menu)

        # ── timers & notifications ────────────────────────────────────────────
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, _delegate, "updateTitle:", None, True
        )

        # System dark/light mode change notification
        NSDistributedNotificationCenter.defaultCenter(
        ).addObserver_selector_name_object_(
            _delegate,
            "systemAppearanceChanged:",
            "AppleInterfaceThemeChangedNotification",
            None,
        )


def _setup_menu_bar(window):
    """Called by pywebview in a background thread — dispatch to main thread."""
    setup = _StatusBarSetup.alloc().initWithWindow_(window)
    setup.performSelectorOnMainThread_withObject_waitUntilDone_(
        "run:", None, False
    )
    _setup_menu_bar._setup = setup  # prevent GC before run_ fires


def _start_server():
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    # Apply follow-system theme at launch if enabled
    s = _theme.load_settings()
    if s.get("follow_system"):
        _apply_theme(_theme.theme_for_system())

    maybe_collect()  # prime psutil cpu_percent baseline

    t = threading.Thread(target=_start_server, daemon=True)
    t.start()

    window = webview.create_window(
        "Mac Monitor",
        URL,
        width=1280,
        height=840,
        resizable=True,
        background_color=monitor._T["bg"],
    )

    webview.start(func=_setup_menu_bar, args=[window], debug=False)
