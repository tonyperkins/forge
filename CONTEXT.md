# CONTEXT — engineering design doc for `forge`

The design constraints, architecture, and boundaries the code and docs reference by
section number (`CONTEXT §N`). It is the project's spec: there is intentionally no
separate `spec.md`. Running decisions and their rationale live in
[`docs/decisions.md`](docs/decisions.md); this file is the stable reference those
decisions are made against.

---

## §1 — Purpose

Convert and harden an upstream container image that has **no existing Chainguard
catalog equivalent**, using Chainguard's own tooling, and ship a signed, SBOM'd
image with a measured CVE diff.

The target is [`uptime-kuma`](https://github.com/louislam/uptime-kuma) (Node.js,
MIT). It is chosen precisely because it is **not** in Chainguard's catalog, so
`dfc` (the Dockerfile Converter) has no image to map it to: `dfc` preserves an
image's basename and only swaps the registry/org, producing a
`FROM cgr.dev/chainguard/uptime-kuma:…` that does not exist (403 on pull). Closing
that gap — turning a non-cataloged app into a hardened, signed image — is the
project premise.

---

## §2 — Honesty guardrails

These are non-negotiable; violating them invalidates the result.

- **Every metric comes from a real scan.** CVE and size numbers are read from actual
  `grype`/`syft` output or a live `docker` query — never estimated or hand-typed
  (see §10). The report regenerates from on-disk artifacts.
- **We run their tools; we do not author what we are demonstrating expertise in
  consuming.** Upstream's Go healthcheck is *compiled unchanged* through Chainguard's
  Go image — we run the toolchain, we do not write Go. melange/apko authoring is out
  of scope (§6 step 4).
- **Agent work and human work are attributed separately.** The agent's autonomous
  fixes and the human touch-ups are tracked in `agent-provenance.md` and committed as
  distinct commits, never folded together to look like one smooth result.
- **Do not manufacture failure classes.** The agent's scope is the failure classes
  this target actually produces (§4). We do not restore excluded packages or switch
  targets to fabricate a failure the real path never hit; a class that did not occur
  is documented as "anticipated, did not occur," not faked.

---

## §3 — Constraints & expected failure surface

**Tooling.** Only free/OSS tooling and **public (free-tier) Chainguard images** are
used — no paid dev-tier image slots. The pipeline tools (`syft`, `grype`, `cosign`,
`dfc`) are pinned to specific releases so results are reproducible (see §10 and
`docs/cve-report.md`).

**Expected failure surface from a naive `dfc` conversion.** Before building, the
anticipated work that `dfc` alone does not finish:

- **Package-name remapping is the expected #1 class.** `dfc` passes Debian apt
  package names through to `apk` unchanged; many need a Wolfi rename (or have no
  Wolfi equivalent). This must be resolved against the live Wolfi index, not assumed.
- **Non-root must be restored.** `dfc` injects `USER root` into install stages; the
  runtime stage must end up non-root.
- **Structural base flattening.** The phantom base (§1) has to be replaced with a
  real Chainguard base, restructured into a normal multi-stage build.

These anticipated classes are formalized as the agent's scope in §4.

---

## §4 — Architecture: the deterministic / LLM boundary

The central design rule. The pipeline is **deterministic code**; the LLM has a
narrow, auditable role.

**Deterministic (no LLM):** running `dfc`, building images, scanning (`syft`,
`grype`), signing/attesting (`cosign`), the verify gate, and report generation. All
of this is plain Python/CI and never depends on model output.

**LLM (diagnosis + fix-drafting only):** on a build failure, the LLM reads the
captured log and deterministic signals and proposes a small vocabulary of **typed
edit-operations** (`replace_base_image`, `set_user`, `add_package`, …). It **never
emits Dockerfile text** and never authors new build logic. Malformed or
out-of-vocabulary output is a hard error, never accepted as free text — relaxing to
free text would silently widen the LLM's remit, which this boundary forbids. The LLM
surface lives in one file (`agent/llm.py`) so the boundary is auditable in one place.

**Locked failure-class scope — A / B / D** (the classes this target actually
produces; see §3 and `docs/decisions.md`):

- **A — Phantom base image / structural flattening.** `dfc` maps
  `louislam/uptime-kuma:*` to a nonexistent `cgr.dev/chainguard/uptime-kuma:*`. Fix:
  flatten onto real bases — `cgr.dev/chainguard/node:latest-dev` (build) → distroless
  `node:latest` (runtime).
- **B — apt→apk package-name misses.** `dfc` leaves Debian names unmapped. The
  resolver checks each against the live Wolfi index and emits `mappings.yaml` (the
  `dfc --mappings` format), reporting three buckets: mapped / already-correct / no
  Wolfi equivalent. It never silently drops or force-matches a package.
- **D — `USER root` insertions → restore non-root.** Restore a non-root runtime user
  (`65532:65532`).

**Out of scope by design — build authoring.** There is deliberately no edit-op for
"add a compile stage" or "add a build step." Authoring new build logic (e.g. a Go
compile stage, a frontend build) is a human task. When the LLM diagnoses a failure
that would require it, the agent stops at a **documented touch-up boundary** rather
than authoring — that boundary is the point, not a gap.

**Anticipated-but-absent class.** A native-module build toolchain was anticipated but
did **not** occur: uptime-kuma 2.4 persists via `@louislam/sqlite3` (a prebuilt
N-API binary, no compile). Per §2 this is documented, not built for.

