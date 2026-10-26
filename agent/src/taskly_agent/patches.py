"""Export a verified test-only proposal as data, never apply it to a checkout."""

import difflib
import hashlib
import json
import os
from pathlib import Path

from .adapters import _reject_credentials
from .application import ProposalApplier, _audit, _DIRECTORY, _Rejected
from .git_changes import GitAnalysisError, GitReader
from .lifecycle import ExecutionPolicy, ExecutionReport, proposal_digest


def _text_lines(text: str) -> list[str]:
    # Git patches count LF-delimited byte lines. Preserve CRLF verbatim, but
    # reject other separators instead of interpreting them as patch lines.
    if any(separator in text.replace("\r\n", "\n")
           for separator in ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")):
        raise ValueError("unsupported test line separator")
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _round_trip(applier, proposal, inventory, changes, data):
    """Apply data, never code, to an audited disposable pinned-head baseline."""
    expected = {}
    for path, entry in inventory.items():
        original = applier.analyzer.reader.blob(entry.object_id,
            max_bytes=applier.limits.max_workspace_file_bytes)
        expected[path] = (hashlib.sha256(original).hexdigest(), len(original))
    receipts = {change.path: change for change in changes}
    if set(receipts) != {change.path for change in proposal.changes}:
        raise ValueError("proposal hash mismatch")
    for change in proposal.changes:
        content = change.content.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        original = expected.get(change.path)
        receipt = receipts[change.path]
        if (receipt.operation != change.operation or receipt.resulting_sha256 != digest
            or receipt.original_sha256 != (original[0] if original else None)):
            raise ValueError("proposal hash mismatch")
        expected[change.path] = (digest, len(content))
    try:
        with applier.baseline(applier.identity) as workspace:
            patch = workspace.parent / "round-trip.patch"
            fd = os.open(patch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            # Fixed arguments, clean Git environment, bounded diagnostics/time;
            # no repository initialization, hooks, index, filters or execution.
            GitReader(workspace)._run("apply", "--whitespace=nowarn", "--", str(patch))
            root = os.open(workspace, _DIRECTORY)
            try:
                _audit(root, expected)  # Includes untouched files and link safety.
            finally:
                os.close(root)
    except (GitAnalysisError, _Rejected, OSError):
        raise ValueError("patch round-trip rejected") from None


def export_verified_patch(applier: ProposalApplier, execution: ExecutionReport,
                          policy: ExecutionPolicy, destination: Path) -> None:
    """Create a new private artifact directory only after passing verification.

    The controller's verifier establishes provenance, not these structural checks.
    Reapply with the existing applier to recheck exact scope/operations. Exported
    code is untrusted review data and must never be executed by a publisher.
    """
    execution = ExecutionReport.model_validate(execution, context={"content_limits": policy.content_limits})
    evidence, proposal = execution.result.verification, execution.proposal
    expected = applier.identity
    if destination.parent.resolve(strict=True).is_relative_to(applier.analyzer.reader.repository):
        raise ValueError("artifact must remain outside the candidate repository")
    if (execution.result.outcome != "passed" or proposal is None or evidence is None
        or evidence.state != "available" or evidence.outcome != "passed" or evidence.identity is None
        or evidence.identity.repository != expected.repository or evidence.identity.head_sha != expected.head_sha
        or evidence.identity.proposal_digest != proposal_digest(proposal) or evidence.identity.policy_digest != policy.digest):
        raise ValueError("passing independent verification required")
    _reject_credentials(proposal.model_dump(mode="json"))
    inventory = applier.analyzer.reader.objects(expected.head_sha)
    chunks = []
    with applier.apply(expected, proposal) as applied:
        if applied.state != "applied" or applied.proposal_digest != evidence.identity.proposal_digest:
            raise ValueError("proposal application rejected")
        for change in proposal.changes:
            before = ""
            if change.operation == "modify":
                before = applier.analyzer.reader.blob(inventory[change.path].object_id,
                    max_bytes=applier.limits.max_workspace_file_bytes).decode("utf-8")
            if before == change.content or "\x00" in before or "\x00" in change.content:
                raise ValueError("a nonempty text test change is required")
            chunks.append(f"diff --git a/{change.path} b/{change.path}\n")
            if change.operation == "create":
                chunks.append("new file mode 100644\n")
            for line in difflib.unified_diff(_text_lines(before), _text_lines(change.content),
                fromfile="/dev/null" if change.operation == "create" else "a/" + change.path,
                tofile="b/" + change.path):
                chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        data = "".join(chunks).encode("utf-8")
        if not data or len(data) > 1_048_576:
            raise ValueError("patch unavailable or oversized")
        _round_trip(applier, proposal, inventory, applied.changes, data)
        manifest = {"identity": expected.model_dump(mode="json"), "verification_identity": evidence.identity.model_dump(mode="json"),
            "patch_sha256": hashlib.sha256(data).hexdigest(), "changes": [item.model_dump(mode="json") for item in applied.changes]}
    # A new directory prevents reuse of an existing/symlinked artifact directory.
    # Its parent is a trusted workflow-owned location outside the candidate tree.
    destination.mkdir(mode=0o700)
    try:
        for name, body in (("tests.patch", data), ("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())):
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
    except BaseException:
        for name in ("tests.patch", "manifest.json"):
            (destination / name).unlink(missing_ok=True)
        destination.rmdir()
        raise
