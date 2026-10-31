# BA Player - ATSC 3.0 broadcaster apps on Windows
# Copyright (C) 2026 Brian Scott
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software under the GNU GPL v3 or later; see LICENSE.txt.
"""BA Player (console): try ATSC 3.0 broadcaster apps and internet-only channels from a
RabbitEars RENDER report, with no tuner.

    "BA Player CLI.exe"                       pick a report (or use the only one in .\\reports)
    "BA Player CLI.exe" report.html
    "BA Player CLI.exe" report.html --port 9000 --ffmpeg-ac4 "C:\\path\\to\\your\\ffmpeg.exe"

Opens http://localhost:8999/ in your browser. Ctrl-C (or closing the window) stops it.
Everything after the report is passed to the probe (--help lists it all).
"""
import glob, os, sys, threading, time, urllib.request, webbrowser

import ba_probe

DEFAULT_PORT = "8999"
NAME = "BA Player"

PAGE_NOTE = (
    "<div style='border:1px solid #c9a227;background:#fff8e1;color:#5d4600;border-radius:6px;"
    "padding:10px 14px;margin:10px 0'>"
    "<strong>Use the keyboard, not the mouse.</strong> Almost none of the apps respond to the mouse: use the "
    "arrow keys, <kbd>Enter</kbd> (OK) and <kbd>Esc</kbd> (Back). Only the live channels' players use the mouse.<br>"
    "<strong>Some apps take a moment to load.</strong> Give each one several seconds after it opens.<br>"
    "<strong>Gameloop is very finicky.</strong> At the very least, press <kbd>Esc</kbd> and then "
    "<kbd>Space</kbd> a few times. If it still doesn't start, close its tab and launch it again; "
    "it can take several tries.</div>")


def here():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    d = os.path.dirname(os.path.realpath(__file__))
    return os.path.dirname(d) if os.path.basename(d).lower() == "src" else d


def brand(port):
    """Make the probe page say BA Player and leave out the Pi-only notes."""
    ba_probe.BRAND = NAME
    ba_probe.HIDE_ENCRYPTED = True
    ba_probe.VIA_TEXT = ""
    ba_probe.NO_CAPTIONS = True
    ba_probe.HOST_NAME = NAME
    ba_probe.PROBE_WORD = NAME
    ba_probe.DEVICE_MAKE = NAME
    ba_probe.NO_AC4_HINT = ("For sound, set an ffmpeg with the AC-4 decoder in the BA Player window "
                            "(<strong>Get ffmpeg-ac4</strong> explains how), then Stop and Start.")
    for d in (getattr(sys, "_MEIPASS", ""), here()):
        ico = os.path.join(d, "ba_player.ico")
        if d and os.path.isfile(ico):
            with open(ico, "rb") as fh:
                ba_probe.FAVICON = fh.read()            # the browser tab shows the BA Player icon
            break
    ba_probe.PAGE_NOTE = PAGE_NOTE
    ba_probe.PAGE_FOOTER = (f"<p class='muted'>Keep using this page at http://localhost:{port}/. "
                            f"The apps can only reach {NAME} on localhost.</p>"
                            f"<p class='muted'>{NAME} &copy; 2026 Brian Scott &middot; free software under the "
                            f"<a href='https://www.gnu.org/licenses/gpl-3.0.html' target='_blank'>GNU GPL v3 or later</a>.</p>")


def pick_report():
    """The only report in .\\reports, else ask with a file dialog."""
    found = sorted(glob.glob(os.path.join(here(), "reports", "*.html")))
    found = [f for f in found if not f.endswith(".guide.html")]
    if len(found) == 1:
        return found[0]
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        path = filedialog.askopenfilename(
            title="Choose a RENDER report (.html) from RabbitEars",
            initialdir=os.path.join(here(), "reports") if found else os.path.expanduser("~\\Downloads"),
            filetypes=[("RENDER report", "*.html"), ("All files", "*.*")])
        root.destroy()
        return path or None
    except Exception:
        return None


def pause():
    """Keep a double-clicked console window open long enough to read the message."""
    try:
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close.")
    except Exception:
        pass


def open_browser_when_ready(port):
    url = f"http://localhost:{port}/"
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.2)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        sys.argv = [sys.argv[0], "--help"]
        return ba_probe.main()

    report = args[0] if args and not args[0].startswith("-") else None
    rest = args[1:] if report else args
    if not report:
        report = pick_report()
        if not report:
            print("No report chosen. Download a RENDER report (.html) for a market from RabbitEars,")
            print('then run: "BA Player CLI.exe" <report.html>, or use BA Player.exe')
            pause()
            return 1
    if not os.path.isfile(report):
        print(f"Report not found: {report}")
        pause()
        return 1

    if "--port" not in rest:
        rest += ["--port", DEFAULT_PORT]
    if "--log" not in rest:
        rest += ["--log", os.path.join(here(), "ba_player.log.jsonl")]
    port = rest[rest.index("--port") + 1]
    if "--no-audio" not in rest and "--ffmpeg-ac4" not in rest and not ba_probe.find_ac4_ffmpeg():
        print("Note: no ffmpeg with the AC-4 decoder found, so internet channels play without sound.")
        print(f"      Put ffmpeg-ac4.exe next to {NAME}, or add --ffmpeg-ac4 <path to ffmpeg.exe>.\n")

    brand(port)
    sys.argv = [sys.argv[0], os.path.abspath(report)] + rest
    if "--check-live" not in rest:
        threading.Thread(target=open_browser_when_ready, args=(port,), daemon=True).start()
    try:
        return ba_probe.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code if isinstance(e.code, str) else f"{NAME} stopped (code {e.code})")
            pause()
        raise


if __name__ == "__main__":
    sys.exit(main())
