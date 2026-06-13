"""Class B — resolve Debian package names to their Wolfi equivalents.

CONTEXT §4 B / decisions.md: dfc passes Debian package names through unchanged when it has
no built-in mapping (verified: dfc v0.10.0 maps none of uptime-kuma's apt packages). This
module is the "manual review" dfc's docs tell you to do, automated: take dfc's missing-package
set and resolve each against the **live Wolfi package index** (ground truth of what actually
exists), then emit a `mappings.yaml` in dfc's own format so a re-run can consume it.

Framing (owner decision, decisions.md Session 2): class B is a *conversion-analysis*
capability, demonstrated against the full upstream package set — NOT an in-loop build fix.
The hardened defensible-core image installs only `dumb-init` (resolves cleanly), so B never
fires inside the build loop; the value is the honest analysis of the whole surface.

Every package lands in exactly one of three buckets, reported honestly (no silent drops, no
forced matches — the no-equivalent bucket is where dfc's real limits live):
  - mapped           : Debian name → a different, existing Wolfi name
  - already-correct  : the identical name already exists in Wolfi
  - no-equivalent    : genuinely nothing in the index resolves it (emitted as `[]`, dfc's
                       own "drop this" convention) with a one-line reason

Resolution is deterministic and general (not hardcoded answers): exact match → `provides`
lookup (`cmd:<name>`) → index-validated Debian→Wolfi naming conventions → optional LLM
adjudication for ambiguous residuals. The Wolfi index is the final arbiter at every step:
a candidate is only ever accepted if it actually exists in the index, so neither a naming
heuristic nor the LLM can fabricate a match.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import tarfile
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

# The published Wolfi APKINDEX — the authoritative list of what exists in Wolfi (Chainguard
# images are built from Wolfi). amd64 only, matching the rest of the project (single-arch).
WOLFI_INDEX_URL = "https://packages.wolfi.dev/os/{arch}/APKINDEX.tar.gz"
DEFAULT_ARCH = "x86_64"
CACHE_DIR = Path(".scan/wolfi")  # gitignored; the index is a moving artifact, never committed
MAX_AGE_HOURS = 24

# An adjudicator is the LLM seam (agent.llm): given an unresolved Debian name and the index,
# it may propose Wolfi candidate name(s) + a one-line reason. Its output is always
# index-validated by the caller, so it cannot invent a package. Default None = deterministic
# only (which fully resolves the uptime-kuma target — see decisions.md Session 2).
Adjudicator = Callable[[str, "WolfiIndex"], "tuple[list[str], str]"]

MAPPED = "mapped"
ALREADY_CORRECT = "already-correct"
NO_EQUIVALENT = "no-equivalent"


# ─────────────────────────────────────────────────────────────────────────────
# The Wolfi index
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class WolfiIndex:
    arch: str
    source_url: str
    fetched_at: str          # ISO8601 UTC
    sha256: str              # of the downloaded APKINDEX.tar.gz (for reproducibility)
    names: frozenset[str]    # every package name (APKINDEX `P:` field)
    provides: dict[str, frozenset[str]]  # provide-token (e.g. "cmd:ping") -> providing names

    def has(self, name: str) -> bool:
        return name in self.names

    def providers(self, token: str) -> list[str]:
        return sorted(self.provides.get(token, ()))

    @classmethod
    def load(cls, arch: str = DEFAULT_ARCH, cache_dir: Path = CACHE_DIR,
             max_age_hours: int = MAX_AGE_HOURS, force_refresh: bool = False) -> "WolfiIndex":
        """Fetch (or reuse a fresh cache of) the Wolfi APKINDEX and parse it."""
        cache_dir = Path(cache_dir)
        meta_path = cache_dir / f"APKINDEX-{arch}.meta.json"
        text_path = cache_dir / f"APKINDEX-{arch}.txt"

        if not force_refresh and meta_path.is_file() and text_path.is_file():
            meta = json.loads(meta_path.read_text())
            age = _now() - _dt.datetime.fromisoformat(meta["fetched_at"])
            if age <= _dt.timedelta(hours=max_age_hours):
                names, provides = _parse_apkindex(text_path.read_text())
                return cls(arch=arch, source_url=meta["source_url"],
                           fetched_at=meta["fetched_at"], sha256=meta["sha256"],
                           names=names, provides=provides)

        url = WOLFI_INDEX_URL.format(arch=arch)
        raw = _download(url)
        sha = hashlib.sha256(raw).hexdigest()
        text = _extract_apkindex(raw)
        names, provides = _parse_apkindex(text)

        meta = {"source_url": url, "fetched_at": _now().isoformat(timespec="seconds"),
                "sha256": sha, "arch": arch, "package_count": len(names)}
        cache_dir.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text)
        meta_path.write_text(json.dumps(meta, indent=2))
        return cls(arch=arch, source_url=url, fetched_at=meta["fetched_at"], sha256=sha,
                   names=names, provides=provides)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https host
        return resp.read()


def _extract_apkindex(tar_gz: bytes) -> str:
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tf:
        member = tf.extractfile("APKINDEX")
        if member is None:  # pragma: no cover - defensive
            raise RuntimeError("APKINDEX missing from Wolfi index tarball")
        return member.read().decode("utf-8", errors="replace")


def _parse_apkindex(text: str) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """APKINDEX is blank-line-separated blocks of `K:value` lines. We need `P:` (package
    name) and `p:` (space-separated provide tokens like `cmd:ping=1.0 so:libfoo.so=1`)."""
    names: set[str] = set()
    provides: dict[str, set[str]] = {}
    for block in text.split("\n\n"):
        name = ""
        provide_tokens: list[str] = []
        for line in block.splitlines():
            if line.startswith("P:"):
                name = line[2:].strip()
            elif line.startswith("p:"):
                provide_tokens.extend(line[2:].split())
        if not name:
            continue
        names.add(name)
        for tok in provide_tokens:
            key = tok.split("=", 1)[0]  # drop the version suffix
            provides.setdefault(key, set()).add(name)
    frozen = {k: frozenset(v) for k, v in provides.items()}
    return frozenset(names), frozen


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Resolution:
    debian: str
    bucket: str            # MAPPED | ALREADY_CORRECT | NO_EQUIVALENT
    wolfi: tuple[str, ...] # the Wolfi package(s); empty for no-equivalent
    method: str            # how it resolved (auditable): exact / provides:<tok> / transform:<rule> / llm / ...
    reason: str            # one-line human explanation (required for no-equivalent)


def _is_local_deb(pkg: str) -> bool:
    return pkg.endswith(".deb") or pkg.startswith(("./", "/"))


# Debian→Wolfi naming conventions. Each yields (rule-label, candidate-name); a candidate is
# accepted only if it actually exists in the index, so a wrong guess simply doesn't match.
def _convention_candidates(pkg: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if pkg.startswith("fonts-"):
        out.append(("fonts-*→font-*", "font-" + pkg[len("fonts-"):]))
    if pkg.startswith("python3-"):
        out.append(("python3-*→py3-*", "py3-" + pkg[len("python3-"):]))
    elif pkg.startswith("python-"):
        out.append(("python-*→py3-*", "py3-" + pkg[len("python-"):]))
    for suffix in ("-server", "-client", "-common", "-bin", "-utils", "-runtime"):
        if pkg.endswith(suffix):
            out.append((f"strip {suffix}", pkg[: -len(suffix)]))
    # progressive hyphenated-parent fallback: a-b-c → a-b → a
    parts = pkg.split("-")
    for i in range(len(parts) - 1, 0, -1):
        out.append(("hyphenated-parent", "-".join(parts[:i])))
    return out


def _near_misses(pkg: str, index: WolfiIndex, limit: int = 3) -> list[str]:
    """Factual near-misses for a no-equivalent: index names containing the package's most
    specific token. Honest context ('we looked, here's what's adjacent'), never a match."""
    stop = {"fonts", "font", "python", "python3", "server", "client", "common", "lib", "dev"}
    tokens = sorted((t for t in pkg.replace("3", "").split("-") if len(t) >= 5 and t not in stop),
                    key=len, reverse=True)
    for tok in tokens:
        hits = sorted(n for n in index.names if tok in n)
        if hits:
            return hits[:limit]
    return []


def resolve_one(pkg: str, index: WolfiIndex, adjudicator: Adjudicator | None = None) -> Resolution:
    # 0. A local .deb file path is not a repo package name at all (dfc class C): apk cannot
    #    install a .deb. Surface it honestly rather than pretend to resolve a name.
    if _is_local_deb(pkg):
        return Resolution(pkg, NO_EQUIVALENT, (), "local-deb-path",
                          "local Debian .deb file install — apk cannot install a .deb; "
                          "invalid on Wolfi (dfc class C)")

    # 1. Identical name already in Wolfi.
    if index.has(pkg):
        return Resolution(pkg, ALREADY_CORRECT, (pkg,), "exact",
                          "identical package name exists in the Wolfi index")

    # 2. `provides` lookup: the Debian package's command lands in a differently-named Wolfi
    #    package (e.g. iputils-ping provides `ping` → Wolfi `iputils`; sqlite3 → `sqlite`).
    cmd_tokens = [f"cmd:{pkg}"]
    if "-" in pkg:
        cmd_tokens.append(f"cmd:{pkg.rsplit('-', 1)[-1]}")
    for tok in cmd_tokens:
        provs = [p for p in index.providers(tok) if p != pkg]
        if len(provs) == 1:
            return Resolution(pkg, MAPPED, (provs[0],), f"provides:{tok}",
                              f"Wolfi `{provs[0]}` provides `{tok}`")
        if len(provs) > 1:
            # ambiguous — let the LLM adjudicate from the real candidate list if available
            picked = _adjudicate(pkg, index, adjudicator, candidates=provs, signal=tok)
            if picked is not None:
                return picked

    # 3. Index-validated Debian→Wolfi naming conventions.
    for rule, cand in _convention_candidates(pkg):
        if cand != pkg and index.has(cand):
            return Resolution(pkg, MAPPED, (cand,), f"transform:{rule}",
                              f"Debian→Wolfi naming convention ({rule}); `{cand}` exists in the index")

    # 4. Residual — optional LLM adjudication (always index-validated below).
    picked = _adjudicate(pkg, index, adjudicator)
    if picked is not None:
        return picked

    # 5. Genuinely no Wolfi equivalent.
    near = _near_misses(pkg, index)
    reason = ("no Wolfi package matches by exact name, provided command (cmd:*), or "
              "Debian→Wolfi naming convention")
    if near:
        reason += f"; nearest index entries: {', '.join(near)}"
    return Resolution(pkg, NO_EQUIVALENT, (), "none", reason)


def _adjudicate(pkg: str, index: WolfiIndex, adjudicator: Adjudicator | None,
                candidates: list[str] | None = None, signal: str = "") -> Resolution | None:
    """Run the LLM seam (if wired) and index-validate its proposal — it can only ever
    confirm a name that actually exists, never fabricate one."""
    if adjudicator is None:
        return None
    proposed, reason = adjudicator(pkg, index)
    valid = [p for p in proposed if index.has(p)]
    if not valid:
        return None
    bucket = ALREADY_CORRECT if valid == [pkg] else MAPPED
    method = "llm-adjudicated" + (f" (from {signal})" if signal else "")
    return Resolution(pkg, bucket, tuple(valid), method, reason)


def resolve_all(packages: Iterable[str], index: WolfiIndex,
                adjudicator: Adjudicator | None = None) -> list[Resolution]:
    return [resolve_one(p, index, adjudicator) for p in packages]


# ─────────────────────────────────────────────────────────────────────────────
# Outputs: dfc-format mappings.yaml + an honest three-bucket report
# ─────────────────────────────────────────────────────────────────────────────
def to_mappings_yaml(resolutions: list[Resolution], index: WolfiIndex) -> str:
    """Emit dfc's `--mappings` format. Only the entries dfc itself doesn't already cover are
    worth shipping; we include every resolved package so the file is a complete, auditable
    record. `no-equivalent` → `[]`, which is dfc's own convention for 'no mapping / drop'."""
    L: list[str] = []
    L.append("# Wolfi package mappings for uptime-kuma — generated by agent/wolfi_resolver.py")
    L.append("# (forge, CONTEXT §4 class B). Consumable by `dfc --mappings=this-file`.")
    L.append("#")
    L.append(f"# Resolved against the live Wolfi index: {index.source_url}")
    L.append(f"#   fetched {index.fetched_at}  sha256 {index.sha256}")
    L.append("# Every entry is index-validated; `[]` = no Wolfi equivalent (dfc 'drop'"
             " convention), see the companion report for the one-line reason.")
    L.append("packages:")
    L.append("    debian:")
    for r in resolutions:
        key = r.debian
        # YAML-safe: local .deb paths contain '/' and '.', quote them.
        if any(c in key for c in "/.:"):
            key = f'"{key}"'
        if r.wolfi:
            L.append(f"        {key}:")
            for w in r.wolfi:
                L.append(f"            - {w}")
        else:
            L.append(f"        {key}: []")
    return "\n".join(L) + "\n"


