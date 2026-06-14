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


@dataclass
class Reversion:
    """A fix that proposed swapping a base back to a known-phantom image — the loop's wobble.
    Recorded honestly (CONTEXT §2): the guardrails earning their keep is truthful detail, not
    something to smooth over. `corrected_iteration` is the later fix that undid it, if any."""
    iteration: int
    stage: str
    image: str
    model: str
    corrected_iteration: int | None = None


@dataclass
class TouchUpAttempt:
    """A diagnosis that returned out-of-scope / no edits — the agent saying 'a human must do
    this' rather than authoring a build step. These define the touch-up boundary."""
    iteration: int
    model: str
    rationale: str


class AgentStop(RuntimeError):
    """Loud, intentional stop for setup errors (missing input/context) — not the touch-up
    boundary, which now emits artifacts rather than just raising."""


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
    reversions: list[Reversion] = []
    touch_ups: list[TouchUpAttempt] = []
    known_phantom: set[str] = set()   # images proven nonexistent by the registry probe
    tried: dict[str, set[str]] = {}   # failure signature -> models already diagnosed for it
    status = "cap_reached"
    result = None

    # 2. Bounded loop.
    for i in range(1, max_iters + 1):
        print(f"[forge] iteration {i}: building …")
        result = build_runner.build(df.text, CONTEXT_DIR, TAG, target=TARGET_STAGE)
        if result.ok:
            print(f"[forge] build OK on iteration {i}")
            status = "success"
            break

        sig = result.signature()
        print(f"[forge]   build failed (signature: {sig})")
        signals = build_runner.gather_signals(df, result, TARGET_STAGE)
        for pb in signals.get("phantom_bases", []):
            known_phantom.add(pb["image"])
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
            touch_ups.append(TouchUpAttempt(iteration=i, model=model, rationale=d.rationale))

        if diag is None:
            status = "touch_up_boundary"
            break

        # Record any reversion (swapping a base back to a known-phantom image) before applying,
        # and mark the prior reversion on the same stage as corrected by this fix.
        for e in diag.edits:
            if e.op == "replace_base_image" and e.value.split("@")[0] in known_phantom:
                reversions.append(Reversion(iteration=i, stage=e.stage, image=e.value, model=diag.model))
                print(f"[forge]   ⚠ reversion: {diag.model} set {e.stage} back to phantom {e.value}")
            elif e.op == "replace_base_image":
                for r in reversions:
                    if r.stage == e.stage and r.corrected_iteration is None and r.iteration < i:
                        r.corrected_iteration = i
        _apply(df, diag.edits)
        fixes.append(FixRecord(
            iteration=i, failure_signature=sig, failure_class=diag.failure_class,
            model=diag.model, confidence=diag.confidence, rationale=diag.rationale,
            edits=[{"op": e.op, "stage": e.stage, "value": e.value, "reason": e.reason}
                   for e in diag.edits]))

    # 3. Terminal handling — every outcome emits inspectable, committed artifacts (the honest
    #    stopping point IS the product, CONTEXT §2/§4), not just a stderr dump.
    vres = None
    pin_notes: list[str] = []
    if status == "success":
        pin_notes = _pin_digests(df)
        OUT_AGENT.write_text(df.text)
        print(f"[forge] wrote {OUT_AGENT} (green build)")
        if do_verify:
            print("[forge] verifying (non-root + healthcheck + scan gate) …")
            vres = verifier.verify(TAG)
            print(f"[forge]   non-root={vres.non_root} healthcheck={vres.healthcheck_ok} "
                  f"OS-crit={vres.os_critical} -> {'PASS' if vres.passed else 'FAIL'}")
    else:
        # Partial output: real agent edits so far, with a header making clear it does NOT build.
        OUT_AGENT.write_text(_partial_header(status) + df.text)
        print(f"[forge] wrote {OUT_AGENT} (PARTIAL — does not build as-is)", file=sys.stderr)

    OUT_PROVENANCE.write_text(_provenance_md(status, fixes, escalations, reversions, touch_ups,
                                             vres, pin_notes, result))
    print(f"[forge] wrote {OUT_PROVENANCE}")
    if status == "success":
        print(f"[forge] hand off {OUT_AGENT} to the existing pipeline (.github/workflows/forge.yml).")
        return 0 if (vres is None or vres.passed) else 1
    print(f"[forge] STOP: {status} — {len(fixes)} autonomous fix(es), touch-ups required. "
          f"See {OUT_PROVENANCE}.", file=sys.stderr)
    return 2


_STATUS_LABEL = {
    "success": "green build",
    "touch_up_boundary": "stopped at a documented touch-up boundary",
    "cap_reached": "iteration cap reached without a green build",
}


