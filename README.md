# BA Player

By Brian Scott. Free software under the GNU GPL v3 or later (see [License](#license)).

Try ATSC 3.0 (NextGen TV) broadcaster apps and internet-only channels on a Windows PC,
from a RabbitEars RENDER report. No tuner needed: the report holds each station's app
links and service list, and the apps themselves load from their vendors' servers.

BA Player plays the part of the TV. It serves a page listing the report's apps and
channels, and answers the apps' requests to the TV (ATSC A/344) over a local WebSocket.

## Folder

| | |
|---|---|
| `build.bat` | builds `BA Player.exe` and `BA Player CLI.exe` here (needs Python for Windows) |
| `src\ba_player.py` | the window (`BA Player.exe`) |
| `src\ba_player_cli.py` | the console version (`BA Player CLI.exe`) |
| `src\ba_probe.py` | the receiver itself (same code as the Raspberry Pi version) |
| `src\combine_reports.py` | merges reports from several markets into one |
| `app-scaler\` | browser extension: fits 1920x1080 apps to the window, adds volume |
| `ba_player.ico` | the icon, built into both .exe files |
| `LICENSE.txt` | the GNU General Public License v3 |
| `THIRD-PARTY-NOTICES.md` | licenses of the parts BA Player uses |
| `package.bat` | packs the .exe files, docs and source into `BA_Player.zip` for sharing |
| `reports\` | reports to open; `sample.html` is a sample (El Paso: 3 apps, T2 and PBTV) |

## What you need

- Windows 10 or 11 and Chrome or Edge. Nothing else has to be installed or turned on: the
  WebSocket the apps talk to is built into BA Player, and browsers support WebSockets
  out of the box. BA Player only listens on this PC (localhost), so Windows Firewall
  doesn't need a rule for it.
- An internet connection (the apps and streams come from the broadcasters' servers).
- Optionally, an ffmpeg with the AC-4 decoder for sound on internet channels (below).
- Windows may warn "Windows protected your PC" the first time you run a .exe you built
  yourself (it isn't signed). Click **More info**, then **Run anyway**.

## Build

1. Install Python 3.9 or newer for Windows from python.org and tick
   **Add python.exe to PATH**. (MSYS2's python won't do; PyInstaller needs the Windows one.)
2. Run `build.bat`. It installs PyInstaller and writes `BA Player.exe` and
   `BA Player CLI.exe` into this folder.

## Share

Run `package.bat` after `build.bat`. It makes `BA_Player.zip` with the two .exe files, the
documents, the source, the extension and the sample report, and leaves out your own
settings (`ba_player.json`), logs and the `build` folder.

## Run

**`BA Player.exe`** opens a window:

- **Open report...** picks a RENDER report; **Combine reports...** picks several and merges
  them (saved in `reports\`). The line under it shows how many apps and internet channels
  the report has. **Get reports** opens RabbitEars and explains how to download one.
- **ffmpeg (AC-4)**: the ffmpeg with the AC-4 decoder, for sound on internet channels. The
  line under it says whether that ffmpeg really has the decoder; it's checked whenever the
  path changes. Left empty, BA Player looks for `ffmpeg-ac4.exe` next to itself.
  **Get ffmpeg-ac4** shows where to get it and how to build it.
- **Clock** shows whether the PC's clock is right. Internet channels need it within a few
  seconds (see below).
- **Start** runs BA Player and opens `http://localhost:8999/`; **Stop** stops it. The window
  shows every request the apps make. **Browse Extension** explains how to install the app
  scaler.
- Settings are remembered (`ba_player.json` next to the .exe).
- Dropping a report onto `BA Player.exe` opens it and starts at once.

**`BA Player CLI.exe`** is the same without a window:
`"BA Player CLI.exe" report.html --ffmpeg-ac4 "C:\path\to\your\ffmpeg.exe" --port 8999`.
Ctrl-C stops it. `--help` lists every option.

## Getting reports

1. Open https://www.rabbitears.info/market.php (or click **Get reports**).
2. In the search box at the top of the page, enter the call sign of a local station
   (for example KFOX) and open its page.
3. Glance down the page for a green **>** arrow. Just to the right of it is the **RE** icon.
4. Right-click the RE icon and choose **Save link as...** to save the report (.html).
5. In BA Player, **Open report...** (or **Combine reports...** for several markets).

**Use the newest report you can find.** Apps load from their own servers, so most still
work from an old report, but internet-only channels stop working once a report gets old:
their playlist is the over-the-air copy saved in the report, and when the station changes
its stream setup the old file names return "404 Not Found".

To combine from the command line:

```
python src\combine_reports.py -o reports\mine.html "DC=dc.html" "Las Vegas=lv.html"
```

Each service gets its market's label, and apps still get their own market's service list.
`--skip "DC:3"` leaves out service 3 from the DC report; `--skip "Las Vegas:5007:stream"`
keeps that service's app but leaves out its internet stream; `--no-encrypted-label` drops
"Encrypted" from the names.

## Sound on internet-only channels (AC-4)

Internet-only channels (T2, PBTV and the like) carry Dolby AC-4 audio, which browsers
can't play. BA Player converts it with an ffmpeg that has an AC-4 decoder. Upstream FFmpeg
has none; build one from https://github.com/ferrellsl/ffmpeg-ac4 (FFmpeg with an unofficial
AC-4 decoder, no ready-made downloads). With MSYS2:

1. Install MSYS2 from https://www.msys2.org and open an **MSYS2 UCRT64** terminal (the
   **Get ffmpeg-ac4** window has buttons for both).
