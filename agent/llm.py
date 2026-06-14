"""The agent's only LLM surface (CONTEXT §4 boundary, kept in one file on purpose).

Transport: the Claude models are reached through the **Kilo Gateway**, which is OpenAI-compatible
only (no native Anthropic `/v1/messages`), so this module uses the OpenAI SDK pointed at Kilo's
base URL. Only the transport changed — the contract did not: `diagnose()` still returns the same
typed *edit operations* (replace_base_image / set_user / add_package) and `adjudicate_wolfi()`
still picks among real Wolfi candidates. The LLM never emits Dockerfile text. We use strict
`json_schema` structured output (verified to round-trip through Kilo) AND validate the result
on our side — if the proxy ever returns something off-contract, we fail loudly rather than
accept free text, which would silently widen the LLM's remit past edit-ops (§4 forbids that).

Secrets stay at the edge: this module is a pure `os.environ` reader (KILO_API_KEY, KILO_BASE_URL)
and never touches `.env` — the entrypoint (`forge_agent`) loads `.env` into the environment.

Model tiering (owner decision, decisions.md): Sonnet by default, one-hop escalation to Opus when
a Sonnet fix fails to move the build. The escalation branch lives in the loop (`forge_agent`);
this module takes whichever model it's told to use and records it on the result for provenance.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

# Two swappable constants — Kilo's provider/model IDs (dotted, not the first-party hyphenated
# form). Verified reachable via Kilo /models (decisions.md).
MODEL = "anthropic/claude-sonnet-4.6"
ESCALATION_MODEL = "anthropic/claude-opus-4.8"

# Canonical Kilo gateway base; KILO_BASE_URL overrides it. KILO_API_KEY is required (no default).
DEFAULT_BASE_URL = "https://api.kilo.ai/api/gateway"

# Scope is locked to the three observed failure classes (CONTEXT §4). The LLM may only ever
# return one of these; anything else is rejected by the loop's scope guard.
ALLOWED_CLASSES = ("A", "B", "D")
_ALLOWED_OPS = ("replace_base_image", "set_user", "add_package")

_MAX_TOKENS = 8000  # diagnosis output is small (a rationale + a few edit ops)


class LLMError(RuntimeError):
    """Surfaced loudly — a diagnosis we can't trust is a stop, never a silent skip."""


# ─────────────────────────────────────────────────────────────────────────────
# Typed results
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Edit:
    """One targeted Dockerfile edit the LLM proposes. The op vocabulary is deliberately tiny so
    the LLM cannot author free-form Dockerfile content — only point at a stage and change one
    bounded thing the deterministic applier knows how to make."""

    op: str        # one of _ALLOWED_OPS
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


# JSON schema for the structured diagnosis. strict json_schema requires every property listed in
# `required` and `additionalProperties: false` on every object — satisfied below.
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
                    "op": {"type": "string", "enum": list(_ALLOWED_OPS)},
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
# Client (lazy so importing this module never requires credentials)
# ─────────────────────────────────────────────────────────────────────────────
def _client():
    key = os.environ.get("KILO_API_KEY")
    if not key:
        raise LLMError("KILO_API_KEY is not set in the environment — set KILO_API_KEY "
                       "(see .env.example; the forge_agent entrypoint loads .env for you).")
    base_url = os.environ.get("KILO_BASE_URL") or DEFAULT_BASE_URL
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError("openai SDK not installed — `pip install -r requirements.txt` into the "
                       "project venv.") from exc
    return OpenAI(api_key=key, base_url=base_url)


def _structured_call(model: str, system: str, user: str, schema: dict, schema_name: str) -> dict:
    """One structured-output request → parsed JSON dict, via Kilo (OpenAI-compatible)."""
    import openai
    client = _client()
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
        )
    except openai.APIStatusError as exc:
        raise LLMError(f"Kilo API error ({exc.status_code}) on {model}: {exc}") from exc
    except openai.OpenAIError as exc:  # connection/timeout/auth/etc.
        raise LLMError(f"Kilo API call failed on {model}: {exc}") from exc

    if not resp.choices:
        raise LLMError(f"empty response from {model} (no choices)")
    content = resp.choices[0].message.content
    if not content:
        raise LLMError(f"empty content from {model} "
                       f"(finish_reason={resp.choices[0].finish_reason})")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        # Defensive (§4): malformed structured output is a hard error, never accepted as text.
        raise LLMError(f"non-JSON structured output from {model}: {exc}; raw={content[:200]!r}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"structured output from {model} was not a JSON object: {type(data)}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# The two call sites
# ─────────────────────────────────────────────────────────────────────────────
def _validate_edits(raw) -> tuple[Edit, ...]:
    """Enforce the edit-op contract on our side regardless of what the proxy returns. An
    out-of-vocabulary op or a non-string field is a hard error — we never widen to free text."""
    if not isinstance(raw, list):
        raise LLMError(f"edits was not a list: {type(raw)}")
    edits: list[Edit] = []
    for e in raw:
        if not isinstance(e, dict):
            raise LLMError(f"edit was not an object: {e!r}")
        op = e.get("op")
        if op not in _ALLOWED_OPS:
            raise LLMError(f"LLM proposed out-of-vocabulary edit op {op!r} "
                           f"(contract forbids free-form edits; allowed: {_ALLOWED_OPS})")
        stage, value, reason = e.get("stage"), e.get("value"), e.get("reason", "")
        if not isinstance(stage, str) or not isinstance(value, str):
            raise LLMError(f"edit {op!r} missing string stage/value: {e!r}")
        edits.append(Edit(op=op, stage=stage, value=value,
                          reason=reason if isinstance(reason, str) else ""))
    return tuple(edits)


def diagnose(dockerfile_text: str, error_tail: str, signals: dict, model: str = MODEL) -> Diagnosis:
    """LLM call #1 — read a failed build and draft a structured fix (edit ops only)."""
    user = (
        "## Current Dockerfile\n```\n" + dockerfile_text + "\n```\n\n"
        "## Deterministic signals gathered from the failed build\n"
        + json.dumps(signals, indent=2) + "\n\n"
        "## Build error (tail)\n```\n" + error_tail + "\n```\n\n"
        "Diagnose the failure class and return the edit operations to fix it."
    )
    data = _structured_call(model, _DIAGNOSE_SYSTEM, user, _DIAGNOSIS_SCHEMA, "diagnosis")

    failure_class = data.get("failure_class")
    if failure_class not in ("A", "B", "D", "unknown"):
        raise LLMError(f"invalid failure_class from {model}: {failure_class!r}")
    return Diagnosis(
        failure_class=failure_class,
        rationale=data.get("rationale", "") if isinstance(data.get("rationale"), str) else "",
        confidence=data.get("confidence", "low") if data.get("confidence") in ("high", "medium", "low") else "low",
        edits=_validate_edits(data.get("edits", [])),
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
    data = _structured_call(model, system, user, _WOLFI_SCHEMA, "wolfi_mapping")
    pkgs = data.get("wolfi_packages", [])
    if not isinstance(pkgs, list) or not all(isinstance(p, str) for p in pkgs):
        raise LLMError(f"invalid wolfi_packages from {model}: {pkgs!r}")
    reason = data.get("reason", "")
    return pkgs, reason if isinstance(reason, str) else ""
