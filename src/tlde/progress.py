"""Lightweight progress bars (tqdm) with graceful fallback.

Bars render to stderr and auto-disable when stderr is not a TTY (so piped/CI
logs stay clean). All helpers degrade to no-ops if tqdm is unavailable. Use
``write()`` instead of ``print()`` for log lines emitted while a bar is active,
so the bar isn't broken.
"""

from __future__ import annotations


def track(iterable, *, total=None, desc="", unit="it"):
    """Wrap an iterable in a progress bar (``for x in track(...)``)."""
    try:
        from tqdm import tqdm
        return tqdm(iterable, total=total, desc=desc, unit=unit,
                    leave=False, dynamic_ncols=True, disable=None)
    except Exception:
        return iterable


class _NoBar:
    def update(self, n: int = 1): ...
    def close(self): ...
    def set_description(self, *_a, **_k): ...


def bar(total: int, desc: str = "", unit: str = "it"):
    """Return a manually-updated bar with ``.update()`` / ``.close()``."""
    try:
        from tqdm import tqdm
        return tqdm(total=total, desc=desc, unit=unit,
                    leave=False, dynamic_ncols=True, disable=None)
    except Exception:
        return _NoBar()


def write(msg: str) -> None:
    """Emit a log line without clobbering an active bar (tqdm.write fallback)."""
    try:
        from tqdm import tqdm
        tqdm.write(msg)
    except Exception:
        print(msg)
