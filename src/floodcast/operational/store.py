"""Output store implementing docs/operational/outputs.md (atomic, crash-safe)."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEVELS = ("none", "watch", "warning", "alert")
LEVEL_LABELS = {
    "none": "No action",
    "watch": "Watch",
    "warning": "Warning",
    "alert": "Alert",
    "unknown": "Status unknown",
}


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(UTC)


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    tmp.replace(path)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def max_level(*levels: str) -> str:
    known = [lv for lv in levels if lv in LEVELS]
    return max(known, key=LEVELS.index) if known else "none"


class OperationalStore:
    def __init__(self, data_root: Path, domain_id: str, *, name: str = "") -> None:
        self.domain_id = domain_id
        self.name = name or domain_id
        self.root = data_root / "operational" / domain_id
        self.cycles = self.root / "cycles"

    def cycle_dir(self, cycle: datetime) -> Path:
        return self.cycles / f"{cycle:%Y%m%d%H}"

    def is_complete(self, cycle: datetime) -> bool:
        status = read_json(self.cycle_dir(cycle) / "status.json")
        return bool(status and status.get("finished_at"))

    def write_cycle_status(self, cycle: datetime, status: dict) -> None:
        write_json(self.cycle_dir(cycle) / "status.json", status)

    def publish_latest(self, cycle: datetime, status: dict, *, version: str,
                       uncalibrated: bool, keep_cycles: int) -> None:
        """Point ``latest.json`` at ``cycle`` (only if newer) and refresh history."""
        current = read_json(self.root / "latest.json")
        if current and current.get("cycle", "") > iso(cycle):
            pass  # a newer cycle already published; keep it
        else:
            write_json(
                self.root / "latest.json",
                {
                    "domain_id": self.domain_id,
                    "name": self.name,
                    "cycle": iso(cycle),
                    "updated_at": iso(now_utc()),
                    "level": status["level"],
                    "level_label": LEVEL_LABELS[status["level"]],
                    "message": status["message"],
                    "message_pt": status.get("message_pt", ""),
                    "path": f"cycles/{cycle:%Y%m%d%H}/status.json",
                    "system": {"version": version, "uncalibrated": uncalibrated},
                },
            )
        history = [h for h in read_json(self.root / "history.json", []) if h["cycle"] != iso(cycle)]
        history.append(
            {
                "cycle": iso(cycle),
                "level": status["level"],
                "message": status["message"],
                "hydrology": status["stages"]["hydrology"]["state"],
                "hydraulics": status["stages"]["hydraulics"]["state"],
                "finished_at": status.get("finished_at"),
            }
        )
        history.sort(key=lambda h: h["cycle"], reverse=True)
        write_json(self.root / "history.json", history[:500])
        self.prune(keep_cycles)

    def prune(self, keep: int) -> None:
        """Drop bulky per-cycle folders beyond ``keep``; history.json keeps the summary."""
        if keep <= 0 or not self.cycles.is_dir():   # 0 = keep every forecast
            return
        dirs = sorted((d for d in self.cycles.iterdir() if d.is_dir()), reverse=True)
        for old in dirs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
