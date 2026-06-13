"""Wrap Chainguard's `dfc` (Dockerfile Converter) and parse its `--json` output.

Deterministic (CONTEXT §4): this only *runs* their tool and structures the result — it
makes no hardening decisions itself. dfc rewrites FROM/RUN lines (apt→apk, registry/org
swap); we parse that into a small model the rest of the agent reasons over:

  - the FROM lines and the bases dfc chose (input to class A — phantom-image detection),
  - the package lists dfc extracted from each RUN line (input to class B — name resolution).

dfc's `--json` schema (verified against v0.10.0) is a top-level `{"lines": [...]}` where
each line may carry:
  from: {base, tag, alias, orig}        # a FROM instruction
  run:  {distro, manager, packages[]}   # a package-install RUN instruction
  raw / converted: the original and rewritten text
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# dfc --warn-missing-packages logs one WARN line per package it could not map, e.g.:
#   level=WARN msg="Package has no mapping, using original package name" package=sqlite3 distro=debian
# This is dfc's own signal for the class-B work set — the packages it passed through unchanged.
_MISSING_PKG_RE = re.compile(r'package=(\S+)')


class DfcError(RuntimeError):
    """dfc is missing or exited non-zero — fail loudly, never silently continue."""


@dataclass(frozen=True)
class FromLine:
    """A parsed FROM instruction, before and after dfc's rewrite."""

    stage: int
    orig: str            # e.g. "louislam/uptime-kuma:base2"
    base: str            # e.g. "louislam/uptime-kuma"
    tag: str             # e.g. "base2"
    alias: str           # stage alias, may be ""
    converted: str       # dfc's rewritten FROM text (may include an injected USER line)


@dataclass(frozen=True)
class PackageInstall:
    """A package-install RUN line dfc identified, with the packages it extracted."""

    stage: int
    manager: str         # "apt", "apk", ...
    distro: str          # "debian", ...
    packages: tuple[str, ...]
    converted: str       # dfc's rewritten RUN text


@dataclass
class Conversion:
    """The structured result of running dfc on one Dockerfile."""

    source: Path
    raw_json: dict
    froms: list[FromLine] = field(default_factory=list)
    installs: list[PackageInstall] = field(default_factory=list)

    def apt_packages(self) -> list[str]:
        """Every distinct package dfc pulled out of apt/apt-get install lines, in the
        order first seen. This is the real upstream package surface the class-B resolver
        works over (CONTEXT §4 B / decisions.md). Local `.deb` *paths* (e.g. `./x.deb`)
        are kept verbatim — the resolver classifies them, it does not invent a name."""
        seen: dict[str, None] = {}
        for inst in self.installs:
            if inst.manager not in ("apt", "apt-get"):
                continue
            for pkg in inst.packages:
                seen.setdefault(pkg, None)
        return list(seen)


def _resolve_dfc() -> str:
    exe = shutil.which("dfc")
    if not exe:
        raise DfcError("`dfc` not found on PATH. Install Chainguard's Dockerfile Converter "
                       "(https://github.com/chainguard-dev/dfc) — see decisions.md Session 1.")
    return exe


def run(dockerfile: Path, org: str = "chainguard") -> Conversion:
    """Run `dfc --json --org=<org>` on `dockerfile` and parse the result.

    `--org=chainguard` targets the free public tier so the converted FROM lines actually
    pull (CONTEXT §7). We deliberately do *not* pass `--in-place`: the agent owns where
    output goes, dfc just reports.
    """
    dockerfile = Path(dockerfile)
    if not dockerfile.is_file():
        raise DfcError(f"Dockerfile not found: {dockerfile}")

    cmd = [_resolve_dfc(), "--json", f"--org={org}", str(dockerfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DfcError(f"dfc failed ({proc.returncode}) on {dockerfile}:\n{proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise DfcError(f"dfc produced non-JSON output on {dockerfile}: {exc}") from exc

    return _parse(dockerfile, data)


def missing_packages(dockerfile: Path, org: str = "chainguard") -> list[str]:
    """Return the packages dfc itself reports it could not map (its `--warn-missing-packages`
    WARN lines), in order, de-duplicated. This is dfc's authoritative "I passed this name
    through unchanged" set — the class-B resolver's work list, straight from their tool.
    """
    dockerfile = Path(dockerfile)
    cmd = [_resolve_dfc(), f"--org={org}", "--warn-missing-packages", str(dockerfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DfcError(f"dfc failed ({proc.returncode}) on {dockerfile}:\n{proc.stderr.strip()}")

    seen: dict[str, None] = {}
    for line in proc.stderr.splitlines():
        if "no mapping" not in line:
            continue
        m = _MISSING_PKG_RE.search(line)
        if m:
            seen.setdefault(m.group(1), None)
    return list(seen)


def _parse(source: Path, data: dict) -> Conversion:
    conv = Conversion(source=source, raw_json=data)
    for line in data.get("lines", []):
        stage = line.get("stage", 0)
        if "from" in line:
            f = line["from"]
            conv.froms.append(FromLine(
                stage=stage,
                orig=f.get("orig", ""),
                base=f.get("base", ""),
                tag=f.get("tag", ""),
                alias=f.get("alias", ""),
                converted=line.get("converted", ""),
            ))
        if "run" in line:
            r = line["run"]
            pkgs = tuple(r.get("packages", []) or ())
            if pkgs:
                conv.installs.append(PackageInstall(
                    stage=stage,
                    manager=r.get("manager", ""),
                    distro=r.get("distro", ""),
                    packages=pkgs,
                    converted=line.get("converted", ""),
                ))
    return conv
