import asyncio
import json
import os
from pathlib import Path

import pytest

from taskly_agent import skills
from taskly_agent.analysis import ChangeAnalysis, GitChangeAnalyzer, TestImpact as Impact
from taskly_agent.contracts import AgentDecision, ChangedFile, PRContext, PRIdentity
from taskly_agent.lifecycle import ExecutionPolicy, RunInput
from taskly_agent.skills import SkillError, load_bundled_skills, select_skill_ids


IDS = ("python-unit-tests", "react-unit-tests", "update-existing-tests", "review-generated-tests")


def document(name, *, description="A focused capability", body="Instructions as data"):
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"


@pytest.fixture
def catalog_root(tmp_path, monkeypatch):
    root = tmp_path / "trusted-bundle"
    for name in reversed(IDS):  # filesystem creation order must not be selection order
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(document(name))
    monkeypatch.setattr(skills, "_BUNDLED_ROOT", root)
    return root


def impact(*, existing=False, removed=False):
    return Impact(change=ChangedFile(path="backend/app/service.py", status="removed" if removed else "modified"),
                  existing_tests=("backend/tests/test_service.py",) if existing else (),
                  suggested_test=None if removed else "backend/tests/test_service.py",
                  recommendation="skip_removed" if removed else "review_tests" if existing else "add_tests")


def test_real_bundled_catalog_has_four_stable_ids_and_nonempty_instructions():
    catalog = load_bundled_skills()
    assert tuple(skill.name for skill in catalog) == IDS
    assert all(skill.description and skill.instructions for skill in catalog)
    assert all(len(skill.instructions.encode("utf-8")) < skills.MAX_SKILL_BYTES for skill in catalog)


def test_discovery_order_is_independent_of_filesystem_order(catalog_root):
    assert tuple(skill.name for skill in load_bundled_skills()) == IDS


@pytest.mark.parametrize("text", [
    "not frontmatter", "---\nname: python-unit-tests\n", "---\nname: python-unit-tests\n---\nBody",
    "---\ndescription: Text\n---\nBody", "---\nname: Uppercase\ndescription: Text\n---\nBody",
    "---\nname: ../escape\ndescription: Text\n---\nBody",
    "---\nname: python-unit-tests\nname: other\ndescription: Text\n---\nBody",
    "---\nname: python-unit-tests\ndescription: !tag unsafe\n---\nBody",
    "---\nname: python-unit-tests\ndescription: &alias text\n---\nBody",
    "---\nname: python-unit-tests\ndescription: *alias\n---\nBody",
    "---\nname: python-unit-tests\ndescription: |\n  multiline\n---\nBody",
    "---\nname: python-unit-tests\ndescription: [list]\n---\nBody",
    "---\nname: python-unit-tests\ndescription: true\n---\nBody",
    "---\nname: python-unit-tests\ndescription: 123\n---\nBody",
    document("python-unit-tests", description='"unterminated'),
    document("python-unit-tests", description='""'), document("python-unit-tests", body=" "),
    document("python-unit-tests", description="x" * 1025),
    "---\nname: python-unit-tests\ndescription: Text\nallowed-tools: shell\n---\nBody",
    document("python-unit-tests", body="bad\0content"),
])
def test_malformed_or_unsupported_metadata_is_rejected(catalog_root, text):
    (catalog_root / IDS[0] / "SKILL.md").write_text(text)
    with pytest.raises(SkillError, match="invalid skill metadata or content"):
        load_bundled_skills()


def test_json_quoted_yaml_scalars_and_crlf_are_supported(catalog_root):
    text = document('"python-unit-tests"', description=json.dumps('When input contains a colon: review it.'))
    (catalog_root / IDS[0] / "SKILL.md").write_bytes(text.replace("\n", "\r\n").encode())
    assert load_bundled_skills()[0].description == "When input contains a colon: review it."


