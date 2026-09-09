"""The model boundary.

A deliberately narrow interface with exactly one real adapter — the Anthropic
Python SDK. This is not a multi-provider framework; it exists so the controller
can be driven by scripted responses in tests without touching the network.

Token counting lives behind the same interface, so context accounting is measured
by the provider in production and stubbed in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from app.config import Settings


class ModelError(Exception):
    """A provider failure the controller records against the attempt."""

    kind = "model_error"


class ModelNotConfigured(ModelError):
    kind = "model_not_configured"


class ModelAuthError(ModelError):
    kind = "model_auth_error"


class ModelRateLimited(ModelError):
    kind = "model_rate_limited"


class ModelTimeout(ModelError):
    kind = "model_timeout"


class ModelRequestFailed(ModelError):
    kind = "model_request_failed"


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    """One normalized assistant turn.

    `assistant_content` is the raw provider content, ready to be appended to the
    message history unchanged — thinking blocks included, which must be echoed
    back verbatim. The controller makes its decisions from the normalized fields.
    """

    stop_reason: str | None
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_content: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelClient(Protocol):
    """What the controller needs from a model provider."""

    @property
    def model(self) -> str: ...

    def count_input_tokens(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        """Tokens the given request would consume, measured by the provider."""
        ...

    def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ModelTurn: ...


class AnthropicModelClient:
    """Adapter over the official Anthropic Python SDK.

    Implicit retries are disabled: a transparent retry would silently re-spend an
    attempt's budget and duplicate tool work. The timeout is per HTTP request and
    is **not** a deadline for the whole attempt — turn and tool-call limits bound
    that.
    """

    def __init__(self, config: Settings) -> None:
        if config.anthropic_api_key is None:
            raise ModelNotConfigured(
                "ANTHROPIC_API_KEY is not set. Export it (or add it to backend/.env) "
                "before running the propose worker."
            )
        if not config.anthropic_model:
            raise ModelNotConfigured(
                "ANTHROPIC_MODEL is empty. Set it to a model identifier you have access to."
            )

        self._model = config.anthropic_model
        self._client = anthropic.Anthropic(
            api_key=config.anthropic_api_key.get_secret_value(),
            timeout=config.agent_request_timeout_seconds,
            max_retries=0,
        )

    @property
    def model(self) -> str:
        return self._model

    def count_input_tokens(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        try:
            counted = self._client.messages.count_tokens(
                model=self._model, system=system, messages=messages, tools=tools
            )
        except Exception as exc:  # noqa: BLE001 - mapped below
            raise _map_error(exc, "Counting request tokens") from exc
        return int(counted.input_tokens)

    def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ModelTurn:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - mapped below
            raise _map_error(exc, "Model request") from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # Tool input is already parsed JSON; never string-matched.
                raw = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=raw))

        usage = getattr(response, "usage", None)
        return ModelTurn(
            stop_reason=response.stop_reason,
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            # Echoed back verbatim on the next turn.
            assistant_content=[block.model_dump() for block in response.content],
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )


def _map_error(exc: Exception, what: str) -> ModelError:
    """Map SDK exceptions to our kinds, most specific first.

    Messages never include request payloads or credentials.
    """
    if isinstance(exc, anthropic.AuthenticationError):
        return ModelAuthError(
            f"{what} was rejected: the API key is invalid or lacks access. "
            f"Check ANTHROPIC_API_KEY."
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ModelAuthError(
            f"{what} was refused: this key lacks permission for the configured model."
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ModelAuthError(
            f"{what} failed: the configured model was not found or is not available "
            f"to this account."
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ModelRateLimited(
            f"{what} was rate limited by the provider. No automatic retry is attempted."
        )
    if isinstance(exc, anthropic.APITimeoutError):
        return ModelTimeout(
            f"{what} timed out. Raise AGENT_REQUEST_TIMEOUT_SECONDS if this recurs."
        )
    if isinstance(exc, anthropic.APIStatusError):
        return ModelRequestFailed(f"{what} failed with HTTP {exc.status_code}.")
    if isinstance(exc, anthropic.APIConnectionError):
        return ModelRequestFailed(f"{what} could not reach the provider.")
    if isinstance(exc, ModelError):
        return exc
    return ModelRequestFailed(f"{what} failed: {type(exc).__name__}.")
