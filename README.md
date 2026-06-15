# forge

**A pipeline that hardens an upstream container image with no existing Chainguard
equivalent: an LLM agent automates the structural conversion of Chainguard's `dfc`
output (phantom-base flattening, non-root), a human authors the build steps the
agent cannot, and CI ships the hand-built reference as a signed, SBOM'd image with a
measured CVE diff.**

Target: [`uptime-kuma`](https://github.com/louislam/uptime-kuma) (Node.js, MIT,
60k+ stars), chosen because it is **not** in Chainguard's catalog, so `dfc` has no
image to map it to. That gap is the problem this project addresses.

The signed, shipped image is the hand-built reference (`Dockerfile.hardened`); the
agent's output is committed separately (`Dockerfile.agent`) as an auditable partial
conversion that shows where the bounded edit-op loop stops.

---

## Where the CVEs went

Image hardening addresses the **OS/runtime layer** — the Debian/Wolfi packages and
language runtime under the app — so that is the layer this measures.

> ### OS/runtime-layer CVEs: **507 → 0**
> Every OS/runtime-layer CVE in the comparable upstream image is eliminated,
> including its **32 Critical** and **117 High**.

| Layer | upstream `2.4.0-slim-rootless` | forge hardened (ours) |
|---|--:|--:|
| **OS/runtime layer** (what hardening targets) | **507** | **0** |
| npm/application layer (the app's own deps) | 32 | 28 |

Full severity breakdown (real `grype` JSON, amd64 — see
[`docs/cve-report.md`](docs/cve-report.md)):

| Image | Critical | High | Medium | Low | Negligible | **Total** |
|---|--:|--:|--:|--:|--:|--:|
| **forge hardened (ours)** ⭐ | 1 | 17 | 8 | 2 | 0 | **28** |
| upstream `2.4.0-slim-rootless` | 33 | 135 | 225 | 24 | 120 | **539** |
| upstream `2.4.0` (full) | 179 | 902 | 798 | 76 | 230 | **2194** |

Total CVEs **539 → 28 (95% fewer)**. The layer decomposition above shows which
findings moved and which did not.

### npm/application layer (out of scope)

The residual **28 findings are 100% npm** — `protobufjs`, `@grpc/grpc-js`, `tar`,
`minimatch`, and the rest of uptime-kuma's own dependency tree. The npm layer is
essentially unchanged (32 → 28): hardening a base image does not patch an
application's JavaScript dependencies. That is the domain of
[**Chainguard Libraries**](https://www.chainguard.dev/libraries), a separate
product, and is out of scope here. The upstream image carries the same class of
findings.

### Why `slim-rootless` is the baseline

The comparison baseline is upstream **`2.4.0-slim-rootless`**, not the full image,
because it matches this project's scope: slim, non-root, no Chromium / MariaDB /
fonts. The full `2.4.0` image carries 2162 OS-layer CVEs across components excluded
here, so it is shown for context only. Same app version on both sides — uptime-kuma
`2.4.0`, source commit `8d36977`.

### Package surface and size

| Image | OS packages | Compressed (pull) | Uncompressed |
|---|--:|--:|--:|
| **forge hardened (ours)** ⭐ | 27 | 117 MB | 472 MB |
| upstream `2.4.0-slim-rootless` | 150 | 180 MB | 657 MB |

OS packages **150 → 27 (82% fewer)**. Compressed size **180 → 117 MB (35%
smaller)** — modest, because this target bundles every knex DB driver and full i18n
assets.

### Reproduce these numbers

Every figure above traces to a JSON under `.scan/` or a live `docker` query —
nothing is hand-typed. [`docs/cve-report.md`](docs/cve-report.md) records the scanned
image digests and the exact tool versions; the same scans rerun with:

```bash
# pinned tools, matching docs/cve-report.md: syft v1.45.1, grype v0.114.0
mkdir -p .scan

# upstream checkout pinned at commit 8d36977 (skip if you already have it)
git clone https://github.com/louislam/uptime-kuma build/uptime-kuma
git -C build/uptime-kuma checkout 8d36977569730b430c269c73c2e4d528e02ecc56

docker build -f targets/uptime-kuma/Dockerfile.hardened \
  -t forge/uptime-kuma:hardened build/uptime-kuma
docker pull louislam/uptime-kuma:2.4.0-slim-rootless
docker pull louislam/uptime-kuma:2.4.0

scan() { syft "$1" -o json=".scan/sbom_$2.json"; grype "$1" -o json=".scan/grype_$2.json"; }
scan forge/uptime-kuma:hardened               forge_uptime-kuma_hardened
scan louislam/uptime-kuma:2.4.0-slim-rootless louislam_uptime-kuma_2.4.0-slim-rootless
scan louislam/uptime-kuma:2.4.0               louislam_uptime-kuma_2.4.0

python3 scripts/gen_report.py   # regenerates docs/cve-report.md from the .scan/ artifacts
```

---

## What it is

Chainguard's ~1,300-image catalog is built **producer-side** with melange + apko
from Wolfi. Customers consume those images in ordinary multi-stage Dockerfiles, and
Chainguard ships [`dfc`](https://github.com/chainguard-dev/dfc) (Dockerfile
Converter) to help. `dfc` preserves an image's *basename* and only swaps the
registry/org — so for an app with **no Chainguard image**, it produces a
`FROM cgr.dev/chainguard/uptime-kuma:…` that does not exist (403 on pull).
Chainguard's docs call out the manual follow-up this needs: restructure the base,
restore non-root, fix package-name misses.

`forge` is a Python agent that automates that manual review-and-adjust loop — the
pattern Chainguard frames for `dfc`'s MCP mode: *"automation handles 90% of the
conversion and AI manages edge cases and custom logic."*

```
upstream Dockerfile (louislam/uptime-kuma — no Chainguard image)
   │
   ├─ dfc --json --org=chainguard        deterministic: rewrite FROM/RUN, apt→apk,
   │                                      swap registry → produces a PHANTOM base
   │
   ├─ docker buildx                       build attempt → captured failure log
   │
   ├─ AGENT LOOP (Python; LLM diagnoses + drafts edit-ops, bounded ~5 iters):
   │     A · phantom base → flatten to real node:latest-dev (build) → distroless
   │           node:latest (runtime)            ← class-A structural base flatten
   │     B · apt→apk name miss → resolve Wolfi APK, emit mappings.yaml (dfc format)
   │     D · dfc's injected USER root → restore non-root runtime (65532)
   │
   ├─ verify gate: starts · healthcheck 200 · runs non-root · grype scan
   └─ EXISTING pipeline: syft SBOM → grype → cosign keyless sign + attest → verify
```

The LLM does only **log diagnosis and fix-drafting** — the manual-review steps
`dfc`'s docs describe — emitted as a small vocabulary of structured edit-ops
(`replace_base_image`, `set_user`, …). It does **not** generate whole Dockerfiles;
the edit-op boundary defines the agent's limits (below). dfc, builds, scans, and
signing are deterministic code.

---

## The agent hand-off

The agent ran live against uptime-kuma and stopped at a defined boundary. The run
is recorded in
[`agent-provenance.md`](targets/uptime-kuma/agent-provenance.md) and the
[decisions log](docs/decisions.md).

**Agent, autonomously (class A + D), iteration 1 — one Sonnet diagnosis, 4 edits:**
- Identified `cgr.dev/chainguard/uptime-kuma:*` as a phantom base (403 on anonymous
  pull) and flattened all three stages to the Node pattern: `node:latest-dev`
  builders → distroless `node:latest` runtime.
- Set the runtime non-root (`65532:65532`).
- These base choices match the hand-built reference, selected autonomously.

**Agent, iteration 2 — it stopped:** the build then failed at
`COPY --from=build_healthcheck /app/extra/healthcheck: not found`. The phantom
`build_healthcheck` stage had been flattened onto a Node base that carries no such
binary. The fix is to author a new compile stage, and authoring build logic is
outside the agent's A/B/D edit-op vocabulary. Sonnet returned out-of-scope; the
one-hop escalation to Opus also returned out-of-scope, in its own words:
*"requires adding build steps to compile the healthcheck binary … out of scope for
the allowed edit vocabulary … requires a human touch-up."* Neither model emitted a
Dockerfile.

**Human, a separate and attributed pass (~10%):** authored the build steps the
edit-ops cannot express —
1. a `cgr.dev/chainguard/go:latest-dev` stage compiling upstream's Go healthcheck
   unchanged (CGO off → static);
2. `dumb-init` (apk in a builder + `COPY` into the distroless runtime);
3. the frontend build (`npm ci` → `npm run build` → `npm prune --omit=dev`);
4. pruned upstream's unused multi-target CI stages.

The result builds green and converges to the hand-built reference: non-root
`65532`, healthcheck `200`, OS/runtime-layer **0 Critical / 0 High**.

The split is committed as two commits — agent output first
([`30a2ec7`](https://github.com/tonyperkins/forge/commit/30a2ec7)), human pass
second ([`e082a03`](https://github.com/tonyperkins/forge/commit/e082a03)).

### Where conversion-automation ends

- **Covered (and demonstrated):** class **A** (phantom base / structural
  flattening), **B** (apt→apk package-name resolution → `mappings.yaml`, exercised
  and round-trip-verified separately), **D** (restore non-root). These are
  name-swaps and base restructuring.
- **Anticipated but did not occur:** native-module toolchain. uptime-kuma 2.4 uses
  `@louislam/sqlite3` (prebuilt N-API, no compile), so no build toolchain was
  needed. Handling for this class is future work, not coded.
- **The boundary:** the agent's output could not have gone green via more or better
  A/B/D edits. Every missing piece — Go compile, dumb-init, frontend build — is
  build-**authoring**, which the edit-op vocabulary does not express. A fail-fast
  build also surfaces these walls serially: each becomes visible only after the
  prior one clears, so they cannot be enumerated up front. The agent stopped at the
  first wall; the human pass cleared the serial set. That line — base flattening
  (automatable, A/B/D) vs. authoring new build logic — is where
  conversion-automation ends and a human build-author begins. dfc's framing
  ("automation handles 90%, AI manages edge cases") lands here: the agent did the
  structural 90%, the human authored the 10%.

---

## The signed artifact & supply chain

Two artifacts, kept separate:

- **`Dockerfile.hardened`** is the shipped, signed pipeline artifact — the
  hand-built reference, built and signed in CI on every push.
- **`Dockerfile.agent`** plus its provenance and the two-commit hand-off are the
  agent demonstration. The agent output converges to hardened but is not
  byte-identical, so CI signs the reference and the agent work stands alongside it.
  The agent output is partial on its own — it stops at the first build-authoring
  wall — so it is not presented as a standalone working image.

The CI pipeline ([`.github/workflows/forge.yml`](.github/workflows/forge.yml)):
**build → syft SBOM → grype scan → cosign keyless sign (GitHub OIDC → Fulcio →
Rekor) → signed SBOM + vuln attestations → verify gate → CVE report into the job
summary.** Committed Dockerfiles pin to digests/version streams, never `latest`.

**Signed image:** `ghcr.io/tonyperkins/uptime-kuma:latest`

The image rebuilds on every push, so verification is by **signing identity, not a
pinned digest**:

```bash
# signature
cosign verify \
  --certificate-identity-regexp 'https://github.com/tonyperkins/forge/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/tonyperkins/uptime-kuma:latest

# attached SBOM (SPDX) attestation
cosign verify-attestation --type spdxjson \
  --certificate-identity-regexp 'https://github.com/tonyperkins/forge/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/tonyperkins/uptime-kuma:latest

# attached vuln-scan attestation
cosign verify-attestation --type vuln \
  --certificate-identity-regexp 'https://github.com/tonyperkins/forge/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/tonyperkins/uptime-kuma:latest
```

---

## Known limitations & future work

Conscious scope boundaries, named rather than left implicit:

- **No automated test suite yet.** The deterministic modules (`dfc_runner`,
  `dockerfile`, `wolfi_resolver`, `verifier`) are the natural unit-test candidates;
  to date the agent is validated by live end-to-end runs, not a committed suite.
- **Single target.** Only `uptime-kuma` is converted; the A/B/D failure classes are
  the ones this target exercises. Other apps may surface other classes.
- **Single architecture.** Images are built and scanned amd64-only; arm64 is future
  work (a separate release job).
- **Minimal Dockerfile parser.** `agent/dockerfile.py` models the subset needed here
  (stages, `FROM`/`ARG`/`USER`, the edit-ops), not the full Dockerfile grammar.
- **`Dockerfile.agent` is partial and not shipped.** It stops at the first
  build-authoring wall by design; the signed artifact is the hand-built
  `Dockerfile.hardened`.
- **Residual npm CVEs are out of scope.** The 28 application-layer findings are
  uptime-kuma's own npm dependencies — Chainguard Libraries' domain.
- **melange/apko (Tier 2) not built.** Producer-side image authoring is a future
  stretch, not implemented here.

---

## Lineage

Chainguard's 2022 "Secure Software Factory" (melange + apko) → 2026 AI-native
Factory with agentic reconciliation loops to harden artifacts at scale. The agent
loop here is a miniature of that arc: deterministic tooling plus a bounded LLM loop
that diagnoses and adjusts, on one real target.

---

## How it was built

Built over a weekend, AI-accelerated. Every CVE number comes from real `grype` JSON
on real images (regenerate with `scripts/gen_report.py`); the agent's autonomous
fixes and the human touch-ups are tracked and attributed separately.

Python is the agent's language. The Go healthcheck is upstream's source compiled
unchanged through Chainguard's toolchain.

## Repo layout

```
forge/
├── CONTEXT.md                   engineering design doc — the §-referenced spec
├── forge_layers.py              shared OS/runtime-vs-application CVE classification
├── targets/uptime-kuma/
│   ├── Dockerfile.upstream      input (no Chainguard image exists for it)
│   ├── Dockerfile.converted     raw dfc output — the phantom base the agent fixes
│   ├── Dockerfile.hardened      hand-built reference — the SIGNED shipped artifact
│   ├── Dockerfile.agent         agent output + attributed human touch-ups
│   ├── agent-provenance.md      autonomous fixes vs. touch-up boundary (verbatim)
│   └── mappings.yaml            agent-resolved apt→apk (class B, dfc format)
├── agent/                       forge_agent.py loop + dfc_runner / wolfi_resolver /
│                                build_runner / verifier / llm.py (single LLM seam)
├── docs/
│   ├── cve-report.md            canonical CVE / size diff (source of every number)
│   └── decisions.md             running decision log + STATE OF PLAY anchor
├── scripts/gen_report.py        deterministic CVE/size diff → docs/cve-report.md
└── .github/workflows/forge.yml  build → SBOM → scan → sign → attest → verify → report
```

To reproduce the conversion: read `Dockerfile.converted` (dfc's phantom output)
against `Dockerfile.hardened` (the target) — the diff is the agent's job. To re-run
the agent: `KILO_API_KEY=… .venv/bin/python -m agent.forge_agent`.
