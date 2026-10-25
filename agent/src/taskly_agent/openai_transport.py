"""Opt-in official Responses transport; supported public entrypoint: openai_provider.

Budget reservation/reconciliation stays in ProviderBoundary. One process store
and a nonblocking process gate prevent independent clients from multiplying the
allowance or launching parallel paid calls. There are no hidden retries/tools.
Only OPENAI_API_KEY supplies credentials; no key is a model/config/report field.
"""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import json
import logging
import os
import re
from threading import Lock

from .budget import InMemoryBudgetStore
from .config import AgentConfig, ConfigurationError
from .contracts import ModelUsage, RunLimits
from .lifecycle import ExecutionPolicy
from .openai_protocol import DEFAULT_MODEL, PRICES, RequestBuilder, decode_json
from .provider import ProviderBoundary, ProviderError, ProviderResponse


_STORE_LOCK = Lock()
_CALL_GATE = Lock()
_PROCESS_STORE = None


class _WireLimit(ValueError):
    pass


def demo_config(*, model=DEFAULT_MODEL, budget_usd=3.0):
    """Existing trusted config, with explicit live opt-in and small request ceilings."""
    return AgentConfig(model=model, mode="live", cumulative_budget_usd=budget_usd,
        provider_timeout_seconds=30.0, execution_policy=ExecutionPolicy(
            limits=RunLimits(max_input_tokens=49_152, max_output_tokens=2000,
                max_model_calls=2, max_repair_attempts=1, estimated_budget_usd=budget_usd),
            estimated_call_ceiling_usd=0.05))


def _validate_config(config):
    config = AgentConfig.model_validate(config)
    config.require_live_budget()
    if config.model not in PRICES or config.cumulative_budget_usd > 3.0:
        raise ConfigurationError("Supported model and cumulative budget at most USD 3 required")
    rates = tuple(Decimal(rate) for rate in PRICES[config.model])
    limits = config.execution_policy.limits
    ceiling = (limits.max_input_tokens * rates[0] + limits.max_output_tokens * rates[1]) / 1_000_000
    if ceiling > Decimal(str(config.execution_policy.estimated_call_ceiling_usd)):
        raise ConfigurationError("Token ceilings exceed the trusted per-call cost reservation")
    return config


def _store(config):
    global _PROCESS_STORE
    with _STORE_LOCK:
        if _PROCESS_STORE is None:
            _PROCESS_STORE = InMemoryBudgetStore(config.cumulative_budget_usd)
        if _PROCESS_STORE.limit_usd != config.cumulative_budget_usd:
            raise ConfigurationError("Process spending ceiling cannot change or reset between sessions")
        return _PROCESS_STORE


class _SerialBoundary(ProviderBoundary):
    async def complete(self, response_kind, input_json):
        if not _CALL_GATE.acquire(blocking=False):
            raise ProviderError("failed")  # reject concurrency before spending/reserving
        try:
            return await super().complete(response_kind, input_json)
        finally:
            _CALL_GATE.release()


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid token count")
    return value


def _usage(raw, config, request_id):
    try:
        if raw["model"] != config.model:
            return None
        usage = raw["usage"]
        incoming, outgoing = _integer(usage["input_tokens"]), _integer(usage["output_tokens"])
        if _integer(usage["total_tokens"]) != incoming + outgoing:
            return None
        detail = usage.get("output_tokens_details")
        reasoning = _integer(detail["reasoning_tokens"]) if detail is not None else None
        rates = tuple(Decimal(rate) for rate in PRICES[config.model])
        cost = (incoming * rates[0] + outgoing * rates[1]) / 1_000_000
        return ModelUsage(model=config.model, request_id=request_id, input_tokens=incoming,
            output_tokens=outgoing, reasoning_tokens=reasoning, estimated_cost_usd=float(cost))
    except (ValueError, TypeError, KeyError):
        return None


def _response(raw, config, key, request_id):
    if request_id is not None and (key in request_id or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is None):
        request_id = None
    usage = _usage(raw, config, request_id)
    error, text = None, ""
    try:
        if raw.get("status") == "incomplete":
            error = "incomplete"
        elif raw.get("status") != "completed" or raw.get("error") is not None or raw.get("incomplete_details") is not None:
            error = "invalid_response"
        else:
            messages = []
            for item in raw["output"]:
                if item["type"] == "message":
                    messages.append(item)
                elif item["type"] != "reasoning" or not isinstance(item.get("id"), str) or not isinstance(item.get("summary"), list):
                    raise ValueError
            if len(messages) != 1:
                raise ValueError
            message = messages[0]
            if message["role"] != "assistant" or message["status"] != "completed" or len(message["content"]) != 1:
                raise ValueError
            content = message["content"][0]
            if content["type"] == "refusal":
                error = "refusal"
            elif content["type"] != "output_text" or content.get("annotations"):
                raise ValueError
            else:
                text = content["text"]
                if type(text) is not str or not text.strip() or key in text:
                    raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        error, text = "invalid_response", ""
    return ProviderResponse(output_json=text if error is None else "", usage=usage, request_id=request_id, error_code=error)


