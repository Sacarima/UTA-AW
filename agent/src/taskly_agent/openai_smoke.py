"""Dry-run by default. Exactly one synthetic review with --allow-paid; no files written."""

import argparse
import asyncio
import sys

from .contracts import RunLimits
from .lifecycle import ExecutionPolicy
from .openai_protocol import DEFAULT_MODEL, RequestBuilder, smoke_payload
from .openai_transport import demo_config, openai_provider
from .provider import ProviderRequest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run or explicitly opt in to one Responses API smoke request")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    arguments = parser.parse_args(argv)
    try:
        config = demo_config(model=arguments.model)
        config = type(config).model_validate({**config.model_dump(), "execution_policy": ExecutionPolicy(
            limits=RunLimits(max_model_calls=1, max_repair_attempts=0, max_input_tokens=49_152,
                max_output_tokens=512, estimated_budget_usd=0.05), estimated_call_ceiling_usd=0.05)})
        builder = RequestBuilder(config)
        payload = smoke_payload(config)
        request = ProviderRequest(model=config.model, response_kind="review", input_json=payload,
            max_input_tokens=config.execution_policy.limits.max_input_tokens,
            max_output_tokens=config.execution_policy.limits.max_output_tokens)
        count = builder.count_tokens(request)
        if count > request.max_input_tokens:
            raise ValueError
        if not arguments.allow_paid:
            from .openai_transport import _validate_config
            _validate_config(config)
            print(f"Dry-run valid: model={config.model}, conservative_input_tokens={count}, max_output_tokens=512; no API call")
            return 0

        async def paid():
            async with openai_provider(config) as boundary:
                return await boundary.complete("review", payload)
        result = asyncio.run(paid())  # one call, no repair/review loop, no source collection
        print(f"Smoke completed: input_tokens={result.usage.input_tokens}, output_tokens={result.usage.output_tokens}, estimated_usd={result.usage.estimated_cost_usd:.6f}")
        return 0
    except Exception:
        print("Smoke failed or unavailable; output and exception details suppressed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
