"""Fixed GitHub metadata/comment boundary. No candidate programs or artifacts.

Use read-only tokens for analysis/generation and a separate write job for publish.
Credentials, remote bodies and exception messages never enter reports or logs.
"""

import json
import urllib.error
import urllib.request
from pydantic import TypeAdapter

from .contracts import Contract, Count, PRIdentity, RepositoryName
from .git_changes import validate_commit


class GitHubError(ValueError):
    def __init__(self, code="github_unavailable"):
        self.code = code
        super().__init__("GitHub operation unavailable")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHub:
    def __init__(self, repository: str, token: str):
        # Reuse the repository identity boundary, never accept a remote URL.
        TypeAdapter(RepositoryName).validate_python(repository, strict=True)
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise GitHubError()
        self.repository, self._token = repository, token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method, suffix, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request("https://api.github.com/repos/" + self.repository + suffix,
            data=data, method=method, headers={"Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json", "User-Agent": "taskly-unit-test-agent"})
        try:
            with self._opener.open(request, timeout=15) as response:
                payload = response.read(1_048_577)
                if len(payload) > 1_048_576:
                    raise ValueError
                return json.loads(payload)
        except Exception:
            pass
        raise GitHubError() from None

    def run(self, number):
        return self.request("GET", f"/actions/runs/{_positive(number)}")

    def pr(self, number):
        return self.request("GET", f"/pulls/{_positive(number)}")

    def comments(self, number):
        return self.request("GET", f"/issues/{_positive(number)}/comments?per_page=100")

    def comment(self, number, body):
        if type(body) is not str or len(body.encode()) > 16_384:
            raise GitHubError()
        return self.request("POST", f"/issues/{_positive(number)}/comments", {"body": body})


def _positive(number):
    if type(number) is not int or number <= 0:
        raise GitHubError("invalid_identity")
    return number


class PullRequestRun(Contract):
    identity: PRIdentity
    source_run_id: Count
    fork: bool
    trusted_author: bool
    draft: bool
    baseline_passed: bool
    first_attempt: bool
    source_bound: bool


def resolve_run(client: GitHub, run_id: int) -> PullRequestRun:
    """Resolve only a unique current open PR from authenticated GitHub metadata.

    Never take the PR number from an untrusted workflow artifact or infer it
    from commit-associated PRs: a later/unrelated PR can reuse the same commit.
    Missing or ambiguous fork associations fail closed without posting elsewhere.
    """
    run = client.run(run_id)
    if (run["id"] != run_id or run["event"] != "pull_request" or run["status"] != "completed"
        or run["path"] != ".github/workflows/taskly-verification.yml"
        or run["name"] != "Taskly isolated verification" or run["repository"]["full_name"] != client.repository):
        raise GitHubError("invalid_source_run")
    run_head = validate_commit(run["head_sha"])
    associations = run["pull_requests"]
    source_bound = bool(associations)
    if len(associations) != 1:
        raise GitHubError("ambiguous_pr")
    association = associations[0]
    head = validate_commit(association["head"]["sha"])
    pr = client.pr(_positive(association["number"]))
    identity = PRIdentity(repository=client.repository, number=pr["number"],
        base_sha=pr["base"]["sha"], head_sha=pr["head"]["sha"])
    if (pr["number"] != association["number"] or pr["state"] != "open" or pr["base"]["ref"] != "main"
        or pr["base"]["repo"]["full_name"] != client.repository or identity.head_sha != head
        or identity.head_sha == identity.base_sha):
        raise GitHubError("stale_commit")
    # PR workflows may identify the synthetic merge commit, while orchestration
    # must analyze the actual PR head. Accept it only when GitHub binds it to the
    # current PR; never treat an arbitrary different run SHA as that head.
    if run_head != head and run_head != pr.get("merge_commit_sha"):
        raise GitHubError("stale_commit")
    if source_bound and (association["base"]["sha"] != identity.base_sha or association["head"]["sha"] != head):
        raise GitHubError("stale_commit")
    return PullRequestRun(identity=identity, source_run_id=run_id,
        fork=pr["head"]["repo"]["full_name"] != client.repository,
        trusted_author=pr["author_association"] in {"OWNER", "MEMBER", "COLLABORATOR"},
        draft=pr["draft"], baseline_passed=run["conclusion"] == "success",
        first_attempt=run["run_attempt"] == 1, source_bound=source_bound)
