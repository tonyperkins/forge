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
