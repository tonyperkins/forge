# forge

**An agentic pipeline that converts and hardens an upstream container image with
no existing Chainguard equivalent — built *from* Chainguard's own tooling, not
beside it — and ships a signed, SBOM'd image with a real CVE diff.**

Target: [`uptime-kuma`](https://github.com/louislam/uptime-kuma) (Node.js, MIT,
60k+ stars) — deliberately chosen because it is **not** in Chainguard's catalog,
so `dfc` has no image to map it to. That gap is the whole problem this project
automates.

---

## The result first: where the CVEs went

Image hardening addresses the **OS/runtime layer** — the Debian/Wolfi packages and
language runtime under the app. So that is the layer to measure honestly.

> ### OS/runtime-layer CVEs: **507 → 0**
> Every OS/runtime-layer CVE in the comparable upstream image was eliminated —
> including its **32 Critical** and **117 High**.

That is the number this project is accountable for, and it is zero.

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

Total CVEs **539 → 28 (95% fewer)** — but the decomposition above is the real
story, because it says exactly *what* moved and *what didn't*.

### What we explicitly do **not** claim credit for

The residual **28 findings are 100% npm** — `protobufjs`, `@grpc/grpc-js`, `tar`,
`minimatch`, and the rest of uptime-kuma's own dependency tree. The npm layer is
**essentially unchanged (32 → 28), and that is correct**: hardening a base image
does not patch an application's JavaScript dependencies. That is the domain of
[**Chainguard Libraries**](https://www.chainguard.dev/libraries), a separate
product — out of scope here, and **no credit claimed for it.** The upstream image
carries the same class of findings.

Giving back the residual we *can't* claim is what makes the 507 → 0 we *can*
claim defensible.

### Why `slim-rootless` is the baseline

We compare against upstream **`2.4.0-slim-rootless`**, not the full image, because
it is the same scope as ours: slim, non-root, no Chromium / MariaDB / fonts. The
full `2.4.0` image (2162 OS-layer CVEs) would inflate the win by counting things
we deliberately exclude. Same app version on both sides — uptime-kuma `2.4.0`,
source commit `8d36977`.

### Package surface and size (reported straight)

| Image | OS packages | Compressed (pull) | Uncompressed |
|---|--:|--:|--:|
| **forge hardened (ours)** ⭐ | 27 | 117 MB | 472 MB |
| upstream `2.4.0-slim-rootless` | 150 | 180 MB | 657 MB |

OS packages **150 → 27 (82% fewer)**. Compressed size **180 → 117 MB (35%
smaller)** — modest and real: this target bundles every knex DB driver and full
i18n assets, so we report the size plainly rather than force a narrative it can't
support.

---

## What it is

Chainguard's ~1,300-image catalog is built **producer-side** with melange + apko
from Wolfi. **Customers consume** those images in ordinary multi-stage
Dockerfiles, and Chainguard ships [`dfc`](https://github.com/chainguard-dev/dfc)
(Dockerfile Converter) to help. But `dfc` preserves an image's *basename* and only
swaps the registry/org — so for an app with **no Chainguard image**, it produces a
`FROM cgr.dev/chainguard/uptime-kuma:…` that **does not exist** (403 on pull).
Chainguard's own docs call out the manual follow-up this needs: restructure the
base, restore non-root, fix package-name misses.

`forge` is a Python agent that **automates that manual review-and-adjust loop** —
the exact pattern Chainguard frames for `dfc`'s MCP mode: *"automation handles 90%
of the conversion and AI manages edge cases and custom logic."* This is a working
implementation of that sentence, and it is honest about where the 90% ends.

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
   │           node:latest (runtime)            ← the marquee restructuring decision
   │     B · apt→apk name miss → resolve Wolfi APK, emit mappings.yaml (dfc format)
   │     D · dfc's injected USER root → restore non-root runtime (65532)
   │
   ├─ verify gate: starts · healthcheck 200 · runs non-root · grype scan
   └─ EXISTING pipeline: syft SBOM → grype → cosign keyless sign + attest → verify
```

The LLM does only **log diagnosis and fix-drafting** — the "manual review" steps
`dfc`'s docs describe — emitted as a small vocabulary of structured edit-ops
(`replace_base_image`, `set_user`, …). It does **not** generate whole Dockerfiles
from scratch: that is off-pattern and undefensible, and the edit-op boundary is
what makes the agent's limits legible (below). dfc, builds, scans, and signing
stay deterministic code.

---

## The hand-off story (the honest part)

The agent ran live against uptime-kuma and **stopped cleanly at a boundary it was
designed to respect.** Here is exactly what happened — recorded in
[`agent-provenance.md`](targets/uptime-kuma/agent-provenance.md) and the
[decisions log](docs/decisions.md).

**Agent, autonomously (class A + D), iteration 1 — one Sonnet diagnosis, 4 edits:**
- Recognized `cgr.dev/chainguard/uptime-kuma:*` as a phantom base (403 on
  anonymous pull) and **flattened all three stages** to the real Node pattern:
  `node:latest-dev` builders → distroless `node:latest` runtime.
- **Set the runtime non-root** (`65532:65532`).
- These base choices **match the hand-built reference exactly** — picked
  autonomously, not copied.

**Agent, iteration 2 — it stopped, correctly:** the build then failed at
`COPY --from=build_healthcheck /app/extra/healthcheck: not found`. The phantom
`build_healthcheck` stage had been flattened onto a Node base that carries no
such binary. The fix is to **author a new compile stage** — and authoring build
logic is outside the agent's A/B/D edit-op vocabulary by design. Sonnet declined
as out-of-scope; the one-hop **escalation to Opus also declined**, in its own
words: *"requires adding build steps to compile the healthcheck binary … out of
scope for the allowed edit vocabulary … requires a human touch-up."* The contract
held: neither model tried to write a Dockerfile.

**Human, a separate and clearly-attributed pass (~10%):** authored the
build-authoring steps the edit-ops can't express —
1. a `cgr.dev/chainguard/go:latest-dev` stage compiling upstream's Go
   healthcheck (CGO off → static; **upstream's source, unchanged** — running their
   toolchain, not authoring Go);
2. `dumb-init` (apk in a builder + `COPY` into the distroless runtime);
3. the frontend build (`npm ci` → `npm run build` → `npm prune --omit=dev`);
4. pruned upstream's unused multi-target CI stages as dead cruft.

The result builds green and **converges to the hand-built reference**: non-root
`65532`, healthcheck `200`, OS/runtime-layer **0 Critical / 0 High**. Convergence
confirms the agent's autonomous A/D portion was right.

This split is committed as two honest commits — agent output first
([`30a2ec7`](https://github.com/tonyperkins/forge/commit/30a2ec7)), human pass
second ([`e082a03`](https://github.com/tonyperkins/forge/commit/e082a03)) — never
folded together.

### Where conversion-automation ends (limitations, stated plainly)

This is the most useful finding, so it gets its own section.

- **Covered (and demonstrated):** class **A** (phantom base / structural
  flattening), **B** (apt→apk package-name resolution → `mappings.yaml`, exercised
  and round-trip-verified separately), **D** (restore non-root). These are
  name-swaps and base restructuring — automatable.
- **Anticipated but did NOT occur:** native-module toolchain. uptime-kuma 2.4 uses
  `@louislam/sqlite3` (prebuilt N-API, no compile), so no build toolchain was
  needed. We **did not** build handling for a failure class this target never
  produced — it's noted as future work, not coded.
- **The real wall:** the agent's output **could not** have gone green via more or
  better A/B/D edits. Every missing piece — Go compile, dumb-init, frontend build
  — is build-**authoring**, which the edit-op vocabulary deliberately cannot
  express. And a **fail-fast build hides these serial walls**: each only becomes
  visible after the prior one clears, so they can't be enumerated up front. The
  agent didn't "miss" them; it stopped at the first wall, and the human pass
  cleared the serial set. That line — base flattening (automatable) vs. authoring
  new build logic (not) — is exactly **where conversion-automation ends and a
  human build-author begins.** dfc's "automation does 90%, AI manages edge cases"
  lands precisely here: the agent did the structural 90%, the human authored the
  10%.

---

## The signed artifact & supply chain

Two distinct things, kept distinct on purpose:

- **`Dockerfile.hardened`** is the **shipped, signed pipeline artifact** — the
  hand-built reference, built and signed in CI on every push.
- **`Dockerfile.agent`** + its provenance + the two-commit hand-off are the
  **agent demonstration**. The agent output converges to hardened but isn't
  byte-identical, so we sign the clean reference and let the agent work stand
  alongside as the "90% / 10%" demo. *(The agent output is partial on its own —
  it stops at the first build-authoring wall — so it is not presented as a
  working image the agent produced alone. The two-commit split is the truth and
  the better story.)*

The CI pipeline ([`.github/workflows/forge.yml`](.github/workflows/forge.yml)) is
green: **build → syft SBOM → grype scan → cosign keyless sign (GitHub OIDC →
Fulcio → Rekor) → signed SBOM + vuln attestations → hard verify gate → CVE report
into the job summary.** Pin to digests/version streams, never `latest`, in every
committed Dockerfile.

**Signed image:** `ghcr.io/tonyperkins/uptime-kuma:latest`

Anyone can verify it — the image rebuilds on every push, so verify by **signing
identity, not a pinned digest**:

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

## Lineage

Chainguard's 2022 "Secure Software Factory" (melange + apko) → 2026 AI-native
Factory with agentic reconciliation loops to harden artifacts at scale. The agent
loop here is a deliberate miniature of that arc: deterministic tooling plus a
bounded LLM loop that diagnoses and adjusts, walking the 2022 → 2026 path on one
real target.

---

## How it was built

A weekend, AI-accelerated — which is the author's actual working style, not a
disclaimer. The honesty bar throughout: every CVE number comes from real
`grype` JSON on real images (regenerate with `scripts/gen_report.py`); the agent's
autonomous fixes vs. the human touch-ups are tracked and attributed rather than
smoothed into a suspiciously perfect result.

The author is a senior platform / infrastructure / SRE engineer; Python is the
agent's language. **melange/apko (Tier 2)** is noted as a future stretch and is
**not built** here — no mastery claimed. The Go healthcheck is upstream's source
compiled unchanged through Chainguard's toolchain, not authored Go.

## Repo layout

```
forge/
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
against `Dockerfile.hardened` (the target) — the diff is the agent's job. To
re-run the agent: `KILO_API_KEY=… .venv/bin/python -m agent.forge_agent`.
