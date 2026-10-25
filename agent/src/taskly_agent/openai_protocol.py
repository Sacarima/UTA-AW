"""Pure Responses wire preparation; no credentials, network or budget mutation.

Transport DTOs use only the portable strict-schema subset. Application models
still enforce content limits, paths and all semantic invariants in ProviderBoundary.
Trust comes from matching local task/skill/config bytes, not a JSON field label.
"""

import json
from typing import Literal

from .adapters import _COMMON, _GENERATE, _REPAIR, _REVIEW
from .config import AgentConfig
from .contracts import Contract, ReviewResult, TestProposal
from .provider import ProviderRequest
from .skills import load_bundled_skills


DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
# Standard text-token USD / million; full input rate deliberately ignores cache discounts.
# Official model pages checked 2026-10-25. Prices/account availability need review before live use.
PRICES = {
    DEFAULT_MODEL: ("0.75", "4.50"),
    "gpt-5.4-nano-2026-03-17": ("0.20", "1.25"),
}


class _Change(Contract):
    path: str
    operation: Literal["create", "modify"]
    content: str


class _Proposal(Contract):
    rationale: str
    changes: list[_Change]


class _Finding(Contract):
    severity: Literal["info", "warning", "error"]
    message: str
    path: str | None
    line: int | None


class _Review(Contract):
    findings: list[_Finding]


def transport_schema(kind):
    if kind not in {"test_proposal", "review"}:
        raise ValueError("unsupported response kind")
    return (_Proposal if kind == "test_proposal" else _Review).model_json_schema()


def trusted_catalog():
    return {skill.name: skill.model_dump(mode="json") for skill in load_bundled_skills()}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_json(value):
    def invalid_constant(value):
        raise ValueError("invalid JSON number")
    return json.loads(value, object_pairs_hook=_object, parse_constant=invalid_constant)


class RequestBuilder:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.catalog = trusted_catalog()  # existing trusted loader, cached for this session

    def body(self, request: ProviderRequest):
        if (request.model, request.max_input_tokens, request.max_output_tokens) != (
            self.config.model, self.config.execution_policy.limits.max_input_tokens,
            self.config.execution_policy.limits.max_output_tokens,
        ):
            raise ValueError("request/config mismatch")
        envelope = decode_json(request.input_json)
        if set(envelope) != {"trusted_instructions", "untrusted_reference"}:
            raise ValueError("adapter envelope required")
        trusted = envelope["trusted_instructions"]
        if set(trusted) != {"task", "skills", "response_schema", "content_limits"}:
            raise ValueError("invalid trusted envelope")
        generation = request.response_kind == "test_proposal"
        tasks = (_COMMON + " " + _GENERATE, _COMMON + " " + _GENERATE + " " + _REPAIR) if generation else (_COMMON + " " + _REVIEW,)
        schema = TestProposal if generation else ReviewResult
        if trusted["task"] not in tasks or trusted["response_schema"] != schema.model_json_schema() or (
            trusted["content_limits"] != self.config.execution_policy.content_limits.model_dump()
        ):
            raise ValueError("untrusted instructions or configuration")
        names = [skill["name"] for skill in trusted["skills"]]
        if not names or names != [name for name in self.catalog if name in names] or any(
            skill != self.catalog.get(skill["name"]) for skill in trusted["skills"]
        ) or (not generation and names != ["review-generated-tests"]):
            raise ValueError("untrusted skill content/order")
        if generation and ("review-generated-tests" not in names or not {"python-unit-tests", "react-unit-tests"}.intersection(names)):
            raise ValueError("generation skill missing")
        developer = {"task": trusted["task"], "skills": [self.catalog[name] for name in names],
            "content_limits": self.config.execution_policy.content_limits.model_dump(),
            "response_schema": transport_schema(request.response_kind)}
        body = {"model": request.model, "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": json.dumps(developer, ensure_ascii=False)}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(
                {"untrusted_reference": envelope["untrusted_reference"]}, ensure_ascii=False)}]},
        ], "text": {"format": {"type": "json_schema", "name": request.response_kind,
            "strict": True, "schema": transport_schema(request.response_kind)}},
            "max_output_tokens": request.max_output_tokens, "store": False, "background": False,
            "tools": [], "tool_choice": "none", "parallel_tool_calls": False, "stream": False,
            "reasoning": {"effort": "none"}, "truncation": "disabled", "service_tier": "default"}
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > self.config.max_request_bytes:
            raise ValueError("complete API request exceeds byte budget")
        return body

    def count_tokens(self, request: ProviderRequest) -> int:
        """Conservative byte upper bound, not a paid tokenizer endpoint.

        UTF-8 byte counts cover text/schema tokens; allowance covers provider
        framing. Final actual usage is still checked/charged. The collector's
        intermediate envelope gets an explicit overhead allowance; the final
        adapter request is rebuilt and counted in full at ProviderBoundary.
        """
        payload = decode_json(request.input_json)
        if isinstance(payload, dict) and set(payload) == {"repository_data", "trusted_skills"}:
            return len(request.input_json.encode("utf-8")) + 8192
        return len(json.dumps(self.body(request), ensure_ascii=False).encode("utf-8")) + 1024


def smoke_payload(config: AgentConfig):
    """Harmless synthetic review: no repository reads, proposal application or evidence."""
    return json.dumps({"trusted_instructions": {"task": _COMMON + " " + _REVIEW,
        "skills": [trusted_catalog()["review-generated-tests"]],
        "response_schema": ReviewResult.model_json_schema(),
        "content_limits": config.execution_policy.content_limits.model_dump()},
        "untrusted_reference": {"smoke_only": True, "proposal": {
            "rationale": "Transport smoke review only", "changes": [{"path": "backend/tests/test_transport_smoke.py",
            "operation": "create", "content": "def test_addition():\n    assert 1 + 1 == 2\n"}]}}})
