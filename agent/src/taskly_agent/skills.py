"""Bundled instructions only; no plugins, PR paths, environment or execution.

The package and resources must come from a reviewed, trusted installation, not
an untrusted PR checkout. Root discovery is fixed relative to this module.
Only the four manifest directories are accepted, each containing SKILL.md only;
extra files/directories are rejected. Nothing is executed. Reads use directory
handles and no-follow.

Portable frontmatter profile: exactly name and description, one scalar per
line. Scalars may be simple plain text or JSON double-quoted YAML strings.
Multiline YAML, tags, aliases and optional metadata fields are intentionally
unsupported. Files remain valid Agent Skills files for other compatible hosts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import Annotated, TYPE_CHECKING

from pydantic import Field

from .contracts import AgentDecision, Contract, NonEmptyText, SkillIdentifier

if TYPE_CHECKING:
    from .analysis import TestImpact


_BUNDLED_ROOT = Path(__file__).resolve().parent / "bundled_skills"
_ORDER = ("python-unit-tests", "react-unit-tests", "update-existing-tests", "review-generated-tests")
MAX_SKILL_BYTES = 16_384


class SkillError(ValueError):
    """A bundled catalog is invalid; diagnostics never contain instruction text."""


class Skill(Contract):
    name: SkillIdentifier
    description: Annotated[NonEmptyText, Field(max_length=1024)]
    instructions: NonEmptyText


def _parse_skill(raw: bytes) -> Skill:
    try:
        text = raw.decode("utf-8", errors="strict")
        lines = text.splitlines()
        if not lines or lines[0] != "---" or "\0" in text:
            raise ValueError
        end = lines.index("---", 1)
        metadata = {}
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if not separator or key not in {"name", "description"} or key in metadata:
                raise ValueError
            value = value.strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif (not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .,;()/_-]*", value)
                  or value.casefold() in {"true", "false", "null", "yes", "no", "on", "off", "y", "n"}):
                raise ValueError
            metadata[key] = value
        return Skill.model_validate({**metadata, "instructions": "\n".join(lines[end + 1:])})
    except ValueError:
        raise SkillError("invalid skill metadata or content") from None


def _read_skill(root_fd: int, directory: str, limit: int) -> bytes:
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        if set(os.listdir(directory_fd)) != {"SKILL.md"}:
            raise SkillError("skill directory must contain only SKILL.md")
        file_fd = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SkillError("skill must be a regular file with one hard link")
            if info.st_size > limit:
                raise SkillError("skill byte limit exceeded")
            raw = bytearray()
            while len(raw) <= limit:
                chunk = os.read(file_fd, limit + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > limit:
                raise SkillError("skill byte limit exceeded")
            return bytes(raw)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def load_bundled_skills(*, max_skill_bytes: int = MAX_SKILL_BYTES) -> tuple[Skill, ...]:
    """Validate the small, fixed catalog; callers pass only selected bodies on.

    No root/path argument exists for PR metadata to override. Tests can replace
    the module-owned root with an isolated trusted fixture. A corrupt catalog
    fails closed, including when one required skill is absent.
    """
    if type(max_skill_bytes) is not int or max_skill_bytes <= 0:
        raise SkillError("invalid skill byte limit")
    try:
        root_fd = os.open(_BUNDLED_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if set(os.listdir(root_fd)) != set(_ORDER):
                raise SkillError("missing or unexpected bundled skill directory")
            skills = tuple(_parse_skill(_read_skill(root_fd, name, max_skill_bytes)) for name in _ORDER)
        finally:
            os.close(root_fd)
        if len({skill.name for skill in skills}) != len(skills):
            raise SkillError("duplicate skill identifier")
        if tuple(skill.name for skill in skills) != _ORDER:
            raise SkillError("skill identifier must match its directory")
        return skills
    except OSError:
        raise SkillError("bundled skill path is missing or unsafe") from None


def select_skill_ids(decision: AgentDecision, impacts: tuple[TestImpact, ...],
                     catalog: tuple[Skill, ...]) -> tuple[str, ...]:
    """Select only approved identifiers; input instructions never control routing.

    Review guidance applies to generated tests without enabling a reviewer call.
    Removed-only targets do not activate the update skill.
    """
    if decision.action != "generate" or decision.classification == "irrelevant":
        return ()
    wanted = {"review-generated-tests"}
    if decision.classification in {"backend", "mixed"}:
        wanted.add("python-unit-tests")
    if decision.classification in {"frontend", "mixed"}:
        wanted.add("react-unit-tests")
    if any(impact.recommendation == "review_tests" for impact in impacts):
        wanted.add("update-existing-tests")
    if not wanted.issubset({skill.name for skill in catalog}):
        raise SkillError("selected skill is missing from the bundled catalog")
    return tuple(name for name in _ORDER if name in wanted)
