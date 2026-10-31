#!/usr/bin/env python3
# BA Player - ATSC 3.0 broadcaster apps on Windows
# Copyright (C) 2026 Brian Scott
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software under the GNU GPL v3 or later; see LICENSE.txt.
"""
ba_probe.py - ATSC 3.0 broadcaster-application probe (stdlib only)

Pretends to be a NEXTGEN TV receiver just enough to launch the broadcaster
apps (A/344 "interactive content") found in a RENDER report, and logs every
request each app makes, so you can see what it needs from the receiver.

  python3 ba_probe.py rf15-bsid2828-....html            # serve on port 8989
  python3 ba_probe.py report.html --port 8989 --strict  # unknown methods -> error

  python3 ba_probe.py                                   # only the over-the-air channels

Also serves the report's TV guide page (/guide), with Open app / Watch buttons,
and live over-the-air channels from the HDHomeRun (/air/<channel>/watch; see
"Over-the-air channels" below and in the README).

Open http://localhost:8989/ in a browser (through an SSH tunnel when the
probe runs on the Pi - see README), click an app, and watch the console or
http://localhost:8989/log.

How an app is launched (A/344): the receiver loads the app's entry page with
  ?wsURL=<receiver WebSocket base>
and the app opens <wsURL>/atscCmd and talks JSON-RPC 2.0 (org.atsc.* methods).
Answers here are placeholders: enough to see how far an app gets, not a
complete receiver. Video/audio/caption sockets (atscVid/Aud/Cap) are accepted
and logged but carry no media.

Live channels: services whose MPD points at an internet BaseURL (e.g. KFOX's
T2 and PBTV, "Internet Required") get a Watch page. The probe builds a live
MPD from the captured one on every request (segment times are wall-clock
based), proxies the segments through the Pi, and plays them with dash.js.
Browsers can't decode AC-4 audio. If an AC-4-capable ffmpeg is installed
(tools/build_ffmpeg_ac4.sh builds /usr/local/bin/ffmpeg-ac4), the probe
transcodes each audio segment to stereo FLAC, which Chrome/Edge/Firefox play;
otherwise the audio is left out and you get video and captions only.

Licensed GPLv3 like the rest of hdhr-render-pi.
"""
import argparse, base64, collections, datetime, hashlib, html, json, os, re, signal, socket, struct
import shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# On Windows, run ffmpeg without a console window: a windowed program (BA Player) would
# otherwise get a new console flashing up for every segment converted.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Branding for the probe page; the Windows build (BA Player) sets these before main().
BRAND = "Broadcaster App Probe"
PAGE_NOTE = ""            # extra html shown under the page heading
PAGE_FOOTER = None        # replaces the SSH-tunnel note at the bottom when set
HIDE_ENCRYPTED = False    # drop "Encrypted" from service names
VIA_TEXT = " via the Pi"  # after the stream's address in a live channel's status line
NO_CAPTIONS = False       # leave captions out of internet channels (no CC in the player)
FAVICON = None            # .ico bytes served at /favicon.ico (the browser tab's icon)
HOST_NAME = "the Pi"      # who fetches and converts the internet channels, in the page's wording
NO_AC4_HINT = "Run <code>tools/build_ffmpeg_ac4.sh</code> once to add AC-4 audio."
DEVICE_MAKE = "hdhr-render-pi"   # what apps are told the TV is (org.atsc.query.deviceInfo)
PROBE_WORD = "this probe"        # how the page refers to the receiver


def _clean_title(t):
    return re.sub(r"\s*\bEncrypted\b", "", t).strip() if HIDE_ENCRYPTED else t

