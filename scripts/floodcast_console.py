#!/usr/bin/env python3
"""Live, coloured console for the floodcast loop (run it in a terminal next to the loop).

    python scripts/floodcast_console.py            # defaults: run/ next to this repo

Top: loop / caffeinate status, forecast on the site, next expected forecast and the three stages
(rain forecasts, hydrology, flood maps) as progress bars with expected finish times, read from
progress.json. Below: the latest lines of the persistent operational.log, coloured by step. Redraws every
second; Ctrl-C quits (the loop keeps running).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "run"
LOG = RUN / "data" / "operational" / "alcacer_do_sal" / "logs" / "operational.log"   # persistent (master.log restarts empty)
DATA = RUN / "data" / "operational" / "alcacer_do_sal"

R = "\033[0m"
B = "\033[1m"
DIM = "\033[2m"
C = {"rain": "\033[38;5;45m", "hyd": "\033[38;5;77m", "flood": "\033[38;5;177m", "pub": "\033[38;5;75m",
     "warn": "\033[38;5;214m", "err": "\033[38;5;203m", "ok": "\033[38;5;83m", "head": "\033[48;5;24m\033[38;5;231m",
     "new": "\033[48;5;25m\033[38;5;231m", "grey": "\033[38;5;245m", "alert": "\033[38;5;203m", "warning": "\033[38;5;214m",
     "watch": "\033[38;5;220m", "none": "\033[38;5;83m"}
STAGES = [("precip", "1  Rain forecasts", "rain"), ("hydrology", "2  Hydrology (SUMMA-mizuRoute)", "hyd"),
          ("hydraulics", "3  Flood maps (FLUXOS)", "flood")]
LEVEL_PT = {"alert": "ALERTA", "warning": "AVISO", "watch": "VIGILÂNCIA", "none": "SEM RISCO", "unknown": "SEM DADOS"}


def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def local(iso: str | None, fmt: str = "%H:%M") -> str:
    if not iso:
        return "–"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except ValueError:
        return "–"


def alive(pidfile: Path) -> int | None:
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def caffeinated() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "caffeinate"], capture_output=True).returncode == 0
    except OSError:
        return False


def bar(pct: float, width: int, colour: str, state: str) -> str:
    n = int(round(width * max(0.0, min(100.0, pct)) / 100))
    if state == "done":
        return colour + "█" * width + R
    if state in ("skipped", "pending"):
        return DIM + "·" * width + R
    if state == "failed":
        return C["err"] + "█" * n + "░" * (width - n) + R
    # running: solid part + a moving shimmer on the next cell
    tick = int(time.time() * 2) % 2
    head = "▓" if tick else "▒"
    return colour + "█" * n + (head if n < width else "") + R + DIM + "░" * max(0, width - n - 1) + R


def colour_line(line: str) -> str:
    s = re.sub(r"^(\d{4}-\d\d-\d\d )(\d\d:\d\d:\d\d),\d+ (INFO|WARNING|ERROR) [\w.]+: ", r"\2 \3 ", line.rstrip())
    m = re.match(r"(\d\d:\d\d:\d\d) (INFO|WARNING|ERROR) (.*)", s)
    if not m:
        return DIM + s + R
    t, lvl, msg = m.groups()
    ts = C["grey"] + t + R + " "
    if lvl == "ERROR" or "FAILED" in msg or "crashed" in msg:
        return ts + C["err"] + B + "✖ " + msg + R
    if lvl == "WARNING":
        return ts + C["warn"] + "▲ " + msg + R
    if msg.startswith("NEW FORECAST"):
        return ts + C["new"] + B + " ▶ " + msg + " " + R
    if msg.startswith("===="):
        return ts + DIM + "─" * 40 + R
    for key, col in (("1/3", "rain"), ("2/3", "hyd"), ("3/3", "flood")):
        if key in msg:
            mark = "▶" if msg.startswith(">") else ("✔" if msg.startswith("<") else ("–" if msg.startswith("-") else "·"))
            return ts + C[col] + mark + " " + msg.lstrip("<>- ").strip() + R
    if "GitHub" in msg or "publishing online" in msg or "Cloudflare" in msg:
        return ts + C["pub"] + "⇪ " + msg + R
    if "complete" in msg and "not complete" not in msg:
        return ts + C["ok"] + B + "✔ " + msg + R
    if "not complete" in msg:
        return ts + C["warn"] + "• " + msg + R
    if msg.startswith("waiting:"):
        return ts + DIM + "⏳ " + msg[8:].strip() + R
    if "loop started" in msg or "auto-update" in msg:
        return ts + B + "⟳ " + msg + R
    if msg.startswith("cycle "):
        return ts + B + "■ " + msg + R
    return ts + msg


def tail(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ["(no log yet)"]
    keep = [ln for ln in lines if not ln.startswith(("Traceback", "  File", "    "))]
    return keep[-n:]


def screen() -> str:
    cols, rows = shutil.get_terminal_size((100, 32))
    width = min(cols, 120)
    out = []
    now = datetime.now().strftime("%H:%M:%S")
    loop = alive(RUN / "master.pid")
    title = f" FLOODCAST · Alcácer do Sal "
    status = (C["ok"] + "● loop running" + R if loop else C["err"] + "● loop STOPPED" + R) + "   " + \
             (C["ok"] + "● caffeinate" + R if caffeinated() else C["warn"] + "○ no caffeinate" + R)
    out.append(C["head"] + B + title + R + "  " + status + "   " + C["grey"] + now + R)

    latest = read_json(DATA / "latest.json") or {}
    lvl = latest.get("level", "unknown")
    cyc = latest.get("cycle")
    lines = tail(LOG, 400)
    last_wait = next((ln for ln in reversed(lines) if "waiting:" in ln), "")
    nxt = re.search(r"next run (\d\d) UTC expected ~(\d\d:\d\d)", last_wait)
    chk = re.search(r"next check (\d\d:\d\d)", last_wait)
    out.append("On the site: " + B + (local(cyc, "%d %b %H:%M") if cyc else "–") + R + "  " +
               C.get(lvl, "") + B + LEVEL_PT.get(lvl, lvl.upper()) + R +
               (f"    next ECMWF run {nxt.group(1)} UTC ≈ {B}{nxt.group(2)}{R}" if nxt else "") +
               (f"    next check {chk.group(1)}" if chk else ""))
    out.append(DIM + "─" * width + R)

    prog = read_json(DATA / "progress.json") or {}
    running = prog.get("state") == "running"
    bw = max(10, min(40, width - 60))
    if running:
        out.append(B + f"Computing forecast {local(prog.get('cycle'), '%d %b %H:%M')} UTC-run" + R +
                   (f"   updated results expected ≈ {B}{local(prog.get('results_expected_utc'))}{R}"
                    if prog.get("results_expected_utc") else ""))
    else:
        fin = prog.get("finished_utc")
        out.append(DIM + ("Idle — last chain finished " + local(fin, "%d %b %H:%M") if fin else "Idle — waiting for a new forecast") + R)
    for key, label, col in STAGES:
        st = (prog.get("stages") or {}).get(key) or {}
        state = st.get("state", "pending") if prog else "pending"
        pct = float(st.get("pct", 0) or 0)
        if state == "done":
            right = C["ok"] + f"✔ done {local(st.get('finished_utc'))}" + R
        elif state == "running":
            right = f"{pct:3.0f}%  ETA {B}{local(st.get('eta_utc'))}{R}  {DIM}{st.get('detail', '')}{R}"
        elif state == "failed":
            right = C["err"] + "✖ failed" + R
        elif state == "skipped":
            right = DIM + "not needed" + R
        else:
            right = DIM + "waiting" + R
        out.append(f"{C[col]}{label:<32}{R} {bar(pct, bw, C[col], state)}  {right}")
    out.append(DIM + "─" * width + R)

    room = max(5, rows - len(out) - 1)
    for ln in tail(LOG, room):
        out.append(colour_line(ln)[: width + 40])
    return "\n".join(out)


def main() -> int:
    sys.stdout.write("\033[?25l")   # hide cursor
    try:
        while True:
            frame = screen()
            sys.stdout.write("\033[H\033[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h" + R + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
