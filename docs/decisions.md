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