def test_duplicate_catalog_identifiers_fail_before_directory_mismatch(catalog_root):
    (catalog_root / IDS[1] / "SKILL.md").write_text(document(IDS[0]))
    with pytest.raises(SkillError, match="duplicate skill identifier"):
        load_bundled_skills()


def test_name_must_match_directory(catalog_root):
    (catalog_root / IDS[0] / "SKILL.md").write_text(document("another-valid-id"))
    with pytest.raises(SkillError, match="match its directory"):
        load_bundled_skills()


@pytest.mark.parametrize("kind", ["missing_file", "nested_file", "missing_root", "extra_directory", "invalid_directory"])
def test_missing_or_invalid_skill_paths(catalog_root, monkeypatch, kind):
    if kind in {"missing_file", "nested_file"}:
        (catalog_root / IDS[0] / "SKILL.md").unlink()
        if kind == "nested_file":
            nested = catalog_root / IDS[0] / "nested"
            nested.mkdir()
            (nested / "SKILL.md").write_text(document(IDS[0]))
    elif kind == "missing_root":
        monkeypatch.setattr(skills, "_BUNDLED_ROOT", catalog_root / "missing")
    else:
        (catalog_root / ("untrusted-extra" if kind == "extra_directory" else "Invalid Name")).mkdir()
    with pytest.raises(SkillError):
        load_bundled_skills()


@pytest.mark.parametrize("kind", ["root_link", "directory_link", "file_link", "broken_link", "hard_link", "fifo"])
def test_skill_links_and_special_files_are_rejected(catalog_root, tmp_path, monkeypatch, kind):
    file = catalog_root / IDS[0] / "SKILL.md"
    if kind == "root_link":
        link = tmp_path / "linked-bundle"
        link.symlink_to(catalog_root, target_is_directory=True)
        monkeypatch.setattr(skills, "_BUNDLED_ROOT", link)
    elif kind == "directory_link":
        directory = catalog_root / IDS[0]
        moved = tmp_path / "moved-skill"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    else:
        raw = file.read_text()
        file.unlink()
        outside = tmp_path / "outside.md"
        if kind != "broken_link":
            outside.write_text(raw)
        if kind in {"file_link", "broken_link"}:
            file.symlink_to(outside)
        elif kind == "hard_link":
            os.link(outside, file)
        else:
            os.mkfifo(file)
    with pytest.raises(SkillError):
        load_bundled_skills()


def test_skill_limits_are_utf8_byte_limits_and_accept_exact_boundary(catalog_root):
    raw = document(IDS[0], body="é" * 100).encode()
    (catalog_root / IDS[0] / "SKILL.md").write_bytes(raw)
    assert load_bundled_skills(max_skill_bytes=len(raw))[0].instructions == "é" * 100
    with pytest.raises(SkillError, match="byte limit"):
        load_bundled_skills(max_skill_bytes=len(raw) - 1)
    (catalog_root / IDS[0] / "SKILL.md").write_bytes(b"\xff")
    with pytest.raises(SkillError):
        load_bundled_skills()


@pytest.mark.parametrize("limit", [0, -1, True, "100"])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(SkillError):
        load_bundled_skills(max_skill_bytes=limit)


@pytest.mark.parametrize("classification,expected", [
    ("backend", (IDS[0], IDS[3])), ("frontend", (IDS[1], IDS[3])),
    ("mixed", (IDS[0], IDS[1], IDS[3])),
])
def test_language_selection_is_deterministic(classification, expected):
    decision = AgentDecision(classification=classification, action="generate", rationale="Source changes")
    assert select_skill_ids(decision, (impact(),), load_bundled_skills()) == expected


