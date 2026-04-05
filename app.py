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
from Foundation import NSTimer
import objc

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


class _MenuDelegate(NSObject):
    """
    Owns the status item menu.
    - showMonitor_  : NSMenuItem action to raise the window
    - updateTitle_  : NSTimer callback — refreshes CPU % in bar title
    - menuWillOpen_ : NSMenuDelegate — refreshes all metric rows on open
    """

    def initWithWindow_(self, win):
        self = objc.super(_MenuDelegate, self).init()
        if self is None:
            return None
        self._window   = win
        self._cpu_item = None
        self._mem_item = None
        self._net_item = None
        self._dsk_item = None
        return self

    @objc.typedSelector(b"v@:@")
    def showMonitor_(self, sender):
        self._window.show()

    @objc.typedSelector(b"v@:@")
    def updateTitle_(self, timer):
        """Fires every 2 s — keeps the bar title current."""
        cpu = sum(b[-1] for b in CPU_BUFS) / _nc
        _status_item.setTitle_(f"{_cpu_spark()} {cpu:.0f}%")

    def menuWillOpen_(self, menu):
        """Refresh all metric rows the moment the user clicks the icon."""
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

        # ── menu ──────────────────────────────────────────────────────────
        menu = NSMenu.alloc().init()
        menu.setDelegate_(_delegate)  # enables menuWillOpen_

        def static_item(title):
            """Non-interactive label row."""
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

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "")
        menu.addItem_(quit_item)

        _status_item.setMenu_(menu)

        # ── live title update every 2 s ───────────────────────────────────
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, _delegate, "updateTitle:", None, True
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
    maybe_collect()  # prime psutil cpu_percent baseline

    t = threading.Thread(target=_start_server, daemon=True)
    t.start()

    window = webview.create_window(
        "Mac Monitor",
        URL,
        width=1280,
        height=840,
        resizable=True,
        background_color="#0d0d0d",
    )

    webview.start(func=_setup_menu_bar, args=[window], debug=False)
