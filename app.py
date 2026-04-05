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
import objc

from monitor import Handler, PORT, maybe_collect

URL = f"http://127.0.0.1:{PORT}"

# Module-level refs — ARC will collect these if they go out of scope.
_status_item = None
_delegate    = None


# NSObject subclass must be at module level so PyObjC registers it with
# the Objective-C runtime before any instances are created.
class _MenuDelegate(NSObject):
    """Handles 'Show Monitor' from the NSMenuItem action/target pair."""

    def initWithWindow_(self, win):
        self = objc.super(_MenuDelegate, self).init()
        if self is None:
            return None
        self._window = win
        return self

    # v@:@ — void, self (id), SEL, sender (id)
    @objc.typedSelector(b"v@:@")
    def showMonitor_(self, sender):
        self._window.show()


class _StatusBarSetup(NSObject):
    """Trampoline that runs _create_status_bar on the main thread."""

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
        _status_item.setTitle_("◉ Mon")
        _status_item.setHighlightMode_(True)

        menu = NSMenu.alloc().init()

        show_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show Monitor", "showMonitor:", ""
        )
        show_item.setTarget_(_delegate)
        menu.addItem_(show_item)

        menu.addItem_(NSMenuItem.separatorItem())

        # terminate: is handled by NSApplication via the responder chain.
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", ""
        )
        menu.addItem_(quit_item)

        _status_item.setMenu_(menu)


def _setup_menu_bar(window):
    """Called by pywebview in a background thread — dispatch to main thread."""
    setup = _StatusBarSetup.alloc().initWithWindow_(window)
    setup.performSelectorOnMainThread_withObject_waitUntilDone_(
        "run:", None, False
    )
    # Keep trampoline alive until after run_ fires.
    _setup_menu_bar._setup = setup


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
