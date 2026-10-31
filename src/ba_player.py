# BA Player - ATSC 3.0 broadcaster apps on Windows
# Copyright (C) 2026 Brian Scott
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software under the GNU GPL v3 or later; see LICENSE.txt.
"""BA Player: try ATSC 3.0 (NextGen TV) broadcaster apps and internet-only channels from a
RabbitEars RENDER report. Pick a report (or several, which are combined), set the AC-4
ffmpeg, Start, and watch the apps' requests.

The probe itself runs as a second copy of this program ("--serve"), so Stop is clean and
the window stays responsive. A report dropped onto "BA Player.exe" opens and starts at once.
"""
import datetime, email.utils, json, os, queue, subprocess, sys, threading, time, urllib.request, webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ba_probe
import ba_player_cli as ba_probe_win

APP = "BA Player"
VERSION = "1.1"
AUTHOR = "Brian Scott"
ABOUT = (f"{APP} {VERSION}\n"
         "ATSC 3.0 (NextGen TV) broadcaster apps and internet channels on Windows,\n"
         "from RabbitEars RENDER reports.\n\n"
         f"Copyright (C) 2026 {AUTHOR}\n\n"
         "This program is free software: you can redistribute it and/or modify it under the\n"
         "terms of the GNU General Public License as published by the Free Software\n"
         "Foundation, either version 3 of the License, or (at your option) any later version.\n\n"
         "It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;\n"
         "without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR\n"
         "PURPOSE. See LICENSE.txt and THIRD-PARTY-NOTICES.md for details.")
REPORTS_URL = "https://www.rabbitears.info/market.php"
FFMPEG_URL = "https://github.com/ferrellsl/ffmpeg-ac4"
MSYS2_URL = "https://www.msys2.org"

REPORTS_HELP = (
    "Getting a report from RabbitEars:\n\n"
    "1. In the search box at the top of the page, enter the call sign of a local station "
    "(for example KFOX) and open its page.\n\n"
    "2. Glance down the page for a green > arrow. Just to the right of it is the RE icon.\n\n"
    "3. Right-click the RE icon and choose 'Save link as...' to save the report (.html).\n\n"
    "4. Back in BA Player, click 'Open report...' and pick the file. "
    "To use several markets at once, pick them all with 'Combine reports...'.\n\n"
    "Use the newest report you can find: internet-only channels stop working once a report gets old.")

FFMPEG_HELP = r"""Browsers can't play Dolby AC-4, the audio on internet-only channels. BA Player converts
it with an ffmpeg that has an AC-4 decoder. Upstream FFmpeg has none, so it has to be built
from ferrellsl/ffmpeg-ac4 (FFmpeg with an unofficial AC-4 decoder). It has no ready-made
downloads; these steps build it with MSYS2 (about 15-30 minutes, mostly compiling).

1. Install MSYS2 from https://www.msys2.org ("Get MSYS2" below), then open an MSYS2 UCRT64
   terminal ("Open MSYS2 UCRT64" below, or from the Start menu). Type the commands there.

2. Install the build tools:
     pacman -S --needed git make mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-pkgconf

3. Get the source:
     git clone https://github.com/ferrellsl/ffmpeg-ac4.git
     cd ffmpeg-ac4

4. Configure (a self-contained ffmpeg.exe that needs no DLLs):
     ./configure --disable-doc --disable-ffplay --disable-ffprobe --disable-autodetect --enable-static --disable-shared --extra-ldflags=-static

5. Build:
     make -j$(nproc)

6. Check it has the decoder (should print a line with "ac4"):
     ./ffmpeg -hide_banner -decoders | grep ac4

7. ffmpeg.exe is in the folder you built it in. With the steps above that's
   C:\msys64\home\<your user name>\ffmpeg-ac4, or wherever you cloned it or moved it to.
   In BA Player, point "ffmpeg (AC-4)" at it with Browse... (it can stay where it is),
   or copy it next to "BA Player.exe" as ffmpeg-ac4.exe and leave the box empty.

If step 4 or 5 fails on the static link, drop --enable-static --disable-shared --extra-ldflags=-static,
build again, and keep ffmpeg.exe together with the DLLs it needs (from C:\msys64\ucrt64\bin)."""


def here():
    return ba_probe_win.here()


def settings_path():
    for d in (here(), os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)):
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "ba_player.json")
            with open(p, "a"):
                pass
            return p
        except OSError:
            continue
    return None


def clock_offset():
    """Seconds this PC's clock is behind (+) or ahead (-) of an internet server, or None."""
    for url in ("https://www.google.com/generate_204", "https://www.microsoft.com/"):
        try:
            t0 = time.time()
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=6) as r:
                date = r.headers.get("Date")
            t1 = time.time()
            if date:
                server = email.utils.parsedate_to_datetime(date).timestamp()
                return server + 0.5 - (t0 + t1) / 2     # Date has 1 s resolution: take the middle
        except Exception:
            continue
    return None


