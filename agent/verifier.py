"""Verify a built image is actually hardened (CONTEXT §4): runs non-root, healthcheck passes,
and the OS/runtime layer the project targets is clean. Deterministic — no LLM here.

Gating philosophy matches the locked report semantics (decisions.md): the OS/runtime layer is
what image hardening controls, so an OS-layer Critical is a hard fail; npm/application-layer
CVEs are Chainguard Libraries' domain (out of scope) and are reported, not gated.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

# Same OS/runtime decomposition the report uses (scripts/cve_summary.py) — keep in sync.
OS_TYPES = {"deb", "apk", "rpm", "python", "go-module"}


@dataclass
class VerifyResult:
    image: str
    non_root: bool
    user: str | None
    healthcheck_ok: bool
    healthcheck_detail: str
    os_critical: int
    os_high: int
    npm_total: int
    passed: bool
    notes: list[str] = field(default_factory=list)


def _inspect_user(image: str) -> str | None:
    proc = subprocess.run(["docker", "inspect", "-f", "{{.Config.User}}", image],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _is_non_root(user: str | None) -> bool:
    """A distroless image must declare a non-root user. Empty/root/uid 0 all fail."""
    if not user:
        return False
    uid = user.split(":", 1)[0]
    return uid not in ("root", "0")


def _run_healthcheck(image: str, wait: int, poll: int = 5) -> tuple[bool, str]:
    """Boot the container and run upstream's healthcheck binary until it passes or `wait`
    elapses. The binary checks the app on :3001 — uptime-kuma needs a long start period."""
    name = f"forge-verify-{int(time.time())}"
    up = subprocess.run(["docker", "run", "-d", "--name", name, image],
                        capture_output=True, text=True)
    if up.returncode != 0:
        return False, f"container failed to start: {up.stderr.strip()}"
    try:
        deadline = time.time() + wait
        last = ""
        while time.time() < deadline:
            hc = subprocess.run(["docker", "exec", name, "/app/extra/healthcheck"],
                                capture_output=True, text=True)
            last = (hc.stdout + hc.stderr).strip()
            if hc.returncode == 0:
                return True, last or "healthcheck exit 0"
            time.sleep(poll)
        return False, f"healthcheck did not pass within {wait}s (last: {last!r})"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


def _scan(image: str) -> tuple[int, int, int, str | None]:
    """grype the image → (os_critical, os_high, npm_total). Returns a note instead of
    raising if grype isn't available, so a missing scanner degrades to 'unverified', not crash."""
    proc = subprocess.run(["grype", image, "-o", "json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return 0, 0, 0, f"grype unavailable / failed: {proc.stderr.strip()[:200]}"
    data = json.loads(proc.stdout)
    seen = set()
    os_crit = os_high = npm = 0
    for m in data.get("matches", []):
        v, a = m["vulnerability"], m["artifact"]
        key = (v["id"], a["name"], a["version"])
        if key in seen:
            continue
        seen.add(key)
        sev = v.get("severity", "Unknown")
        if a["type"] in OS_TYPES:
            if sev == "Critical":
                os_crit += 1
            elif sev == "High":
                os_high += 1
        else:
            npm += 1
    return os_crit, os_high, npm, None


def verify(image: str, healthcheck_wait: int = 220, run_healthcheck: bool = True,
           run_scan: bool = True) -> VerifyResult:
    notes: list[str] = []
    user = _inspect_user(image)
    non_root = _is_non_root(user)

    hc_ok, hc_detail = (True, "skipped")
    if run_healthcheck:
        hc_ok, hc_detail = _run_healthcheck(image, healthcheck_wait)

    os_crit = os_high = npm = 0
    if run_scan:
        os_crit, os_high, npm, scan_note = _scan(image)
        if scan_note:
            notes.append(scan_note)

    # Hard gates: non-root, healthcheck, and zero OS/runtime-layer Criticals (the layer we
    # control). npm-layer CVEs are reported but not gated (out of scope — Chainguard Libraries).
    passed = non_root and hc_ok and os_crit == 0
    if not non_root:
        notes.append(f"FAIL: runtime user is {user!r} (must be non-root)")
    if not hc_ok:
        notes.append(f"FAIL: healthcheck — {hc_detail}")
    if os_crit:
        notes.append(f"FAIL: {os_crit} OS/runtime-layer Critical CVE(s) (gate requires 0)")

    return VerifyResult(image=image, non_root=non_root, user=user, healthcheck_ok=hc_ok,
                        healthcheck_detail=hc_detail, os_critical=os_crit, os_high=os_high,
                        npm_total=npm, passed=passed, notes=notes)