2. `pacman -S --needed git make mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-pkgconf`
3. `git clone https://github.com/ferrellsl/ffmpeg-ac4.git` then `cd ffmpeg-ac4`
4. `./configure --disable-doc --disable-ffplay --disable-ffprobe --disable-autodetect --enable-static --disable-shared --extra-ldflags=-static`
5. `make -j$(nproc)`
6. `./ffmpeg -hide_banner -decoders | grep ac4` should print a line with `ac4`.
7. `ffmpeg.exe` is in the folder you built it in. With the steps above that's
   `C:\msys64\home\<your user name>\ffmpeg-ac4`, or wherever you cloned it or moved it to.
   In BA Player, point **ffmpeg (AC-4)** at it with **Browse...** (it can stay where it is),
   or copy it next to `BA Player.exe` as `ffmpeg-ac4.exe` and leave the box empty.

If the static build fails, drop the last three configure options, build again, and keep
`ffmpeg.exe` together with the DLLs it needs from `C:\msys64\ucrt64\bin`.

Without it, internet channels play video only.

## The PC's clock must be right

Internet channels keep only about a minute of video on their servers, and BA Player works
out the newest piece from the PC's clock. A clock that's off by more than a few seconds
asks for pieces that don't exist (yet, or any more), and every request comes back
"404 Not Found". The window's **Clock** line warns about this. Fix it with
**Settings > Time & language > Date & time > Sync now**, or in an Administrator PowerShell:

```
sc.exe config w32time start= auto
net start w32time
w32tm /resync /force
```

## App scaler extension (Chrome / Edge)

Broadcaster apps are built for a 1920x1080 TV. To fit them to the window: open
`chrome://extensions` (or `edge://extensions`), turn on **Developer mode**, click
**Load unpacked** and pick the `app-scaler` folder. Then click **Details** on the
extension and set **Site access** to **On all sites**; otherwise you have to click the
extension's icon every time an app or live channel opens. It acts only on pages opened
from BA Player (their address carries `?wsURL=`), so it leaves every other site alone. **Alt+Z** turns scaling off and on, **Alt+M** mutes, **Alt+Up/Down** sets volume.

Use an app with the keyboard: the arrow keys, **Enter** (OK) and **Esc** (Back). Almost
none of the apps respond to the mouse; only the live channels' players use it.

## What to expect

- **Some apps take a moment to load.** Give each one several seconds.
- **Gameloop is very finicky.** At the very least, press **Esc** and then **Space** a few
  times. If it doesn't start, close its tab and launch it again; it can take several tries.
- **Some stations show nothing.** Their app vendor has no app set up for that station, so
  the app falls back to a placeholder or error page. That happens on the vendor's server,
  and BA Player can't fix it. 
- **Turn off ad blocking for the app sites.** Some apps (Gray's Local News Live, for
  example) deliver their streams through Google's ad-insertion service, so an ad blocker
  or Edge's Strict tracking prevention blocks the video, not just the ads.
- **Apps localize by your internet connection.** Weather, alerts and times are for where
  you are and your PC's time zone, not the station's market.
- **On screens larger than 1080p,** some apps' on-demand video spills past their frame
  when scaled. Full screen (F11) helps; Alt+Z turns scaling off for that app.
- Keep the page on `localhost`. The apps are https pages and browsers only let them reach
  BA Player's `ws://` receiver at `localhost`.

## Logs

Every request an app makes, and BA Player's answer, shows in the window and at
`http://localhost:8999/log`, and is saved to `ba_player.log.jsonl` next to the .exe.

## License

BA Player is Copyright (C) 2026 Brian Scott.

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See `LICENSE.txt` for the full license, and `THIRD-PARTY-NOTICES.md` for the software
BA Player uses (Python, Tcl/Tk, PyInstaller's bootloader, dash.js). ffmpeg-ac4 isn't
included: each user supplies their own.

If you share the .exe files, share the source with them (this folder, minus `build` and
the .exe files), as the GPL requires.
