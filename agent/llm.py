"""The agent's only Claude API surface (CONTEXT §4 boundary, kept in one file on purpose).

The LLM does *log diagnosis and fix-drafting* — the "manual review" steps dfc's docs
describe — and nothing else. It never emits a whole Dockerfile: `diagnose()` returns a small,
typed set of **edit operations** (swap a base image, set a stage's USER, add an apk) that the
deterministic loop validates and applies. `adjudicate_wolfi()` only picks among real Wolfi
candidates for an ambiguous package name. Everything that executes (dfc, builds, scans, sign,
report) stays deterministic code elsewhere.

Model tiering (owner decision, decisions.md Session 2): Sonnet by default, one-hop escalation
to Opus when a Sonnet fix fails to move the build. The escalation branch lives in the loop
(`forge_agent`); this module just takes whichever model it's told to use and records it on the
result so the fix-provenance can attribute every fix to a model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# Two swappable constants (the only model knobs). Sonnet handles the bounded diagnosis loop;
# Opus is the one-hop escalation target for diagnoses Sonnet couldn't resolve.
MODEL = "claude-sonnet-4-6"
ESCALATION_MODEL = "claude-opus-4-8"

# Scope is locked to the three observed failure classes (CONTEXT §4). The LLM may only ever
# return one of these; anything else is rejected by the loop's scope guard.
ALLOWED_CLASSES = ("A", "B", "D")

_MAX_TOKENS = 8000  # diagnosis output is small (a rationale + a few edit ops)


class LLMError(RuntimeError):
    """Surfaced loudly — a diagnosis we can't get is a stop, never a silent skip."""


# ─────────────────────────────────────────────────────────────────────────────
# Typed results
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Edit:
    """One targeted Dockerfile edit the LLM proposes. The op vocabulary is deliberately
    tiny so the LLM cannot author free-form Dockerfile content — only point at a stage and
    change one bounded thing the deterministic applier knows how to make."""

    op: str        # "replace_base_image" | "set_user" | "add_package"
    stage: str     # the stage alias the edit targets (e.g. "build", "release")
    value: str     # new image ref / user spec / package name
    reason: str


@dataclass(frozen=True)
class Diagnosis:
    failure_class: str         # "A" | "B" | "D" | "unknown"
    rationale: str
    confidence: str            # "high" | "medium" | "low"
    edits: tuple[Edit, ...]
    model: str                 # which model produced this (for fix-provenance attribution)


# JSON schema for the structured diagnosis (structured outputs — guarantees parseable JSON;
# no unsupported constraints like minLength so the API accepts it).
_DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "failure_class": {"type": "string", "enum": ["A", "B", "D", "unknown"]},
        "rationale": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                            "enum": ["replace_base_image", "set_user", "add_package"]},
                    "stage": {"type": "string"},
                    "value": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["op", "stage", "value", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["failure_class", "rationale", "confidence", "edits"],
    "additionalProperties": False,
}