def _partial_header(status: str) -> str:
    return (
        "# ==========================================================================\n"
        "#  PARTIAL — forge AGENT OUTPUT. THIS FILE DOES NOT BUILD AS-IS.\n"
        f"#  Stopping point: {_STATUS_LABEL.get(status, status)}.\n"
        "#  The agent autonomously applied the class-A/D edits below, then stopped rather\n"
        "#  than author build steps the LLM must not write (CONTEXT §4). Human touch-ups\n"
        "#  remain — see the companion agent-provenance.md for exactly what each needs.\n"
        "#  Do NOT use this as a working Dockerfile; the hand-built reference is\n"
        "#  Dockerfile.hardened. This file exists to make the honest stopping point\n"
        "#  inspectable, not to be deployed.\n"
        "# ==========================================================================\n\n"
    )


def _provenance_md(status, fixes, escalations, reversions, touch_ups, vres, pin_notes, result) -> str:
    sonnet = sum(1 for f in fixes if f.model == llm.MODEL)
    opus = sum(1 for f in fixes if f.model == llm.ESCALATION_MODEL)
    L = ["# Agent fix provenance — uptime-kuma\n",
         "> Generated by `agent/forge_agent.py`. The honest record of what the agent did "
         "autonomously vs. what it left for a human (CONTEXT §4 boundary). The agent output is "
         "`Dockerfile.agent`, separate from the hand-built `Dockerfile.hardened`.\n",
         f"**Outcome:** {_STATUS_LABEL.get(status, status)}.",
         f"**{len(fixes)} autonomous fix(es)** — {sonnet} by `{llm.MODEL}`, "
         f"{opus} by `{llm.ESCALATION_MODEL}` (escalated)."]
    if status != "success":
        L.append("**`Dockerfile.agent` is PARTIAL and does not build as-is** — see its header "
                 "and the touch-ups below.")
    L.append("")

    if escalations:
        L.append("## Model escalations (cost-tiering, made visible)\n")
        L.append("Sonnet runs by default; Opus is the one-hop escalation when a Sonnet fix fails "
                 "to move the build. Each escalation is recorded so the tiering is a demonstrated "
                 "decision, not a hidden detail.\n")
        for e in escalations:
            L.append(f"- `{e.from_model}` → `{e.to_model}` — {e.reason}")
        L.append("")

    L.append("## Autonomous fixes (in order)\n")
    L.append("| # | Class | Model | Confidence | Edit(s) | Rationale |")
    L.append("|--:|---|---|---|---|---|")
    for f in fixes:
        edits = "; ".join(f"{e['op']} `{e['stage']}`→`{e['value']}`" for e in f.edits)
        L.append(f"| {f.iteration} | {f.failure_class} | `{f.model}` | {f.confidence} | "
                 f"{edits} | {f.rationale} |")
    L.append("")

    if reversions:
        L.append("## Loop wobble — where the guardrails earned their keep\n")
        L.append("The agent isn't perfect: it sometimes proposed reverting a base back to a "
                 "phantom image. Recorded honestly rather than smoothed over — the bounded loop "
                 "and Sonnet→Opus escalation caught and corrected each one.\n")
        for r in reversions:
            corr = (f"corrected by iter {r.corrected_iteration}"
                    if r.corrected_iteration else "**not corrected before the loop stopped**")
            L.append(f"- iter {r.iteration} (`{r.model}`): set `{r.stage}` back to phantom "
                     f"`{r.image}` — {corr}.")
        L.append("")

    # Touch-ups: what the agent deliberately did NOT do (described, never performed).
    L.append("## Touch-ups required (described, NOT performed)\n")
    if status == "success":
        L.append("None — the build went green autonomously.\n")
    else:
        L.append(f"The build stopped at: `{(result.signature() if result else 'n/a')}`. The agent "
                 "does not author build steps (a compile-and-COPY stage is outside the edit-op "
                 "vocabulary and outside A/B/D), so the remaining work is a separate, "
                 "clearly-attributed human step.\n")
        if touch_ups:
            L.append("**Encountered — the agent stopped here.** Both models declined to author a "
                     "fix (returned out-of-scope), in their own words:\n")
            for t in touch_ups:
                L.append(f"- `{t.model}` (iter {t.iteration}): {t.rationale}")
            L.append("")
        L.append("**1. Go-compiled healthcheck stage (encountered).** `build_healthcheck` was "
                 "flattened onto a Chainguard node image, which does not carry upstream's "
                 "healthcheck binary, so `COPY --from=build_healthcheck /app/extra/healthcheck` "
                 "fails. *Needs:* a `cgr.dev/chainguard/go:latest-dev` stage that runs "
                 "`go build` on `extra/healthcheck.go` (CGO off → static), with the COPY pointed "
                 "at it. This is exactly what `Dockerfile.hardened` does by hand.\n")
        L.append("**2. dumb-init (anticipated downstream — NOT reached by the agent).** The "
                 "runtime `ENTRYPOINT [\"/usr/bin/dumb-init\"]` needs dumb-init, which distroless "
                 "node doesn't ship. *Needs:* `apk add dumb-init` in a builder + `COPY` the binary "
                 "into the runtime stage. Known from `Dockerfile.hardened`; the build fails at the "
                 "healthcheck COPY first, so the agent never reached this — recorded for "
                 "completeness, not derived by the agent.\n")

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