class OpenAITransport:
    """Internal raw transport. Use openai_provider so calls cannot bypass accounting."""
    def __init__(self, config, client, builder, key):
        self.config, self.client, self.builder, self._key = config, client, builder, key

    async def complete(self, request):
        import openai
        error, raw, request_id, cancelled = None, None, None, False
        try:
            body = self.builder.body(request)
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            if len(encoded) > self.config.max_request_bytes or self._key in encoded.decode("utf-8"):
                return ProviderResponse(output_json="", usage=None, error_code="input_limit")
            # SDK's response-id debug field is untrusted remote text. Suppress
            # this logger during our call rather than exposing headers/payloads.
            logger = logging.getLogger("openai._base_client")
            previous_logging = logger.disabled
            logger.disabled = True
            try:
                async with self.client.responses.with_streaming_response.create(
                    **body, timeout=self.config.provider_timeout_seconds,
                ) as response:
                    request_id = response.headers.get("x-request-id")
                    data = bytearray()
                    async for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 2 * self.config.max_response_bytes + 65_536:
                            raise _WireLimit("wire response limit")
                    raw = decode_json(bytes(data))
            finally:
                logger.disabled = previous_logging
        except asyncio.CancelledError:
            cancelled = True
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            error = "authentication"
        except openai.RateLimitError:
            error = "rate_limit"
        except (openai.APITimeoutError, TimeoutError):
            error = "timeout"
        except openai.APIConnectionError:
            error = "network_error"
        except openai.InternalServerError:
            error = "service_error"
        except openai.BadRequestError as exc:
            error = "unsupported_schema" if exc.code in {"invalid_json_schema", "invalid_schema", "unsupported_response_format"} else "invalid_response"
        except _WireLimit:
            error = "output_limit"
        except Exception:
            error = "invalid_response"
        if cancelled:
            raise asyncio.CancelledError() from None
        if error is not None or not isinstance(raw, dict):
            return ProviderResponse(output_json="", usage=None, error_code=error or "invalid_response")
        return _response(raw, self.config, self._key, request_id)


@asynccontextmanager
async def openai_provider(config=None, *, http_transport=None):
    """Official SDK session; injected HTTP transports are trusted test code only.

    No reset API exists for the shared in-memory ledger. Other processes and
    direct SDK use are outside this software guard, not an account billing cap.
    """
    config = _validate_config(demo_config() if config is None else config)
    # Do not let SDK ambient credentials/headers silently replace configured auth.
    if any(name in os.environ for name in ("OPENAI_ADMIN_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
        "OPENAI_WEBHOOK_SECRET", "OPENAI_CUSTOM_HEADERS", "OPENAI_LOG")):
        raise ConfigurationError("Ambient SDK overrides are unsupported")
    key = os.environ.get("OPENAI_API_KEY")
    if not key or key != key.strip() or any(not 33 <= ord(char) <= 126 for char in key):
        raise ConfigurationError("Valid OPENAI_API_KEY is required")
    import httpx2
    import openai

    class LimitedStream(httpx2.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream
        async def __aiter__(self):
            size = 0
            async for chunk in self.stream:
                size += len(chunk)
                if size > 2 * config.max_response_bytes + 65_536:
                    await self.stream.aclose()
                    raise _WireLimit("response byte limit")
                yield chunk
        async def aclose(self):
            await self.stream.aclose()

    class LimitedTransport(httpx2.AsyncBaseTransport):
        def __init__(self, inner):
            self.inner = inner
        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            response.stream = LimitedStream(response.stream)
            return response
        async def aclose(self):
            await self.inner.aclose()

    store = _store(config)
    builder = RequestBuilder(config)
    limits = httpx2.Limits(max_connections=1, max_keepalive_connections=0)
    transport = LimitedTransport(httpx2.AsyncHTTPTransport(retries=0, trust_env=False, limits=limits)
        if http_transport is None else http_transport)
    http_client = httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
        timeout=config.provider_timeout_seconds, limits=limits)
    client = openai.AsyncOpenAI(api_key=key, admin_api_key="", organization=None, project=None, webhook_secret="",
        base_url="https://api.openai.com/v1", timeout=config.provider_timeout_seconds, max_retries=0, http_client=http_client)
    try:
        yield _SerialBoundary(config, OpenAITransport(config, client, builder, key), store, builder.count_tokens)
    finally:
        failed = cancelled = False
        try:
            await client.close()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            failed = True
        if failed:
            raise ConfigurationError("OpenAI session cleanup failed")
        if cancelled:
            raise asyncio.CancelledError() from None