_WOLFI_SCHEMA = {
    "type": "object",
    "properties": {
        "wolfi_packages": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["wolfi_packages", "reason"],
    "additionalProperties": False,
}

_DIAGNOSE_SYSTEM = """\
You are the diagnosis step of `forge`, an agent that hardens a dfc-converted Dockerfile.
You perform the "manual review" a Chainguard engineer would do on a failed build — you read
the error, identify which of THREE known failure classes it is, and draft targeted edits.
You do NOT write Dockerfiles. You only return edit operations from a fixed vocabulary.

The only failure classes in scope (reject anything else as "unknown"):
  A — structural / phantom base image: dfc mapped a FROM to a cgr.dev/chainguard/<name>
      image that does not exist (the upstream image had no Chainguard equivalent). Fix by
      replacing each phantom base with the real Chainguard pattern: a builder stage on
      `cgr.dev/chainguard/node:latest-dev` (Node build) or `cgr.dev/chainguard/go:latest-dev`
      (Go build), and the runtime stage on distroless `cgr.dev/chainguard/node:latest`.
  B — package mapping: an `apk add` failed because a Debian package name has no Wolfi
      package of that name. (Handled mostly out-of-loop; only diagnose if it appears.)
  D — non-root: dfc injected `USER root` for installs, or the runtime stage inherits a
      root/unknown user; production must run non-root (distroless node default is 65532).

Edit op vocabulary (this is ALL you may emit):
  - replace_base_image: stage=<stage alias>, value=<new image ref WITHOUT digest>
  - set_user:           stage=<stage alias>, value=<user, e.g. "65532:65532" or "root">
  - add_package:        stage=<stage alias>, value=<single apk package name>

Rules: prefer the fewest edits that address the diagnosed class. Use the real stage aliases
from the Dockerfile. Never invent build steps (compiling a binary, copying artifacts) — those
are out of scope and must be left for a human touch-up; if the failure needs one, return
failure_class "unknown" with an empty edits list and explain in the rationale. Be concise."""


# ─────────────────────────────────────────────────────────────────────────────
# Client (lazy so importing this module never requires a key)
# ─────────────────────────────────────────────────────────────────────────────
def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMError("anthropic SDK not installed — `pip install -r requirements.txt` "
                       "into the project venv.") from exc
    try:
        return anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY from the environment
    except Exception as exc:  # pragma: no cover - surfaced loudly
        raise LLMError(f"could not construct Anthropic client: {exc}") from exc


def _structured_call(model: str, system: str, user: str, schema: dict) -> dict:
    """One structured-output request → parsed JSON dict. `effort: high` for the reasoning;
    no thinking blocks (keeps the structured parse clean)."""
    import anthropic
    client = _client()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema},
                           "effort": "high"},
        )
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Claude API error ({exc.status_code}) on {model}: {exc.message}") from exc
    except anthropic.APIError as exc:  # connection/timeout/etc.
        raise LLMError(f"Claude API call failed on {model}: {exc}") from exc

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMError(f"empty diagnosis from {model} (stop_reason={resp.stop_reason})")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - schema should prevent this
        raise LLMError(f"non-JSON structured output from {model}: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# The two call sites
# ─────────────────────────────────────────────────────────────────────────────
def diagnose(dockerfile_text: str, error_tail: str, signals: dict, model: str = MODEL) -> Diagnosis:
    """LLM call #1 — read a failed build and draft a structured fix (edit ops only)."""
    user = (
        "## Current Dockerfile\n```\n" + dockerfile_text + "\n```\n\n"
        "## Deterministic signals gathered from the failed build\n"
        + json.dumps(signals, indent=2) + "\n\n"
        "## Build error (tail)\n```\n" + error_tail + "\n```\n\n"
        "Diagnose the failure class and return the edit operations to fix it."
    )
    data = _structured_call(model, _DIAGNOSE_SYSTEM, user, _DIAGNOSIS_SCHEMA)
    edits = tuple(Edit(op=e["op"], stage=e["stage"], value=e["value"], reason=e["reason"])
                  for e in data.get("edits", []))
    return Diagnosis(
        failure_class=data["failure_class"],
        rationale=data["rationale"],
        confidence=data["confidence"],
        edits=edits,
        model=model,
    )


def adjudicate_wolfi(debian_name: str, candidates: list[str], model: str = MODEL) -> tuple[list[str], str]:
    """LLM call #2 (class B residuals only) — pick the right Wolfi package(s) for an ambiguous
    Debian name from a list of REAL index candidates. The caller index-validates the result, so
    this can only ever confirm a name that exists; it cannot fabricate one."""
    system = ("You map Debian package names to their Wolfi (Chainguard) equivalents. You are "
              "given a Debian package and a list of candidate Wolfi package names that actually "
              "exist in the index. Choose the package(s) that provide the same functionality, or "
              "return an empty list if none genuinely match. One-line reason. Never invent a name "
              "outside the candidate list.")
    user = (f"Debian package: `{debian_name}`\n"
            f"Candidate Wolfi packages (all exist in the index):\n"
            + "\n".join(f"- {c}" for c in candidates) + "\n\n"
            "Which, if any, are the correct Wolfi equivalent?")
    data = _structured_call(model, system, user, _WOLFI_SCHEMA)
    return list(data.get("wolfi_packages", [])), data.get("reason", "")