# --------------------------------------------------------------------------
# Apps from a RENDER report
# --------------------------------------------------------------------------
def apps_from_report(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    parts = re.split(r'<h2 id="service_(\d+)">', s)
    apps = []
    for i in range(1, len(parts) - 1, 2):
        sid, body = parts[i], parts[i + 1]
        title = re.match(r"(.*?)</h2>", body, re.S)
        title = html.unescape(re.sub(r"<[^>]+>", " ", title.group(1))) if title else f"Service {sid}"
        title = _clean_title(re.sub(r"\s+", " ", title).strip())
        text = html.unescape(body)
        gsid = re.search(r"Global Service ID:\s*(?:</strong>)?\s*([^<\s]+)", text)
        seen = set()
        for pkg in re.finditer(r"<HTMLEntryPackage\b([^>]*)>", text):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', pkg.group(1)))
            key = (attrs.get("appContextId"), attrs.get("bbandEntryPageUrl"), attrs.get("bcastEntryPageUrl"))
            if key in seen:
                continue
            seen.add(key)
            apps.append({
                "service_id": sid,
                "title": title,
                "global_service_id": gsid.group(1) if gsid else f"service-{sid}",
                "app_context_id": attrs.get("appContextId", ""),
                "bband_url": attrs.get("bbandEntryPageUrl", ""),
                "bcast_url": attrs.get("bcastEntryPageUrl", ""),
                "slt": None,
            })
        if apps and apps[-1]["service_id"] == sid and "Service List Table (SLT)" in body:
            slt = slt_from_text(body)           # combined reports carry each market's SLT per service
            for a in apps:
                if a["service_id"] == sid:
                    a["slt"] = slt
    return apps

def slt_from_report(path):
    """The station's SLT as RENDER saved it ("Service List Table (SLT) Instance 1 - Raw XML"),
    for answering apps' org.atsc.query.signaling. -> {"xml", "version"} or None."""
    try:
        return slt_from_text(open(path, encoding="utf-8", errors="replace").read())
    except OSError:
        return None

def slt_from_text(s):
    for pre in re.finditer(r"Service List Table \(SLT\)(?:(?!</summary>).)*Raw XML</summary>\s*<div[^>]*>\s*<pre>(.*?)</pre>", s, re.S):
        xml = html.unescape(re.sub(r"<[^>]+>", "", pre.group(1)))
        m = re.search(r"(?:<\?xml[^>]*\?>\s*)?<SLT\b.*?</SLT>", xml, re.S)
        if m:
            x = m.group(0).strip()
            return {"xml": x, "version": zlib.crc32(x.encode()) & 0xFF}
    m = re.search(r"&lt;SLT\b.*?&lt;/SLT&gt;", s, re.S)          # older layouts: anywhere in the page
    if m:
        x = html.unescape(re.sub(r"<[^>]+>", "", m.group(0))).strip()
        return {"xml": x, "version": zlib.crc32(x.encode()) & 0xFF}
    return None

# --------------------------------------------------------------------------
# Live (broadband-delivered) channels
# --------------------------------------------------------------------------
LIVE_LAG_S = 6.0   # stay this far behind "now" so segments exist on the server

def live_channels_from_report(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    parts = re.split(r'<h2 id="service_(\d+)">', s)
    chans = []
    for i in range(1, len(parts) - 1, 2):
        sid, body = parts[i], parts[i + 1]
        title = re.match(r"(.*?)</h2>", body, re.S)
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", title.group(1)))).strip() if title else sid
        title = _clean_title(title)
        mpds = [html.unescape(m) for m in re.findall(r"<pre>(.*?)</pre>", body, re.S)
                if "<MPD" in html.unescape(m)[:600]]
        for mpd in reversed(mpds):          # newest copy first
            base = re.search(r"<BaseURL>\s*(https?://[^<\s]+)\s*</BaseURL>", mpd)
            if base and "<SegmentTimeline>" in mpd:
                chans.append({"service_id": sid, "title": title, "mpd": mpd, "base_url": base.group(1),
                              "ac4": ac4_sets(mpd)})
                break
    return chans

def _drop_ac4(mpd):
    out, pos = [], 0
    for m in re.finditer(r"<AdaptationSet\b.*?</AdaptationSet>", mpd, re.S):
        out.append(mpd[pos:m.start()])
        if 'codecs="ac-4' not in m.group(0):
            out.append(m.group(0))
        pos = m.end()
    out.append(mpd[pos:])
    return "".join(out)

# --------------------------------------------------------------------------
# AC-4 -> FLAC (needs an ffmpeg built with the AC-4 decoder)
# --------------------------------------------------------------------------
FLAC_PREFIX = "flac~"          # segment names starting with this are transcoded
FLAC_BANDWIDTH = 900000

def _tmpl_regex(tmpl):
    r = re.escape(tmpl)
    r = r.replace(re.escape("$RepresentationID$"), r"(?P<rid>[^/]+?)")
    r = r.replace(re.escape("$Time$"), r"(?P<t>\d+)").replace(re.escape("$Number$"), r"(?P<n>\d+)")
    return re.compile("^" + r + "$")

def ac4_sets(mpd):
    out = []
    for a in re.findall(r"<AdaptationSet\b.*?</AdaptationSet>", mpd, re.S):
        if 'codecs="ac-4' not in a:
            continue
        tm = re.search(r"<SegmentTemplate\b[^>]*>", a)
        if not tm:
            continue
        attr = dict(re.findall(r'(\w+)="([^"]*)"', tm.group(0)))
        d = re.search(r'<S\b[^>]*\bd="(\d+)"', a)
        if "media" not in attr or "initialization" not in attr:
            continue
        out.append({"init": attr["initialization"], "media": attr["media"],
                    "init_re": _tmpl_regex(attr["initialization"]), "media_re": _tmpl_regex(attr["media"]),
                    "timescale": int(attr.get("timescale", "1")),
                    "d": int(d.group(1)) if d else int(attr.get("duration", "0") or 0),
                    "start_number": int(attr.get("startNumber", "1") or 1)})
    return out

def _flac_mpd(mpd):
    """Point AC-4 adaptation sets at transcoded FLAC segments."""
    def fix(m):
        a = m.group(0)
        if 'codecs="ac-4' not in a:
            return a
        a = re.sub(r'codecs="ac-4[^"]*"', 'codecs="flac"', a)
        a = re.sub(r'(initialization|media)="', lambda x: x.group(0) + FLAC_PREFIX, a)
        a = re.sub(r'(<AudioChannelConfiguration\b[^>]*value=")[^"]*"', r'\g<1>2"', a)
        a = re.sub(r'(<Representation\b[^>]*bandwidth=")\d+"', rf'\g<1>{FLAC_BANDWIDTH}"', a)
        return a
    return re.sub(r"<AdaptationSet\b.*?</AdaptationSet>", fix, mpd, flags=re.S)

def app_dirs():
    """Folders that may hold ba_probe's own files: next to the script, next to a built
    .exe (and PyInstaller's unpack folder), and the Pi's installed share folder."""
    out = [os.path.dirname(os.path.realpath(__file__))]
    if getattr(sys, "frozen", False):
        out.insert(0, os.path.dirname(os.path.realpath(sys.executable)))
        if getattr(sys, "_MEIPASS", None):
            out.append(sys._MEIPASS)
    return out

def find_ac4_ffmpeg(explicit=None):
    local = [os.path.join(d, n) for d in app_dirs() for n in ("ffmpeg-ac4.exe", "ffmpeg-ac4")]
    cands = [explicit] if explicit else local + [shutil.which("ffmpeg-ac4"), "/usr/local/bin/ffmpeg-ac4", shutil.which("ffmpeg")]
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            out = subprocess.run([c, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=10,
                                 creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if re.search(r"^\s*A\S*\s+ac4\b", out, re.M):
            return c
    return None

def _boxes(d, o=0, end=None):
    end = len(d) if end is None else end
    while o + 8 <= end:
        size, typ = struct.unpack(">I4s", d[o:o + 8])
        if size == 1:
            size = struct.unpack(">Q", d[o + 8:o + 16])[0]
        elif size == 0:
            size = end - o
        if size < 8:
            return
        yield typ, o, size
        o += size

def _wav(pcm, rate, ch=2):
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * ch * 2, ch * 2, 16) +
            b"data" + struct.pack("<I", len(pcm)) + pcm)

def _wav_rate(d):
    i = d.find(b"fmt ")
    return struct.unpack("<I", d[i + 12:i + 16])[0] if i >= 0 and len(d) >= i + 16 else 0

def _wav_pcm(d):
    i = d.find(b"data")
    return d[i + 8:] if i >= 0 else b""

def prev_segment_name(a, m, k):
    """Name of the segment k before the one matched by m (AC-4 set a)."""
    g = m.groupdict()
    name = a["media"].replace("$RepresentationID$", g.get("rid") or "")
    if g.get("n"):
        return name.replace("$Number$", str(int(g["n"]) - k))
    if g.get("t") and a["d"]:
        return name.replace("$Time$", str(int(g["t"]) - k * a["d"]))
    return None

def media_start_s(init, media):
    """Presentation time (s) of an fMP4 media segment: tfdt / the track's mdhd timescale."""
    i = init.find(b"mdhd")
    if i < 0:
        return 0.0
    v = init[i + 4]
    ts = struct.unpack(">I", init[i + 16:i + 20] if v == 0 else init[i + 24:i + 28])[0] or 1
    j = media.find(b"tfdt")
    if j < 0:
        return 0.0
    t = struct.unpack(">Q", media[j + 8:j + 16])[0] if media[j + 4] == 1 else struct.unpack(">I", media[j + 8:j + 12])[0]
    return t / ts

class Ac4Transcoder:
    """AC-4 fMP4 segment -> stereo FLAC fMP4 segment at the same presentation time.

    Two ffmpeg passes per segment. Pass 1 decodes and downmixes to 16-bit PCM.
    AC-4 coded at video frame rates (29.97 here) decodes to 1536 samples per
    frame, i.e. a 48 kHz * 1536/1601.6 stream, so pass 2 labels the PCM with the
    rate implied by its length and resamples it to exactly 48 kHz before FLAC.
    Every FLAC segment stands alone (no encoder priming), so they join cleanly."""
    FMP4 = ["-c:a", "flac", "-strict", "-2", "-sample_fmt", "s16", "-ar", "48000", "-ac", "2",
            "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "pipe:1"]

    def __init__(self, ffmpeg):
        self.ffmpeg = ffmpeg
        self.sem = threading.Semaphore(2)
        self.lock = threading.Lock()
        self.cache = {}          # url -> bytes (inits and finished segments)
        self.order = []
        self._init = None
        self.stats = {"ok": 0, "fail": 0, "last_ms": 0, "last_error": ""}

    def _run(self, args, data, timeout=30):
        p = subprocess.run([self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error"] + args, creationflags=NO_WINDOW,
                           input=data, capture_output=True, timeout=timeout)
        if p.returncode != 0 or not p.stdout:
            raise RuntimeError("ffmpeg: " + (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["failed"])[-1])
        return p.stdout

    def _encode(self, wav):
        return self._run(["-f", "wav", "-i", "pipe:0", "-af", "aresample=48000"] + self.FMP4, wav)

    def init_segment(self):
        if self._init is None:
            out = self._encode(_wav(b"\0" * 4 * 4800, 48000))      # 0.1 s of silence
            self._init = b"".join(out[o:o + n] for t, o, n in _boxes(out) if t in (b"ftyp", b"moov"))
        return self._init

    def remember(self, key, val):
        with self.lock:
            self.cache[key] = val
            self.order.append(key)
            while len(self.order) > 40:
                self.cache.pop(self.order.pop(0), None)

    def segment(self, init, media, start_s, dur_s, prev=()):
        """prev: earlier media segments (oldest first). The AC-4 decoder outputs
        nothing until it meets an I-frame, and not every segment starts with
        one (KVIA's often don't), so a segment can be decoded together with the
        ones before it; only the samples belonging to this segment are kept."""
        t0 = time.time()
        with self.sem:
            wav = self._run(["-f", "mp4", "-i", "pipe:0", "-vn", "-ac", "2",
                             "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"], init + b"".join(prev) + media)
            pcm, wav_rate = _wav_pcm(wav), _wav_rate(wav)
            n = len(pcm) // 4
            want = int(round(dur_s * wav_rate)) if dur_s > 0 and wav_rate else 0
            if prev and want:
                pcm = pcm[-want * 4:] if n >= want else b"\0" * ((want - n) * 4) + pcm
                n = want
            if not n or (want and n < want * 0.9):
                raise NoAudio(f"decoded {n} of {want} samples")
            rate = 48000
            if dur_s > 0 and abs(n / dur_s - 48000) > 100:
                rate = int(round(n / dur_s))
            out = self._encode(_wav(pcm, rate))
        self.stats["last_ms"] = int((time.time() - t0) * 1000)
        return self._place(out, start_s)

    def silence(self, start_s, dur_s):
        """A silent segment of the right length, so playback carries on when a
        segment can't be decoded (or never arrived)."""
        n = max(1, int(round(dur_s * 48000)))
        return self._place(self._encode(_wav(b"\0" * 4 * n, 48000)), start_s)

    @staticmethod
    def _place(out, start_s):
        frags = []
        tfdt = int(round(start_s * 48000))
        for t, o, size in _boxes(out):
            if t == b"moof":
                moof = bytearray(out[o:o + size])
                for t2, o2, s2 in _boxes(moof, 8):
                    if t2 == b"traf":
                        for t3, o3, s3 in _boxes(moof, o2 + 8, o2 + s2):
                            if t3 == b"tfdt" and moof[o3 + 8] == 1:
                                moof[o3 + 12:o3 + 20] = tfdt.to_bytes(8, "big")
                            elif t3 == b"tfdt":
                                raise RuntimeError("ffmpeg wrote a 32-bit tfdt")
                frags.append(bytes(moof))
            elif t == b"mdat":
                frags.append(out[o:o + size])
        return b"".join(frags)

class NoAudio(Exception):
    pass


def live_mpd(chan, now, seg_prefix, time_url, keep_ac4=False, segments=30, flac=False):
    """Rewrite the captured MPD into a current live MPD.
    The captured SegmentTimeline uses S@t = n * S@d ticks since 1970 (checked in
    the KFOX captures), so the newest available segment can be computed from the clock."""
    mpd = chan["mpd"] if keep_ac4 else _flac_mpd(chan["mpd"]) if flac else _drop_ac4(chan["mpd"])
    if NO_CAPTIONS:
        mpd = re.sub(r'<AdaptationSet\b[^>]*contentType="text".*?</AdaptationSet>\s*', "", mpd, flags=re.S)

    def fix_template(m):
        tmpl = m.group(0)
        ts = int(re.search(r'timescale="(\d+)"', tmpl).group(1))
        sm = re.search(r'<S\b[^>]*d="(\d+)"[^>]*/>', tmpl)
        if not sm:
            return tmpl
        d = int(sm.group(1))
        n_end = int(((now - LIVE_LAG_S) * ts) // d)
        start = (n_end - segments + 1) * d
        return re.sub(r"<SegmentTimeline>.*?</SegmentTimeline>",
                      f'<SegmentTimeline><S t="{start}" d="{d}" r="{segments - 1}"/></SegmentTimeline>',
                      tmpl, flags=re.S)

    mpd = re.sub(r"<SegmentTemplate\b.*?</SegmentTemplate>", fix_template, mpd, flags=re.S)
    mpd = re.sub(r"<BaseURL>[^<]*</BaseURL>", f"<BaseURL>{seg_prefix}</BaseURL>", mpd)
    pub = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mpd = re.sub(r'publishTime="[^"]*"', f'publishTime="{pub}"', mpd)
    mpd = re.sub(r'minimumUpdatePeriod="[^"]*"', 'minimumUpdatePeriod="PT2S"', mpd)
    mpd = re.sub(r'suggestedPresentationDelay="[^"]*"', "", mpd)
    mpd = mpd.replace("<MPD ", '<MPD suggestedPresentationDelay="PT8S" ', 1)
    if "<UTCTiming" not in mpd:
        mpd = mpd.replace("</MPD>", f'<UTCTiming schemeIdUri="urn:mpeg:dash:utc:http-iso:2014" value="{time_url}"/></MPD>')
    return mpd

def latest_segment_url(chan, now):
    """First video representation's init and newest media segment URLs (for --check-live)."""
    vset = re.search(r'<AdaptationSet\b[^>]*contentType="video".*?</AdaptationSet>', chan["mpd"], re.S).group(0)
    tmpl = re.search(r"<SegmentTemplate\b.*?</SegmentTemplate>", vset, re.S).group(0)
    ts = int(re.search(r'timescale="(\d+)"', tmpl).group(1))
    d = int(re.search(r'<S\b[^>]*d="(\d+)"', tmpl).group(1))
    rep_id = re.findall(r'<Representation\b[^>]*\bid="([^"]+)"', vset)[0]
    init = re.search(r'initialization="([^"]+)"', tmpl).group(1).replace("$RepresentationID$", rep_id)
    media = re.search(r'media="([^"]+)"', tmpl).group(1).replace("$RepresentationID$", rep_id)
    t = int(((now - LIVE_LAG_S) * ts) // d) * d
    return chan["base_url"] + init, chan["base_url"] + media.replace("$Time$", str(t))

ORIGIN_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) ATSC3 receiver (hdhr-render-pi ba_probe)"}

def fetch_origin(url, timeout=10):
    req = urllib.request.Request(url, headers=ORIGIN_HEADERS)
    return urllib.request.urlopen(req, timeout=timeout)

def channel_number(title):
    """'Service 5007: T2 (14.10) Internet Required' -> '14.10'"""
    m = re.search(r"\((\d+\.\d+)\)", title)
    return m.group(1) if m else None

def guide_for_report(path):
    base = path[:-5] if path.endswith(".html") else path
    return base + ".guide.html"

def launch_url(entry, ws_base):
    """Insert wsURL into the query, before any #fragment (ROXi's URL has one)."""
    base, sep, frag = entry.partition("#")
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}wsURL={urllib.parse.quote(ws_base, safe=':/')}{sep}{frag}"

# --------------------------------------------------------------------------
# Live mode: continuous whole-RF capture, ESG that builds up, broadcast
# services played from the capture with a rolling buffer
# --------------------------------------------------------------------------
LLS_ADDR = ("224.0.23.60", 4937)

def load_conf(path="/etc/hdhr-render.conf"):
    conf = {}
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                conf[m.group(1)] = m.group(2).strip('"')
    except OSError:
        pass
    return conf

def _slt_xml(payload):
    """SLT bytes from an LLS packet: plain (table_id 1) or inside an A/331
    signed multi-table (table_id 0xFE, as KFOX's RF 15 sends it)."""
    gz = None
    if len(payload) >= 6 and payload[0] == 1:
        gz = payload[4:]
    elif len(payload) >= 6 and payload[0] == 0xFE:
        n, o = payload[4], 5
        for _ in range(n):
            if o + 4 > len(payload):
                break
            pid, ln = payload[o], struct.unpack(">H", payload[o + 2:o + 4])[0]
            if pid == 1:
                gz = payload[o + 4:o + 4 + ln]
                break
            o += 4 + ln
    if not gz:
        return None
    try:
        return zlib.decompress(gz, 47)
    except zlib.error:
        return None


def alp_hdr_len(b, d=0):
    """Bytes of ALP header before the IPv4 packet at b[d:], or 0 if it isn't a whole IPv4 packet.
    The HDHomeRun writes one ALP packet per pcap record; packets over 2047 bytes (e.g. ESG
    units) carry a 3-byte header (header_mode=1), optionally with a sub-stream id / extension."""
    h0 = b[d]
    if h0 >> 5 != 0 or h0 & 0x10:          # not IPv4, or segmented/concatenated
        return 0
    n = 2
    if h0 & 0x08:                          # header_mode: one more byte of length + flags
        a = b[d + 2]
        n = 3 + (1 if a & 2 else 0)
        if a & 1:                          # header extension: type, length-1, data
            n += 2 + b[d + n + 1] + 1
    return n

def parse_slt(payload):
    import xml.etree.ElementTree as ET
    xml = _slt_xml(payload)
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    out = {}
    for svc in root.iter():
        if not svc.tag.endswith("Service"):
            continue
        a = svc.attrib
        sls = next((c for c in svc if c.tag.endswith("BroadcastSvcSignaling")), None)
        if sls is None:
            continue
        out[a.get("serviceId", "")] = {
            "num": f"{a.get('majorChannelNo', '')}.{a.get('minorChannelNo', '')}",
            "name": a.get("shortServiceName", ""), "category": a.get("serviceCategory", ""),
            "protected": a.get("protected", "false").lower() == "true",
            "ip": sls.attrib.get("slsDestinationIpAddress"),
            "port": int(sls.attrib.get("slsDestinationUdpPort", "0") or 0)}
    return out

def lct_header(p):
    """ROUTE/LCT packet -> (tsi, toi, start_offset, payload_offset, tol, close) or None"""
    if len(p) < 12 or p[0] >> 4 != 1:
        return None
    c = (p[0] >> 2) & 3
    s, o, h = p[1] >> 7, (p[1] >> 5) & 3, (p[1] >> 4) & 1
    close, hl = p[1] & 1, p[2] * 4
    off = 4 + 4 * (c + 1)
    nt, no = 4 * s + 2 * h, 4 * o + 2 * h
    tsi = int.from_bytes(p[off:off + nt], "big"); off += nt
    toi = int.from_bytes(p[off:off + no], "big"); off += no
    tol = None
    while off + 4 <= hl:
        het = p[off]
        if het >= 128:
            hel = 4
        else:
            hel = p[off + 1] * 4
            if not hel:
                break
        if het == 194:                                   # EXT_TOL, 24 bit
            tol = int.from_bytes(p[off + 1:off + 4], "big")
        elif het in (67, 64) and hel >= 8:               # EXT_TOL 48 bit / EXT_FTI
            tol = int.from_bytes(p[off + 2:off + 8], "big")
        off += hel
    if len(p) < hl + 4:
        return None
    return tsi, toi, struct.unpack(">I", p[hl:hl + 4])[0], hl + 4, tol, close

class ObjectStore:
    """Reassembles ROUTE objects (media, captions) for the services being kept
    and stores complete ones as files, for buffer_s seconds."""
    def __init__(self, folder, buffer_s):
        self.dir, self.buffer_s = folder, buffer_s
        self.partial = {}        # (ip, port, tsi, toi) -> [bytearray, offsets, got, tol, close_end, last_ts]
        self.files = {}          # (ip, port, tsi) -> {toi: (path, done_ts, size)}
        self.ips = set()         # destinations to keep (filled from the SLT)
        self.lock = threading.Lock()
        self.complete = self.incomplete = 0
        self.last_prune = 0
        os.makedirs(folder, exist_ok=True)

    def feed(self, ts, ip, port, p):
        if ip not in self.ips:
            return
        h = lct_header(p)
        if not h or h[0] == 0:          # TSI 0 = signaling: RENDER handles it
            return
        tsi, toi, so, po, tol, close = h
        key = (ip, port, tsi, toi)
        o = self.partial.get(key)
        if o is None:
            o = self.partial[key] = [bytearray(), set(), 0, None, None, ts]
        n = len(p) - po
        end = so + n
        if len(o[0]) < end:
            o[0].extend(b"\0" * (end - len(o[0])))
        if so not in o[1]:
            o[0][so:end] = p[po:]
            o[1].add(so)
            o[2] += n
        if tol:
            o[3] = tol
        if close:
            o[4] = end
        o[5] = ts
        total = o[3] or o[4]
        if total and o[2] >= total:
            self._done(key, bytes(o[0][:total]), ts)
            del self.partial[key]
        if ts - self.last_prune > 5:
            self.last_prune = ts
            self._prune(ts)

    def _done(self, key, data, ts):
        ip, port, tsi, toi = key
        d = os.path.join(self.dir, f"{ip}_{port}", str(tsi))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, str(toi))
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        with self.lock:
            self.files.setdefault((ip, port, tsi), {})[toi] = (path, ts, len(data))
        self.complete += 1

    def _prune(self, now):
        for key, o in list(self.partial.items()):
            if now - o[5] > 15:                 # never finished (packets lost)
                del self.partial[key]
                self.incomplete += 1
        with self.lock:
            for k, objs in self.files.items():
                newest = max((v[1] for v in objs.values()), default=now)
                for toi, (path, t, _) in list(objs.items()):
                    # keep inits (re-sent with the same TOI) as long as the flow lives
                    if now - t > self.buffer_s and not (now - newest < 30 and t == newest):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        del objs[toi]

    def get(self, ip, port, tsi, toi):
        with self.lock:
            v = self.files.get((ip, port, tsi), {}).get(toi)
        if not v:
            return None
        try:
            return open(v[0], "rb").read()
        except OSError:
            return None

    def span(self, ip, port, tsi):
        """(oldest, newest) TOI held for a flow, ignoring the init TOI"""
        with self.lock:
            tois = [t for t in self.files.get((ip, port, tsi), {}) if t > 1000]
        return (min(tois), max(tois)) if tois else (None, None)

def track_timescale(init):
    i = init.find(b"mdhd")
    if i < 0:
        return 1
    return struct.unpack(">I", init[i + 16:i + 20] if init[i + 4] == 0 else init[i + 24:i + 28])[0] or 1

def retime_segment(seg, delta, seqno=None):
    """Shift the tfdt of every fragment (and the sidx earliest time) of an fMP4
    segment by delta ticks, walking the box tree (no byte searching)."""
    b = bytearray(seg)
    for t, o, size in list(_boxes(b)):
        if t == b"sidx":
            n = 8 if b[o + 8] == 1 else 4
            q = o + 20
            b[q:q + n] = ((int.from_bytes(b[q:q + n], "big") + delta) % (1 << (8 * n))).to_bytes(n, "big")
        elif t == b"moof":
            for t2, o2, s2 in list(_boxes(b, o + 8, o + size)):
                if t2 == b"mfhd" and seqno is not None:
                    b[o2 + 12:o2 + 16] = (seqno & 0xFFFFFFFF).to_bytes(4, "big")
                elif t2 == b"traf":
                    for t3, o3, s3 in list(_boxes(b, o2 + 8, o2 + s2)):
                        if t3 == b"tfdt":
                            n = 8 if b[o3 + 8] == 1 else 4
                            q = o3 + 12
                            b[q:q + n] = ((int.from_bytes(b[q:q + n], "big") + delta) % (1 << (8 * n))).to_bytes(n, "big")
    return bytes(b)

def stsid_flows(stsid_xml):
    """S-TSID -> list of {tsi, template, init_name, init_toi, ip, port}"""
    flows = []
    for rs in re.finditer(r"<RS\b([^>]*)>(.*?)</RS>", stsid_xml, re.S):
        rattr = dict(re.findall(r'(\w+)="([^"]*)"', rs.group(1)))
        for ls in re.finditer(r'<LS\b[^>]*\btsi="(\d+)"[^>]*>(.*?)</LS>', rs.group(2), re.S):
            body = ls.group(2)
            tm = re.search(r'fileTemplate="([^"]+)"', body)
            files = re.findall(r'<(?:fdt:)?File\b[^>]*\bTOI="(\d+)"[^>]*Content-Location="([^"]+)"', body) + \
                    [(b, a) for a, b in re.findall(r'<(?:fdt:)?File\b[^>]*Content-Location="([^"]+)"[^>]*\bTOI="(\d+)"', body)]
            ct = re.search(r'contentType="([^"]+)"', body)
            flows.append({"tsi": int(ls.group(1)), "template": tm.group(1) if tm else "", "type": ct.group(1) if ct else "",
                          "init_toi": int(files[0][0]) if files else None,
                          "init_name": files[0][1] if files else "",
                          "ip": rattr.get("dIpAddr"), "port": int(rattr["dPort"]) if rattr.get("dPort") else None})
    return flows

def bcast_services_from_report(path):
    """Broadcast (over-the-air A/V) services in a RENDER report: sid -> {title, mpd, stsid}"""
    s = open(path, encoding="utf-8", errors="replace").read()
    parts = re.split(r'<h2 id="service_(\d+)">', s)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        sid, body = parts[i], parts[i + 1]
        title = re.match(r"(.*?)</h2>", body, re.S)
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", title.group(1)))).strip() if title else sid
        pres = [html.unescape(m) for m in re.findall(r"<pre>(.*?)</pre>", body, re.S)]
        mpd = next((x for x in reversed(pres) if "<MPD" in x[:800]), None)
        stsid = next((x for x in reversed(pres) if "<S-TSID" in x[:400]), None)
        if not mpd or not stsid or re.search(r"<BaseURL>\s*https?://", mpd):
            continue                      # no A/V, or internet-delivered
        out[sid] = {"title": title, "mpd": mpd, "stsid": stsid, "encrypted": "Encrypted" in title}
    return out

class GuideMerger:
    """Program guide built up over consecutive captures."""
    def __init__(self, path):
        self.path = path
        self.channels = {}      # num -> {"name", "logo", "events": {start: [..]}}
        self.pre = self.post = None
        self.lock = threading.Lock()
        try:
            for c in json.load(open(path)).get("channels", []):
                self.channels[c["num"]] = {"name": c["name"], "logo": c.get("logo"),
                                           "events": {e[0]: e for e in c["events"]}}
        except (OSError, ValueError, KeyError):
            pass

    def add_page(self, guide_html):
        s = open(guide_html, encoding="utf-8").read()
        m = re.search(r"const GUIDE = (.*?);\nconst WATCH", s, re.S)
        if not m:
            return 0
        g = json.loads(m.group(1))
        with self.lock:
            self.pre, self.post = s[:m.start(1)], s[m.end(1):]
            for c in g.get("channels", []):
                ch = self.channels.setdefault(c["num"], {"name": c["name"], "logo": c.get("logo"), "events": {}})
                for e in c["events"]:
                    old = ch["events"].get(e[0])
                    if not old or (e[2] and not old[2]) or (e[3] and not old[3] and e[2]):
                        ch["events"][e[0]] = e
            # forget programs that ended more than a day ago
            cutoff = time.time() - 86400
            for ch in self.channels.values():
                for st in [st for st, e in ch["events"].items() if st + e[1] < cutoff]:
                    del ch["events"][st]
            data = self._data()
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)
        return sum(len(c["events"]) for c in data["channels"])

    def _data(self):
        chans = [{"num": n, "name": c["name"], "logo": c["logo"],
                  "events": sorted(c["events"].values())} for n, c in self.channels.items()]
        def key(c):
            try:
                return [float(x) for x in c["num"].split(".")]
            except ValueError:
                return [9999]
        return {"title": "TV Guide (live)", "subtitle": "Built up from the continuous capture; "
                "times shown in this browser's time zone", "channels": sorted(chans, key=key)}

    def count(self):
        with self.lock:
            return sum(len(c["events"]) for c in self.channels.values())

    def page(self, watch):
        with self.lock:
            if not self.pre:
                return None
            data = json.dumps(self._data()).replace("<", "\\u003c")
            pre, post = self.pre, self.post
        inject = "<script>window.WATCH = " + json.dumps(watch).replace("<", "\\u003c") + ";</script>\n"
        return (pre + data + post).replace("<script>\nconst GUIDE", inject + "<script>\nconst GUIDE", 1)

class LiveCapture:
    """tuner -> back-to-back CHUNK_S pcaps (each through RENDER), and the
    broadcast services' objects into the ObjectStore."""
    def __init__(self, cfg, store, on_chunk):
        self.cfg, self.store, self.on_chunk = cfg, store, on_chunk
        self.dir = cfg["dir"]
        self.slt = {}
        self.state, self.error = "starting", ""
        self.bytes = 0
        self.started = time.time()
        self.reconnects = 0
        self.chunks = 0
        self.rate_bps = 0.0
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def url(self):
        c = self.cfg
        plps = "".join(f"p{p.strip()}" for p in c["plps"].split(",") if p.strip())
        return f"http://{c['hdhr']}:5004/tuner{c['tuner']}/ch{c['rf']}{plps}?format=alp-pcap"

    def _open_chunk(self, ts):
        name = f"rf{self.cfg['rf']}-live-{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}.pcap"
        path = os.path.join(self.dir, name)
        f = open(path, "wb")
        f.write(self.header)
        return path, f

    def _run(self):
        chunk_s = self.cfg["chunk_s"]
        cur = f = None
        cur_start = 0
        rate_t, rate_b = time.time(), 0
        while not self.stop:
            try:
                resp = urllib.request.urlopen(self.url(), timeout=20)
            except urllib.error.HTTPError as e:
                detail = e.headers.get("X-HDHomeRun-Error", "") if e.headers else ""
                self.state, self.error = "error", f"HDHomeRun: HTTP {e.code} {detail or e.reason}"
                log("Capture", "!!", self.error)
                time.sleep(10)
                continue
            except Exception as e:
                self.state, self.error = "error", f"can't reach the HDHomeRun: {e}"
                log("Capture", "!!", self.error)
                time.sleep(10)
                continue
            try:
                hdr = resp.read(24)
                if len(hdr) < 24:
                    raise EOFError("no data")
                if hdr[:4] in (b"\xa1\xb2\x3c\x4d", b"\xa1\xb2\xc3\xd4"):
                    fmt = ">IIII"
                elif hdr[:4] in (b"\x4d\x3c\xb2\xa1", b"\xd4\xc3\xb2\xa1"):
                    fmt = "<IIII"
                else:
                    raise ValueError(f"not a pcap stream ({hdr[:4].hex()})")
                self.header = hdr
                self.state, self.error = "capturing", ""
                buf = bytearray()
                while not self.stop:
                    data = resp.read1(1 << 20)       # whatever has arrived: never throttle the tuner
                    if not data:
                        break
                    buf += data
                    self.bytes += len(data)
                    rate_b += len(data)
                    if time.time() - rate_t >= 2:
                        self.rate_bps = rate_b * 8 / (time.time() - rate_t)
                        rate_t, rate_b = time.time(), 0
                    o, n = 0, len(buf)
                    wstart = 0
                    while o + 16 <= n:
                        ts_s, ts_f, cl, _ = struct.unpack_from(fmt, buf, o)
                        if o + 16 + cl > n:
                            break
                        if cl > 262144:
                            raise ValueError("corrupt pcap record")
                        if f is None or ts_s - cur_start >= chunk_s:
                            if f is not None:
                                f.write(buf[wstart:o])
                                f.close()
                                self.chunks += 1
                                self.on_chunk(cur)
                            cur, f = self._open_chunk(ts_s)
                            cur_start = ts_s
                            wstart = o
                        d = o + 16
                        an = alp_hdr_len(buf, d) if cl > 30 else 0
                        if an and cl > an + 28 and buf[d + an] >> 4 == 4 and buf[d + an + 9] == 17:   # ALP + IPv4/UDP
                            ihl = (buf[d + an] & 15) * 4
                            ipb = bytes(buf[d + an + 16:d + an + 20])
                            dst = socket.inet_ntoa(ipb)
                            u = d + an + ihl
                            port = (buf[u + 2] << 8) | buf[u + 3]
                            if dst == LLS_ADDR[0] and port == LLS_ADDR[1]:
                                svcs = parse_slt(bytes(buf[u + 8:d + cl]))
                                if svcs and svcs != self.slt:
                                    self.slt = svcs
                                    self.store.ips = {v["ip"] for v in svcs.values()
                                                      if v["category"] == "1" and not v["protected"] and v["ip"]}
                            elif dst in self.store.ips:
                                self.store.feed(ts_s + ts_f / 1e9, dst, port, bytes(buf[u + 8:d + cl]))
                        o += 16 + cl
                    if f is not None:
                        f.write(buf[wstart:o])
                    del buf[:o]
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log("Capture", "!!", f"stream error: {self.error}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if not self.stop:
                self.reconnects += 1
                self.state = "reconnecting"
                time.sleep(0.5)
        if f is not None:
            f.close()
            self.on_chunk(cur)

class ObjectView:
    """Read side of the ObjectStore: the capture runs in its own process and
    writes complete objects as files; the web server reads them from disk."""
    def __init__(self, folder):
        self.dir = folder

    def _d(self, ip, port, tsi):
        return os.path.join(self.dir, f"{ip}_{port}", str(tsi))

    def get(self, ip, port, tsi, toi):
        try:
            with open(os.path.join(self._d(ip, port, tsi), str(toi)), "rb") as f:
                return f.read()
        except OSError:
            return None

    def span(self, ip, port, tsi):
        try:
            tois = [int(f) for f in os.listdir(self._d(ip, port, tsi)) if f.isdigit()]
        except OSError:
            return (None, None)
        tois = [t for t in tois if t > 1000]
        return (min(tois), max(tois)) if tois else (None, None)

def capture_url(cfg):
    plps = "".join(f"p{p.strip()}" for p in cfg["plps"].split(",") if p.strip())
    return f"http://{cfg['hdhr']}:5004/tuner{cfg['tuner']}/ch{cfg['rf']}{plps}?format=alp-pcap"

def _capture_process(cfg, q, status_path):
    """Child process: nothing else here competes with reading the tuner. (In
    one process, RENDER's output and the web server took turns with the reader;
    the HDHomeRun dropped packets meanwhile and segments went missing.)"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)      # the parent stops us
    parent = os.getppid()
    store = ObjectStore(os.path.join(cfg["dir"], "objects"), cfg["buffer_s"])
    cap = LiveCapture(cfg, store, q.put)
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            cap.stop = True
            os._exit(0)
        st = {"state": cap.state, "error": cap.error, "bytes": cap.bytes, "rate_bps": cap.rate_bps,
              "started": cap.started, "reconnects": cap.reconnects, "chunks": cap.chunks,
              "complete": store.complete, "incomplete": store.incomplete, "slt": cap.slt}
        try:
            with open(status_path + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(status_path + ".tmp", status_path)
        except OSError:
            pass

class LiveState:
    """Everything live mode knows: services from the newest report, the
    merged guide, the capture process and its objects on disk."""
    def __init__(self, cfg, srv=None):
        import multiprocessing
        self.cfg, self.srv = cfg, srv
        os.makedirs(cfg["dir"], exist_ok=True)
        # leftovers from an earlier run (the guide is kept: it only grows)
        shutil.rmtree(os.path.join(cfg["dir"], "objects"), ignore_errors=True)
        for f in os.listdir(cfg["dir"]):
            if re.match(r"rf\d+-live-\d{8}-\d{6}\.", f):
                try:
                    os.remove(os.path.join(cfg["dir"], f))
                except OSError:
                    pass
        self.store = ObjectView(os.path.join(cfg["dir"], "objects"))
        self.guide = GuideMerger(os.path.join(cfg["dir"], "guide.json"))
        self.bcast = {}           # sid -> {title, mpd, stsid, flows, encrypted}
        self.reports = []         # (time, chunk pcap path, report path)
        self.last_report = ""
        self.status_path = os.path.join(cfg["dir"], "capture.json")
        self.filled = {}
        self._status, self._status_t = {}, 0
        ctx = multiprocessing.get_context("fork")
        self.chunk_q = ctx.Queue()
        self.proc = ctx.Process(target=_capture_process, args=(cfg, self.chunk_q, self.status_path), daemon=True)
        self.proc.start()

    def start_worker(self, srv):
        self.srv = srv
        self.disk_bytes = 0
        threading.Thread(target=self._render_worker, daemon=True).start()
        threading.Thread(target=self._pruner, daemon=True).start()

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.join(3)
        except Exception:
            pass

    def status(self):
        if time.time() - self._status_t > 1:
            try:
                with open(self.status_path) as f:
                    self._status = json.load(f)
            except (OSError, ValueError):
                pass
            self._status_t = time.time()
        return self._status

    def _render_worker(self):
        while True:
            try:
                pcap = self.chunk_q.get(timeout=1)
            except Exception:
                continue
            base = pcap[:-5]
            try:
                # RENDER prints megabytes: straight to a file, then keep only its end
                logp = base + ".render.log"
                cmd = ["nice", "-n", "10"] + (["ionice", "-c", "3"] if shutil.which("ionice") else []) + [self.cfg["render"], pcap]
                with open(logp, "wb") as lf:
                    subprocess.run(cmd, cwd=self.cfg["dir"], stdout=lf, stderr=subprocess.STDOUT, timeout=600)
                if os.path.getsize(logp) > 65536:
                    with open(logp, "rb") as lf:
                        lf.seek(-65536, 2)
                        tail = lf.read()
                    with open(logp, "wb") as lf:
                        lf.write(tail)
            except Exception as e:
                log("RENDER", "!!", f"{os.path.basename(pcap)}: {e}")
                continue
            report = base + ".html"
            if os.path.exists(report):
                self._take_report(report)
            if os.path.exists(base + ".guide.html"):
                n = self.guide.add_page(base + ".guide.html")
                log("ESG", "**", f"{os.path.basename(pcap)}: guide now has {n} programs")
            self.reports.append((time.time(), pcap, report if os.path.exists(report) else ""))
            if not self.cfg.get("keep_pcap"):
                try:
                    os.remove(pcap)            # the report and guide are what's needed; ~170 MB a minute saved
                except OSError:
                    pass

    def _take_report(self, report):
        self.last_report = report
        slt = slt_from_report(report)
        if slt:
            self.srv.slt = slt
        apps = apps_from_report(report)
        known = {(a["service_id"], a["app_context_id"], a["bband_url"], a["bcast_url"]) for a in self.srv.apps}
        for a in apps:
            if (a["service_id"], a["app_context_id"], a["bband_url"], a["bcast_url"]) not in known:
                self.srv.apps.append(a)          # append-only: app numbers in open WebSockets stay valid
        for c in live_channels_from_report(report):
            old = next((i for i, x in enumerate(self.srv.live) if x["service_id"] == c["service_id"]), None)
            if old is None:
                self.srv.live.append(c)
            else:
                self.srv.live[old] = c
        if self.srv.live and self.srv.ac4 is None and self.cfg.get("ffmpeg") and any(c["ac4"] for c in self.srv.live):
            self.srv.ac4 = Ac4Transcoder(self.cfg["ffmpeg"])
        for sid, b in bcast_services_from_report(report).items():
            b["flows"] = stsid_flows(b["stsid"])
            b["ac4"] = ac4_sets(b["mpd"])
            self.bcast[sid] = b

    def _prune_chunks(self):
        """Delete live files older than the buffer, by age on disk (so nothing is
        left behind if a RENDER run failed or the probe was killed), and count
        what the live folder holds."""
        now = time.time()
        self.reports = [r for r in self.reports if now - r[0] <= self.cfg["buffer_s"]]
        total = 0
        for f in os.listdir(self.cfg["dir"]):
            if not re.match(r"rf\d+-live-\d{8}-\d{6}\.", f):
                continue
            path = os.path.join(self.cfg["dir"], f)
            try:
                st = os.stat(path)
                if now - st.st_mtime > self.cfg["buffer_s"] + 60:
                    os.remove(path)
                else:
                    total += st.st_size
            except OSError:
                pass
        for root, _, files in os.walk(os.path.join(self.cfg["dir"], "objects")):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        self.disk_bytes = total

    def _pruner(self):
        while True:
            try:
                self._prune_chunks()
            except Exception as e:
                log("Prune", "!!", f"{type(e).__name__}: {e}")
            time.sleep(60)

    # ---- broadcast services ----
    def svc_addr(self, sid):
        s = self.status().get("slt", {}).get(sid)
        return (s["ip"], s["port"]) if s else (None, None)

    def resolve(self, sid, name):
        """segment name -> (ip, port, tsi, toi) via the S-TSID"""
        b = self.bcast.get(sid)
        ip, port = self.svc_addr(sid)
        if not b or not ip:
            return None
        for fl in b["flows"]:
            fip, fport = fl["ip"] or ip, fl["port"] or port
            if name == fl["init_name"] and fl["init_toi"] is not None:
                return fip, fport, fl["tsi"], fl["init_toi"]
            if fl["template"]:
                rx = "^" + re.escape(fl["template"]).replace(re.escape("$TOI$"), r"(\d+)") + "$"
                m = re.match(rx, name)
                if m:
                    return fip, fport, fl["tsi"], int(m.group(1))
        return None

    def fetch(self, sid, name, wait=6.0):
        """Segment bytes. A segment newer than anything held yet is probably still
        arriving: wait for it rather than answering 404 (players ask at the
        MPD's availability time, a second or two before the last packet is in)."""
        r = self.resolve(sid, name)
        if not r:
            return None
        data = self.store.get(*r)
        if data is not None or r[3] <= 1000:
            return data
        lo, hi = self.store.span(*r[:3])
        if hi is not None and r[3] <= hi:
            return self._fill(sid, r)        # older segment that never arrived whole
        end = time.time() + wait
        while time.time() < end:
            time.sleep(0.2)
            data = self.store.get(*r)
            if data is not None:
                return data
        return self._fill(sid, r)

    def _fill(self, sid, r):
        """Video segment lost in reception: repeat the previous one, re-stamped
        to this one's time, so the player keeps going (a TV freezes briefly too).
        Audio is handled by the AC-4 path (silence); captions just skip."""
        ip, port, tsi, toi = r
        b = self.bcast.get(sid)
        fl = next((f for f in b["flows"] if f["tsi"] == tsi), None) if b else None
        if not fl or fl.get("type") != "video" or fl["init_toi"] is None:
            return None
        init = self.store.get(ip, port, tsi, fl["init_toi"])
        if not init:
            return None
        for k in (1, 2, 3):
            prev = self.store.get(ip, port, tsi, toi - k)
            if prev:
                self.filled[sid] = self.filled.get(sid, 0) + 1
                return retime_segment(prev, round(k * self.seg_dur(sid) * track_timescale(init)), toi)
        return None

    def buffered_s(self, sid):
        """seconds of video held for a service"""
        b = self.bcast.get(sid)
        ip, port = self.svc_addr(sid)
        if not b or not ip:
            return 0
        best = 0
        for fl in b["flows"]:
            lo, hi = self.store.span(fl["ip"] or ip, fl["port"] or port, fl["tsi"])
            if lo is not None:
                best = max(best, (hi - lo + 1) * self.seg_dur(sid))
        return best

    def seg_dur(self, sid):
        m = re.search(r'duration="(\d+)"[^>]*timescale="(\d+)"|timescale="(\d+)"[^>]*duration="(\d+)"', self.bcast[sid]["mpd"])
        if not m:
            return 2.0
        d, ts = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        return int(d) / int(ts)

    def mpd(self, sid, host):
        b = self.bcast[sid]
        mpd = b["mpd"]
        mpd = _flac_mpd(mpd) if self.srv.ac4 else _drop_ac4(mpd)
        depth = max(4, min(self.cfg["buffer_s"], int(self.buffered_s(sid)) - 4))   # only what is really held
        mpd = re.sub(r'timeShiftBufferDepth="[^"]*"', "", mpd)
        mpd = re.sub(r'suggestedPresentationDelay="[^"]*"', "", mpd)
        mpd = re.sub(r'minimumUpdatePeriod="[^"]*"', 'minimumUpdatePeriod="PT10S"', mpd)
        pub = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mpd = re.sub(r'publishTime="[^"]*"', f'publishTime="{pub}"', mpd)
        mpd = re.sub(r"<MPD\b", f'<MPD timeShiftBufferDepth="PT{depth}S" suggestedPresentationDelay="PT10S"', mpd, count=1)
        mpd = re.sub(r"<BaseURL>[^<]*</BaseURL>", "", mpd)
        mpd = re.sub(r"(<Period\b)", f"<BaseURL>http://{host}/bcast/{sid}/seg/</BaseURL>\\1", mpd, count=1)
        if "<UTCTiming" not in mpd:
            mpd = mpd.replace("</MPD>", f'<UTCTiming schemeIdUri="urn:mpeg:dash:utc:http-iso:2014" value="http://{host}/time"/></MPD>')
        return mpd

    def state(self, host):
        c = self.status()
        bc = []
        for sid, b in sorted(self.bcast.items()):
            num = channel_number(b["title"]) or ""
            bc.append({"sid": sid, "title": b["title"], "num": num, "encrypted": b["encrypted"],
                       "buffer_s": 0 if b["encrypted"] else int(self.buffered_s(sid)),
                       "watch": "" if b["encrypted"] else f"/bcast/{sid}/watch"})
        apps = []
        for i, a in enumerate(self.srv.apps):
            entry = a["bband_url"] or a["bcast_url"]
            apps.append({"title": a["title"], "context": a["app_context_id"], "entry": entry,
                         "launch": launch_url(entry, f"ws://{host}/app/{i}") if entry else ""})
        st = self.srv.live_stats
        live = [{"title": x["title"], "server": x["base_url"], "watch": f"/live/{x['service_id']}/watch",
                 "stat": (f"{st[x['service_id']]['ok']} segments ok, {st[x['service_id']]['fail']} failed"
                          if x["service_id"] in st else "")} for x in self.srv.live]
        tc = self.srv.ac4
        alive = self.proc.is_alive()
        return {"capture": {"state": c.get("state", "starting") if alive else "stopped (capture process ended)",
                            "error": c.get("error", ""), "url": capture_url(self.cfg),
                            "mbps": round(c.get("rate_bps", 0) / 1e6, 1), "mb": round(c.get("bytes", 0) / 1e6),
                            "chunks": c.get("chunks", 0), "running_s": int(time.time() - c.get("started", time.time())),
                            "reconnects": c.get("reconnects", 0), "chunk_s": self.cfg["chunk_s"],
                            "objects": c.get("complete", 0), "lost_objects": c.get("incomplete", 0),
                            "filled": dict(self.filled)},
                "report": os.path.basename(self.last_report) if self.last_report else "",
                "guide_programs": self.guide.count(), "buffer_min": self.cfg["buffer_s"] // 60,
                "disk_mb": round(getattr(self, "disk_bytes", 0) / 1e6), "keep_pcap": bool(self.cfg.get("keep_pcap")),
                "broadcast": bc, "apps": apps, "internet": live,
                "audio": ("AC-4 converted by " + tc.ffmpeg) if tc else
                         ("no ffmpeg-ac4: video only" if not self.cfg.get("ffmpeg") else "AC-4 converted on demand")}

LIVE_PAGE = """<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>ATSC 3.0 Live</title><style>%CSS%
.st{display:flex;flex-wrap:wrap;gap:6px 18px;margin:.4em 0 1em}.st span{white-space:nowrap}.ok{color:#198754}.bad{color:#dc3545}
.bar{height:6px;background:#e9ecef;border-radius:3px;min-width:80px}.bar i{display:block;height:6px;background:#0d6efd;border-radius:3px}
</style></head><body><div class='c'><h1>ATSC 3.0 Live <span id='rf' class='muted' style='font-size:.6em'></span></h1>
<div class='st' id='cap'>Connecting&hellip;</div>
<p id='links'></p>
<h2>Live broadcast</h2><p class='muted'>Played from the capture: the Pi rebuilds each service's video and audio segments from the
air, keeps a rolling buffer, and converts AC-4 audio. Drag the player's timeline back to rewind within the buffer.</p>
<table id='bc'><tr><td class='muted'>Waiting for the first capture to finish&hellip;</td></tr></table><p class='muted' id='enc'></p>
<h2>Interactive apps</h2><p class='muted'>Click inside the app, then arrow keys, <kbd>Enter</kbd> = Select/OK, <kbd>Esc</kbd> = Back.
Requests are on the <a href='/log' target='_blank'>request log</a>. To fit apps to the browser window, add the
<a href='/app-scaler.zip'>app scaler extension</a> to Chrome once (unzip, chrome://extensions, Developer mode, Load unpacked);
Alt+Z turns it off and on.</p>
<table id='apps'><tr><td class='muted'>Waiting for the first capture to finish&hellip;</td></tr></table>
<h2>Live internet channels</h2><p class='muted' id='aud'></p>
<table id='net'><tr><td class='muted'>Waiting for the first capture to finish&hellip;</td></tr></table>
<p class='muted'>Use this page through an SSH tunnel (http://localhost:%PORT%/). The page updates itself.</p></div>
<script>
const e = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = s => s >= 60 ? Math.floor(s / 60) + ' min ' + (s % 60 ? s % 60 + ' s' : '') : s + ' s';
async function tick() {
  let d; try { d = await (await fetch('/state.json')).json(); } catch (x) { return; }
  const c = d.capture, next = c.chunk_s - (c.running_s % c.chunk_s);
  document.getElementById('cap').innerHTML =
    '<span class="' + (c.state === 'capturing' ? 'ok' : 'bad') + '">&#9679; ' + e(c.state) + (c.error ? ': ' + e(c.error) : '') + '</span>' +
    '<span>' + c.mbps + ' Mbps</span><span>' + c.mb + ' MB in ' + fmt(c.running_s) + '</span>' +
    '<span>' + c.chunks + ' captures done, next in ' + next + ' s</span><span>' + c.reconnects + ' reconnects</span>' +
    '<span>' + c.objects + ' segments rebuilt' + (c.lost_objects ? ', ' + c.lost_objects + ' incomplete' : '') + '</span>' +
    '<span>live folder ' + (d.disk_mb >= 1000 ? (d.disk_mb / 1000).toFixed(1) + ' GB' : d.disk_mb + ' MB') + '</span>';
  document.getElementById('rf').textContent = c.url;
  document.getElementById('links').innerHTML =
    (d.guide_programs ? '<a class=btn href="/guide" target=_blank>&#128250; TV guide (' + d.guide_programs + ' programs)</a> ' : '<span class=muted>TV guide: waiting for ESG data&hellip;</span> ') +
    (d.report ? ' <a href="/reports/' + encodeURIComponent(d.report) + '" target=_blank>latest RENDER report</a>' : '');
  const enc = d.broadcast.filter(b => b.encrypted).map(b => b.title.replace(/^Service \\d+: /, '').replace(/ Encrypted$/, ''));
  document.getElementById('enc').textContent = enc.length ? 'Not playable (encrypted with DRM): ' + enc.join(', ') + '.' : '';
  const bc = d.broadcast.filter(b => !b.encrypted).map(b => '<tr><td><strong>' + e(b.title) + '</strong></td><td style="width:30%">' +
      (b.encrypted ? '<span class=muted>encrypted (DRM)</span>' :
       '<div class=bar><i style="width:' + Math.min(100, b.buffer_s / (d.buffer_min * 0.6)) + '%"></i></div><small>' + fmt(b.buffer_s) + ' of ' + d.buffer_min + ' min buffered</small>') +
      '</td><td>' + (b.watch ? '<a class=btn target=_blank href="' + b.watch + '">Watch</a>' : '') + '</td></tr>').join('');
  if (bc) document.getElementById('bc').innerHTML = '<tr><th>Service</th><th>Buffer</th><th></th></tr>' + bc;
  const ap = d.apps.map((a, i) => '<tr><td><strong>' + e(a.title) + '</strong><br><span class=muted>' + e(a.context) + '</span></td><td>' + e(a.entry) +
      '</td><td>' + (a.launch ? '<a class=btn target=_blank href="' + e(a.launch) + '">Launch</a>' : '<span class=muted>broadcast-only</span>') + '</td></tr>').join('');
  if (ap) document.getElementById('apps').innerHTML = '<tr><th>Service</th><th>Entry page</th><th></th></tr>' + ap;
  const nt = d.internet.map(x => '<tr><td><strong>' + e(x.title) + '</strong></td><td>' + e(x.server) + '</td><td class=muted>' + e(x.stat) +
      '</td><td><a class=btn target=_blank href="' + x.watch + '">Watch</a></td></tr>').join('');
  if (nt) document.getElementById('net').innerHTML = '<tr><th>Service</th><th>Server</th><th>Status</th><th></th></tr>' + nt;
  document.getElementById('aud').textContent = 'Audio: ' + d.audio;
}
tick(); setInterval(tick, 3000);
</script></body></html>"""

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG = []
LOG_LOCK = threading.Lock()
LOG_FILE = None

def log(app, direction, payload):
    entry = {"t": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3], "app": app,
             "dir": direction, "msg": payload}
    with LOG_LOCK:
        LOG.append(entry)
        del LOG[:-2000]
        if LOG_FILE:
            LOG_FILE.write(json.dumps(entry) + "\n")
            LOG_FILE.flush()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    say(f"{entry['t']} [{app}] {direction} {text[:300]}")

_TERM_OK = True
def say(msg):
    """print, but keep running when the terminal is gone (SSH dropped)"""
    global _TERM_OK
    if not _TERM_OK:
        return
    try:
        print(msg, flush=True)
    except (OSError, ValueError):
        _TERM_OK = False

# --------------------------------------------------------------------------
# A/344 receiver API stub
# --------------------------------------------------------------------------
class Receiver:
    def __init__(self, app, args, server=None):
        self.app = app
        self.args = args
        self.server = server
        self.subscriptions = set()

    def signaling(self, params):
        """A/344 query.signaling. Names are LLS table ids as decimal strings ("1" = SLT) or
        signaling object types; some apps send "SLT". Only the SLT is available here."""
        params = params or {}
        names = params.get("nameList", params.get("objectList"))
        if isinstance(names, str):
            names = [names]
        slt = self.app.get("slt") or getattr(self.server, "slt", None)
        out = []
        if slt:
            want = [n for n in (names or ["1"]) if str(n).strip().upper() in ("1", "SLT")]
            for n in want:
                out.append({"name": str(n), "group": 1, "version": slt["version"], "table": slt["xml"]})
        return {"objectList": out}

    def handle(self, method, params):
        """Return (result, error). Field names follow A/344 where known."""
        a = self.app
        if method == "org.atsc.query.service":
            return {"service": a["global_service_id"]}, None
        if method == "org.atsc.query.ratingLevel":
            return {"rating": ""}, None
        if method == "org.atsc.query.cc":
            return {"ccEnabled": False}, None
        if method == "org.atsc.query.languages":
            return {"preferredAudioLang": "en", "preferredUiLang": "en",
                    "preferredCaptionSubtitleLang": "en"}, None
        if method == "org.atsc.query.captionDisplay":
            return {"msgType": "captionDisplayPrefs"}, None
        if method in ("org.atsc.query.baseURI", "org.atsc.query.receiverWebServerURI"):
            uri = f"http://localhost:{self.args.port}/cache/{a['service_id']}/"
            return ({"baseURI": uri} if method.endswith("baseURI") else {"receiverWebServerURI": uri}), None
        if method == "org.atsc.query.deviceInfo":
            return {"deviceMake": DEVICE_MAKE, "deviceModel": "ba_probe",
                    "deviceInput": {"ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37,
                                    "ArrowRight": 39, "Select": 13, "Back": 461},
                    "deviceInfo": {"numberOfTuners": 1, "yearOfMfr": 2026}}, None
        if method == "org.atsc.query.alerting":
            return {"alertList": []}, None
        if method == "org.atsc.query.serviceGuideUrls":
            return {"urlList": []}, None
        if method == "org.atsc.query.signaling":
            return self.signaling(params), None
        if method == "org.atsc.query.MPDUrl":
            return {"MPDUrl": ""}, None
        if method == "org.atsc.subscribe":
            types = (params or {}).get("msgType", [])
            self.subscriptions.update(types if isinstance(types, list) else [types])
            return {"msgType": types}, None
        if method == "org.atsc.unsubscribe":
            types = (params or {}).get("msgType", [])
            for t in (types if isinstance(types, list) else [types]):
                self.subscriptions.discard(t)
            return {"msgType": types}, None
        if method == "org.atsc.request.keys":
            return {"accepted": (params or {}).get("keys", [])}, None
        if method == "org.atsc.relinquish.keys":
            return {}, None
        # Anything else: log loudly; by default answer with an empty result so the
        # app keeps going and we see its next request.
        log(a["title"], "!!", f"UNKNOWN METHOD {method} (stubbed)")
        if self.args.strict:
            return None, {"code": -32601, "message": "Method not found"}
        return {}, None

# --------------------------------------------------------------------------
# Minimal WebSocket (RFC 6455) on top of http.server
# --------------------------------------------------------------------------
def ws_recv(rfile):
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None, None
    b1, b2 = hdr
    opcode = b1 & 0x0F
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if b2 & 0x80 else b"\0\0\0\0"
    data = bytearray(rfile.read(n))
    for i in range(len(data)):
        data[i] ^= mask[i % 4]
    # (fragmented messages are rare for JSON-RPC; FIN is assumed)
    return opcode, bytes(data)

def ws_send(wfile, payload, opcode=1):
    data = payload.encode() if isinstance(payload, str) else payload
    n = len(data)
    if n < 126:
        hdr = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 65536:
        hdr = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        hdr = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    wfile.write(hdr + data)
    wfile.flush()

# --------------------------------------------------------------------------
# HTTP: launcher, log viewer, WebSocket endpoints
# --------------------------------------------------------------------------
PAGE_CSS = """body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:1em;background:#f8f9fa;color:#212529}
.c{background:#fff;padding:1.2em 1.5em;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.1);max-width:1100px;margin:auto}
h1{border-bottom:2px solid #007bff;padding-bottom:.3em}table{border-collapse:collapse;width:100%}
th,td{border:1px solid #dee2e6;padding:6px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#e9ecef}
a.btn{display:inline-block;background:#007bff;color:#fff;padding:4px 12px;border-radius:4px;text-decoration:none;white-space:nowrap}
.muted{color:#6c757d}pre{background:#e9ecef;padding:8px;white-space:pre-wrap;margin:0;max-height:240px;overflow:auto}"""

class Handler(BaseHTTPRequestHandler):
    server_version = "ba_probe/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self.websocket(path)
        lv = self.server.livestate
        if path == "/favicon.ico":
            if not FAVICON:
                return self._send(404, "no icon", "text/plain")
            return self._send_bytes(FAVICON, "image/x-icon", cache="max-age=86400")
        if path == "/":
            if lv:
                return self._send(200, LIVE_PAGE.replace("%CSS%", PAGE_CSS).replace("%PORT%", str(self.server.args.port)))
            return self.launcher()
        if path == "/state.json" and lv:
            return self._send(200, json.dumps(lv.state(self.headers.get("Host", "localhost"))), "application/json")
        m = re.match(r"^/reports/([\w.\-]+\.html)$", path)
        if m and lv:
            f = os.path.join(lv.cfg["dir"], m.group(1))
            if os.path.isfile(f):
                return self._send(200, open(f, encoding="utf-8", errors="replace").read())
            return self._send(404, "report no longer in the buffer", "text/plain")
        if path == "/app-scaler.zip":
            return self.app_scaler_zip()
        m = re.match(r"^/appview/(\d+)$", path)
        if m:
            return self.app_view(int(m.group(1)))
        m = re.match(r"^/bcast/(\d+)/(watch|manifest\.mpd|seg/([^/]+))$", path)
        if m and lv:
            return self.bcast(lv, m.group(1), m.group(2), m.group(3))
        if path == "/guide":
            return self.guide_page()
        if path == "/log":
            return self.log_page()
        if path == "/log.json":
            with LOG_LOCK:
                return self._send(200, json.dumps(LOG), "application/json")
        if path == "/time":
            return self._send(200, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
                              "text/plain")
        m = re.match(r"^/live/(\d+)/(manifest\.mpd|watch|seg/(.+))$", path)
        if m:
            return self.live(m.group(1), m.group(2), m.group(3))
        if path == "/static/dash.all.min.js" and self.server.args.dashjs:
            b = open(self.server.args.dashjs, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if path.startswith("/cache/"):
            log("cache", "GET", self.path)
            return self._send(404, "not cached (probe has no broadcast files)", "text/plain")
        self._send(404, "not found", "text/plain")

    def launcher(self):
        ws_host = self.headers.get("Host", f"localhost:{self.server.args.port}")
        rows = []
        for i, a in enumerate(self.server.apps):
            entry = a["bband_url"] or a["bcast_url"]
            ws_base = f"ws://{ws_host}/app/{i}"
            url = launch_url(entry, ws_base) if entry else ""
            rows.append(
                f"<tr><td><strong>{html.escape(a['title'])}</strong><br><span class='muted'>{html.escape(a['global_service_id'])}</span></td>"
                f"<td>{html.escape(a['app_context_id'])}</td><td>{html.escape(entry)}</td>"
                f"<td>{f'<a class=btn target=_blank href={html.escape(url, quote=True)!r}>Launch</a>' if url else '<span class=muted>broadcast-only</span>'}</td></tr>")
        body = (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(BRAND)}</title>"
                f"<style>{PAGE_CSS}</style></head><body><div class='c'><h1>{html.escape(BRAND)}</h1>{PAGE_NOTE}"
                f"<p>Each app opens with <code>?wsURL=ws://{html.escape(ws_host)}/app/N</code> and talks to {html.escape(PROBE_WORD)} as if it were the TV. "
                f"Watch the requests on the <a href='/log' target='_blank'>request log</a>.</p>"
                + (f"<p><a class=btn href='/guide' target='_blank'>&#128250; TV guide</a> <span class='muted'>program "
                   f"listings from this capture; channels with an app or live stream get a button</span></p>"
                   if self.server.guide else "") +
                f"<p><strong>Remote keys:</strong> click inside the app first, then use the arrow keys, "
                f"<kbd>Enter</kbd> = Select/OK, <kbd>Esc</kbd> = Back. Gameloop's title screen needs Esc to start.</p>"
                f"<table><tr><th>Service</th><th>App context</th><th>Entry page</th><th></th></tr>{''.join(rows) or '<tr><td colspan=4>No apps in this report.</td></tr>'}</table>"
                + self.live_section()
                + (PAGE_FOOTER if PAGE_FOOTER is not None else
                   f"<p class='muted'>Use this page through an SSH tunnel (http://localhost:{self.server.args.port}/). "
                   f"Browsers only let an https app talk to an unencrypted ws:// receiver on localhost.</p>")
                + "</div></body></html>")
        self._send(200, body)

    def guide_page(self):
        lv = self.server.livestate
        if lv:
            host = self.headers.get("Host", "localhost")
            st = lv.state(host)
            watch = {}
            for a in self.server.apps:
                num = channel_number(a["title"])
                i = self.server.apps.index(a)
                entry = a["bband_url"] or a["bcast_url"]
                if num and entry:
                    watch[num] = {"url": launch_url(entry, f"ws://{host}/app/{i}"), "label": "Open app"}
            for c in self.server.live:
                num = channel_number(c["title"])
                if num:
                    watch[num] = {"url": f"/live/{c['service_id']}/watch", "label": "Watch"}
            for b in st["broadcast"]:
                if b["watch"] and b["num"]:
                    watch[b["num"]] = {"url": b["watch"], "label": "Watch"}
            page = lv.guide.page(watch)
            return self._send(200, page) if page else self._send(503, "No ESG data yet; try again after the next capture.", "text/plain")
        g = self.server.guide
        if not g or not os.path.isfile(g):
            return self._send(404, "No TV guide for this report (the capture had no program schedule).", "text/plain")
        page = open(g, encoding="utf-8").read()
        host = self.headers.get("Host", f"localhost:{self.server.args.port}")
        watch = {}
        for i, a in enumerate(self.server.apps):
            num = channel_number(a["title"])
            entry = a["bband_url"] or a["bcast_url"]
            if num and entry:
                watch[num] = {"url": launch_url(entry, f"ws://{host}/app/{i}"), "label": "Open app"}
        for c in self.server.live:
            num = channel_number(c["title"])
            if num:
                watch[num] = {"url": f"/live/{c['service_id']}/watch", "label": "Watch"}
        inject = "<script>window.WATCH = " + json.dumps(watch).replace("<", "\\u003c") + ";</script>\n"
        page = page.replace("<script>\nconst GUIDE", inject + "<script>\nconst GUIDE", 1)
        self._send(200, page)

    def live_section(self):
        if not self.server.live:
            return ""
        rows = []
        for c in self.server.live:
            st = self.server.live_stats.get(c["service_id"], {})
            stat = f"{st.get('ok',0)} segments ok, {st.get('fail',0)} failed" if st else "not watched yet"
            if st.get("last_error"):
                stat += f" (last: {html.escape(st['last_error'])})"
            rows.append(f"<tr><td><strong>{html.escape(c['title'])}</strong></td><td>{html.escape(c['base_url'])}</td>"
                        f"<td class='muted'>{stat}</td>"
                        f"<td><a class=btn target=_blank href='/live/{c['service_id']}/watch'>Watch</a></td></tr>")
        tc = self.server.ac4
        if tc:
            audio = (f"<strong>Audio:</strong> AC-4 is transcoded to stereo FLAC by {html.escape(HOST_NAME)} with <code>{html.escape(tc.ffmpeg)}</code> "
                     f"({tc.stats['ok']} segments done, {tc.stats['fail']} failed, last took {tc.stats['last_ms']} ms"
                     + (f"; last error: {html.escape(tc.stats['last_error'])}" if tc.stats['last_error'] else "") + ").")
        else:
            audio = (f"<strong>{'Video only' if NO_CAPTIONS else 'Video and captions only'}</strong>: the audio is AC-4, "
                     "which browsers can't decode. " + NO_AC4_HINT)
        return ("<h2>Live channels (internet-delivered)</h2><p>These services' video comes from the broadcaster's server, "
                f"not the air. {html.escape(HOST_NAME[:1].upper() + HOST_NAME[1:])} fetches the segments and your browser plays them. " + audio + "</p>"
                "<table><tr><th>Service</th><th>Server</th><th>Status</th><th></th></tr>" + "".join(rows) + "</table>")

    def live(self, sid, what, seg):
        chan = next((c for c in self.server.live if c["service_id"] == sid), None)
        if not chan:
            return self._send(404, "unknown live channel", "text/plain")
        if what == "manifest.mpd":
            host = self.headers.get("Host", f"localhost:{self.server.args.port}")
            mpd = live_mpd(chan, time.time(), f"http://{host}/live/{sid}/seg/", f"http://{host}/time",
                           keep_ac4=self.server.args.keep_ac4, flac=self.server.ac4 is not None)
            return self._send(200, mpd, "application/dash+xml")
        if what == "watch":
            return self.watch_page(chan)
        # segment proxy: Pi fetches from the broadcaster's server, browser gets it from localhost
        if "/" in seg or ".." in seg:
            return self._send(400, "bad segment name", "text/plain")
        if seg.startswith(FLAC_PREFIX):
            return self.flac_segment(chan, seg[len(FLAC_PREFIX):])
        url = chan["base_url"] + seg
        st = self.server.live_stats.setdefault(sid, {"ok": 0, "fail": 0, "bytes": 0, "last_error": ""})
        try:
            with fetch_origin(url) as r:
                data = r.read()
            st["ok"] += 1
            st["bytes"] += len(data)
            ctype = "video/mp4" if seg.endswith((".mp4", ".mp4v")) else \
                    "audio/mp4" if seg.endswith(".mp4a") else "application/mp4"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=60")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            st["fail"] += 1
            st["last_error"] = f"HTTP {e.code} for {seg}"
            log(chan["title"], "!!", f"origin HTTP {e.code}: {url}")
            self._send(e.code, f"origin returned {e.code}", "text/plain")
        except Exception as e:  # network errors, timeouts
            st["fail"] += 1
            st["last_error"] = f"{type(e).__name__}: {e}"
            log(chan["title"], "!!", f"origin error {type(e).__name__}: {e} ({url})")
            self._send(502, f"origin error: {e}", "text/plain")

    def _send_bytes(self, data, ctype, cache="max-age=60"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def flac_segment(self, chan, name, fetch=None):
        """fetch(name) -> bytes; default: from the channel's internet server"""
        if fetch is None:
            def fetch(n):
                with fetch_origin(chan["base_url"] + n) as r:
                    return r.read()
        tc = self.server.ac4
        if tc is None:
            return self._send(404, "AC-4 transcoding is off", "text/plain")
        st = tc.stats
        try:
            for a in chan["ac4"]:
                if a["init_re"].match(name):
                    return self._send_bytes(tc.init_segment(), "audio/mp4")
                m = a["media_re"].match(name)
                if not m:
                    continue
                key = chan.get("base_url", chan.get("title", "")) + name
                with tc.lock:
                    hit = tc.cache.get(key)
                if hit is not None:
                    return self._send_bytes(hit, "audio/mp4")
                rid = m.groupdict().get("rid") or ""
                init_name = a["init"].replace("$RepresentationID$", rid)
                init_key = chan.get("base_url", chan.get("title", "")) + init_name
                with tc.lock:
                    init = tc.cache.get(init_key)
                if init is None:
                    init = fetch(init_name)
                    if init is None:
                        return self._send(404, "init segment not received yet", "text/plain")
                    tc.remember(init_key, init)
                media = fetch(name)
                if media is None:
                    # never arrived (reception): silence keeps the picture going
                    g = m.groupdict()
                    ts = a["timescale"] or 1
                    start = None
                    for k in (1, 2, 3):            # the timeline can be offset from the numbering: follow a neighbour
                        pn = prev_segment_name(a, m, k)
                        pd = fetch(pn) if pn else None
                        if pd is not None:
                            start = media_start_s(init, pd) + k * a["d"] / ts
                            break
                    if start is not None:
                        pass
                    elif g.get("n"):
                        start = (int(g["n"]) - a.get("start_number", 1)) * a["d"] / ts
                    elif g.get("t"):
                        start = int(g["t"]) / ts
                    else:
                        return self._send(404, "segment not in the buffer", "text/plain")
                    st["missing"] = st.get("missing", 0) + 1
                    return self._send_bytes(tc.silence(start, a["d"] / ts), "audio/mp4")
                ts = a["timescale"] or 1
                start = int(m.group("t")) / ts if m.groupdict().get("t") else media_start_s(init, media)
                dur = a["d"] / ts
                try:
                    data = tc.segment(init, media, start, dur)
                except NoAudio:
                    # no I-frame at the start of this segment: decode it after the previous ones
                    prev = []
                    for k in (1, 2):
                        pn = prev_segment_name(a, m, k)
                        pd = fetch(pn) if pn else None
                        if pd is None:
                            break
                        prev.insert(0, pd)
                    try:
                        data = tc.segment(init, media, start, dur, prev) if prev else None
                    except (NoAudio, RuntimeError):
                        data = None
                    if data is None:
                        data = tc.silence(start, dur)
                        st["silent"] = st.get("silent", 0) + 1
                        st["last_error"] = f"undecodable AC-4 in {name}: sent silence"
                        if st["silent"] in (1, 10, 100) or st["silent"] % 500 == 0:
                            log(chan["title"], "!!", f"{st['last_error']} ({st['silent']} so far)")
                tc.remember(key, data)
                st["ok"] += 1
                return self._send_bytes(data, "audio/mp4")
            return self._send(404, "not an AC-4 segment of this channel", "text/plain")
        except urllib.error.HTTPError as e:
            st["fail"] += 1
            st["last_error"] = f"origin HTTP {e.code} for {name}"
            log(chan["title"], "!!", st["last_error"])
            return self._send(e.code, f"origin returned {e.code}", "text/plain")
        except Exception as e:
            st["fail"] += 1
            st["last_error"] = f"{type(e).__name__}: {e}"
            log(chan["title"], "!!", f"AC-4 transcode failed for {name}: {st['last_error']}")
            return self._send(502, f"transcode failed: {e}", "text/plain")

    def bcast(self, lv, sid, what, seg):
        b = lv.bcast.get(sid)
        if not b:
            return self._send(404, "unknown service (or no capture report yet)", "text/plain")
        if b["encrypted"]:
            return self._send(403, "encrypted (DRM) service", "text/plain")
        if what == "watch":
            return self.watch_page(b["title"], f"from the live capture (RF {lv.cfg['rf']}), "
                                   f"{lv.cfg['buffer_s'] // 60}-minute buffer", f"/bcast/{sid}/manifest.mpd")
        if what == "manifest.mpd":
            return self._send(200, lv.mpd(sid, self.headers.get("Host", "localhost")), "application/dash+xml")
        if seg.startswith(FLAC_PREFIX):
            return self.flac_segment({"title": f"bcast{sid}:", "ac4": b["ac4"]}, seg[len(FLAC_PREFIX):],
                                     fetch=lambda n: lv.fetch(sid, n))
        data = lv.fetch(sid, seg)
        if data is None:
            return self._send(404, "segment not in the buffer", "text/plain")
        ctype = "video/mp4" if ".mp4v" in seg or "video" in seg else "audio/mp4" if ".mp4a" in seg else "application/mp4"
        return self._send_bytes(data, ctype, cache="max-age=600")

    def app_scaler_zip(self):
        import io, zipfile
        cands = []
        for here in app_dirs():
            cands += [os.path.join(here, "app-scaler"), os.path.join(here, "..", "app-scaler"),
                      os.path.join(here, "..", "share", "hdhr-render-pi", "app-scaler")]
        for d in cands + ["/usr/local/share/hdhr-render-pi/app-scaler"]:
            if os.path.isfile(os.path.join(d, "manifest.json")):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(os.listdir(d)):
                        z.write(os.path.join(d, f), "ba_probe-app-scaler/" + f)
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", "attachment; filename=ba_probe-app-scaler.zip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
        return self._send(404, "app-scaler extension files not found (the app-scaler folder)", "text/plain")

    def app_view(self, i):
        """The app at its native TV size (1920x1080 unless ?w=&h=) in a frame
        scaled to fit the window, so it follows the browser size."""
        if i >= len(self.server.apps):
            return self._send(404, "unknown app", "text/plain")
        a = self.server.apps[i]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        w = int((q.get("w") or ["1920"])[0]); h = int((q.get("h") or ["1080"])[0])
        host = self.headers.get("Host", f"localhost:{self.server.args.port}")
        entry = a["bband_url"] or a["bcast_url"]
        url = launch_url(entry, f"ws://{host}/app/{i}")
        body = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{html.escape(a['title'])}</title><style>
html,body{{margin:0;height:100%;background:#000;overflow:hidden;font:13px system-ui,sans-serif;color:#ccc}}
#stage{{position:absolute;left:0;top:0;right:0;bottom:30px}}
#app{{position:absolute;width:{w}px;height:{h}px;border:0;transform-origin:0 0;background:#000}}
#bar{{position:absolute;left:0;right:0;bottom:0;height:30px;display:flex;align-items:center;gap:14px;padding:0 10px;background:#111}}
#bar a,#bar button{{color:#8ab4ff;background:none;border:0;font:inherit;cursor:pointer;padding:0}}
#bar .sp{{flex:1}}
</style></head><body>
<div id='stage'><iframe id='app' src='{html.escape(url, quote=True)}' allow='autoplay; fullscreen; encrypted-media' allowfullscreen></iframe></div>
<div id='bar'><strong>{html.escape(a['title'])}</strong><span id='sz'></span>
<span class='sp'></span><span>Click the app, then arrows / Enter = OK / Esc = Back</span>
<button onclick="document.documentElement.requestFullscreen()">Fullscreen</button>
<a href='/log' target='_blank'>request log</a>
<a href='{html.escape(url, quote=True)}' title='if the app stays blank, it refuses to run inside a frame'>open directly</a></div>
<script>
const W = {w}, H = {h}, f = document.getElementById('app'), st = document.getElementById('stage');
function fit() {{
  const sw = st.clientWidth, sh = st.clientHeight, k = Math.min(sw / W, sh / H);
  f.style.transform = 'scale(' + k + ')';
  f.style.left = Math.round((sw - W * k) / 2) + 'px'; f.style.top = Math.round((sh - H * k) / 2) + 'px';
  document.getElementById('sz').textContent = W + 'x' + H + ' at ' + Math.round(k * 100) + '%';
}}
addEventListener('resize', fit); document.addEventListener('fullscreenchange', fit); fit();
f.addEventListener('load', () => f.focus());
</script></body></html>"""
        self._send(200, body)

    def watch_page(self, chan, source=None, manifest=None, status_url=None):
        if isinstance(chan, dict):
            title, source, manifest = chan["title"], f"from {chan['base_url']}{VIA_TEXT}", f"/live/{chan['service_id']}/manifest.mpd"
        else:
            title = chan
        dash_src = "/static/dash.all.min.js" if self.server.args.dashjs else \
                   "https://cdn.jsdelivr.net/npm/dashjs@4.7.4/dist/dash.all.min.js"
        body = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>
<style>{PAGE_CSS} body{{background:#000;margin:0}} video{{width:100%;max-height:92vh;background:#000;display:block}}
#bar{{color:#ccc;font:13px monospace;padding:6px 10px;background:#111}} #err{{color:#ff6b6b}}</style></head><body>
<div style='position:relative'><video id='v' controls autoplay muted playsinline></video>
<div id='ttml' style='position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none'></div></div>
<div id='unmute' style='display:none;position:fixed;top:12px;right:12px'><a class=btn href='#' onclick="soundOn();this.parentNode.style.display='none';return false">&#128264; Click for sound</a></div>
<div id='bar'><strong>{html.escape(title)}</strong> &middot; {html.escape(source)}
 &middot; <span id='st'>starting&hellip;</span> <span id='err'></span>
 <span style='float:right;white-space:nowrap' title='Audio boost (a limiter keeps loud parts from distorting). Remembered.'>&#128266; boost
 <input type='range' id='bst' min='0' max='800' step='10' style='width:140px;vertical-align:middle'> <span id='bstv'></span></span></div>
<script src='{dash_src}'></script><script>
const p = dashjs.MediaPlayer().create();
p.updateSettings({{streaming: {{delay: {{liveDelay: 10}}, text: {{defaultEnabled: true}},
  gaps: {{jumpGaps: true, jumpLargeGaps: true, smallGapLimit: 1.5}},
  retryAttempts: {{MediaSegment: 4}}, retryIntervals: {{MediaSegment: 700}}}}}});
let errAt = 0, errT = -1;
// Audio boost: video -> gain -> limiter -> speakers. Broadcast AC-4 is mixed quiet.
const BKEY = 'ba_probe_boost_' + ({json.dumps("bcast" if (manifest or "").startswith("/bcast") else "net")});
let boost = 100, actx = null, gainN = null;
try {{ boost = parseInt(localStorage.getItem(BKEY) || '{200 if (manifest or "").startswith("/bcast") else 100}', 10); }} catch (x) {{}}
const bst = document.getElementById('bst'), bstv = document.getElementById('bstv');
function showBoost() {{ bst.value = boost; bstv.textContent = boost + '%'; }}
function audioGraph() {{          // needs a click (browsers start audio only after one)
  if (actx) {{ actx.resume(); return; }}
  try {{
    actx = new (window.AudioContext || window.webkitAudioContext)();
    const src = actx.createMediaElementSource(document.getElementById('v'));
    gainN = actx.createGain();
    const lim = actx.createDynamicsCompressor();
    lim.threshold.value = -2; lim.knee.value = 0; lim.ratio.value = 20; lim.attack.value = 0.002; lim.release.value = 0.2;
    src.connect(gainN); gainN.connect(lim); lim.connect(actx.destination);
    gainN.gain.value = boost / 100;
  }} catch (x) {{ actx = null; }}
}}
function soundOn() {{ const v = document.getElementById('v'); audioGraph(); v.muted = false; v.play(); }}
bst.addEventListener('input', () => {{
  boost = parseInt(bst.value, 10); showBoost(); audioGraph();
  if (gainN) gainN.gain.value = boost / 100;
  try {{ localStorage.setItem(BKEY, String(boost)); }} catch (x) {{}}
}});
document.addEventListener('click', () => audioGraph(), true);   // also covers unmuting with the player's own button
showBoost();
p.initialize(document.getElementById('v'), {json.dumps(manifest)}, true);
p.attachTTMLRenderingDiv(document.getElementById('ttml'));
const STATUS = {json.dumps(status_url)};
if (STATUS) setInterval(async () => {{
  try {{
    const s = await (await fetch(STATUS)).json();
    if (s.state === 'error') {{ document.getElementById('err').textContent = s.error + ' (reload to retry)'; p.reset(); }}
    document.title = {json.dumps(title)} + (s.info ? ' (' + s.info + ')' : '');
  }} catch (x) {{}}
}}, 3000);
p.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, () => {{
  if ((p.getTracksFor('audio') || []).length) document.getElementById('unmute').style.display = 'block';
  if (!(p.getTracksFor('video') || []).length) {{
    document.getElementById('err').textContent = "This browser cannot play the video track (HEVC needs Chrome or Edge with a GPU that decodes HEVC); audio only.";
  }}
}});
p.on(dashjs.MediaPlayer.events.ERROR, e => {{
  document.getElementById('err').textContent = 'Error: ' + (e.error && (e.error.message || e.error.code) || JSON.stringify(e).slice(0,200));
  errAt = Date.now(); errT = document.getElementById('v').currentTime;
}});
setInterval(() => {{   // a passing problem: clear the message once playback has moved on
  const v = document.getElementById('v');
  if (errAt && Date.now() - errAt > 5000 && v.currentTime > errT + 3) {{ document.getElementById('err').textContent = ''; errAt = 0; }}
}}, 1000);
setInterval(() => {{
  try {{
    const q = p.getQualityFor('video'), list = p.getBitrateInfoListFor('video') || [], b = list[q];
    const v = document.getElementById('v');
    document.getElementById('st').textContent =
      (b ? b.width + 'x' + b.height + ' @ ' + Math.round(b.bitrate/1000) + ' kbps' : '') +
      ' | buffer ' + p.getBufferLength('video').toFixed(1) + ' s | live delay ' + (p.getCurrentLiveLatency() || 0).toFixed(1) + ' s' +
      (v.videoWidth ? ' | decoding ' + v.videoWidth + 'x' + v.videoHeight : '') +
      ((p.getTracksFor('audio') || []).length ? ' | audio buffer ' + p.getBufferLength('audio').toFixed(1) + ' s' + (v.muted ? ' (muted)' : '') : ' | no audio');
  }} catch (x) {{}}
}}, 1000);
</script></body></html>"""
        self._send(200, body)

    def log_page(self):
        body = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Request Log</title>"
                f"<style>{PAGE_CSS} td.d{{white-space:nowrap}}</style></head><body><div class='c'><h1>Request Log</h1>"
                "<p class='muted'>Newest first, refreshes every 2 s. <code>&rarr;</code> = from the app, <code>&larr;</code> = " + html.escape(PROBE_WORD) + "'s answer, <code>!!</code> = method " + html.escape(PROBE_WORD) + " doesn't know.</p>"
                "<table id='t'><tr><th>Time</th><th>App</th><th></th><th>Message</th></tr></table></div><script>"
                "async function r(){try{const l=await (await fetch('/log.json')).json();const t=document.getElementById('t');"
                "t.innerHTML='<tr><th>Time</th><th>App</th><th></th><th>Message</th></tr>'+l.slice().reverse().map(e=>"
                "'<tr><td class=d>'+e.t+'</td><td class=d>'+e.app+'</td><td class=d>'+e.dir+'</td><td><pre>'+"
                "(typeof e.msg==='string'?e.msg:JSON.stringify(e.msg,null,1)).replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</pre></td></tr>').join('')}catch(x){}}"
                "r();setInterval(r,2000)</script></body></html>")
        self._send(200, body)

    def websocket(self, path):
        m = re.match(r"^/app/(\d+)(/.*)?$", path)
        if not m or int(m.group(1)) >= len(self.server.apps):
            return self._send(404, "unknown app", "text/plain")
        app = self.server.apps[int(m.group(1))]
        endpoint = (m.group(2) or "/").strip("/") or "(root)"
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        proto = self.headers.get("Sec-WebSocket-Protocol")
        if proto:
            self.send_header("Sec-WebSocket-Protocol", proto.split(",")[0].strip())
        self.end_headers()
        name = app["title"]
        log(name, "**", f"WebSocket opened: {endpoint} (Origin: {self.headers.get('Origin')})")
        recv = Receiver(app, self.server.args, self.server)
        self.connection.settimeout(None)
        try:
            while True:
                op, data = ws_recv(self.rfile)
                if op is None or op == 8:
                    break
                if op == 9:
                    ws_send(self.wfile, data, 10)
                    continue
                if op != 1:
                    continue
                text = data.decode("utf-8", "replace")
                try:
                    msg = json.loads(text)
                except ValueError:
                    log(name, "->", text)
                    continue
                log(name, "->", msg)
                if endpoint != "atscCmd":
                    continue
                batch = msg if isinstance(msg, list) else [msg]
                replies = []
                for req in batch:
                    if not isinstance(req, dict) or "method" not in req:
                        continue
                    result, error = recv.handle(req["method"], req.get("params"))
                    if "id" not in req:
                        continue  # notification: no reply
                    reply = {"jsonrpc": "2.0", "id": req["id"]}
                    if error:
                        reply["error"] = error
                    else:
                        reply["result"] = result
                    replies.append(reply)
                if replies:
                    out = replies if isinstance(msg, list) else replies[0]
                    log(name, "<-", out)
                    ws_send(self.wfile, json.dumps(out))
        except (ConnectionError, OSError):
            pass
        log(name, "**", f"WebSocket closed: {endpoint}")

def running_probe(pidfile):
    """(pid, port) of a live-mode ba_probe that is still running, else None"""
    if os.name == "nt":                 # no live mode on Windows (and os.kill(pid, 0) means Ctrl-C there)
        return None
    try:
        pid, port = open(pidfile).read().split()[:2]
        pid, port = int(pid), int(port)
        os.kill(pid, 0)
        if "ba_probe" not in open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace"):
            return None
        return pid, port
    except (OSError, ValueError):
        return None

def main():
    global LOG_FILE
    conf = load_conf()
    ap = argparse.ArgumentParser(description="ATSC 3.0 live receiver and broadcaster-app probe")
    ap.add_argument("report", nargs="?", help="a RENDER .html report to probe instead of capturing live")
    ap.add_argument("--hdhr", default=conf.get("HDHR_IP", "192.168.1.199"), help="HDHomeRun address (HDHR_IP)")
    ap.add_argument("--tuner", type=int, default=int(conf.get("LIVE_TUNER", "0")), help="tuner (LIVE_TUNER, default 0)")
    ap.add_argument("--rf", type=int, default=int(conf.get("LIVE_RF", "15")), help="RF channel (LIVE_RF, default 15)")
    ap.add_argument("--plp", default=conf.get("LIVE_PLPS", "0,1"), help="PLPs (LIVE_PLPS, default 0,1)")
    ap.add_argument("--buffer", type=int, default=int(conf.get("LIVE_BUFFER_MIN", "10")),
                    help="minutes kept for rewinding and of captures/reports (LIVE_BUFFER_MIN, default 10)")
    ap.add_argument("--chunk", type=int, default=int(conf.get("LIVE_CHUNK_S", "60")),
                    help="seconds per capture file sent to RENDER (LIVE_CHUNK_S, default 60)")
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser(conf.get("CAPTURE_DIR", "~/hdhr-captures")), "live"),
                    help="where live captures, reports and segments go (default CAPTURE_DIR/live)")
    ap.add_argument("--keep-pcap", action="store_true", default=conf.get("LIVE_KEEP_PCAP", "no").lower() == "yes",
                    help="keep each 60-second capture for the buffer time (LIVE_KEEP_PCAP=yes); "
                         "by default it is deleted once RENDER has made its report")
    ap.add_argument("--port", type=int, default=8989)
    ap.add_argument("--stop", action="store_true", help="stop the ba_probe that is running in the background")
    ap.add_argument("--status", action="store_true", help="show whether ba_probe is running and how the capture is doing")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1: reach it through an SSH tunnel)")
    ap.add_argument("--strict", action="store_true", help="answer unknown methods with an error")
    ap.add_argument("--log", default="ba_probe.log.jsonl", help="also write requests here (JSON lines)")
    ap.add_argument("--check-live", action="store_true",
                    help="(with a report) test whether the internet channels' servers answer from here, then exit")
    ap.add_argument("--keep-ac4", action="store_true", help="leave AC-4 audio in MPDs (for players that decode it)")
    ap.add_argument("--ffmpeg-ac4", metavar="PATH",
                    help="ffmpeg with the AC-4 decoder (default: ffmpeg-ac4 on PATH, /usr/local/bin/ffmpeg-ac4, then ffmpeg)")
    ap.add_argument("--no-audio", action="store_true", help="don't convert AC-4; play without sound")
    ap.add_argument("--render-bin", default=conf.get("RENDER_BIN", ""), help="RENDER binary")
    ap.add_argument("--dashjs", help="serve this local dash.all.min.js instead of loading dash.js from jsDelivr")
    args = ap.parse_args()

    pidfile = os.path.join(os.path.abspath(args.dir), "ba_probe.pid")
    running = running_probe(pidfile)
    if args.stop:
        if not running:
            sys.exit("ba_probe isn't running.")
        os.kill(running[0], signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.2)
            if not running_probe(pidfile):
                print(f"Stopped ba_probe (pid {running[0]}); the tuner stream is closed.")
                return
        sys.exit(f"ba_probe (pid {running[0]}) didn't stop; try: kill {running[0]}")
    if args.status:
        if not running:
            sys.exit("ba_probe isn't running.")
        print(f"ba_probe is running (pid {running[0]}) on port {running[1]}.")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{running[1]}/state.json", timeout=5) as r:
                d = json.load(r)
            c = d["capture"]
            print(f"  capture: {c['state']}{' - ' + c['error'] if c['error'] else ''}, {c['mbps']} Mbps, "
                  f"{c['chunks']} captures, {c['reconnects']} reconnects, running {c['running_s'] // 60} min")
            print(f"  segments rebuilt {c['objects']}, incomplete {c['lost_objects']}; guide: {d['guide_programs']} programs; "
                  f"live folder {d.get('disk_mb', 0)} MB")
            for b in d["broadcast"]:
                if not b["encrypted"]:
                    print(f"  {b['title']}: {b['buffer_s'] // 60} min {b['buffer_s'] % 60} s buffered")
        except Exception as e:
            print(f"  (couldn't read its status: {e})")
        return
    if running and not args.report:
        print(f"ba_probe is already running (pid {running[0]}), capturing and serving on port {running[1]}.")
        print(f"  Just open the tunnel and browse: ssh -L {running[1]}:localhost:{running[1]} <you>@<pi>, "
              f"then http://localhost:{running[1]}/")
        print("  ba_probe --status shows how it's doing; ba_probe --stop stops it.")
        return

    ff = None if (args.no_audio or args.keep_ac4) else find_ac4_ffmpeg(args.ffmpeg_ac4)
    apps = apps_from_report(args.report) if args.report else []
    live = live_channels_from_report(args.report) if args.report else []
    if args.report:
        print(f"Apps found in {os.path.basename(args.report)}:")
        for i, a in enumerate(apps):
            print(f"  [{i}] {a['title']}: {a['bband_url'] or a['bcast_url']}")
        if live:
            print("Live (internet-delivered) channels:")
            for c in live:
                print(f"  {c['title']}: {c['base_url']}")
        slt0 = slt_from_report(args.report)
        own = {id(a["slt"]) for a in apps if a.get("slt")}
        if len(own) > 1:
            print(f"Service lists (SLT): one per market ({len(own)}); each app gets its own market's")
        else:
            print("Service list (SLT): " + (f"found, {slt0['xml'].count('<Service ')} services; apps asking for "
                  "signaling get it" if slt0 else "not in this report; apps asking for signaling get an empty list"))

    if args.check_live:
        ok_all = True
        for c in live:
            init, seg = latest_segment_url(c, time.time())
            for label, url in (("init", init), ("latest segment", seg)):
                try:
                    with fetch_origin(url) as r:
                        n = len(r.read())
                    print(f"  OK   {c['title']}: {label} {r.status}, {n} bytes  ({url})")
                except urllib.error.HTTPError as e:
                    ok_all = False
                    print(f"  FAIL {c['title']}: {label} HTTP {e.code}  ({url})")
                except Exception as e:
                    ok_all = False
                    print(f"  FAIL {c['title']}: {label} {type(e).__name__}: {e}  ({url})")
        sys.exit(0 if ok_all else 1)

    LOG_FILE = open(args.log, "a", encoding="utf-8")
    livestate = None
    if not args.report:
        render = os.path.abspath(os.path.expanduser(args.render_bin or shutil.which("render") or "/usr/local/bin/render"))
        if not os.access(render, os.X_OK):
            sys.exit(f"RENDER not found ({render}); use --render-bin")
        cfg = {"hdhr": args.hdhr, "tuner": args.tuner, "rf": args.rf, "plps": args.plp,
               "buffer_s": args.buffer * 60, "chunk_s": args.chunk, "dir": os.path.abspath(args.dir),
               "render": render, "ffmpeg": ff, "keep_pcap": args.keep_pcap}
        livestate = LiveState(cfg)          # starts the capture process before any server threads exist
    try:
        srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as e:
        if livestate:
            livestate.stop()
        sys.exit(f"Port {args.port} is in use ({e.strerror}). Another ba_probe? Try: ba_probe --status, "
                 f"or pick another port with --port.")
    srv.daemon_threads = True
    srv.apps, srv.args = apps, args
    srv.slt = slt_from_report(args.report) if args.report else None
    srv.live, srv.live_stats = live, {}
    srv.ac4 = Ac4Transcoder(ff) if ff and (not args.report or any(c["ac4"] for c in live)) else None
    srv.livestate = livestate
    g = guide_for_report(args.report) if args.report else ""
    srv.guide = g if g and os.path.isfile(g) else None
    if args.report:
        if srv.guide:
            print(f"TV guide: {os.path.basename(g)} (http://localhost:{args.port}/guide)")
    else:
        srv.livestate.start_worker(srv)
        print(f"Live: capturing {capture_url(srv.livestate.cfg)} (in its own process)")
        print(f"  {args.chunk}-second captures go through RENDER; the ESG builds up as they come in.")
        print(f"  {args.buffer}-minute buffer of captures, reports and segments in {srv.livestate.cfg['dir']}")
        print("  AC-4 audio: " + (f"converted with {ff}" if ff else "no ffmpeg-ac4 found, so no sound (sudo ./install.sh --with-ac4)"))
    print(f"\nProbe listening on {args.bind}:{args.port}. Open http://localhost:{args.port}/ "
          f"(via SSH tunnel if remote). Requests are logged here and to {args.log}. Ctrl-C to stop.\n")
    def _quit(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _quit)       # ba_probe --stop, systemctl stop, kill
    if livestate:
        # live mode keeps capturing when the SSH session drops: reopen the tunnel and carry on
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with open(pidfile, "w") as f:
            f.write(f"{os.getpid()} {args.port}\n")
    elif hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _quit)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        say("\nStopped.")
    finally:
        if srv.livestate:
            srv.livestate.stop()
            try:
                os.remove(pidfile)
            except OSError:
                pass

if __name__ == "__main__":
    main()
