"""Sandboxed execution (FR2.3).

Two backends:
  * docker  - no network, read-only root, memory/CPU/pid caps (used when `docker` works)
  * process - isolated interpreter (-I), empty env, throwaway cwd, rlimits on CPU, memory,
              file size and process count, hard wall-clock timeout (POSIX only)
The process backend does NOT block network access; prefer docker for untrusted code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

MAX_OUT = 8000


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    backend: str = "process"

    def text(self) -> str:
        out = self.stdout.strip()
        if self.timed_out:
            return f"error: timed out\n{out}"
        if not self.ok:
            return f"error (exit {self.returncode}): {self.stderr.strip()[-1500:]}\n{out}".strip()
        return out or "(no output)"


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _limits(cpu_s: int, mem_mb: int):
    def apply():
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        os.setsid()
    return apply


def run_python(code: str, timeout: int = 10, mem_mb: int = 256, backend: str = "auto") -> SandboxResult:
    use_docker = backend == "docker" or (backend == "auto" and docker_available())
    with tempfile.TemporaryDirectory(prefix="ragnarok-sbx-") as work:
        path = os.path.join(work, "main.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        if use_docker:
            cmd = ["docker", "run", "--rm", "--network", "none", "--read-only", "--memory", f"{mem_mb}m",
                   "--cpus", "1", "--pids-limit", "64", "-v", f"{work}:/work:ro", "-w", "/work",
                   "python:3.12-slim", "python", "-I", "main.py"]
            kwargs = {}
            name = "docker"
        else:
            cmd = [sys.executable, "-I", path]
            kwargs = {"preexec_fn": _limits(timeout + 1, mem_mb)} if os.name == "posix" else {}
            name = "process"
        try:
            p = subprocess.run(cmd, cwd=work, env={"PATH": "/usr/bin:/bin"} if name == "process" else None,
                               capture_output=True, text=True, timeout=timeout, **kwargs)
            return SandboxResult(p.returncode == 0, p.stdout[:MAX_OUT], p.stderr[:MAX_OUT], p.returncode, backend=name)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"")
            out = out.decode() if isinstance(out, bytes) else out
            return SandboxResult(False, out[:MAX_OUT], "", -1, timed_out=True, backend=name)
