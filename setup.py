"""
setup.py — py2app bundle configuration for Mac Monitor.app

Quick start
-----------
    python3 -m venv .venv && source .venv/bin/activate
    pip install py2app pywebview pyobjc-framework-Cocoa psutil

    # Alias mode — symlinked bundle, reflects source changes immediately
    python setup.py py2app -A

    # Full release build
    python setup.py py2app
    open "dist/Mac Monitor.app"
"""

from setuptools import setup

VERSION = open("VERSION").read().strip()

APP     = ["app.py"]
OPTIONS = {
    "iconfile": "icon.icns",
    # Full package directories (includes compiled .so extensions like psutil).
    "packages": [
        "webview",
        "psutil",
        "yaml",
        "AppKit",
        "Foundation",
        "objc",
    ],
    # Explicit module includes for anything py2app's static scanner may miss
    # (pywebview selects its backend at runtime via platform detection).
    "includes": [
        "monitor",
        "theme",
        "http.server",
        "urllib.parse",
        "collections",
        "threading",
        "webview.platforms.cocoa",
    ],
    "excludes": [
        "tkinter",
        "unittest",
        "xmlrpc",
        "distutils",
        "test",
    ],
    "plist": {
        # Menu-bar / background agent — no Dock icon.
        "LSUIElement": True,
        "CFBundleName": "Mac Monitor",
        "CFBundleDisplayName": "Mac Monitor",
        "CFBundleIdentifier": "com.macmonitor.app",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        # Allow http://localhost without TLS in WKWebView.
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
        },
        "NSHighResolutionCapable": True,
    },
}

DATA_FILES = [
    ("themes", ["themes/spring-dark.yaml",
                "themes/apple-iie.yaml",
                "themes/commodore-64.yaml",
                "themes/bbc-micro.yaml",
                "themes/amber-phosphor.yaml",
                "themes/macintosh.yaml",
                "themes/paper.yaml"]),
    ("", ["pricing.yaml"]),
]

setup(
    name="Mac Monitor",
    version=VERSION,
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
