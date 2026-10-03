import asyncio
import json
import time
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from src.policy.safety import UnsafeContentError, sanitize_text

T = TypeVar("T", bound=BaseModel)

class ModelGateway:
    """One per request, sharing a token ledger across planning, decisions and writing."""
    def __init__(self, settings):
        self.settings = settings
        self.remaining = settings.model_total_tokens
        self.deadline = time.monotonic() + settings.request_timeout
        self.lock = asyncio.Lock()

    async def structured(self, instruction: str, payload: dict, schema: type[T]) -> T:
        settings = self.settings
        if settings.model_mode != "openai" or not settings.approved_model_processing:
            raise UnsafeContentError("model_processing_not_enabled")
        if self.deadline <= time.monotonic():
            raise TimeoutError("model_deadline_expired")
        text = json.dumps(payload, ensure_ascii=False)
        # Check again at the provider boundary. Unscreened input is not silently modified here.
        if sanitize_text(text) != text or len(text) > settings.model_max_input_chars:
            raise UnsafeContentError("model_payload_rejected")
        # Conservative UTF-8 byte reservation includes structured schema and protocol framing.
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        reserve = len((instruction + text + schema_text).encode("utf-8")) + 2048 + settings.model_max_output_tokens
        async with self.lock:
            if reserve > self.remaining:
                raise UnsafeContentError("model_budget_exhausted")
            self.remaining -= reserve
        timeout = min(15, self.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("model_deadline_expired")
        async with AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=0,
                               timeout=timeout) as client:
            response = await client.responses.parse(
                model=settings.openai_model,
                input=[{"role": "system", "content": instruction}, {"role": "user", "content": text}],
                text_format=schema, max_output_tokens=settings.model_max_output_tokens, store=False,
            )
        if response.output_parsed is None:
            raise UnsafeContentError("model_response_unavailable")
        return response.output_parsed

