"""forge agent — the bounded build → diagnose → fix → rebuild loop (CONTEXT §4, §6 step 3).

Orchestrates the deterministic pieces (dfc_runner, build_runner, dockerfile, verifier) around
the one LLM seam (agent.llm). dfc converts once; then each iteration builds the candidate,
gathers deterministic signals, asks the LLM to diagnose + draft edit ops (Sonnet, one-hop Opus
escalation), applies them, and rebuilds — capped, failing loudly with a diagnostic dump.

Scope is locked to A/B/D (CONTEXT §2/§10): any diagnosis outside those classes, or one that
needs build content the LLM must not author (e.g. a compile step), stops the loop as a
*documented touch-up boundary* rather than letting the LLM invent a Dockerfile. The agent's
output is `Dockerfile.agent` (separate from the hand-built `Dockerfile.hardened`); on success
it hands that file to the existing CI pipeline unchanged, and always emits a fix-provenance
report attributing every autonomous fix to a model and listing any touch-ups still required —
"agent-generated with N documented touch-ups" is the honest claim, not a perfect result.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent import build_runner, dfc_runner, llm, verifier
from agent.dockerfile import Dockerfile

# Paths (the uptime-kuma target). The build context is the upstream checkout (gitignored,
# fetched the same way CI does — see decisions.md "Build context").
TARGET_DIR = Path("targets/uptime-kuma")
UPSTREAM = TARGET_DIR / "Dockerfile.upstream"
CONTEXT_DIR = Path("build/uptime-kuma")
OUT_CONVERTED = TARGET_DIR / "Dockerfile.agent-converted"  # raw dfc output (the A/B/D delta)
OUT_AGENT = TARGET_DIR / "Dockerfile.agent"                # the agent's generated, hardened file
OUT_PROVENANCE = TARGET_DIR / "agent-provenance.md"
TAG = "forge/uptime-kuma:agent"
# Upstream's Dockerfile is multi-target; `release` is its "⭐ Main Image" runtime stage. We
# build that, not the default last stage (which is the GitHub upload-artifact helper).
TARGET_STAGE = "release"
MAX_ITERS = 5


@dataclass
class FixRecord:
    iteration: int
    failure_signature: str
    failure_class: str
    model: str
    confidence: str
    rationale: str
    edits: list[dict]


@dataclass
class Escalation:
    failure_signature: str
    from_model: str
    to_model: str
    reason: str


class AgentStop(RuntimeError):
    """Loud, intentional stop — carries the diagnostic dump (cap reached / touch-up boundary)."""


def _apply(df: Dockerfile, edits) -> None:
    """Deterministically apply the LLM's edit ops (the LLM never touches the file directly)."""
    for e in edits:
        if e.op == "replace_base_image":
            df.replace_base_image(e.stage, e.value)
        elif e.op == "set_user":
            df.set_user(e.stage, e.value)
        elif e.op == "add_package":
            df.add_package(e.stage, e.value)
        else:  # pragma: no cover - schema-constrained
            raise AgentStop(f"unknown edit op from LLM: {e.op!r}")


def _pin_digests(df: Dockerfile) -> list[str]:
    """CONTEXT §7 — committed Dockerfiles pin digests, never `latest`. After a green build,
    resolve each cgr.dev base's digest and pin it. Best-effort: a failed resolve is noted."""
    notes = []
    for s in list(df.stages):
        ref = s.image
        if not ref.startswith("cgr.dev/") or "@sha256:" in ref:
            continue
        proc = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{.Manifest.Digest}}"],
            capture_output=True, text=True)
        digest = proc.stdout.strip()
        if proc.returncode == 0 and digest.startswith("sha256:"):
            df.replace_base_image(s.alias, f"{ref}@{digest}")
        else:
            notes.append(f"could not resolve digest for {ref} (left unpinned)")
    return notes


