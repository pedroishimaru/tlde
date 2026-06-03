"""Run a generated .resc headless under Renode and capture logs.

Execution is bounded twice: by *virtual* time inside the script (``RunFor``) and
by a *wall-clock* timeout here (so a hung model can't stall the pipeline).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Common install location when ``renode`` isn't on PATH.
_FALLBACK_BINS = ("/opt/renode/renode", "/usr/bin/renode", "/usr/local/bin/renode")


def find_renode(explicit: str | None = None) -> str | None:
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("renode")
    if found:
        return found
    for cand in _FALLBACK_BINS:
        if Path(cand).exists():
            return cand
    return None


@dataclass
class RunResult:
    ran: bool                 # did Renode execute at all
    log: str = ""             # captured stdout+stderr (Renode log)
    uart: str = ""            # console UART output (if a file backend was set)
    exit_code: int | None = None
    timed_out: bool = False
    error: str = ""           # harness-level error (renode missing, etc.)


def run(resc_path: str, renode_bin: str | None = None, wall_timeout_s: int = 180,
        uart_log_path: str | None = None) -> RunResult:
    """Run ``resc_path`` headless. Captures the Renode log + optional UART file."""
    binpath = find_renode(renode_bin)
    if binpath is None:
        return RunResult(ran=False, error="renode binary not found (set [binaries].renode_bin)")

    cmd = [binpath, "--disable-xwt", "--console", "-p", resc_path]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=wall_timeout_s,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        log = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        exit_code = None
        timed_out = True
    except Exception as e:
        return RunResult(ran=False, error=f"{type(e).__name__}: {e}")

    uart = ""
    if uart_log_path and Path(uart_log_path).is_file():
        uart = Path(uart_log_path).read_text(errors="ignore")

    return RunResult(ran=True, log=log, uart=uart, exit_code=exit_code, timed_out=timed_out)
