"""A minimal Dockerfile model: parse into stages, apply the LLM's bounded edit ops, render.

Deterministic (CONTEXT §4). The LLM proposes edit *operations* (agent.llm.Edit); this module
is what actually mutates the file, so the LLM never touches Dockerfile text directly. The op
vocabulary is intentionally tiny — replace a stage's base image, set its USER, add an apk
package — which is all the A/B/D fixes need and nothing that amounts to authoring a Dockerfile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# FROM <image>[@sha256:...] [AS <alias>]  — case-insensitive, tolerant of extra spacing.
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<alias>\S+))?\s*$", re.IGNORECASE)
_USER_RE = re.compile(r"^\s*USER\s+(?P<user>\S+)\s*$", re.IGNORECASE)
_APK_ADD_RE = re.compile(r"\bapk\s+add\b", re.IGNORECASE)
_ARG_RE = re.compile(r"^\s*ARG\s+(?P<name>\w+)(?:=(?P<default>\S+))?\s*$", re.IGNORECASE)
_COPY_FROM_RE = re.compile(r"--from=(?P<ref>[\w./:-]+)")
_VAR_RE = re.compile(r"^\$\{?(?P<name>\w+)\}?$")


class DockerfileError(RuntimeError):
    pass


@dataclass
class Stage:
    """One FROM…stage. `start`/`end` are line indices into the owning Dockerfile's `lines`
    (end exclusive); the stage body is lines[start:end]."""

    index: int
    alias: str          # "" if the FROM had no AS clause
    image: str          # the full image ref including any @digest
    start: int          # index of the FROM line
    end: int            # exclusive end (next FROM, or EOF)


class Dockerfile:
    def __init__(self, text: str):
        self.lines: list[str] = text.splitlines()
        self.stages: list[Stage] = self._parse_stages()

    @classmethod
    def from_file(cls, path) -> "Dockerfile":
        from pathlib import Path
        return cls(Path(path).read_text())

    def _parse_stages(self) -> list[Stage]:
        # Resolve `FROM $BASE_IMAGE` against a preceding `ARG BASE_IMAGE=<default>` so the
        # stage's real base (often a phantom cgr.dev image hidden behind the ARG) is visible.
        args: dict[str, str] = {}
        froms: list[tuple[int, str, str]] = []
        for i, line in enumerate(self.lines):
            am = _ARG_RE.match(line)
            if am and am.group("default"):
                args[am.group("name")] = am.group("default")
                continue
            m = _FROM_RE.match(line)
            if m:
                image = m.group("image")
                vm = _VAR_RE.match(image)
                if vm and vm.group("name") in args:
                    image = args[vm.group("name")]
                froms.append((i, image, m.group("alias") or ""))
        stages: list[Stage] = []
        for n, (i, image, alias) in enumerate(froms):
            end = froms[n + 1][0] if n + 1 < len(froms) else len(self.lines)
            stages.append(Stage(index=n, alias=alias, image=image, start=i, end=end))
        return stages

    def reachable_from(self, alias: str) -> set[str]:
        """Aliases the given stage actually depends on (FROM-parent + `COPY --from=`). Lets us
        scope signals/edits to the stages a `--target` build will really compile, ignoring the
        upstream Dockerfile's unrelated targets (nightly, pr-test2, upload-artifact, …)."""
        aliases = {s.alias for s in self.stages if s.alias}
        seen: set[str] = set()
        stack = [alias]
        while stack:
            a = stack.pop()
            if a in seen or not self.has_stage(a):
                continue
            seen.add(a)
            s = self.stage(a)
            base = s.image.split("@")[0]
            if base in aliases:
                stack.append(base)
            for i in range(s.start + 1, s.end):
                for m in _COPY_FROM_RE.finditer(self.lines[i]):
                    if m.group("ref") in aliases:
                        stack.append(m.group("ref"))
        return seen

    # ── lookup ────────────────────────────────────────────────────────────────
    def stage(self, alias: str) -> Stage:
        for s in self.stages:
            if s.alias == alias:
                return s
        raise DockerfileError(f"no stage with alias {alias!r} "
                              f"(have: {[s.alias for s in self.stages]})")

    def has_stage(self, alias: str) -> bool:
        return any(s.alias == alias for s in self.stages)

    # ── edits (each re-parses so indices stay valid for the next edit) ──────────
    def replace_base_image(self, alias: str, new_image: str) -> None:
        """Class A — swap a stage's FROM image, preserving the `AS <alias>` clause."""
        s = self.stage(alias)
        suffix = f" AS {s.alias}" if s.alias else ""
        self.lines[s.start] = f"FROM {new_image}{suffix}"
        self.stages = self._parse_stages()

    def set_user(self, alias: str, user: str) -> None:
        """Class D — ensure the stage ends running as `user`. If the stage already has a USER
        line we rewrite the last one; otherwise we append a USER at the end of the stage."""
        s = self.stage(alias)
        last_user = None
        for i in range(s.start + 1, s.end):
            if _USER_RE.match(self.lines[i]):
                last_user = i
        if last_user is not None:
            self.lines[last_user] = f"USER {user}"
        else:
            self.lines.insert(s.end, f"USER {user}")
        self.stages = self._parse_stages()

    def add_package(self, alias: str, package: str) -> None:
        """Class B (rarely needed in-loop) — add an apk package. Appends to an existing
        `apk add` line in the stage if present, else inserts a new install line after FROM."""
        s = self.stage(alias)
        for i in range(s.start + 1, s.end):
            if _APK_ADD_RE.search(self.lines[i]) and package not in self.lines[i].split():
                self.lines[i] = self.lines[i].rstrip() + f" {package}"
                return
        self.lines.insert(s.start + 1, f"RUN apk add --no-cache {package}")
        self.stages = self._parse_stages()

    # ── render ─────────────────────────────────────────────────────────────────
    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def runtime_stage(self) -> Stage:
        """The last stage — the image that actually ships (what class-D non-root targets)."""
        if not self.stages:
            raise DockerfileError("Dockerfile has no FROM stages")
        return self.stages[-1]

    def final_user(self, alias: str | None = None) -> str | None:
        """The USER in effect for a stage (the runtime stage by default), or None if it never
        sets one (then the base image's default applies)."""
        s = self.stage(alias) if alias else self.runtime_stage()
        user = None
        for i in range(s.start + 1, s.end):
            m = _USER_RE.match(self.lines[i])
            if m:
                user = m.group("user")
        return user