def run(max_iters: int = MAX_ITERS, do_verify: bool = True) -> int:
    if not UPSTREAM.is_file():
        raise AgentStop(f"missing input Dockerfile: {UPSTREAM}")
    if not CONTEXT_DIR.is_dir():
        raise AgentStop(f"missing build context {CONTEXT_DIR} — fetch the upstream checkout "
                        "(pinned commit) the way CI does; see decisions.md.")

    # 1. dfc converts once (deterministic). Persist the raw output — it shows the A/B/D delta.
    print(f"[forge] dfc converting {UPSTREAM} …")
    conv = dfc_runner.run(UPSTREAM)
    converted_text = "\n".join(line.get("converted", line.get("raw", ""))
                               for line in conv.raw_json.get("lines", [])) + "\n"
    OUT_CONVERTED.write_text(converted_text)
    df = Dockerfile(converted_text)

    fixes: list[FixRecord] = []
    escalations: list[Escalation] = []
    tried: dict[str, set[str]] = {}  # failure signature -> models already diagnosed for it

    # 2. Bounded loop.
    for i in range(1, max_iters + 1):
        print(f"[forge] iteration {i}: building …")
        result = build_runner.build(df.text, CONTEXT_DIR, TAG, target=TARGET_STAGE)
        if result.ok:
            print(f"[forge] build OK on iteration {i}")
            break

        sig = result.signature()
        print(f"[forge]   build failed (signature: {sig})")
        signals = build_runner.gather_signals(df, result, TARGET_STAGE)
        used = tried.setdefault(sig, set())

        diag = None
        for model in (llm.MODEL, llm.ESCALATION_MODEL):
            if model in used:
                continue
            if model == llm.ESCALATION_MODEL and llm.MODEL in used:
                reason = f"Sonnet fix failed to resolve build error: {sig}"
                escalations.append(Escalation(sig, llm.MODEL, llm.ESCALATION_MODEL, reason))
                print(f"[forge]   escalating {llm.MODEL} → {llm.ESCALATION_MODEL} ({reason})")
            used.add(model)
            d = llm.diagnose(df.text, result.error_tail, signals, model=model)
            print(f"[forge]   {model}: class {d.failure_class}, "
                  f"{len(d.edits)} edit(s), confidence {d.confidence}")
            if d.failure_class in llm.ALLOWED_CLASSES and d.edits:
                diag = d
                break

        if diag is None:
            raise AgentStop(_dump(
                "no in-scope fix available — documented touch-up boundary",
                df, result, signals, fixes, escalations))

        _apply(df, diag.edits)
        fixes.append(FixRecord(
            iteration=i, failure_signature=sig, failure_class=diag.failure_class,
            model=diag.model, confidence=diag.confidence, rationale=diag.rationale,
            edits=[{"op": e.op, "stage": e.stage, "value": e.value, "reason": e.reason}
                   for e in diag.edits]))
    else:
        raise AgentStop(_dump(
            f"iteration cap ({max_iters}) reached without a green build",
            df, result, build_runner.gather_signals(df, result, TARGET_STAGE), fixes, escalations))

    # 3. Finalize: pin digests, write the generated Dockerfile.
    pin_notes = _pin_digests(df)
    OUT_AGENT.write_text(df.text)
    print(f"[forge] wrote {OUT_AGENT}")

    # 4. Verify (non-root + healthcheck + OS-layer scan gate).
    vres = None
    if do_verify:
        print("[forge] verifying (non-root + healthcheck + scan gate) …")
        vres = verifier.verify(TAG)
        print(f"[forge]   non-root={vres.non_root} healthcheck={vres.healthcheck_ok} "
              f"OS-crit={vres.os_critical} -> {'PASS' if vres.passed else 'FAIL'}")

    # 5. Fix-provenance report (autonomous fixes, model attribution, touch-ups).
    OUT_PROVENANCE.write_text(_provenance_md(fixes, escalations, vres, pin_notes))
    print(f"[forge] wrote {OUT_PROVENANCE}")
    print(f"[forge] hand off {OUT_AGENT} to the existing pipeline (.github/workflows/forge.yml).")
    return 0 if (vres is None or vres.passed) else 1


def _dump(reason: str, df, result, signals, fixes, escalations) -> str:
    return (f"AGENT STOP: {reason}\n\n"
            f"== failure signature ==\n{result.signature()}\n\n"
            f"== deterministic signals ==\n{json.dumps(signals, indent=2)}\n\n"
            f"== autonomous fixes so far ({len(fixes)}) ==\n"
            + "\n".join(f"  iter {f.iteration} [{f.failure_class}/{f.model}]: {f.rationale}"
                        for f in fixes)
            + (f"\n\n== escalations ==\n" + "\n".join(f"  {e.from_model}→{e.to_model}: {e.reason}"
                                                       for e in escalations) if escalations else "")
            + f"\n\n== build error tail ==\n{result.error_tail}\n\n"
            f"== current Dockerfile ==\n{df.text}")


