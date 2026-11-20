"""Fingerprint the code base so two machines can be compared file by file (stdlib only, any OS).

    python tools/manifest.py                       # print   <sha256-16>  <size>  <path>   for every code file
    python tools/manifest.py --out here.txt        # save it (dev PC)
    python3 tools/manifest.py --out there.txt      # save it (shop PC)
    python tools/manifest.py --compare there.txt   # list files that differ / are missing on either side

Covers main.py, the launchers, pyproject.toml, bootstrap/, src/, tests/core/, tools/, docs/, config/ — not logs,
caches, the venv or data. Line endings are normalised (CRLF → LF) so a Windows checkout and a macOS checkout of
the same commit hash identically.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ("main.py", "pyproject.toml", "Start Vaishnavi.bat", "Start Vaishnavi.command", "start-vaishnavi.sh", "bootstrap", "src", "tests/core", "tools", "docs", "config")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sha256"}


def _digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    if path.suffix in {".py", ".md", ".toml", ".json", ".sql", ".txt", ".bat", ".sh", ".command", ".html", ".css", ".mako"}:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()[:16], len(data)


def manifest(root: Path = ROOT) -> dict[str, tuple[str, int]]:
    """``{relative/path: (sha16, size)}`` for every included file."""
    out: dict[str, tuple[str, int]] = {}
    for entry in INCLUDE:
        p = root / entry
        if p.is_file():
            out[entry] = _digest(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.suffix in SKIP_SUFFIXES or any(part in SKIP_DIRS for part in f.relative_to(root).parts):
                    continue
                out[f.relative_to(root).as_posix()] = _digest(f)
    return out


def parse(text: str) -> dict[str, tuple[str, int]]:
    """Read a saved manifest."""
    out: dict[str, tuple[str, int]] = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and len(parts[0]) == 16:
            out[parts[2].strip()] = (parts[0], int(parts[1]))
    return out


def compare(here: dict[str, tuple[str, int]], there: dict[str, tuple[str, int]]) -> list[str]:
    """Human lines: ``DIFFERENT``, ``MISSING THERE``, ``EXTRA THERE``."""
    lines: list[str] = []
    for path in sorted(set(here) | set(there)):
        a, b = here.get(path), there.get(path)
        if a and b and a != b:
            lines.append(f"DIFFERENT      {path}   (here {a[0]} {a[1]} B · there {b[0]} {b[1]} B)")
        elif a and not b:
            lines.append(f"MISSING THERE  {path}")
        elif b and not a:
            lines.append(f"EXTRA THERE    {path}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--compare", metavar="MANIFEST", help="a manifest saved on the other machine; prints only the differences")
    ap.add_argument("--out", metavar="FILE", help="write the manifest to FILE (UTF-8) instead of printing it — use this rather than '>' on Windows PowerShell, which writes UTF-16")
    ns = ap.parse_args(argv)
    here = manifest()
    if ns.compare:
        there = parse(Path(ns.compare).read_text(encoding="utf-8-sig"))
        diffs = compare(here, there)
        print("\n".join(diffs) if diffs else f"IDENTICAL — {len(here)} files match")
        return 1 if diffs else 0
    total = hashlib.sha256("".join(f"{p}{h}" for p, (h, _s) in sorted(here.items())).encode()).hexdigest()[:16]
    body = "".join(f"{sha}  {size:>8}  {path}\n" for path, (sha, size) in here.items())
    if ns.out:
        Path(ns.out).write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    print(f"# {len(here)} files · overall {total}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
