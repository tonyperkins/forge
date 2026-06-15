#!/usr/bin/env python3
"""Print a Markdown CVE summary for a single grype JSON — severity counts plus the
OS/runtime vs npm/application decomposition that is the project's whole point.

Portable (reads only the grype JSON; no docker/registry calls), so it runs the same in
CI as locally. Used to write the pipeline's report into the GitHub job summary.

Usage:  python3 scripts/cve_summary.py <grype.json> [image-label]
"""
from __future__ import annotations
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path
from forge_layers import OS_TYPES  # shared OS/runtime-vs-application classification

SEVERITIES = ["Critical", "High", "Medium", "Low", "Negligible", "Unknown"]


def main() -> None:
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else path
    g = json.loads(open(path).read())

    seen = set()
    by_sev = {s: 0 for s in SEVERITIES}
    os_layer = 0
    npm_layer = 0
    crit_high: list[str] = []
    for m in g["matches"]:
        v, a = m["vulnerability"], m["artifact"]
        key = (v["id"], a["name"], a["version"])
        if key in seen:
            continue
        seen.add(key)
        s = v.get("severity", "Unknown")
        if s not in by_sev:
            s = "Unknown"
        by_sev[s] += 1
        if a["type"] in OS_TYPES:
            os_layer += 1
        else:
            npm_layer += 1
        if s in ("Critical", "High"):
            crit_high.append(f"{s} · `{v['id']}` · {a['name']} {a['version']} [{a['type']}]")

    total = sum(by_sev.values())
    print(f"### CVE summary — {label}\n")
    print("| Critical | High | Medium | Low | Negligible | **Total** |")
    print("|--:|--:|--:|--:|--:|--:|")
    print(f"| {by_sev['Critical']} | {by_sev['High']} | {by_sev['Medium']} | "
          f"{by_sev['Low']} | {by_sev['Negligible']} | **{total}** |\n")
    print(f"- **OS/runtime-layer CVEs: {os_layer}** (the layer image hardening targets)")
    print(f"- npm/application-layer CVEs: {npm_layer} "
          f"(uptime-kuma's own deps — Chainguard Libraries' domain, out of scope)\n")
    if crit_high:
        print("<details><summary>Critical/High detail</summary>\n")
        for line in crit_high:
            print(f"- {line}")
        print("\n</details>")


if __name__ == "__main__":
    main()
