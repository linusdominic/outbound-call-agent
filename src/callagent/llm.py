"""The model client: streaming tool-use turns with a spend ledger, a budget cap and a cache.

Every call is priced from the token usage the API reports and added to a ledger on disk.
Once the ledger reaches the budget, further calls are refused. The cache is switched on for
evaluation runs so that re-running them is free and repeatable.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# USD per million tokens (input, output). Anthropic list prices as of 2026-11-03.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

TRANSIENT = (
    TimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)

OnText = Callable[[str], Awaitable[None]]


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would go past the spending cap."""


@dataclass(frozen=True)
class Turn:
    """One model response: its content blocks, ready to append to the conversation."""

    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    cached: bool = False

    @property
    def text(self) -> str:
        return "".join(block["text"] for block in self.content if block["type"] == "text")

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [block for block in self.content if block["type"] == "tool_use"]


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    try:
        input_rate, output_rate = PRICES[model]
    except KeyError:
        raise ValueError(f"No price configured for model {model!r}") from None
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


class Ledger:
    """Running total of money spent, kept on disk so the cap holds across runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = (
            json.loads(path.read_text())
            if path.exists()
            else {"total_usd": 0.0, "calls": 0, "by_model": {}}
        )

    @property
    def total(self) -> float:
        return self.data["total_usd"]

    def add(self, model: str, turn: Turn) -> None:
        entry = self.data["by_model"].setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0}
        )
        entry["calls"] += 1
        entry["input_tokens"] += turn.input_tokens
        entry["output_tokens"] += turn.output_tokens
        entry["usd"] = round(entry["usd"] + turn.cost_usd, 6)
        self.data["calls"] += 1
        self.data["total_usd"] = round(self.data["total_usd"] + turn.cost_usd, 6)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


def _block(block: Any) -> dict[str, Any]:
    """An SDK content block as a plain dict that the API accepts back unchanged."""
    data = block.model_dump(exclude_none=True)
    if data["type"] == "text":
        return {"type": "text", "text": data["text"]}
    if data["type"] == "tool_use":
        return {k: data[k] for k in ("type", "id", "name", "input")}
    return data  # thinking blocks go back exactly as they came


class LLM:
    def __init__(
        self,
        client: Any = None,
        ledger_path: Path = Path("results/spend.json"),
        budget_usd: float = 5.0,
        cache_dir: Path | None = None,
        max_concurrency: int = 6,
        timeout_s: float = 90.0,
        retries: int = 3,
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic(max_retries=0)
        self.ledger = Ledger(ledger_path)
        self.budget_usd = budget_usd
        self._cache_dir = cache_dir
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout_s = timeout_s
        self._retries = retries

    async def _stream(self, request: dict[str, Any], on_text: OnText | None) -> Any:
        async with self._client.messages.stream(**request) as stream:
            async for text in stream.text_stream:
                if on_text:
                    await on_text(text)
            return await stream.get_final_message()

    async def _send(self, request: dict[str, Any], on_text: OnText | None) -> tuple[Any, float]:
        """Send with a hard timeout. Retry only while nothing has been shown to the user."""
        shown = False

        async def forward(text: str) -> None:
            nonlocal shown
            shown = True
            if on_text:
                await on_text(text)

        for attempt in range(self._retries + 1):
            started = time.perf_counter()
            try:
                message = await asyncio.wait_for(
                    self._stream(request, forward), timeout=self._timeout_s
                )
                return message, time.perf_counter() - started
            except TRANSIENT as error:
                logger.warning("%s attempt %d: %s", request["model"], attempt + 1, type(error))
                if shown or attempt == self._retries:
                    raise
                await asyncio.sleep(min(2**attempt, 20))
        raise AssertionError("unreachable")

    async def respond(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 4000,
        effort: str = "low",
        on_text: OnText | None = None,
    ) -> Turn:
        """One model response. With ``schema`` the reply is JSON that matches it."""
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort},
        }
        if tools:
            request["tools"] = tools
        if schema is not None:
            request["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        cache_path = None
        if self._cache_dir is not None:
            digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            cache_path = self._cache_dir / f"{digest}.json"
            if cache_path.exists():
                turn = Turn(**{**json.loads(cache_path.read_text()), "cached": True})
                if on_text and turn.text:
                    await on_text(turn.text)
                return turn

        async with self._semaphore:
            if self.ledger.total >= self.budget_usd:
                raise BudgetExceeded(
                    f"Spent ${self.ledger.total:.2f} of the ${self.budget_usd:.2f} budget."
                )
            message, latency = await self._send(request, on_text)

        usage = message.usage
        turn = Turn(
            content=[_block(block) for block in message.content],
            stop_reason=message.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=price(model, usage.input_tokens, usage.output_tokens),
            latency_s=latency,
        )
        self.ledger.add(model, turn)
        if cache_path is not None and turn.stop_reason in ("end_turn", "tool_use"):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({k: v for k, v in asdict(turn).items() if k != "cached"})
            )
        return turn
