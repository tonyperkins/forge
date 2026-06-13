"""forge agent — automates the manual review/test/adjust loop dfc's docs prescribe.

Scope is locked to the three failure classes the uptime-kuma manual build actually
produced (CONTEXT §4, decisions.md): A (phantom base image → structural flatten),
B (apt→apk package-name resolution) and D (USER root → restore non-root).

Architecture boundary (CONTEXT §4): dfc, builds, scans, signing and the report stay
deterministic code. The Claude API is used *only* for log diagnosis and fix-drafting —
the "manual review" steps — and lives behind `agent.llm`. Nothing here generates a whole
Dockerfile from scratch.
"""
