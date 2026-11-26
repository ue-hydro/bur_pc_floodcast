"""Run coupled SUMMA-mizuRoute and FLUXOS in their Docker images (or locally) with hard timeouts."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ModelSettings

log = logging.getLogger("floodcast.operational.runners")

# Signs that the Docker engine itself is down or wedged (e.g. Docker Desktop's Resource Saver
# failed to wake the VM), as opposed to the model failing inside its container.
DAEMON_ERRORS = ("Internal Server Error", "Cannot connect to the Docker daemon",
                 "Is the docker daemon running", "error during connect", "no route to host")
_HEAL_LOCK = threading.Lock()
_last_heal = 0.0


def docker_ok(timeout_s: int = 20) -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=timeout_s).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def heal_docker(wait_s: int = 300) -> bool:
    """Restart Docker Desktop if the engine does not answer; one restart at a time across threads."""
    global _last_heal
    with _HEAL_LOCK:
        if docker_ok():
            return True
        if time.time() - _last_heal < 60:  # another thread just restarted it: only wait
            restart = False
        else:
            restart, _last_heal = True, time.time()
        if restart:
            log.warning("Docker engine not answering; restarting Docker Desktop")
            try:
                subprocess.run(["docker", "desktop", "restart"], capture_output=True, timeout=wait_s)
            except (subprocess.TimeoutExpired, OSError):
                subprocess.run(["open", "-a", "Docker"], capture_output=True, check=False)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if docker_ok():
                log.info("Docker engine is back")
                return True
            time.sleep(5)
        log.error("Docker engine still down after %d s", wait_s)
        return False


class ModelRunError(RuntimeError):
    def __init__(self, model: str, returncode: int | None, log_path: Path, reason: str = "") -> None:
        super().__init__(f"{model} failed ({reason or f'exit {returncode}'}); see {log_path}")
        self.model, self.returncode, self.log_path = model, returncode, log_path


class DockerUnavailable(ModelRunError):
    """The Docker engine could not start the container (not a model failure): retry the cycle later."""


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
            platform = getattr(self.settings, f"{model}_docker_platform", "") or self.settings.docker_platform
            if platform:
                cmd += ["--platform", platform]
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
        docker = self.settings.runtime != "local"
        if docker and not docker_ok():
            heal_docker()
        for attempt in (1, 2, 3):
            proc = self._exec(cmd, log_path, timeout_s, cwd, environment, model)
            text = log_path.read_text(errors="replace") if log_path.is_file() else ""
            if attempt < 3 and docker and proc.returncode == 125 and any(e in text for e in DAEMON_ERRORS):
                log.warning("docker engine error for %s; healing Docker and retrying", model)
                if heal_docker():
                    continue
                break
            # Docker Desktop can briefly fail to see a just-created host folder (mount race): retry once
            if (attempt == 1 and docker
                    and ((proc.returncode in (125, 127) and "mount source path" in text)
                         or (proc.returncode == 1 and "cannot open model log" in text))):
                log.warning("docker mount race for %s; retrying", model)
                time.sleep(3)
                continue
            break
        if proc.returncode != 0:
            text = log_path.read_text(errors="replace") if log_path.is_file() else ""
            if docker and proc.returncode == 125 and any(e in text for e in DAEMON_ERRORS):
                raise DockerUnavailable(model, proc.returncode, log_path, "docker engine unavailable")
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
