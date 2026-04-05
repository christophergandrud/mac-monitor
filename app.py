"""
app.py — macOS desktop entry point for Mac Monitor.

Threading model
  background thread : HTTPServer.serve_forever()
  main thread       : webview.start()  — required by Cocoa/WKWebView
  NSStatusItem      : created inside the `started` callback pywebview
                      fires once its NSApplication run loop is live
"""

import threading
from http.server import HTTPServer

import webview

from monitor import Handler, PORT, maybe_collect

URL = f"http://127.0.0.1:{PORT}"

# Module-level refs so ARC doesn't collect the status bar objects.
_status_item = None
_delegate    = None


def _start_server():
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def _setup_menu_bar(window):
    """
    Called by pywebview on the main thread once NSApplication is running.
    Safe to import and use AppKit here.
    """
    global _status_item, _delegate

    from AppKit import (
        NSStatusBar, NSVariableStatusItemLength,
        NSMenu, NSMenuItem, NSObject,
    )
    import objc

    class MenuDelegate(NSObject):
        """Receives 'Show Monitor' from the NSMenuItem action/target pair."""

        def initWithWindow_(self, win):
            self = objc.super(MenuDelegate, self).init()
            if self is None:
                return None
            self._window = win
            return self

        # v@:@ — void return, self (id), SEL, sender (id)
        @objc.typedSelector(b"v@:@")
        def showMonitor_(self, sender):
            self._window.show()

    _delegate = MenuDelegate.alloc().initWithWindow_(window)

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


if __name__ == "__main__":
    maybe_collect()  # prime psutil cpu_percent baseline before first request

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

    # func= is called on the main thread once NSApplication is running.
    webview.start(func=_setup_menu_bar, args=[window], debug=False)
