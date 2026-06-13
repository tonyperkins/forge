# Decisions log — `forge`

Running log per CONTEXT.md §10. Dated bullets: *what* was decided/discovered, *why*
(one clause). Not prose. This log is the emergent spec of the project; it feeds the
README's honest-limitations section directly. There is intentionally no `spec.md`.

---

## 2026-06-13 — Session 1: environment + dfc baseline

### Environment verified (CONTEXT §10 "verify before assuming")
- Host: Pop!_OS 24.04 LTS (Ubuntu 24.04 base), x86_64. Docker 29.5.3 daemon live.
- Pre-installed: docker 29.5.3, buildx v0.34.1, node v20.20.2, python 3.12.3.
- Missing on entry — installed to `~/.local/bin` (all free/OSS, no dev-tier slots spent, per §3):
  - syft 1.45.1, grype 0.114.0 (via official anchore `install.sh`)
  - cosign v3.1.1, dfc v0.10.0, melange v0.53.1, apko v1.2.16 (GitHub release binaries)
- No Go toolchain on host; deliberately **not** installed — we run their tools, we don't
  author Go (§2). Revisit only if a melange step needs it.
- `cgr.dev/chainguard/node:latest` and `cgr.dev/chainguard/python:latest` public pulls
  confirmed working. Digests at fetch:
  - node:   `sha256:27bf957bdf6d189108c8908c958fd966d9814f78e7172c2d791940f4e208a334`
  - python: `sha256:6a9e1eed2c9f3ea955a63455c0417a2177f5ce669d2587da6f7d01d738c683d6`

### Upstream target captured
- uptime-kuma's Docker build uses **base-image indirection**, not a single Dockerfile:
  - `docker/dockerfile` builds `FROM louislam/uptime-kuma:base2` (+ `:builder-go`, `:base2-slim`)
  - those bases are pre-built upstream images defined in `docker/debian-base.dockerfile`
    and `docker/builder-go.dockerfile`.
- Saved `docker/dockerfile` → `targets/uptime-kuma/Dockerfile.upstream`.
- Saved `docker/debian-base.dockerfile` → `targets/uptime-kuma/Dockerfile.upstream-base`
  (this is where the real `apt install` / package-mapping surface lives).
- Pinned to upstream `master` @ `8d36977569730b430c269c73c2e4d528e02ecc56` (2026-06-13 fetch).

### dfc v0.10.0 `--json --org=chainguard` baseline (= agent test-case inventory)
Ran on both files. Findings, grouped by failure class:

- **A. Phantom image mapping (project premise).** dfc preserves the image *basename* and
  only swaps registry/org, so `louislam/uptime-kuma:{base2,builder-go,base2-slim}` →
  `cgr.dev/chainguard/uptime-kuma:latest[-dev]`. **That image does not exist** — the whole
  point of the project (§1). Affects 4 FROM lines + the `ARG BASE_IMAGE` default.
  Real fix is structural: flatten the indirection onto `cgr.dev/chainguard/node:*-dev`
  (build) → distroless `node` (runtime); handle the Go healthcheck builder separately.