def report_markdown(resolutions: list[Resolution], index: WolfiIndex,
                    dfc_misses: list[str] | None = None) -> str:
    by = {MAPPED: [], ALREADY_CORRECT: [], NO_EQUIVALENT: []}
    for r in resolutions:
        by[r.bucket].append(r)

    L: list[str] = []
    L.append("# Class B — Debian→Wolfi package resolution (uptime-kuma)\n")
    L.append("> Generated by `agent/wolfi_resolver.py`. Conversion-analysis capability "
             "(CONTEXT §4 B): resolves the upstream apt surface against the live Wolfi index. "
             "Not an in-loop build fix — the defensible-core image installs only `dumb-init`.\n")
    L.append(f"- Wolfi index: `{index.source_url}` ({len(index.names):,} packages)")
    L.append(f"- Fetched: {index.fetched_at} · sha256 `{index.sha256[:16]}…`")
    if dfc_misses is not None:
        L.append(f"- dfc v0.10.0 mapped **0 / {len(dfc_misses)}** of these natively "
                 f"(its own `--warn-missing-packages` set) — the resolver supplies the rest.")
    L.append(f"- Totals: **{len(by[MAPPED])} mapped**, **{len(by[ALREADY_CORRECT])} already-correct**, "
             f"**{len(by[NO_EQUIVALENT])} no Wolfi equivalent**.\n")

    L.append("## Mapped (Debian name → different Wolfi name)\n")
    L.append("| Debian | → Wolfi | How (auditable) |")
    L.append("|---|---|---|")
    for r in sorted(by[MAPPED], key=lambda r: r.debian):
        L.append(f"| `{r.debian}` | `{', '.join(r.wolfi)}` | {r.method} — {r.reason} |")
    L.append("")

    L.append("## Already correct (identical name exists in Wolfi)\n")
    L.append("| Package | Note |")
    L.append("|---|---|")
    for r in sorted(by[ALREADY_CORRECT], key=lambda r: r.debian):
        L.append(f"| `{r.debian}` | {r.reason} |")
    L.append("")

    L.append("## No Wolfi equivalent (honest gaps — one-line reason each)\n")
    L.append("These are where dfc's real limits live. Reported, never silently dropped; "
             "emitted as `[]` in `mappings.yaml` (dfc's own 'drop' convention).\n")
    L.append("| Package | Why no equivalent |")
    L.append("|---|---|")
    for r in sorted(by[NO_EQUIVALENT], key=lambda r: r.debian):
        L.append(f"| `{r.debian}` | {r.reason} |")
    L.append("")
    return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# CLI — standalone class-B conversion-analysis pass
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent import dfc_runner

    ap = argparse.ArgumentParser(
        description="Resolve a Dockerfile's Debian apt packages to Wolfi (forge class B).")
    ap.add_argument("dockerfile", type=Path,
                    help="upstream Dockerfile to analyze (e.g. targets/uptime-kuma/Dockerfile.upstream-base)")
    ap.add_argument("--mappings-out", type=Path, default=None,
                    help="write dfc-format mappings.yaml here (default: print to stdout only)")
    ap.add_argument("--report-out", type=Path, default=None,
                    help="write the three-bucket markdown report here (default: stdout)")
    ap.add_argument("--refresh-index", action="store_true", help="force re-fetch the Wolfi index")
    args = ap.parse_args(argv)

    # dfc is the source of both the package surface and the 'what I couldn't map' signal.
    conv = dfc_runner.run(args.dockerfile)
    packages = conv.apt_packages()
    dfc_misses = dfc_runner.missing_packages(args.dockerfile)

    index = WolfiIndex.load(force_refresh=args.refresh_index)
    resolutions = resolve_all(packages, index)  # adjudicator=None: deterministic (see module docstring)

    report = report_markdown(resolutions, index, dfc_misses=dfc_misses)
    if args.report_out:
        args.report_out.write_text(report)
        print(f"wrote {args.report_out}")
    else:
        print(report)

    mappings = to_mappings_yaml(resolutions, index)
    if args.mappings_out:
        args.mappings_out.parent.mkdir(parents=True, exist_ok=True)
        args.mappings_out.write_text(mappings)
        print(f"wrote {args.mappings_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

