"""Run docker builds and gather deterministic signals from failures (CONTEXT §4).

This is the deterministic half of the loop: it builds a candidate Dockerfile, captures the log,
and extracts *facts* the LLM then diagnoses from — it makes no fix decisions itself. The key
class-A signal is a real registry probe: dfc's phantom bases (`cgr.dev/chainguard/<name>` with
no Chainguard equivalent) are confirmed by asking the registry whether they exist, not guessed.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent.dockerfile import Dockerfile

# apk's "can't find this package" error, e.g.:
#   ERROR: unable to select packages:\n  iputils-ping (no such package):
_APK_MISS_RE = re.compile(r"^\s*(?P<pkg>[\w][\w.+-]*)\s+\(no such package\)", re.MULTILINE)
# buildkit's missing-base-image error names the ref it failed to resolve.
_PULL_FAIL_RE = re.compile(r"(?:failed to resolve source metadata for|not found:|failed to do request:.*?)\s*"
                           r"(?P<ref>[\w./:-]+@?sha256:[0-9a-f]+|[\w./:-]+:[\w.-]+)", re.IGNORECASE)
_ERROR_LINE_RE = re.compile(r"^.*?(ERROR|error:|failed to).*$", re.MULTILINE)


@dataclass
class BuildResult:
    ok: bool
    exit_code: int
    log: str
    tag: str

    @property
    def error_tail(self, n: int = 60) -> str:
        return "\n".join(self.log.splitlines()[-n:])

    def signature(self) -> str:
        """A normalized fingerprint of *why* this build failed — used by the loop to tell
        whether a fix moved the build (new signature = progress) or not (same = escalate)."""
        if self.ok:
            return "ok"
        # First apk miss, else first error line, normalized of volatile bits (hashes, ids).
        miss = _APK_MISS_RE.search(self.log)
        if miss:
            return f"apk-miss:{miss.group('pkg')}"
        m = _ERROR_LINE_RE.search("\n".join(self.log.splitlines()[-40:]))
        if m:
            line = re.sub(r"\b[0-9a-f]{12,}\b", "<hash>", m.group(0).strip())
            return f"err:{line[:160]}"
        return f"exit:{self.exit_code}"


def build(dockerfile_text: str, context_dir: Path, tag: str, target: str | None = None,
          platform: str = "linux/amd64", timeout: int = 1800) -> BuildResult:
    """Build `dockerfile_text` against `context_dir`, loading the image locally. `target` builds
    a specific stage (the upstream Dockerfile is multi-target — we want the runtime stage, not
    the default last stage). Output is captured (combined stdout+stderr) for diagnosis."""
    context_dir = Path(context_dir)
    if not context_dir.is_dir():
        raise FileNotFoundError(f"build context not found: {context_dir} "
                                "(upstream checkout — see decisions.md 'Build context')")
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as tf:
        tf.write(dockerfile_text)
        df_path = tf.name
    try:
        cmd = ["docker", "buildx", "build", "--load", "--platform", platform,
               "-t", tag, "-f", df_path]
        if target:
            cmd += ["--target", target]
        cmd.append(str(context_dir))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        log = proc.stdout + proc.stderr
        return BuildResult(ok=proc.returncode == 0, exit_code=proc.returncode, log=log, tag=tag)
    except subprocess.TimeoutExpired as exc:
        return BuildResult(ok=False, exit_code=124,
                           log=f"build timed out after {timeout}s\n{exc.output or ''}", tag=tag)
    finally:
        Path(df_path).unlink(missing_ok=True)


def image_exists(ref: str) -> bool:
    """Real registry probe — does this image actually exist? This is what turns dfc's
    'phantom base' from a guess into a fact (class A). Uses buildx imagetools, which works
    for cgr.dev public images without a pull."""
    proc = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref],
                          capture_output=True, text=True)
    return proc.returncode == 0


def gather_signals(dockerfile: Dockerfile, result: BuildResult, target_stage: str) -> dict:
    """Extract deterministic facts from a failed build for the LLM to diagnose. No fix logic
    here — just 'here is what is true about this failure'. Scoped to the stages the `target_stage`
    build actually depends on, so the LLM doesn't fix the upstream Dockerfile's unrelated targets."""
    signals: dict = {"failure_signature": result.signature(), "build_target": target_stage}
    reachable = dockerfile.reachable_from(target_stage)

    # Class A — which referenced cgr.dev bases (in the build's dependency closure) do NOT exist.
    phantom = []
    for s in dockerfile.stages:
        if s.alias not in reachable:
            continue
        ref = s.image.split("@")[0]  # ignore any digest for the existence check
        if ref.startswith("cgr.dev/") and not image_exists(ref):
            phantom.append({"stage": s.alias or f"#{s.index}", "image": ref})
    if phantom:
        signals["phantom_bases"] = phantom

    # Class B — apk package names the build could not resolve.
    misses = sorted({m.group("pkg") for m in _APK_MISS_RE.finditer(result.log)})
    if misses:
        signals["apk_unresolved_packages"] = misses

    # Class D — what user the runtime (target) stage ends as (None = base default applies).
    signals["runtime_stage"] = target_stage
    signals["runtime_user"] = dockerfile.final_user(target_stage)

    # Stage aliases in the build's closure, so the LLM targets edits at real, relevant stages.
    signals["stages"] = [{"alias": s.alias or f"#{s.index}", "image": s.image.split("@")[0]}
                         for s in dockerfile.stages if s.alias in reachable]
    return signals