class Gui:
    def __init__(self, root, report=None):
        self.root, self.proc, self.q = root, None, queue.Queue()
        self.sp = settings_path()
        cfg = self.load()
        root.title("BA Player - ATSC 3.0 broadcaster apps")
        root.geometry("980x640")
        root.minsize(760, 460)
        self.report = tk.StringVar(value=report or cfg.get("report", ""))
        self.ffmpeg = tk.StringVar(value=cfg.get("ffmpeg", ""))
        self.port = tk.StringVar(value=str(cfg.get("port", ba_probe_win.DEFAULT_PORT)))
        self.ac4_text = tk.StringVar(value="")
        self.clock_text = tk.StringVar(value="Clock: checking...")
        self.info_text = tk.StringVar(value="")
        self.status = tk.StringVar(value="Stopped")

        f = ttk.Frame(root, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Report").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(f, textvariable=self.report).grid(row=0, column=1, sticky="ew")
        ttk.Button(f, text="Open report...", command=self.pick_report).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(f, text="Combine reports...", command=self.combine).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(f, text="Get reports", command=self.get_reports).grid(row=0, column=4, padx=(6, 0), sticky="ew")
        ttk.Label(f, textvariable=self.info_text, foreground="#555").grid(row=1, column=1, columnspan=4, sticky="w", pady=(2, 8))

        ttk.Label(f, text="ffmpeg (AC-4)").grid(row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(f, textvariable=self.ffmpeg).grid(row=2, column=1, sticky="ew")
        ttk.Button(f, text="Browse...", command=self.pick_ffmpeg).grid(row=2, column=2, padx=(6, 0), sticky="ew")
        ttk.Button(f, text="Get ffmpeg-ac4", command=self.get_ffmpeg).grid(row=2, column=3, columnspan=2, padx=(6, 0), sticky="ew")
        self.ac4_label = ttk.Label(f, textvariable=self.ac4_text)
        self.ac4_label.grid(row=3, column=1, columnspan=4, sticky="w", pady=(2, 8))

        row = ttk.Frame(f)
        row.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(0, 8))
        ttk.Label(row, text="Port").pack(side="left")
        ttk.Spinbox(row, from_=1024, to=65535, width=7, textvariable=self.port).pack(side="left", padx=(8, 16))
        self.clock_label = ttk.Label(row, textvariable=self.clock_text)
        self.clock_label.pack(side="left")

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(0, 8))
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Open page", command=self.open_page).pack(side="left", padx=(18, 0))
        ttk.Button(btns, text="Request log", command=lambda: self.open_page("log")).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Browse Extension", command=self.open_extension).pack(side="left", padx=(18, 0))
        ttk.Button(btns, text="Reports folder", command=self.open_reports).pack(side="left", padx=(6, 0))
        ttk.Label(btns, textvariable=self.status).pack(side="right")
        ttk.Button(btns, text="About", command=lambda: messagebox.showinfo("About " + APP, ABOUT)).pack(side="right", padx=(0, 12))

        lf = ttk.Frame(f)
        lf.grid(row=6, column=0, columnspan=5, sticky="nsew")
        f.rowconfigure(6, weight=1)
        self.log = tk.Text(lf, wrap="none", height=12, font=("Consolas", 9), state="disabled",
                           background="#111418", foreground="#d8dde6", insertbackground="#d8dde6")
        ys = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(lf, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        for tag, color in (("warn", "#f0b35a"), ("err", "#ff7b72"), ("info", "#7ec699")):
            self.log.tag_configure(tag, foreground=color)

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.report.trace_add("write", lambda *_: self.describe_report())
        self.describe_report()
        self._ff_after = None
        self.ffmpeg.trace_add("write", lambda *_: self.ffmpeg_changed())
        self.check_ffmpeg()
        threading.Thread(target=self.check_clock, daemon=True).start()
        root.after(100, self.pump)
        if report:
            root.after(600, self.start)

    # ---------------------------------------------------------------- settings
    def load(self):
        try:
            with open(self.sp, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save(self):
        if not self.sp:
            return
        try:
            with open(self.sp, "w", encoding="utf-8") as fh:
                json.dump({"report": self.report.get(), "ffmpeg": self.ffmpeg.get(), "port": self.port.get()}, fh, indent=1)
        except OSError:
            pass

    # ---------------------------------------------------------------- report
    def reports_dir(self):
        d = os.path.join(here(), "reports")
        return d if os.path.isdir(d) else os.path.expanduser("~\\Downloads")

    def pick_report(self):
        p = filedialog.askopenfilename(title="Open a RENDER report", initialdir=self.reports_dir(),
                                       filetypes=[("RENDER report", "*.html"), ("All files", "*.*")])
        if p:
            self.report.set(os.path.normpath(p))

    def combine(self):
        ps = filedialog.askopenfilenames(title="Choose two or more RENDER reports to combine",
                                         initialdir=self.reports_dir(),
                                         filetypes=[("RENDER report", "*.html"), ("All files", "*.*")])
        if not ps:
            return
        if len(ps) == 1:
            self.report.set(os.path.normpath(ps[0]))
            return
        import combine_reports
        out_dir = os.path.join(here(), "reports")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "combined-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".html")
        specs = [os.path.splitext(os.path.basename(p))[0] + "=" + p for p in ps]
        try:
            n = combine_reports.combine(specs, out, say=lambda m: self.write(m + "\n", "info"))
        except Exception as e:
            messagebox.showerror(APP, f"Couldn't combine those reports:\n{e}")
            return
        self.write(f"Combined {len(ps)} reports: {n} services with an app or internet channel.\n", "info")
        self.report.set(out)

    def describe_report(self):
        p = self.report.get().strip().strip('"')
        if not p:
            self.info_text.set("Open a RabbitEars RENDER report (.html), or combine several.")
            return
        if not os.path.isfile(p):
            self.info_text.set("File not found.")
            return
        try:
            apps = ba_probe.apps_from_report(p)
            live = ba_probe.live_channels_from_report(p)
            slt = ba_probe.slt_from_report(p)
            self.info_text.set(f"{len(apps)} apps, {len(live)} internet channels"
                               + ("" if slt else "  (no service list in this report)"))
        except Exception as e:
            self.info_text.set(f"Couldn't read that report: {e}")

    # ---------------------------------------------------------------- ffmpeg / clock
    def pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="ffmpeg with the AC-4 decoder",
                                       filetypes=[("ffmpeg", "*.exe"), ("All files", "*.*")])
        if p:
            self.ffmpeg.set(os.path.normpath(p))

    def ffmpeg_changed(self):
        """Re-check a moment after the path stops changing (typing or pasting)."""
        if self._ff_after:
            self.root.after_cancel(self._ff_after)
        self._ff_after = self.root.after(800, self.check_ffmpeg)

    def check_ffmpeg(self):
        self._ff_after = None
        p = self.ffmpeg.get().strip().strip('"')
        self.ac4_text.set("Checking...")

        def work():
            found = ba_probe.find_ac4_ffmpeg(p or None)
            if found:
                msg, col = ("AC-4 decoder found" + ("" if p else f": {found}"), "#2e7d32")
                if not p:
                    def fill():
                        self.ffmpeg.set(found)
                        if self._ff_after:                 # filling the box isn't a change to re-check
                            self.root.after_cancel(self._ff_after)
                            self._ff_after = None
                    self.root.after(0, fill)
            elif p:
                msg, col = ("This ffmpeg has no AC-4 decoder (or won't run). Internet channels will have no sound.", "#c62828")
            else:
                msg, col = ("No AC-4 ffmpeg set. Internet channels will play without sound.", "#b26a00")
            self.root.after(0, lambda: (self.ac4_text.set(msg), self.ac4_label.configure(foreground=col)))
        threading.Thread(target=work, daemon=True).start()

    def check_clock(self):
        off = clock_offset()
        if off is None:
            msg, col = "Clock: couldn't check (no internet?)", "#b26a00"
        elif abs(off) <= 3:
            msg, col = "Clock: OK", "#2e7d32"
        else:
            msg = (f"Clock: {abs(off):.0f} s {'slow' if off > 0 else 'fast'}. Live channels correct for it, "
                   "but it's worth fixing: Settings > Time > Sync now")
            col = "#b26a00"
        self.root.after(0, lambda: (self.clock_text.set(msg), self.clock_label.configure(foreground=col)))

    # ---------------------------------------------------------------- run
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        rep = self.report.get().strip().strip('"')
        if not os.path.isfile(rep):
            messagebox.showwarning(APP, "Open a report first.")
            return
        port = self.port.get().strip()
        if not port.isdigit():
            messagebox.showwarning(APP, "The port must be a number.")
            return
        self.save()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--serve"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--serve"]
        cmd += [rep, "--port", port]
        ff = self.ffmpeg.get().strip().strip('"')
        if ff:
            cmd += ["--ffmpeg-ac4", ff]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.write(f"\n== Starting on port {port}: {os.path.basename(rep)}\n", "info")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         env=env, creationflags=flags, cwd=here())
        except OSError as e:
            messagebox.showerror(APP, f"Couldn't start the probe:\n{e}")
            return
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status.set(f"Running on http://localhost:{port}/")

    def reader(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            self.q.put(raw.decode("utf-8", "replace"))
        proc.wait()
        self.q.put(None)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def stopped(self):
        code = self.proc.returncode if self.proc else 0
        self.write(f"== Stopped{'' if code in (0, 1, None) or code < 0 else f' (exit code {code})'}\n", "info")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status.set("Stopped")

    def pump(self):
        try:
            while True:
                line = self.q.get_nowait()
                if line is None:
                    self.stopped()
                    continue
                tag = "err" if ("Traceback" in line or "Error" in line or "in use" in line) else \
                      "warn" if (" !! " in line or "Note:" in line) else None
                self.write(line, tag)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def write(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        n = int(self.log.index("end-1c").split(".")[0])
        if n > 5000:
            self.log.delete("1.0", f"{n - 5000}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------------- buttons
    def open_page(self, path=""):
        webbrowser.open(f"http://localhost:{self.port.get().strip()}/{path}")

    def open_folder(self, d):
        if os.path.isdir(d):
            os.startfile(d) if hasattr(os, "startfile") else webbrowser.open("file://" + d)
        else:
            messagebox.showinfo(APP, f"Folder not found:\n{d}")

    def open_extension(self):
        d = os.path.join(here(), "app-scaler")
        messagebox.showinfo(APP + " - app scaler extension",
                            "The app scaler fits the 1920x1080 TV apps to your browser window and adds a volume control.\n\n"
                            "To install it in Chrome or Edge:\n"
                            "1. Open chrome://extensions (or edge://extensions).\n"
                            "2. Turn on Developer mode.\n"
                            "3. Click 'Load unpacked' and pick this folder:\n\n"
                            f"{d}\n\n"
                            "4. Click 'Details' on the extension and set Site access to 'On all sites'. "
                            "Otherwise you have to click the extension's icon every time an app or channel opens. "
                            "It only acts on pages opened from BA Player.\n\n"
                            "In an app: Alt+Z turns scaling off/on, Alt+M mutes, Alt+Up/Down sets the volume.")

    def open_msys2(self):
        """Open an MSYS2 UCRT64 terminal, if MSYS2 is installed in one of the usual places."""
        roots = [os.environ.get("MSYS2_ROOT", "")] + [f"{d}:\\msys64" for d in "CDEFG"]
        for r in roots:
            if not r:
                continue
            exe, cmd = os.path.join(r, "ucrt64.exe"), os.path.join(r, "msys2_shell.cmd")
            try:
                if os.path.isfile(exe):
                    subprocess.Popen([exe], cwd=r)
                    return
                if os.path.isfile(cmd):
                    subprocess.Popen(["cmd", "/c", cmd, "-ucrt64"], cwd=r, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    return
            except OSError as e:
                messagebox.showerror(APP, f"Couldn't open MSYS2:\n{e}")
                return
        if messagebox.askyesno(APP, "MSYS2 wasn't found (looked for C:\\msys64 and the other drives).\n\n"
                                    "Open msys2.org to download it?"):
            webbrowser.open(MSYS2_URL)

    def get_reports(self):
        webbrowser.open(REPORTS_URL)
        messagebox.showinfo(APP + " - getting reports", REPORTS_HELP)

    def get_ffmpeg(self):
        w = tk.Toplevel(self.root)
        w.title(APP + " - getting ffmpeg-ac4")
        w.geometry("860x560")
        t = tk.Text(w, wrap="word", font=("Consolas", 10), padx=10, pady=10)
        t.insert("1.0", FFMPEG_HELP)
        t.configure(state="disabled")                      # read-only, but text can still be selected and copied
        t.pack(fill="both", expand=True)
        b = ttk.Frame(w, padding=8)
        b.pack(fill="x")
        ttk.Button(b, text="Open MSYS2 UCRT64", command=self.open_msys2).pack(side="left")
        ttk.Button(b, text="Get MSYS2", command=lambda: webbrowser.open(MSYS2_URL)).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Open the GitHub page", command=lambda: webbrowser.open(FFMPEG_URL)).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Close", command=w.destroy).pack(side="right")

    def open_reports(self):
        self.open_folder(os.path.join(here(), "reports"))

    def quit(self):
        self.save()
        self.stop()
        self.root.destroy()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--serve":
        sys.argv = [sys.argv[0]] + args[1:]
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
        return ba_probe_win.main()
    report = args[0] if args and os.path.isfile(args[0]) else None
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    for d in (getattr(sys, "_MEIPASS", ""), here()):
        ico = os.path.join(d, "ba_player.ico")
        if d and os.path.isfile(ico):
            try:
                root.iconbitmap(default=ico)              # title bar and taskbar, and the help windows too
            except tk.TclError:
                pass
            break
    Gui(root, report)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