def _provenance_md(fixes, escalations, vres, pin_notes) -> str:
    auto = len(fixes)
    sonnet = sum(1 for f in fixes if f.model == llm.MODEL)
    opus = sum(1 for f in fixes if f.model == llm.ESCALATION_MODEL)
    L = ["# Agent fix provenance — uptime-kuma\n",
         "> Generated by `agent/forge_agent.py`. Tracks which fixes the agent made autonomously "
         "and which model produced each (CONTEXT §4 honesty boundary). The agent output is "
         "`Dockerfile.agent`, separate from the hand-built `Dockerfile.hardened`.\n",
         f"**{auto} autonomous fix(es)** — {sonnet} by `{llm.MODEL}`, "
         f"{opus} by `{llm.ESCALATION_MODEL}` (escalated).\n"]
    if escalations:
        L.append("## Model escalations (cost-tiering, made visible)\n")
        for e in escalations:
            L.append(f"- `{e.from_model}` → `{e.to_model}` — {e.reason}")
        L.append("")
    L.append("## Autonomous fixes (in order)\n")
    L.append("| # | Class | Model | Confidence | Edit(s) | Rationale |")
    L.append("|--:|---|---|---|---|---|")
    for f in fixes:
        edits = "; ".join(f"{e['op']} {e['stage']}→`{e['value']}`" for e in f.edits)
        L.append(f"| {f.iteration} | {f.failure_class} | `{f.model}` | {f.confidence} | "
                 f"{edits} | {f.rationale} |")
    L.append("")
    if vres is not None:
        L.append("## Verification\n")
        L.append(f"- non-root: **{vres.non_root}** (user `{vres.user}`)")
        L.append(f"- healthcheck: **{vres.healthcheck_ok}** — {vres.healthcheck_detail}")
        L.append(f"- OS/runtime-layer CVEs: **{vres.os_critical} Critical**, {vres.os_high} High "
                 f"(gate: 0 Critical) · npm-layer: {vres.npm_total} (out of scope)")
        L.append(f"- **gate: {'PASS' if vres.passed else 'FAIL'}**")
        for n in vres.notes:
            L.append(f"  - {n}")
        L.append("")
    if pin_notes:
        L.append("## Digest pinning notes\n")
        for n in pin_notes:
            L.append(f"- {n}")
        L.append("")
    return "\n".join(L) + "\n"


def dry_run() -> int:
    """Deterministic-only: convert + one build + signal gather, no LLM. Lets us exercise the
    real phantom-image failure and the signal extraction without an API key."""
    conv = dfc_runner.run(UPSTREAM)
    converted_text = "\n".join(line.get("converted", line.get("raw", ""))
                               for line in conv.raw_json.get("lines", [])) + "\n"
    OUT_CONVERTED.write_text(converted_text)
    df = Dockerfile(converted_text)
    print(f"[dry-run] stages: {[s.alias or f'#{s.index}' for s in df.stages]}")
    print(f"[dry-run] target {TARGET_STAGE} depends on: {sorted(df.reachable_from(TARGET_STAGE))}")
    print(f"[dry-run] building --target {TARGET_STAGE} (expected to fail on phantom base) …")
    result = build_runner.build(df.text, CONTEXT_DIR, TAG, target=TARGET_STAGE)
    print(f"[dry-run] build ok={result.ok} signature={result.signature()}")
    signals = build_runner.gather_signals(df, result, TARGET_STAGE)
    print("[dry-run] deterministic signals:\n" + json.dumps(signals, indent=2))
    return 0


def _load_env() -> None:
    """Load `.env` into the process environment, best-effort and at the edge only (secrets stay
    out of agent.llm, which is a pure os.environ reader). If python-dotenv or .env is absent, we
    fall through to whatever's already exported (so a shell export or CI works unchanged).
    override=False means a real shell export wins over .env."""
    try:
        from dotenv import find_dotenv, load_dotenv
        # usecwd=True searches from the working directory (the repo root the agent runs in)
        # instead of walking the caller's stack frame, which is fragile. No .env → no-op.
        load_dotenv(find_dotenv(usecwd=True), override=False)
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(description="forge agent — harden a dfc-converted Dockerfile (A/B/D).")
    ap.add_argument("--max-iters", type=int, default=MAX_ITERS)
    ap.add_argument("--no-verify", action="store_true", help="skip the verify stage")
    ap.add_argument("--dry-run", action="store_true",
                    help="deterministic only: convert + one build + signals, no LLM/API key")
    args = ap.parse_args(argv)
    try:
        if args.dry_run:
            return dry_run()
        return run(max_iters=args.max_iters, do_verify=not args.no_verify)
    except AgentStop as stop:
        print("\n" + str(stop), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
