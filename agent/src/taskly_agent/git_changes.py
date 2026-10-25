"""Bounded, read-only Git metadata. No checkout, fetch, shell, or PR execution.

Only a trusted local repository and full commit object IDs are accepted. Diff
is explicitly base-tree to head-tree; callers should supply a merge base when
that is their intended PR range. Git configuration/environment cannot enable
external diff helpers, textconv, pagers, hooks, replacement objects or protocols.
"""

import contextlib
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from pydantic import TypeAdapter, ValidationError

from .contracts import ChangedFile, CommitSHA
from .policy import validate_repository_path


GIT_EXECUTABLE = "/usr/bin/git"
_SHA = TypeAdapter(CommitSHA)
_MODES = {b"100644": b"blob", b"100755": b"blob", b"120000": b"blob", b"160000": b"commit"}


class GitAnalysisError(ValueError):
    """Fixed diagnostics only; never include raw Git stderr or untrusted data."""


def validate_commit(value: str) -> str:
    try:
        return _SHA.validate_python(value, strict=True)
    except ValidationError:
        raise GitAnalysisError("invalid_commit") from None


def _records(output: bytes) -> list[bytes]:
    if not isinstance(output, bytes) or (output and not output.endswith(b"\0")):
        raise GitAnalysisError("invalid_output")
    return output[:-1].split(b"\0") if output else []


def _path(raw: bytes) -> str:
    try:
        return validate_repository_path(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError):
        raise GitAnalysisError("invalid_output") from None


def parse_name_status(output: bytes) -> tuple[ChangedFile, ...]:
    """Parse --name-status -z, including R<similarity>\0old\0new\0."""
    records = iter(_records(output))
    changes = []
    seen = set()
    try:
        for status in records:
            if status in {b"A", b"M", b"D"}:
                change = ChangedFile(path=_path(next(records)), status={
                    b"A": "added", b"M": "modified", b"D": "removed",
                }[status])
            elif re.fullmatch(rb"R[0-9]{1,3}", status) and int(status[1:]) <= 100:
                previous, path = _path(next(records)), _path(next(records))
                change = ChangedFile(path=path, previous_path=previous, status="renamed")
            else:
                raise GitAnalysisError("unsupported_change_status")
            if change.path in seen:
                raise GitAnalysisError("invalid_output")
            seen.add(change.path)
            changes.append(change)
    except (StopIteration, ValidationError):
        raise GitAnalysisError("invalid_output") from None
    return tuple(changes)


@dataclass(frozen=True)
class GitObject:
    mode: str
    object_id: str


def parse_tree_objects(output: bytes) -> dict[str, GitObject]:
    """Parse ls-tree -r -z without reading or following filesystem paths."""
    files = {}
    for record in _records(output):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
            if _MODES.get(mode) != kind:
                raise ValueError
            validate_commit(oid.decode("ascii"))
            path = _path(raw_path)
            if path in files:
                raise ValueError
            files[path] = GitObject(mode.decode("ascii"), oid.decode("ascii"))
        except ValueError:
            raise GitAnalysisError("invalid_output") from None
    return files


def parse_tree(output: bytes) -> dict[str, str]:
    return {path: entry.mode for path, entry in parse_tree_objects(output).items()}


class GitReader:
    def __init__(self, repository: Path, *, timeout_seconds: float = 5.0,
                 max_output_bytes: int = 2_097_152):
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid Git timeout")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("invalid Git output limit")
        self.repository = repository.resolve(strict=True)
        if not self.repository.is_dir():
            raise ValueError("repository must be a directory")
        self.timeout_seconds, self.max_output_bytes = timeout_seconds, max_output_bytes

    def _run(self, *arguments: str, max_output_bytes: int | None = None) -> bytes:
        limit = self.max_output_bytes if max_output_bytes is None else min(self.max_output_bytes, max_output_bytes)
        if type(limit) is not int or limit <= 0:
            raise GitAnalysisError("invalid_output_limit")
        command = [GIT_EXECUTABLE, "--no-pager", "-c", "core.hooksPath=/dev/null",
                   "-c", "core.fsmonitor=false", "-c", "protocol.allow=never", *arguments]
        environment = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
        }
        try:
            with subprocess.Popen(command, cwd=self.repository, env=environment, shell=False,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, start_new_session=True) as process:
                deadline = time.monotonic() + self.timeout_seconds
                output, size = bytearray(), 0
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        selector.register(process.stderr, selectors.EVENT_READ)
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise GitAnalysisError("timeout")
                            for key, _ in selector.select(remaining):
                                chunk = os.read(key.fd, 65_536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                size += len(chunk)
                                if size > self.max_output_bytes:
                                    raise GitAnalysisError("output_limit")
                                if key.fileobj is process.stdout:
                                    if len(output) + len(chunk) > limit:
                                        raise GitAnalysisError("output_limit")
                                    output.extend(chunk)
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        if process.poll() is None:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except PermissionError:
                                # Some sandboxes deny group signals, including
                                # after Git has exited. Popen.kill rechecks the
                                # owned child; preserve the original limit error.
                                process.kill()
                    process.wait()
                    raise
                if process.returncode != 0:
                    raise GitAnalysisError("command_error")
                return bytes(output)
        except subprocess.TimeoutExpired:
            raise GitAnalysisError("timeout") from None
        except OSError:
            raise GitAnalysisError("command_error") from None

    def changes(self, base: str, head: str) -> tuple[ChangedFile, ...]:
        base, head = validate_commit(base), validate_commit(head)
        for commit in (base, head):
            if self._run("cat-file", "-t", commit) != b"commit\n":
                raise GitAnalysisError("invalid_commit_object")
        return parse_name_status(self._run(
            "diff", "--no-ext-diff", "--no-textconv", "--no-relative", "--no-color", "--name-status", "-z",
            "--find-renames=50%", "-l1000", base, head, "--",
        ))

    def files(self, head: str) -> dict[str, str]:
        return parse_tree(self._run("ls-tree", "-r", "-z", "--full-tree", validate_commit(head)))

    def objects(self, head: str) -> dict[str, GitObject]:
        return parse_tree_objects(self._run("ls-tree", "-r", "-z", "--full-tree", validate_commit(head)))

    def blob(self, object_id: str, *, max_bytes: int) -> bytes:
        """Read an inventory-selected blob, with size and object-hash checks."""
        object_id = validate_commit(object_id)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise GitAnalysisError("invalid_output_limit")
        size_text = self._run("cat-file", "-s", object_id)
        if not re.fullmatch(rb"[0-9]{1,20}\n", size_text):
            raise GitAnalysisError("invalid_output")
        size = int(size_text)
        if size > max_bytes:
            raise GitAnalysisError("blob_limit")
        data = self._run("cat-file", "blob", object_id, max_output_bytes=max_bytes)
        digest = hashlib.sha1 if len(object_id) == 40 else hashlib.sha256
        if len(data) != size or digest(f"blob {size}\0".encode() + data).hexdigest() != object_id:
            raise GitAnalysisError("invalid_blob")
        return data

    def diff(self, base: str, head: str, paths: tuple[str, ...], *, max_bytes: int) -> bytes:
        """No pathspec magic: paths are literal data, even when they contain '*'."""
        base, head = validate_commit(base), validate_commit(head)
        for path in paths:
            validate_repository_path(path)
        if not paths:
            return b""
        return self._run(
            "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
            "--no-relative", "--unified=3", "--find-renames=50%", "-l1000", base, head,
            "--", *paths, max_output_bytes=max_bytes,
        )
