"""Always-on loop: poll for new IFS cycles, process each exactly once, publish, repeat."""

from __future__ import annotations

import logging
import logging.handlers
import signal
import time
from datetime import timedelta
from pathlib import Path

from filelock import FileLock, Timeout

from . import ifs
from .config import OperationalConfig
from .cycle import CycleProcessor
from .domain import Domain
from .store import OperationalStore, iso, now_utc, write_json

log = logging.getLogger("floodcast.operational")


class Daemon:
    def __init__(self, config: OperationalConfig, *, processor: CycleProcessor | None = None) -> None:
        self.config = config
        self.domain = Domain.load(config.domain)
        self.store = OperationalStore(config.data_root, self.domain.domain_id, name=self.domain.name)
        self.processor = processor or CycleProcessor(config, self.domain, self.store)
        self._stop = False

    # ------------------------------------------------------------------------------
    def pending_cycles(self) -> list:
        """Cycles to process, oldest first: not yet complete, already published by ECMWF.

        Older cycles are processed too (up to ``max_cycle_age_h``) because the SUMMA state
        chain needs every 6-hourly step; only the newest gets full ensemble treatment.
        """
        fc = self.config.forecast
        out = []
        for cycle in sorted(ifs.cycle_candidates(now_utc(), fc.cycles, max_age_h=fc.max_cycle_age_h)):
            if self.store.is_complete(cycle):
                continue
            if ifs.cycle_is_available(
                cycle, source=fc.source, resolution=fc.resolution,
                max_lead_h=fc.screening_max_lead_h, ensemble=False,
            ):
                out.append(cycle)
        return out

    def heartbeat(self, state: str, **extra) -> None:
        write_json(
            self.store.root / "health.json",
            {"state": state, "time": iso(now_utc()), "pid": _pid(), **extra},
        )

    def run_once(self) -> int:
        """One polling pass. Returns the number of cycles processed."""
        pending = self.pending_cycles()
        done = 0
        for index, cycle in enumerate(pending):
            if self._stop:
                break
            latest = index == len(pending) - 1
            self.heartbeat("processing", cycle=iso(cycle))
            try:
                self.processor.process(cycle, full=latest)
                done += 1
            except Exception:  # never let one bad cycle kill the service
                log.exception("cycle %s failed", iso(cycle))
        self.heartbeat("idle", processed=done, pending_before=len(pending))
        return done

    def run_forever(self) -> None:
        lock_path = self.store.root / "daemon.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(lock_path))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise SystemExit("another floodcast operate daemon is already running") from None
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: setattr(self, "_stop", True))
        log.info("daemon started for %s (poll %d min)", self.domain.domain_id,
                 self.config.poll_interval_minutes)
        try:
            while not self._stop:
                started = now_utc()
                try:
                    self.run_once()
                    self._refresh_site()
                except Exception:
                    log.exception("polling pass failed")
                    self.heartbeat("error")
                wait = max(30.0, self.config.poll_interval_minutes * 60
                           - (now_utc() - started).total_seconds())
                deadline = time.monotonic() + wait
                while not self._stop and time.monotonic() < deadline:
                    time.sleep(1)
        finally:
            lock.release()
            log.info("daemon stopped")

    def _refresh_site(self) -> None:
        out = self.config.publish.output_dir
        if out is None:
            return
        from .site import build_site

        build_site(
            self.store.root, out, site_title=self.config.publish.site_title,
            base_url=self.config.publish.base_url,
        )
        command = self.config.publish.post_command
        if command:
            import subprocess

            try:
                subprocess.run(
                    command, shell=True, cwd=out, check=True,
                    timeout=self.config.publish.post_command_timeout_s,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                log.error("publish.post_command failed: %s", type(exc).__name__)


def _pid() -> int:
    import os

    return os.getpid()


def setup_logging(config: OperationalConfig, domain_id: str, *, verbose: bool = False) -> Path:
    logdir = config.data_root / "operational" / domain_id / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    path = logdir / "operational.log"
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return path


__all__ = ["Daemon", "setup_logging", "timedelta"]
