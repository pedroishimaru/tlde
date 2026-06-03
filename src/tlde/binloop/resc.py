"""Generate a per-binary Renode .resc that loads the platform + firmware and
runs headless, bounded by *virtual* time.

The console UART is mirrored to a file backend so output markers can be checked
after a headless run (no GUI analyzer needed).
"""

from __future__ import annotations

from pathlib import Path

from tlde.binloop.metadata import BinaryMeta


def _vt(seconds: int) -> str:
    seconds = max(1, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def generate_resc(
    meta: BinaryMeta,
    repl_files: list[str],
    cs_files: list[str] | None = None,
    uart_log_path: str | None = None,
    machine: str = "m",
) -> str:
    """Build the .resc text for one binary."""
    lines = [
        f":name: {meta.name}",
        f":description: tlde binary-in-the-loop run for {meta.name}",
        "",
        f'mach create "{machine}"',
    ]
    for cs in (cs_files or []):
        lines.append(f"include @{cs}")          # compile custom C# peripherals first
    for repl in repl_files:
        lines.append(f"machine LoadPlatformDescription @{repl}")

    art = meta.artifact_path
    if meta.format == "elf":
        lines.append(f"sysbus LoadELF @{art}")
    elif meta.format == "hex":
        lines.append(f"sysbus LoadHEX @{art}")
    elif meta.format == "bin":
        addr = meta.load_address if meta.load_address is not None else 0x0
        lines.append(f"sysbus LoadBinary @{art} {hex(addr)}")

    if meta.console_uart and uart_log_path:
        # Mirror console output to a file so markers can be checked headlessly.
        lines.append(f"sysbus.{meta.console_uart} CreateFileBackend @{uart_log_path} true")

    lines += [
        "logLevel 0",                            # capture INFO+ (unmapped/Tag/faults)
        f'emulation RunFor "{_vt(meta.success.virtual_time_budget_s)}"',
        "quit",
        "",
    ]
    return "\n".join(lines)


def write_resc(text: str, dest: str | Path) -> str:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return str(dest)
