# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ishaan Dugar
"""Input formats: what WaveQ reads natively, what it converts with open tools, and what it explains.

Native: VCD and Extended VCD (IEEE 1364), optionally gzipped.
Converted: FST, LXT, LXT2 and VZT through GTKWave's own command-line converters
(fst2vcd, lxt2vcd, vzt2vcd), when they are on PATH. The VCD they write is kept in the cache
directory and reused until the source file changes. Set WAVEQ_FST2VCD (or WAVEQ_LXT2VCD,
WAVEQ_VZT2VCD) to a command to use a converter that is not on PATH.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from pathlib import Path

NATIVE_SUFFIXES = (".vcd", ".vcd.gz", ".evcd", ".evcd.gz")

# suffix -> (converter, where to get it)
CONVERTED = {
    ".fst": ("fst2vcd", "GTKWave (gtkwave.github.io; most package managers ship it)"),
    ".lxt": ("lxt2vcd", "GTKWave"),
    ".lxt2": ("lxt2vcd", "GTKWave"),
    ".vzt": ("vzt2vcd", "GTKWave"),
}

# Formats with a better path than conversion, or that need their vendor's tool.
ADVICE = {
    ".ghw": "GHDL can write VCD or FST directly: run with --vcd=wave.vcd (or --fst=wave.fst and install GTKWave)",
    ".fsdb": "FSDB is Verdi's proprietary format; convert with fsdb2vcd, or dump VCD from the simulator",
    ".vpd": "VPD is VCS's proprietary format; convert with vpd2vcd, or dump VCD from the simulator",
    ".wlf": "WLF is Questa/ModelSim's format; convert with wlf2vcd, or dump VCD from the simulator",
    ".shm": "SHM is Xcelium's format; convert with simvisdbutil -vcd, or dump VCD from the simulator",
    ".trn": "TRN is Xcelium's format; convert with simvisdbutil -vcd, or dump VCD from the simulator",
}


class FormatError(ValueError):
    pass


def suffix_of(path: Path) -> str:
    name = path.name.lower()
    for suffix in NATIVE_SUFFIXES + tuple(CONVERTED) + tuple(ADVICE):
        if name.endswith(suffix):
            return suffix
    return path.suffix.lower()


def is_native(path: str | Path) -> bool:
    return Path(path).name.lower().endswith(NATIVE_SUFFIXES)


def _converter_command(tool: str) -> list[str] | None:
    override = os.environ.get(f"WAVEQ_{tool.upper()}")
    if override:
        return [part.strip('"') for part in shlex.split(override, posix=os.name != "nt")]
    found = shutil.which(tool)
    return [found] if found else None


def prepare_source(path: str | Path, cache_root: Path) -> tuple[Path, dict[str, str] | None]:
    """Return a path WaveQ can parse, converting open binary formats into cache_root when needed.

    The second value describes a conversion (or None for native files).
    """
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    if is_native(src):
        return src, None
    suffix = suffix_of(src)
    if suffix in ADVICE:
        raise FormatError(f"{src.name}: {ADVICE[suffix]}")
    if suffix not in CONVERTED:
        raise FormatError(
            f"{src.name}: unsupported format; WaveQ reads .vcd/.evcd (optionally .gz) and converts .fst, .lxt, .lxt2 and .vzt with GTKWave's tools"
        )
    tool, source = CONVERTED[suffix]
    cmd = _converter_command(tool)
    if cmd is None:
        raise FormatError(f"{src.name}: needs {tool} from {source} on PATH (or set WAVEQ_{tool.upper()}); or run `{tool} {src.name} > {src.stem}.vcd` yourself")
    digest = hashlib.sha256(str(src).encode()).hexdigest()[:16]
    out = cache_root / f"{src.stem}.{digest}.converted.vcd"
    st = src.stat()
    info = {"converted_from": str(src), "converter": tool, "converted_vcd": str(out)}
    if out.exists() and out.stat().st_size > 0 and out.stat().st_mtime_ns >= st.st_mtime_ns:
        info["conversion"] = "reused"
        return out, info
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".vcd.tmp")
    with tmp.open("wb") as fh:
        proc = subprocess.run([*cmd, str(src)], stdout=fh, stderr=subprocess.PIPE)
    if proc.returncode != 0 or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        err = proc.stderr.decode(errors="replace").strip()[-400:]
        raise FormatError(f"{tool} failed on {src.name} (exit {proc.returncode}): {err or 'no output'}")
    os.replace(tmp, out)
    info["conversion"] = "converted"
    return out, info