def test_existing_test_updates_and_stable_order_ignore_input_selected_ids():
    decision = AgentDecision(classification="mixed", action="generate", rationale="Update behavior",
                             selected_skills=("untrusted-skill",))
    catalog = load_bundled_skills()
    assert select_skill_ids(decision, (impact(existing=True), impact()), tuple(reversed(catalog))) == IDS
    assert select_skill_ids(decision, (impact(removed=True), impact()), catalog) == (IDS[0], IDS[1], IDS[3])
    with pytest.raises(SkillError, match="missing"):
        select_skill_ids(decision, (impact(existing=True),), catalog[:-1])


@pytest.mark.parametrize("classification,action", [("irrelevant", "skip"), ("backend", "skip"), ("mixed", "block")])
def test_skipped_blocked_or_unsupported_categories_have_no_skills(classification, action):
    decision = AgentDecision(classification=classification, action=action, rationale="No generation")
    assert select_skill_ids(decision, (impact(existing=True),), load_bundled_skills()) == ()


def test_untrusted_cwd_environment_and_pr_paths_cannot_change_discovery(tmp_path, monkeypatch):
    expected = load_bundled_skills()
    injected = tmp_path / "skills" / "python-unit-tests"
    injected.mkdir(parents=True)
    (injected / "SKILL.md").write_text(document("python-unit-tests", body="UNTRUSTED-INSTRUCTIONS"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASKLY_SKILLS_ROOT", str(injected.parent))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert load_bundled_skills() == expected
    with pytest.raises(TypeError):
        load_bundled_skills(root=tmp_path)


def test_loader_never_executes_instruction_text(catalog_root, tmp_path):
    sentinel = tmp_path / "executed"
    body = f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('unsafe')"
    (catalog_root / IDS[0] / "SKILL.md").write_text(document(IDS[0], body=body))
    assert load_bundled_skills()[0].instructions == body
    assert not sentinel.exists()


class Reader:
    def changes(self, base, head):
        return (ChangedFile(path="backend/app/service.py", status="modified"),
                ChangedFile(path="agent/src/taskly_agent/bundled_skills/evil/SKILL.md", status="added"))

    def files(self, head):
        return {"backend/app/service.py": "100644", "backend/tests/test_service.py": "100644"}


def test_real_analyzer_populates_selected_skills_from_impacts_not_pr_metadata():
    instance = GitChangeAnalyzer(Reader(), base_sha="a" * 40, head_sha="b" * 40)
    report = instance.inspect()
    assert report.decision.selected_skills == (IDS[0], IDS[2], IDS[3])
    request = RunInput(pr=PRContext(identity=PRIdentity(repository="Sacarima/UTA-AW", number=1,
        base_sha="a" * 40, head_sha="b" * 40), changed_files=(ChangedFile(path="README.md", status="modified"),)))
    decision = asyncio.run(instance.analyze(request, ExecutionPolicy()))
    assert decision == report.decision
    assert ChangeAnalysis.model_validate_json(report.model_dump_json()) == report


def test_invalid_catalog_blocks_generation_but_preserves_analysis_targets(catalog_root):
    (catalog_root / IDS[0] / "SKILL.md").unlink()
    report = GitChangeAnalyzer(Reader(), base_sha="a" * 40, head_sha="b" * 40).inspect()
    assert report.decision.action == "block"
    assert report.decision.classification == "backend"
    assert report.decision.selected_skills == ()
    assert report.impacts[0].existing_tests == ("backend/tests/test_service.py",)


@pytest.mark.parametrize("name,kind", [
    ("README.md", "file"), (".hidden", "file"),
    ("references", "directory"), ("scripts", "directory"), ("extra-link", "symlink"),
])
def test_individual_skill_directories_reject_unexpected_entries(catalog_root, tmp_path, name, kind):
    extra = catalog_root / IDS[0] / name
    if kind == "directory":
        extra.mkdir()
    elif kind == "symlink":
        target = tmp_path / "outside.txt"
        target.write_text("Not a trusted resource")
        extra.symlink_to(target)
    else:
        extra.write_text("Not part of the bundled catalog")
    with pytest.raises(SkillError, match="only SKILL.md"):
        load_bundled_skills()