**Vulnerability-layer classification.** The verify gate and the report split CVEs
into the OS/runtime layer (what hardening controls) vs the application layer
(uptime-kuma's npm deps). That classification is centralized in
[`forge_layers.py`](forge_layers.py); the gate fails on an OS/runtime-layer Critical
and reports — does not gate — the application layer.

---

## §5 — Module layout

The agent is a small set of single-responsibility modules:

- `agent/forge_agent.py` — the bounded `build → diagnose → apply → rebuild` loop
  (≤ ~5 iterations), scope-guarded to A/B/D, with loud failure on exhaustion.
- `agent/dfc_runner.py` — runs `dfc` and structures its JSON output (deterministic).
- `agent/wolfi_resolver.py` — class B: resolves the apt surface against the live
  Wolfi index → `mappings.yaml`.
- `agent/dockerfile.py` — a minimal Dockerfile model (parse stages, resolve
  `FROM $ARG`, compute a target's stage closure, apply edit-ops, render).
- `agent/build_runner.py` — `docker buildx` + log capture, a real registry probe
  (turns "phantom base" from a guess into a fact), and a failure-signature
  fingerprint used for model escalation.
- `agent/verifier.py` — the hardening gate: non-root + healthcheck + grype layer
  gate.
- `agent/llm.py` — the **only** LLM call site (§4 boundary). Default model
  `anthropic/claude-sonnet-4.6`; one-hop escalation to `anthropic/claude-opus-4.8`
  when a Sonnet-drafted fix fails to move the build. Transport is OpenAI-compatible
  (Kilo Gateway); credentials come from `os.environ` (`KILO_API_KEY`).
- `scripts/gen_report.py`, `scripts/cve_summary.py` — deterministic report
  generation, sharing `forge_layers.py`.

The agent is **local-only**. CI (§6 step 2) never calls the LLM and holds no LLM
credentials.

---

## §6 — Build order

1. **Hand-built hardened image.** `targets/uptime-kuma/Dockerfile.hardened` — the
   reference build: `cgr.dev/chainguard/node:latest-dev` builders → distroless
   `node:latest` runtime, non-root `65532`, upstream's Go healthcheck compiled
   unchanged in a `go:latest-dev` stage, `dumb-init` via apk + `COPY`. This is the
   shipped, signed artifact. (The native-build "fight" anticipated here did not occur
   — see §4.)
2. **CI supply-chain pipeline.** `.github/workflows/forge.yml`: build → push (ghcr) →
   SBOM (`syft`) → scan (`grype`) → keyless `cosign` sign + signed SBOM/vuln
   attestations → verify gate → CVE report into the job summary. Keyless throughout:
   GitHub OIDC → Fulcio → Rekor; no private keys.
3. **The agent.** `agent/forge_agent.py` reproduces the structural conversion
   autonomously (§4) and stops at the documented touch-up boundary, emitting
   `Dockerfile.agent` + `agent-provenance.md`. Presented as a demonstration alongside
   the signed hand-built artifact, not wired into CI as a second signed image.
4. **Tier 2 — melange/apko (optional, not built).** Producer-side image authoring is
   a hard-timeboxed future stretch, out of scope for a complete, presentable artifact.

---

## §7 — Pinning

Every committed Dockerfile pins bases by **digest**. A tag such as `latest-dev` may
appear only as a human-readable stream label *before* the `@sha256:…`
(e.g. `node:latest-dev@sha256:…`); the digest is authoritative and is what the build
resolves — the tag never floats the committed artifact. Tool versions are pinned in
CI and recorded in `docs/cve-report.md` (§10). After a green build, the agent
rewrites its base references to the resolved digests so the emitted Dockerfile is
pinned too.

---

## §8 — Documentation standards (README)

- **Lead with the measured result, decomposed honestly.** The headline is the
  OS/runtime-layer CVE reduction (the layer hardening controls); total CVEs are
  supporting context. The application (npm) layer is shown as out of scope.
- **Attribute agent vs. human** in the hand-off narrative; do not imply the agent
  produced the shipped image.
- **No overclaiming.** Describe the base as minimal/distroless + non-root, not
  "shell-less" (it bundles busybox). Report size plainly; do not force a size
  narrative the target cannot support.
- **Numbers are reproducible.** The README points at the regeneration recipe and the
  recorded digests/tool versions, so any figure can be independently checked.
- **Name limitations.** Conscious scope boundaries are listed, not hidden.

---

## §9 — Delivery

The deliverable is a **publicly verifiable** signed image: anyone can
`cosign verify` (and `verify-attestation`) it by signing identity, plus the repo
with the hardened/agent Dockerfiles, the CVE report, the decision log, and this
design doc. Making the ghcr package public is a deliberate, separate step from
publishing the repo.

---

## §10 — Verify before assuming

Empirical discipline. Claims about images, package indexes, tool behavior, and sizes
are **verified by running the thing**, not assumed from memory or documentation —
and the verification is recorded in `docs/decisions.md`. Examples this discipline
caught: a package assumed to be Debian-only that in fact exists in Wolfi; a base
assumed shell-less that bundles busybox; the absence of the anticipated native build.
Every number in `docs/cve-report.md` traces to a scan artifact or a live query, with
the scanned image digests and exact tool versions recorded alongside it.
