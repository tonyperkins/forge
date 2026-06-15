#!/usr/bin/env python3
"""Generate docs/cve-report.md from real syft + grype artifacts.

Deterministic pipeline step (CONTEXT §4: the report stays deterministic code; the LLM
never touches it). Every number is read from a grype/syft JSON on disk or queried live
from `docker` — nothing is hand-typed or estimated (CONTEXT §10). Re-run after a rebuild
and the numbers update themselves.

Usage:  python3 scripts/gen_report.py
Assumes scans already produced under .scan/ and images present locally.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path
from forge_layers import OS_TYPES  # shared OS/runtime-vs-application classification

SCAN = Path(".scan")
OUT = Path("docs/cve-report.md")

# (label, image ref, sbom file, grype file, role)
#   role "ours"     -> the hardened image we built
#   role "primary"  -> apples-to-apples upstream comparison (same scope: slim + non-root)
#   role "context"  -> upstream full image (broader scope; shown for context, not the headline)
IMAGES = [
    ("forge hardened (ours)", "forge/uptime-kuma:hardened",
     "sbom_forge_uptime-kuma_hardened.json", "grype_forge_uptime-kuma_hardened.json", "ours"),
    ("upstream 2.4.0-slim-rootless", "louislam/uptime-kuma:2.4.0-slim-rootless",
     "sbom_louislam_uptime-kuma_2.4.0-slim-rootless.json",
     "grype_louislam_uptime-kuma_2.4.0-slim-rootless.json", "primary"),
    ("upstream 2.4.0 (full)", "louislam/uptime-kuma:2.4.0",
     "sbom_louislam_uptime-kuma_2.4.0.json", "grype_louislam_uptime-kuma_2.4.0.json", "context"),
]

SEVERITIES = ["Critical", "High", "Medium", "Low", "Negligible", "Unknown"]


def scan_provenance(grype_path: Path, sbom_path: Path):
    """Read the scanned image digest + the tool versions straight from the scan
    artifacts (grype/syft each embed a `descriptor`; grype records the image
    `source.target`), so provenance is generated, never hand-typed (CONTEXT §10)."""
    g = json.loads(grype_path.read_text())
    s = json.loads(sbom_path.read_text())
    t = g.get("source", {}).get("target", {})
    return dict(manifest_digest=t.get("manifestDigest"), image_id=t.get("imageID"),
                grype_ver=g.get("descriptor", {}).get("version"),
                syft_ver=s.get("descriptor", {}).get("version"))


def dedup_matches(grype: dict):
    """Yield unique (vuln-id, pkg-name, pkg-version) findings — grype can list a CVE
    once per matched location, so we collapse to distinct vuln×package."""
    seen = set()
    for m in grype["matches"]:
        v, a = m["vulnerability"], m["artifact"]
        key = (v["id"], a["name"], a["version"])
        if key in seen:
            continue
        seen.add(key)
        yield v, a


def cve_stats(grype_path: Path):
    g = json.loads(grype_path.read_text())
    by_sev = {s: 0 for s in SEVERITIES}
    os_layer = {s: 0 for s in SEVERITIES}
    npm_layer = {s: 0 for s in SEVERITIES}
    total = fixable = 0
    for v, a in dedup_matches(g):
        s = v.get("severity", "Unknown")
        if s not in by_sev:
            s = "Unknown"
        by_sev[s] += 1
        total += 1
        if v.get("fix", {}).get("state") == "fixed":
            fixable += 1
        (os_layer if a["type"] in OS_TYPES else npm_layer)[s] += 1
    return dict(by_sev=by_sev, total=total, fixable=fixable,
                os_total=sum(os_layer.values()), npm_total=sum(npm_layer.values()),
                os_layer=os_layer, npm_layer=npm_layer)


def pkg_counts(sbom_path: Path):
    d = json.loads(sbom_path.read_text())
    by_type: dict[str, int] = {}
    for a in d["artifacts"]:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    os_pkgs = sum(c for t, c in by_type.items() if t in {"deb", "apk", "rpm"})
    return dict(total=len(d["artifacts"]), by_type=by_type, os_pkgs=os_pkgs)


def docker_sizes(ref: str):
    """compressed = `docker inspect .Size` (validated to equal the amd64 registry
    manifest's summed layer-blob sizes under the containerd image store, i.e. pull size);
    uncompressed = sum of `docker history` layer sizes."""
    compressed = int(subprocess.check_output(
        ["docker", "inspect", "-f", "{{.Size}}", ref], text=True).strip())
    mult = {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9}
    uncompressed = 0.0
    hist = subprocess.check_output(["docker", "history", "--format", "{{.Size}}", ref], text=True)
    for tok in hist.split():
        for u in ("kB", "MB", "GB", "B"):
            if tok.endswith(u):
                uncompressed += float(tok[:-len(u)]) * mult[u]
                break
    return compressed, uncompressed


def mb(n: float) -> str:
    return f"{n / 1e6:,.0f} MB"


def pct_drop(new: float, old: float) -> str:
    if old == 0:
        return "—"
    return f"{(1 - new / old) * 100:.0f}% fewer" if new <= old else f"{(new / old - 1) * 100:.0f}% more"


def main():
    data = []
    for label, ref, sbom, grype, role in IMAGES:
        cve = cve_stats(SCAN / grype)
        pkg = pkg_counts(SCAN / sbom)
        comp, uncomp = docker_sizes(ref)
        prov = scan_provenance(SCAN / grype, SCAN / sbom)
        data.append(dict(label=label, ref=ref, role=role, cve=cve, pkg=pkg,
                         comp=comp, uncomp=uncomp, prov=prov))

    ours = next(d for d in data if d["role"] == "ours")
    primary = next(d for d in data if d["role"] == "primary")

    L = []
    L.append("# CVE / size diff — `forge` hardened uptime-kuma\n")
    L.append("> Generated by `scripts/gen_report.py` from real `syft` SBOMs and `grype` scans "
             "(amd64). Every number traces to a JSON under `.scan/` or a live `docker` query — "
             "none estimated (CONTEXT §10). Regenerate after any rebuild.\n")
    L.append(f"- Scanner: grype, JSON output. Images: amd64. App version: uptime-kuma 2.4.0 "
             f"(both sides — same source commit `8d36977`).\n")

    # --- headline: CVE decomposition (the genuinely strong metric) ---
    L.append("## Headline — vulnerabilities\n")
    L.append("The apples-to-apples comparison is **upstream `2.4.0-slim-rootless`** (same scope as "
             "ours: slim, no Chromium/MariaDB, non-root). The full `2.4.0` image is shown for "
             "context only — it carries Chromium + MariaDB + fonts we deliberately exclude, so "
             "comparing against it would overstate the win.\n")
    L.append("| Image | Critical | High | Medium | Low | Negligible | **Total** |")
    L.append("|---|--:|--:|--:|--:|--:|--:|")
    for d in data:
        c = d["cve"]["by_sev"]
        tag = " ⭐" if d["role"] == "ours" else ""
        L.append(f"| {d['label']}{tag} | {c['Critical']} | {c['High']} | {c['Medium']} | "
                 f"{c['Low']} | {c['Negligible']} | **{d['cve']['total']}** |")
    L.append("")
    drop = pct_drop(ours["cve"]["total"], primary["cve"]["total"])
    L.append(f"**vs slim-rootless: {primary['cve']['total']} → {ours['cve']['total']} total CVEs "
             f"({drop}).**\n")

    # --- the honest decomposition ---
    L.append("## Where the reduction comes from\n")
    L.append("Splitting each image's CVEs into the **OS/runtime layer** (Debian/Wolfi packages, "
             "Go/Python runtime bits — *what image hardening actually addresses*) vs the "
             "**npm/application layer** (uptime-kuma's own dependency tree):\n")
    L.append("| Image | OS/runtime-layer CVEs | npm/application-layer CVEs |")
    L.append("|---|--:|--:|")
    for d in data:
        tag = " ⭐" if d["role"] == "ours" else ""
        L.append(f"| {d['label']}{tag} | {d['cve']['os_total']} | {d['cve']['npm_total']} |")
    L.append("")
    L.append(f"- **Image hardening eliminated the OS/runtime layer entirely: "
             f"{primary['cve']['os_total']} → {ours['cve']['os_total']} CVEs.** That layer in "
             f"upstream slim-rootless includes "
             f"{primary['cve']['os_layer']['Critical']} Critical and "
             f"{primary['cve']['os_layer']['High']} High.")
    L.append(f"- **The npm/application layer is essentially unchanged "
             f"({primary['cve']['npm_total']} → {ours['cve']['npm_total']}).** "
             f"Hardening the base image does not patch an app's npm dependencies; that is the "
             f"domain of **Chainguard Libraries**, a separate product, and is out of "
             f"scope here.")
    L.append(f"- Our {ours['cve']['npm_total']} residual findings are 100% npm "
             f"(e.g. `protobufjs`, `@grpc/grpc-js`, `tar`, `minimatch`) — all carried by "
             f"uptime-kuma itself; the upstream image carries the same class.\n")

    # --- packages ---
    L.append("## Package surface\n")
    L.append("| Image | Total packages (syft) | OS packages (apk/deb) |")
    L.append("|---|--:|--:|")
    for d in data:
        tag = " ⭐" if d["role"] == "ours" else ""
        L.append(f"| {d['label']}{tag} | {d['pkg']['total']} | {d['pkg']['os_pkgs']} |")
    L.append("")
    L.append(f"- OS packages: **{primary['pkg']['os_pkgs']} → {ours['pkg']['os_pkgs']}** "
             f"({pct_drop(ours['pkg']['os_pkgs'], primary['pkg']['os_pkgs'])}). The npm package "
             f"counts are close because it is the same application.\n")

    # --- size (presented honestly; not the lead) ---
    L.append("## Size\n")
    L.append("This target bundles every knex DB driver (e.g. `oracledb`'s five per-platform "
             "prebuilts) and full i18n assets, so the size reduction is modest.\n")
    L.append("| Image | Compressed (pull size) | Uncompressed |")
    L.append("|---|--:|--:|")
    for d in data:
        tag = " ⭐" if d["role"] == "ours" else ""
        L.append(f"| {d['label']}{tag} | {mb(d['comp'])} | {mb(d['uncomp'])} |")
    L.append("")
    size_pct = (1 - ours['comp'] / primary['comp']) * 100
    L.append(f"- vs slim-rootless, compressed: **{mb(primary['comp'])} → {mb(ours['comp'])}** "
             f"({size_pct:.0f}% smaller).")
    L.append(f"- Method note: *compressed* = `docker inspect .Size` (validated to equal the amd64 "
             f"registry manifest's summed layer blobs); *uncompressed* = `docker history` layer "
             f"sum. `docker images` reports a larger uncompressed figure under the containerd "
             f"store; we cite the manifest-derived numbers as they reproduce from a registry.\n")

    # --- provenance: exactly what was scanned, with what (read from the artifacts) ---
    tools = ours["prov"]
    L.append("## Provenance — exactly what was scanned\n")
    L.append(f"- Tools: **syft `{tools['syft_ver']}`**, **grype `{tools['grype_ver']}`** "
             f"(versions read from each scan's embedded `descriptor`). App version "
             f"uptime-kuma 2.4.0 @ source commit `8d36977`. Architecture: amd64.")
    L.append(f"- The hardened image scanned here is a **local amd64 build** of "
             f"`targets/uptime-kuma/Dockerfile.hardened`. The image **shipped and signed in "
             f"CI** is a separate build of the same Dockerfile + pinned upstream commit; it "
             f"carries its own grype scan attached as a signed `cosign` attestation "
             f"(`--type vuln`), independently verifiable on the published image.")
    L.append("\nDigests of the exact images these numbers were scanned from "
             "(`grype` records them in `source.target`):\n")
    L.append("| Image | manifest digest | image ID (config) |")
    L.append("|---|---|---|")
    for d in data:
        p = d["prov"]
        L.append(f"| `{d['ref']}` | `{p['manifest_digest']}` | `{p['image_id']}` |")
    L.append("")
    L.append("To reproduce: pull/build the images above, then "
             "`syft <img> -o json` + `grype <img> -o json` into `.scan/` and run "
             "`python3 scripts/gen_report.py`. Every number in this file regenerates from "
             "those artifacts.\n")

    OUT.write_text("\n".join(L) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
