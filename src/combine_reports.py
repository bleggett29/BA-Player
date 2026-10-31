#!/usr/bin/env python3
# BA Player - ATSC 3.0 broadcaster apps on Windows
# Copyright (C) 2026 Brian Scott
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software under the GNU GPL v3 or later; see LICENSE.txt.
"""combine_reports - merge RENDER reports from several markets into one report for ba_probe.

    combine_reports -o combined.html DC=dc.html PHX=phx-kasw.html LV=lv.html ...
    ba_probe combined.html

Keeps only the services ba_probe can use from a report: those with an interactive app
and the internet-delivered channels. Broadcast-only services are left out. Each kept
service is renumbered so numbers from different markets don't clash, gets its market's
label in front of its name, and carries its own market's Service List Table (SLT), so an
app asking for signaling gets the stations of its own market.
"""
import argparse, datetime, html, os, re, sys


def sections(s):
    """(service number, h2 inner html, body) for each service in a RENDER report."""
    out = []
    ms = list(re.finditer(r'<h2 id="service_(\d+)">', s))
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(s)
        nxt = re.search(r"<h2[ >]", s[m.end():end])          # stop at the report's next section
        if k + 1 == len(ms) and nxt:
            end = m.end() + nxt.start()
        inner = re.match(r"(.*?)</h2>", s[m.end():end], re.S)
        if not inner:
            continue
        out.append((m.group(1), inner.group(1), s[m.end() + inner.end():end]))
    return out


def slt_block(s):
    m = re.search(r"<details><summary>Service List Table \(SLT\)(?:(?!</summary>).)*Raw XML</summary>.*?</details>", s, re.S)
    return m.group(0) if m else ""


def report_time(s):
    m = re.search(r"Time:\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4} [0-9:]+)", html.unescape(re.sub(r"<[^>]+>", " ", s)))
    return m.group(1) if m else ""


def useful(body):
    """True for a service with an app or an internet-delivered MPD."""
    if "<HTMLEntryPackage" in html.unescape(body):
        return True
    for pre in re.findall(r"<pre>(.*?)</pre>", body, re.S):
        x = html.unescape(pre)
        if "<MPD" in x[:600] and re.search(r"<BaseURL>\s*https?://", x):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reports", nargs="+", help="LABEL=report.html (or just report.html; the label is then the file name)")
    ap.add_argument("-o", "--out", default="combined.html")
    ap.add_argument("--skip", action="append", default=[], metavar="LABEL:SERVICE[:stream]",
                    help="leave out a service (e.g. \"DC:3\"), or only its internet stream and keep its app "
                         "(\"Las Vegas:5007:stream\"); can be repeated")
    ap.add_argument("--no-encrypted-label", action="store_true", help="drop \"Encrypted\" from service names")
    a = ap.parse_args()
    combine(a.reports, a.out, skip=a.skip, drop_encrypted=a.no_encrypted_label)


def strip_stream(body):
    """Remove internet-delivered MPDs from a service section (keeps its app)."""
    def drop(m):
        x = html.unescape(m.group(1))
        return "" if "<MPD" in x[:600] and re.search(r"<BaseURL>\s*https?://", x) else m.group(0)
    return re.sub(r"<pre>(.*?)</pre>", drop, body, flags=re.S)


def combine(reports, out, say=print, skip=(), drop_encrypted=False):
    """reports: ["LABEL=path", ...] or plain paths. Writes the combined report to out;
    returns the number of services kept. skip: "LABEL:SERVICE" leaves a service out,
    "LABEL:SERVICE:stream" leaves out only its internet stream."""
    drop_all = {tuple(x.rsplit(":", 1)) for x in skip if not x.endswith(":stream")}
    drop_stream = {tuple(x[:-7].rsplit(":", 1)) for x in skip if x.endswith(":stream")}
    parts, index = [], []
    for n, spec in enumerate(reports, 1):
        label, _, path = spec.rpartition("=")
        label = label or os.path.splitext(os.path.basename(path))[0]
        s = open(path, encoding="utf-8", errors="replace").read()
        slt = slt_block(s)
        bsid = re.search(r"SLT BSID:\s*(\d+)", s)
        when = report_time(s)
        kept = []
        for sid, inner, body in sections(s):
            if (label, sid) in drop_all:
                continue
            if (label, sid) in drop_stream:
                body = strip_stream(body)
            if not useful(body):
                continue
            if drop_encrypted:
                inner = re.sub(r"\s*\bEncrypted\b", "", inner)
            new = n * 100000 + int(sid)
            parts.append(f'<h2 id="service_{new}">{html.escape(label)} &middot; {inner}</h2>\n'
                         f'<p class="ba-origin">From {html.escape(os.path.basename(path))}'
                         f'{" (" + html.escape(when) + ")" if when else ""}, service {sid}.</p>\n'
                         f"{body}\n{slt}\n")
            kept.append(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip())
        index.append((label, os.path.basename(path), bsid.group(1) if bsid else "?", when, kept, bool(slt)))
        say(f"{label}: {len(kept)} services from {path}" + ("" if slt else "  (no SLT in this report)"))

    rows = "".join(f"<tr><td>{html.escape(l)}</td><td>{html.escape(f)}</td><td>{b}</td><td>{html.escape(w)}</td>"
                   f"<td>{html.escape(', '.join(k)) or '-'}</td></tr>" for l, f, b, w, k, _ in index)
    page = f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><title>Combined RENDER reports</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:16px}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left}}table{{border-collapse:collapse}}</style></head><body>
<h1>Combined RENDER reports</h1>
<p>Made by combine_reports on {datetime.date.today().isoformat()} for ba_probe. Only services with an app or an internet-delivered channel are kept.</p>
<table><tr><th>Label</th><th>Report</th><th>BSID</th><th>Captured</th><th>Services kept</th></tr>{rows}</table>
{''.join(parts)}
</body></html>
"""
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    say(f"Wrote {out}")
    return sum(len(k) for _, _, _, _, k, _ in index)


if __name__ == "__main__":
    sys.exit(main())
