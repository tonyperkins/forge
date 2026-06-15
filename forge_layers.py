"""Shared vulnerability-layer classification for forge.

Single source of truth for the OS/runtime vs application split that underpins the
507 -> 0 headline. Imported by the report generators (`scripts/gen_report.py`,
`scripts/cve_summary.py`) and the agent's verify gate (`agent/verifier.py`) so the
classification can never drift between them (CONTEXT §4 / §10).

Why these types are "OS/runtime layer": grype/syft tag every package with a `type`.
`deb`/`apk`/`rpm` are OS package-manager packages; `python` and `go-module` are
language-runtime and system-binary components that ship *in the base image and the
image build* (e.g. the Go-compiled system tooling and Python runtime a base carries).
All of these are what choosing and building a hardened base image actually controls.
`npm`, by contrast, is uptime-kuma's own JavaScript dependency tree, declared in the
application's `package.json` — hardening the base image does not change it; that is
the domain of Chainguard Libraries and is out of scope here.

Target-specific note (honest scoping): for uptime-kuma the application's dependencies
are exclusively npm, so any `python`/`go-module` artifacts present come from the base
image and system tooling, not the app — which is why they belong to the layer image
hardening controls. An app that shipped first-party Python/Go code would need this
split revisited.
"""
from __future__ import annotations

# OS/runtime layer = the layer image hardening controls (base image + image build).
OS_TYPES = frozenset({"deb", "apk", "rpm", "python", "go-module"})