- **B. Package-name passthrough (the #1 failure class, §3).** dfc dedup/sorted the apt
  package lists and assumed identical Wolfi names — it did **not** actually remap any of
  them. Candidates to verify against the Wolfi apk index (NOT yet verified — that is the
  agent's `wolfi_resolver` job): `iputils-ping`, `nscd`, `sqlite3`, `ca-certificates`,
  `dumb-init`, `sudo`, `util-linux`, `cloudflared`, `chromium`, `fonts-indic`,
  `fonts-noto`, `fonts-noto-cjk`, `mariadb-server`, `python3-paho-mqtt`. Several are
  near-certain Debian-only names (`iputils-ping`→`iputils`, `fonts-*`→`font-*`,
  `python3-paho-mqtt`→`py3-paho-mqtt`) but each gets confirmed by `apk search`, not asserted.
- **C. Invalid conversion artifacts.** `apk add ./apprise.deb` — dfc fed a Debian `.deb`
  path straight to `apk`, which cannot install a `.deb`; the whole download-a-.deb apprise
  strategy is invalid on Wolfi. Plus dead Debian apt-repo plumbing left intact after the
  install was folded to apk (github-cli gpg/`echo … sources.list.d`, cloudflare gpg/echo).
- **D. `USER root` insertions.** dfc injected `USER root` into the dev/apt stages to allow
  installs; runtime `release` stage was left as-is. Per §3 we restore non-root at runtime
  manually (the agent automates this).
- **E. Dynamic base.** `FROM $BASE_IMAGE` flagged `baseDynamic`; dfc rewrote the ARG default
  to the phantom image (see A) but correctly left the `$BASE_IMAGE` reference as a variable.
- **F. Invisible to dfc.** `better-sqlite3` native build (the expected fight, §6) and the
  apprise/python runtime deps are transitive `npm ci` / dpkg concerns — they don't appear
  in dfc output and will only surface at build time. Reinforces: dfc sees the Dockerfile,
  not the dependency graph.

**Next (pending owner input):** verify category B against the live Wolfi index, then begin
the manual hardened multi-stage build (§6 step 1). Not started this session — paused after
dfc baseline per session plan.

---

## 2026-06-13 — Session 1 (cont.): scope decision + hardened build path

### Scope: defensible core, not full feature set (owner decision)
Harden **uptime-kuma + node runtime + SQLite (better-sqlite3), non-root, multi-arch.**
Minimizing attack surface *is* the point — a hardened image bundling Chromium/MariaDB/
cloudflared would be a bad Chainguard image. Excluded, each with one-line rationale:
- **MariaDB / mariadb-server** — SQLite is the default backend; MariaDB is optional
  external-DB convenience, not core.
- **Chromium + CJK fonts** (`chromium`, `fonts-indic`, `fonts-noto`, `fonts-noto-cjk`) —
  only used by the screenshot feature; huge attack surface for a monitoring tool.
- **cloudflared** — bundled tunnel convenience; orthogonal to monitoring, large surface.
- **apprise** (`./apprise.deb`, `python3-paho-mqtt`) — one notification backend; uptime-kuma
  has many native notification providers that need no extra OS packages.
- **nscd, sudo** — Debian-shaped DNS-cache-via-sudo mechanism; irrelevant to a non-root
  distroless image (glibc does its own resolution; no privilege-drop dance needed).
- Each exclusion goes in the README too (the judgment is part of the demo).

### dfc class C (apprise .deb) — closed
`apk add ./apprise.deb` is invalid on Wolfi (apk can't install a Debian .deb) **and**
apprise is out of scope. Logged, not solved. Done.

### Go healthcheck — in scope, compiled not authored
`extra/healthcheck.go` is a self-contained stdlib-only `package main` (no go.mod). Upstream
builds it with `go build -o extra/healthcheck extra/healthcheck.go`. We replicate that
unchanged in a `cgr.dev/chainguard/go:latest-dev` stage (`CGO_ENABLED=0` → static binary),
then COPY the binary into the distroless runtime. Running their Go toolchain on upstream's
code = in scope; authoring Go = not (§2). If it fights, stop and ask.

### Verified build facts (run, not assumed)
- Chainguard tags pull on the free tier: `node:latest` / `node:latest-dev` = **node v26.3.0**
  (engines need ≥20.4.0 ✓), `go:latest-dev` = go 1.26.4, `wolfi-base:latest`.
- Distroless `node:latest` **defaults to non-root** `node` uid/gid 65532 — non-root is the
  base default, not something we bolt on. Node ships **120 TLS roots** built in → no extra
  `ca-certificates` package needed for HTTPS monitoring.
- **The curated free repo `apk.cgr.dev/chainguard` is minimal**: `build-base`, `dumb-init`,
  `ca-certificates-bundle` present; **`python3`/`py3-setuptools` absent**, and `apk search`
  returns nothing (resolvable-but-not-listable index). `curl`/`wget` not in it either.
- node-gyp needs python3 to compile `better-sqlite3` (transitive via `redbean-node ~0.3.3`;
  node 26 is too new for prebuilds → source compile expected, the §6 fight). So the builder
  must add the **Wolfi OS repo** `packages.wolfi.dev/os` (Chainguard images are built from
  Wolfi). There it resolves: `build-base python-3.13 py3.13-setuptools` (also tini/iputils/
  util-linux/sqlite if later needed).
- **Wolfi key handling (clean + reproducible):** copy `/etc/apk/keys/wolfi-signing.rsa.pub`
  from the official `cgr.dev/chainguard/wolfi-base` image via `COPY --from` — no vendored
  key, no build-time network fetch, fully digest-pinnable. (curl-based fetch ruled out: curl
  isn't in the curated repo, chicken-and-egg with the key.)

### Pinned base digests (this build)
- go:latest-dev   `sha256:c14a464b801730991755d178ad7e59f9756e72b585e98f2a24293588fae12ad1`
- node:latest-dev `sha256:f2fab62fb18ddc1279344e7a05fb37169d4e3e12b9ca9a9048b408137a14618c`
- node:latest     `sha256:27bf957bdf6d189108c8908c958fd966d9814f78e7172c2d791940f4e208a334`
- wolfi-base       `sha256:34977aa13765da89f60fee8fe5230e2bb1c55192df08e383c58221ee0d1277fb`

### Build context
Upstream source is cloned at the pinned commit (`8d36977`) into a gitignored working dir;
not vendored into the repo. The committed artifacts are the Dockerfiles + this log. CI will
clone the same way.

### Deferred (documented caveat, not silently dropped)
- **ping monitors / iputils** — ping needs raw sockets (`CAP_NET_RAW`), awkward for a non-root
  distroless image. Getting the app building & running non-root (UI + HTTP monitoring +
  SQLite persistence) comes first; ping is revisited with an explicit capability caveat.

---

## 2026-06-13 — Session 1 (cont.): manual hardened build WORKS + a premise correction

### Outcome: `Dockerfile.hardened` builds & runs non-root ✓ (§6 step 1 met)
- `docker buildx build` (amd64) succeeds. Container boots: node 26.3.0, uptime-kuma 2.4.0,
  SQLite DB initializes, "Waiting for user action", serves HTTP 302 on :3001.
- **Non-root verified**: image `USER 65532:65532`; PID-1 dumb-init, node, and the worker all
  run as uid 65532. Non-root user can't write outside `/app/data` (EACCES).
- **Healthcheck verified**: upstream's Go `healthcheck` (compiled unchanged in `go:latest-dev`,
  static, CGO off) returns `Health Check OK [Res Code: 200]`, exit 0, inside the container.
- Three committed Dockerfile variants now exist: `.upstream`, `.converted` (raw dfc output —
  shows the phantom `cgr.dev/chainguard/uptime-kuma` images + dead apt-repo plumbing that
  make naive dfc insufficient), `.hardened`.

### ⚠ Premise correction (CONTEXT §6): there is NO better-sqlite3 native-build fight
- uptime-kuma **2.4.0 does not use better-sqlite3.** It persists via **`@louislam/sqlite3`**
  (v15.1.6) through knex. That package ships a **prebuilt N-API binary** (`napi-v6-linux-x64/
  node_sqlite3.node`); N-API ABI stability means it loads on node 26 **without compiling**.
- Verified: build log shows **0** node-gyp/gyp/prebuild compile events; only prebuilt `.node`
  files exist in the image (`@louislam/sqlite3`, `oracledb` — both N-API prebuilts).
- Consequence 1: the python3/build-base/Wolfi-repo toolchain I first added was **never used** —
  removed it. Build stage now only `apk add dumb-init` (curated chainguard repo; Wolfi repo not
  needed after all). The runtime image is **byte-identical** before/after the removal (same
  digest) — clean multi-stage hygiene: build-only deps never reached the runtime.
- Consequence 2 (**affects the agent's spec**): the **"native-module toolchain" failure class
  in CONTEXT §4 is not exercised by this target.** The manual path did not produce it, so per
  §6/§10 the agent must not speculatively implement it. The failure classes this target *does*
  produce (the real agent scope) are: (A) phantom image-mapping / structural base-image
  flattening, (B) apt→apk package-name misses, (D) USER-root → restore non-root. Native-toolchain
  becomes documented "anticipated, did not occur" — honest README material, not a feature to fake.

### Other honest notes
- `cgr.dev/chainguard/node:latest` is **not fully shell-less**: it bundles busybox
  (`/bin/sh → /bin/busybox`). Describe as minimal/distroless-style + non-root, not "shell-less".
- Image size **590 MB** (amd64). Heavier than ideal: uptime-kuma bundles every knex DB driver
  (e.g. `oracledb` carries 5 per-platform prebuilts) plus full i18n locale assets in `dist/`.
  Trimming = future optimization; the real number to publish is the grype/size **diff vs the
  upstream `louislam/uptime-kuma` image**, generated by the report step (not yet run).
- Frontend `dist/` is gitignored upstream → the hardened build runs `npm run build` (vite)
  itself, then `npm prune --omit=dev`, so the image is self-contained (no host pre-build).

**Next (pending owner input):** the build goal is met. Before CI: decide how the §6 premise
correction reshapes the agent scope, and whether to add a SBOM(syft)+scan(grype)+sign(cosign)
pipeline next as planned. Paused here per "running non-root before we touch CI".

---

## 2026-06-13 — Session 1 (cont.): first real CVE/size diff (syft + grype)

### Owner decisions logged
- **Agent scope = A/B/D, stay with uptime-kuma.** Do NOT switch to changedetection.io to
  manufacture a native-build failure (§2). Native-toolchain class = "anticipated, did not
  occur for this target" — honest adaptation, README material, not a gap.
- **Diff framing:** lead with the genuinely strong metric (CVE), present size plainly even if
  modest; never force a size narrative this target can't support.

### Comparison baseline (fairness)
Compared our hardened image against upstream **`louislam/uptime-kuma:2.4.0-slim-rootless`**
as the apples-to-apples baseline (same scope: slim — no Chromium/MariaDB/fonts — and non-root).
Full `2.4.0` shown for context only; leading with it would overstate the win (it bundles
Chromium+MariaDB+fonts we deliberately exclude). Both sides are app version 2.4.0.

### Real numbers (grype/syft JSON under .scan/, regen via `scripts/gen_report.py`)
- **Total CVEs: 539 → 28 (95% fewer) vs slim-rootless.** Critical 33 → 1, High 135 → 17.
- **Honest decomposition (the real story):**
  - **OS/runtime-layer CVEs: 507 → 0** — image hardening eliminated that entire layer (incl.
    32 Critical, 117 High). This is the layer the project actually targets.
  - **npm/application-layer CVEs: 32 → 28 — essentially unchanged, and that's correct.** Base
    hardening doesn't patch an app's npm deps; that's **Chainguard Libraries'** domain, out of
    scope. We claim no credit for it. Our 28 residual are 100% npm (protobufjs/grpc/tar/
    minimatch/glob/lodash); the 1 Critical = `GHSA-xq3m-2v4x-88gg` in `protobufjs 7.2.6`.
- **OS packages: 150 → 27 (82% fewer).** Total packages (syft): 1116 → 869 (npm counts close —
  same app).
- **Size (modest, reported straight):** compressed pull size 180 MB → **117 MB (35% smaller)**;
  uncompressed 657 MB → 472 MB. Heavier than ideal because uptime-kuma bundles every knex DB
  driver (oracledb ships 5 per-platform prebuilts) + full i18n assets — not trimmed.

### Method notes (so numbers reproduce)
- `scripts/gen_report.py` reads the grype/syft JSON and queries `docker` live — no hand-typed
  metrics. CVE counts de-duplicated to distinct (vuln-id × package).
- **Size reporting under Docker 29 containerd store is inconsistent**: `docker images` (590 MB
  ours), `docker inspect .Size` (117 MB), `docker history` sum (472 MB) all differ. Resolved by
  validating that `inspect .Size` == the amd64 **registry manifest's** summed compressed layer
  blobs (upstream: 180/601 MB matched exactly) → use that as "compressed/pull size"; `docker
  history` sum as "uncompressed". Stated explicitly in the report.
- grype DB downloaded fresh this run; scans are amd64 single-arch (§6 step 2 order).

### Still TODO for the pipeline skeleton (not yet done)
- **cosign keyless signing + the `.github/workflows/forge.yml`** (build→SBOM→scan→sign→report).
  Keyless signing needs registry + OIDC, so it's wired in CI (GitHub OIDC), not run locally.
  Paused here per "pause after the first real diff so I can see the actual numbers".

---

## 2026-06-13 — Session 1 (cont.): CI pipeline (build→sign→attest→verify→report)

### Owner decisions
- Wire CI next to **close the supply-chain loop before the agent** — signing is the one step
  that can't be shown locally and is the most on-thesis (verifiable provenance). Banks a whole,
  presentable, publicly-verifiable artifact = Saturday's exit criterion.
- **ghcr images public under the owner's GitHub identity = conscious yes.**
- **SBOM must be an attached, signed `cosign attest` attestation, not a loose JSON** (the
  Chainguard-shaped artifact). Same for scan results.

### `.github/workflows/forge.yml` design
- Push to `main` → build `Dockerfile.hardened` (context = upstream checkout pinned to
  `8d36977`) → push `ghcr.io/<owner>/uptime-kuma` (amd64) → `cosign sign` →
  `syft -o spdx-json` + `cosign attest --type spdxjson` → `grype -o json` +
  `cosign attest --type vuln` → `cosign verify` + `verify-attestation` (hard gate) →
  CVE decomposition to the job summary + SBOM/scan uploaded as artifacts.
- **Keyless** throughout: `id-token: write` → GitHub OIDC → Fulcio cert → Rekor tlog. No keys.
- `provenance: false, sbom: false` on build-push-action so the pushed artifact is a **single
  image manifest** (not a buildkit attestation manifest *list*) — cosign then signs/attests the
  image digest directly; we attach our **own** cosign attestations instead.
- Identity for verify = `https://github.com/<repo>/.github/workflows/forge.yml@refs/heads/main`,
  issuer `https://token.actions.githubusercontent.com`. (This is why the pipeline runs on
  `main`, not a branch — the keyless cert SAN is branch-specific.)

### Validated before committing (run, don't hypothesize)
- syft `-o spdx-json=FILE` → valid SPDX-2.3 (870 pkgs); grype `-o json=FILE` works.
- **cosign v3 flag change caught locally:** `--tlog-upload=false` now conflicts with the default
  signing-config — irrelevant for CI (keyless *wants* the Rekor upload), but confirms not to
  copy old `--tlog-upload=false` snippets. Verified the v3 keyless flag surface
  (`sign --yes`, `attest --type/--predicate`, `verify[-attestation] --certificate-identity*
  --certificate-oidc-issuer`).
- Size metrics are intentionally **local-report-only**: `docker inspect .Size` means compressed
  under our containerd store but uncompressed on GH runners (overlay2) — not portable, so CI
  reports the portable CVE decomposition (from grype JSON), and `docs/cve-report.md` stays the
  canonical size source.

### Post-green TODO
- Make the ghcr package **public** (separate from the private repo) so anyone can pull+verify.
- Capture `cosign verify` output for the owner before discussing the agent.

### ✅ Pipeline runs GREEN (run 27474323485) — Saturday exit criterion met
- All 15 steps succeeded: build+push (amd64) → keyless sign → SBOM(SPDX)+attest →
  grype scan+attest → **cosign verify + verify-attestation gate passed** → CVE summary.
- Signed image digest: `sha256:99af11714682058f169b7b83d957836caaa6c956ea1d74291e5c190591badfe2`
  (`ghcr.io/tonyperkins/uptime-kuma:latest`).
- `cosign verify` reported all three checks: cosign claims validated, **transparency-log
  (Rekor) existence verified offline**, code-signing cert verified via trusted CA. Identity
  `https://github.com/tonyperkins/forge/.github/workflows/forge.yml@refs/heads/main`, issuer
  `https://token.actions.githubusercontent.com`. SBOM (spdxjson) and vuln attestations both
  verified. Three attestations attached: `spdx.dev/Document`, `cosign.../vuln/v1`, `sign/v1`.
- Two fixes to first-run: `sigstore/cosign-installer` has no moving `v4` tag → pinned `v4.1.2`;
  `grype --file` ambiguous with `-o json` → used `-o json=FILE`.
- Non-blocking: `actions/upload-artifact@v4` warns about Node20 runtime (deprecation only).
- **Remaining (owner action — gh token lacks `write:packages`):** flip the ghcr package to
  Public (Package settings → Change visibility), or `gh auth refresh -s write:packages` and I'll
  script it. Until then the image is private (still verifiable when authenticated; the CI gate
  proves it). README verify command depends on it being public.

---

## 2026-06-13 — Session 2: agent plan signed off + class-B framing

### Plan approved (CONTEXT §6 step 3, scope A/B/D)
- Module layout per §5: `forge_agent` (loop), `dfc_runner`, `wolfi_resolver`, `build_runner`,
  `verifier`; report reused from `scripts/`. **One added module `agent/llm.py`** — the only file
  that calls the Claude API, so the §4 boundary (LLM = diagnosis + fix-drafting only) is auditable
  in one place. Two LLM call sites: `diagnose()` (build-failure → structured *edit ops*, never a
  whole Dockerfile) and `adjudicate_wolfi()` (ambiguous package-name only).
- Loop (forge_agent): dfc convert once → for ≤5 iters {build → gather deterministic signals
  (registry probe / apk-error parse / USER scan) → LLM `diagnose` → apply edit ops → rebuild},
  scope-guard rejects any class outside A/B/D, loud diagnostic dump on exhaustion → verify
  (non-root + healthcheck + grype gate) → hand the generated `Dockerfile.agent` to the EXISTING
  `forge.yml` (unchanged) → emit fix-provenance (autonomous vs manual touch-up).

### Class B = conversion-analysis capability, NOT an in-loop build fix (owner decision)
- A is what actually breaks the uptime-kuma build; **B is demonstrated against the full upstream
  package set** because the defensible-core scope removed those packages (only `dumb-init` survives,
  which resolves cleanly → B would never fire in-loop). Do **not** restore packages to manufacture
  an in-loop B failure (the option-2 trap, §2).
- `wolfi_resolver` runs over the **real** upstream-base apt list (extracted from `dfc --json`
  `run.packages`) and reports **three honest buckets**, one-line reason on every no-equivalent:
  **mapped** (Debian→Wolfi rename) / **already-correct** (identical name exists) / **no Wolfi
  equivalent** (genuinely unmappable). Never silently drop an unmappable package; never force a
  wrong match. The no-equivalent bucket is where dfc's real limits live — surfacing it is more
  defensible than claiming full coverage.

### Wolfi index ground-truth (run, not assumed — §10), source `packages.wolfi.dev/os/x86_64`
Resolution method is general, not hardcoded answers: exact `P:` match → `provides` lookup
(`cmd:<name>`) → index-validated naming transforms (`fonts-`→`font-`, `python3-`→`py3-`, strip
`-server`, hyphenated-parent) → LLM residual (index-validated) → else no-equivalent.
- **mapped (5):** `iputils-ping`→`iputils` (provides `cmd:ping`), `sqlite3`→`sqlite` (provides
  `cmd:sqlite3`; libs in `sqlite-libs`), `fonts-noto`→`font-noto`, `fonts-noto-cjk`→`font-noto-cjk`,
  `mariadb-server`→`mariadb`.
- **already-correct (8):** `ca-certificates`, `curl`, `dumb-init`, `nscd`, `sudo`, `util-linux`,
  `chromium`, **`cloudflared`** — *correction*: the a-priori guess that cloudflared has no Wolfi
  equivalent was **wrong**; `P:cloudflared` (provides `cmd:cloudflared`) is in the index. Exactly the
  "verify before assuming" case (§10) — report the truth.
- **no Wolfi equivalent (3):** `fonts-indic` (Debian metapackage of Indic fonts; Wolfi ships
  individual families e.g. `font-lohit-*`, no single equivalent), `python3-paho-mqtt` (no paho-mqtt
  Python binding packaged in Wolfi), `./apprise.deb` (local `.deb` *path*, not a repo name — apk
  cannot install a `.deb`; dfc class C).
- dfc confirmed (decisions Session 1) to pass these names through **unmapped** — the resolver's
  value is real, and its `mappings.yaml` is consumable by `dfc --mappings`.

### LLM model tiering (owner decision) — Sonnet default, Opus one-hop escalation
- `agent/llm.py` holds two swappable constants: `MODEL` = `claude-sonnet-4-6` (default for all
  diagnosis/adjudication) and `ESCALATION_MODEL` = `claude-opus-4-8`.
- **Escalation rule (one hop, no creep):** if a Sonnet-drafted fix fails to *improve* the build
  (same failure signature after rebuild), the *same* diagnosis is re-run once on Opus. If Opus also
  fails to move it, the existing bounded-loop/loud-failure path takes over — NOT a new retry
  framework, NOT multi-model voting. It is a single model-selection branch in the loop.
- **Provenance is mandatory (else §2 violation):** every escalation is a logged line with model
  attribution — e.g. "class A diagnosis escalated sonnet-4-6 → opus-4-8 after the Sonnet fix failed
  to resolve build error X." This makes the tiering a *demonstrated* cost-engineering decision
  ("Sonnet handled N fixes; Opus escalated M times on structural diagnosis, here's when/why") rather
  than a hidden detail. A flat "Sonnet-powered" claim without visible escalation would misrepresent.
- `pip install anthropic` approved. `ANTHROPIC_API_KEY` to be set by owner before the first live
  A-loop run — **pause for that confirmation once the loop is built** (owner instruction).

---

## 🧭 STATE OF PLAY — resumption anchor (2026-06-13, before agent session)

Read this block first; it's the cold-start anchor. Detail lives in the dated entries above.

**Status:** §6 step 1 (manual hardened non-root build) and step 2 (CI pipeline:
build→SBOM→scan→cosign keyless sign→attest→verify→report) are both **green and banked**.

**Signed artifact:** `ghcr.io/tonyperkins/uptime-kuma:latest`
@ `sha256:99af11714682058f169b7b83d957836caaa6c956ea1d74291e5c190591badfe2`
— keyless-signed (GitHub OIDC→Fulcio→Rekor), with **SBOM (spdxjson) + vuln attestations
attached and verified**; `cosign verify` + both `verify-attestation` pass. Identity
`…/forge/.github/workflows/forge.yml@refs/heads/main`, issuer `token.actions.githubusercontent.com`.

**Open owner action (only one):** ghcr package is still **private** — flip to Public
(Package settings → Change visibility) or `gh auth refresh -s write:packages`. Needed for
public verifiability / README verify command; not blocking the agent.

**Locked headline numbers** (vs upstream `2.4.0-slim-rootless`, the same-scope baseline;
real grype/syft, regen via `scripts/gen_report.py`):
- **OS/runtime-layer CVEs 507 → 0** (the layer hardening targets) — *this is the headline lead*.
- Total 539 → 28 (95% fewer) — supporting context, not the lead.
- npm/application layer 32 → 28 — out of scope (Chainguard Libraries' domain), **no credit claimed**.
- Size 180 → 117 MB compressed (modest, reported straight).

**Agent scope (LOCKED) — observed failure classes from the manual path = §4 A/B/D only:**
- **A** — phantom image mapping / structural base-image flattening (dfc maps
  `louislam/uptime-kuma:*` → nonexistent `cgr.dev/chainguard/uptime-kuma:*`).
- **B** — apt→apk package-name misses (dfc passes names through unmapped).
- **D** — `USER root` inserted for installs → restore non-root at runtime.
- Native-module toolchain = **anticipated, did NOT occur** (uptime-kuma 2.4 uses
  `@louislam/sqlite3` prebuilt N-API, no compile). Do not build for it. Do not switch targets
  to manufacture it (§2).

**Next:** §6 step 3 — the agent. Scope locked to A/B/D. Plan signed off (see Session 2 entry).
- **Class B — DONE & verified.** `agent/wolfi_resolver.py` (+ `agent/dfc_runner.py`) resolves the
  upstream apt surface against the live Wolfi index → `targets/uptime-kuma/mappings.yaml` (dfc
  format). Round-trip proven: `dfc --mappings=… --warn-missing-packages` reports **0** unmapped;
  buckets **5 mapped / 8 already-correct / 3 no-equivalent**. Deterministic; LLM seam present but
  unused for this target (residuals correctly land no-equivalent).
- **Class A + D — NEXT.** The bounded build→diagnose→fix→rebuild loop (`forge_agent`, `build_runner`,
  `verifier`) flattening the phantom base image and restoring non-root, with the LLM doing
  log-diagnosis + structured edit-op drafting (never whole Dockerfiles). dfc/build/scan/sign/report
  stay deterministic. Python, clean/idiomatic, ~5-iter cap, loud failure. Don't expand scope (§2).
- **Pending before A-loop:** LLM model choice (sonnet-4-6 vs opus-4-8); `pip install anthropic`;
  `ANTHROPIC_API_KEY` in env.

**Read first (in order):**
1. `CONTEXT.md` — purpose, honesty guardrails (§2), architecture (§4), build order (§6).
2. `docs/decisions.md` — this log (start at this anchor, then skim dated entries).
3. `targets/uptime-kuma/Dockerfile.hardened` — the hand-walked path the agent automates.
4. `targets/uptime-kuma/Dockerfile.converted` + `.converted-base` — raw dfc output showing
   the A/B/D failures the agent must fix (phantom images, unmapped apk names, USER root).
   Supporting: `Dockerfile.upstream` / `.upstream-base` (inputs), `scripts/gen_report.py` +
   `cve_summary.py` (deterministic report), `.github/workflows/forge.yml` (pipeline).
