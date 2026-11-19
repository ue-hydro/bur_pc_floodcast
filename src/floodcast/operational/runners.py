"""Run coupled SUMMA-mizuRoute and FLUXOS in their Docker images (or locally) with hard timeouts."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ModelSettings

log = logging.getLogger("floodcast.operational.runners")


class ModelRunError(RuntimeError):
    def __init__(self, model: str, returncode: int | None, log_path: Path, reason: str = "") -> None:
        super().__init__(f"{model} failed ({reason or f'exit {returncode}'}); see {log_path}")
        self.model, self.returncode, self.log_path = model, returncode, log_path


@dataclass(frozen=True)
class Mount:
    host: Path
    target: str
    mode: str = "ro"


class ModelRunner:
    def __init__(self, settings: ModelSettings, *, data_root: Path | None = None) -> None:
        self.settings = settings
        self.data_root = data_root

    # ---- image/executable resolution -------------------------------------------------
    def image_for(self, model: str) -> str:
        explicit = {
            "summa": self.settings.summa_image,
            "fluxos": self.settings.fluxos_image,
        }[model]
        if explicit:
            return explicit
        return self._from_registry(model)

    def _from_registry(self, model: str) -> str:
        if self.data_root is None:
            raise ModelRunError(model, None, Path("-"), "no image configured")
        from floodcast.core.config import RuntimeConfig
        from floodcast.services.installation_registry import Registry

        config = RuntimeConfig(data_root=self.data_root)
        registry = Registry(config.registry_root, lock_timeout_s=config.registry_lock_timeout_s)
        installation = registry.resolve(model, "docker", None)
        return installation.record.artifact.backend_locator.image_id

    def executable_for(self, model: str) -> str:
        return {
            "summa": self.settings.summa_executable,
            "fluxos": self.settings.fluxos_executable,
        }[model]

    # ---- execution -------------------------------------------------------------------
    def run(
        self,
        model: str,
        args: list[str],
        mounts: list[Mount],
        *,
        workdir: str,
        log_path: Path,
        timeout_s: int,
        env: dict[str, str] | None = None,
        cpus: int | None = None,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        exe = self.executable_for(model)
        if self.settings.runtime == "local":
            cmd = [exe, *args]
            cwd, environment = workdir, {**os.environ, **(env or {})}
        else:
            cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-w", workdir]
            if self.settings.docker_platform:
                cmd += ["--platform", self.settings.docker_platform]
            if cpus:
                cmd += ["--cpus", str(cpus)]
            for key, value in (env or {}).items():
                cmd += ["-e", f"{key}={value}"]
            for mount in mounts:
                cmd += ["-v", f"{mount.host}:{mount.target}:{mount.mode}"]
            cmd += ["--entrypoint", exe] if exe else []
            cmd += [self.image_for(model), *args]
            cwd, environment = None, None
        log.info("run %s: %s", model, shlex.join(cmd))
        for attempt in (1, 2):
            proc = self._exec(cmd, log_path, timeout_s, cwd, environment, model)
            # Docker Desktop can briefly fail to see a just-created host folder (mount race): retry once
            if (proc.returncode in (125, 127) and attempt == 1 and self.settings.runtime != "local"
                    and "mount source path" in log_path.read_text(errors="replace")):
                log.warning("docker mount race for %s; retrying", model)
                import time
                time.sleep(3)
                continue
            break
        if proc.returncode != 0:
            raise ModelRunError(model, proc.returncode, log_path)

    @staticmethod
    def _exec(cmd, log_path: Path, timeout_s: int, cwd, environment, model: str):
        with log_path.open("wb") as handle:
            handle.write(f"$ {shlex.join(cmd)}\n".encode())
            handle.flush()
            try:
                proc = subprocess.run(
                    cmd, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout_s,
                    cwd=cwd, env=environment, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ModelRunError(model, None, log_path, f"timeout after {timeout_s}s") from exc
        return proc
