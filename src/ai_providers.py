"""AI provider abstraction for multiple LLM backends."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence
from urllib.parse import urlparse, urlunparse

import aiohttp
import httpx
from openai import AsyncOpenAI
from openai import BadRequestError as OpenAIBadRequestError

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GOOGLE_MAX_OUTPUT_TOKENS = 65_536
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _redact_url(url: str) -> str:
    """Redact credentials from a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        redacted_netloc = f"***@{parsed.hostname}"
        if parsed.port:
            redacted_netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=redacted_netloc))
    return url


@dataclass(frozen=True)
class ProviderSlotFailure:
    """Safe typed record of a single slot failure without raw error text."""

    slot: str
    kind: str
    exception_type: str


class TokenBudgetExhaustedError(RuntimeError):
    """Raised when a provider exhausts its token budget without producing visible output."""


class ProviderCascadeError(RuntimeError):  # noqa: B042
    """Raised when every provider slot in a fallback cascade fails."""

    def __init__(  # noqa: B042
        self,
        message: str,
        *,
        failure_kinds: Sequence[str] = (),
        failure_labels: Sequence[str] = (),
        slot_failures: Sequence[ProviderSlotFailure] = (),
    ):
        super().__init__(message)
        self.failure_kinds = tuple(failure_kinds)
        self.failure_labels = tuple(failure_labels)
        if slot_failures:
            self.slot_failures = tuple(slot_failures)
        else:
            self.slot_failures = tuple(
                ProviderSlotFailure(
                    slot=label,
                    kind=kind,
                    exception_type="UnknownException",
                )
                for label, kind in zip(self.failure_labels, self.failure_kinds)
            )

    @property
    def context_only(self) -> bool:
        """Whether every attempted slot rejected the request for its size."""
        return bool(self.failure_kinds) and all(
            kind == "context_size" for kind in self.failure_kinds
        )

    @property
    def has_context_size(self) -> bool:
        """Whether at least one attempted slot rejected the request for its size."""
        return "context_size" in self.failure_kinds

    @property
    def has_token_budget(self) -> bool:
        """Whether at least one attempted slot failed due to token budget."""
        return "token_budget" in self.failure_kinds

    @property
    def is_pure_outage(self) -> bool:
        """Whether all failures were purely infrastructure/auth outages with no size signals."""
        return bool(self.failure_kinds) and set(self.failure_kinds) <= {
            "auth",
            "quota",
            "server",
            "timeout",
        }

    def dominant_kind(self) -> str:
        """Return the most specific failure kind across attempted slots."""
        if not self.failure_kinds:
            return "other"
        for priority in ("token_budget", "context_size", "quota", "auth", "server", "timeout"):
            if priority in self.failure_kinds:
                return priority
        return self.failure_kinds[0]

    def diagnostic_summary(self) -> str:
        """Safe compact summary formatted as slot:kind:exception_type without raw messages."""
        return ", ".join(f"{f.slot}:{f.kind}:{f.exception_type}" for f in self.slot_failures)


def is_token_budget_error(exc: BaseException) -> bool:
    """Public semantic check for token budget exhaustion without string parsing."""
    if isinstance(exc, TokenBudgetExhaustedError):
        return True
    if isinstance(exc, ProviderCascadeError) and "token_budget" in exc.failure_kinds:
        return True
    return False


def _classify_provider_failure(exc: BaseException) -> str:
    """Classify an SDK failure without retaining provider error text."""
    text = str(exc).lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in text:
        return "timeout"
    if any(
        token in text
        for token in (
            "context_length",
            "context length",
            "context window",
            "maximum context",
            "input token limit",
            "too many tokens",
            "prompt too long",
            "request too large",
            "payload size",
        )
    ):
        return "context_size"
    if isinstance(exc, TokenBudgetExhaustedError) or any(
        token in text for token in ("token budget", "finish_reason=length", "max_tokens")
    ):
        return "token_budget"
    if any(
        token in text
        for token in ("quota", "rate limit", "rate_limit", "resource exhausted", "429")
    ):
        return "quota"
    if any(
        token in text
        for token in ("unauthorized", "authentication", "invalid api key", "401", "403")
    ):
        return "auth"
    if any(
        token in text
        for token in ("server error", "internal server", " 500", " 502", " 503", " 504")
    ):
        return "server"
    return "other"


class AIProvider(ABC):
    """Abstract base class for AI providers."""

    @abstractmethod
    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: Dict[str, Any] | None = None,
    ) -> str:
        """
        Generate a chat completion.

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Model name
            temperature: Optional sampling temperature (None uses model/provider defaults)
            max_tokens: Maximum tokens in response
            reasoning_effort: Optional reasoning effort hint passed to the API when not None.
                Supported by some providers (e.g. OpenAI). Ignored by others.
            thinking: Optional DeepSeek thinking-mode toggle.
            response_format: Optional structured-output format for compatible providers.

        Returns:
            Generated text content
        """


class ProviderCascade(AIProvider):
    """Try compatible providers in round-robin order with quota cooldown until one returns a non-empty response."""

    _global_slot_cooldowns: dict[str, float] = {}
    _global_round_robin_index: int = 0

    @classmethod
    def reset_global_state(cls) -> None:
        """Reset global cooldowns and round-robin index (useful for tests)."""
        cls._global_slot_cooldowns.clear()
        cls._global_round_robin_index = 0

    def __init__(
        self,
        providers: Sequence[tuple[str, AIProvider] | tuple[str, AIProvider, str]],
        logger: logging.Logger,
        *,
        cooldown_seconds: float = 900.0,
    ):
        self.providers = [
            (slot[0], slot[1], slot[2] if len(slot) == 3 else None) for slot in providers
        ]
        self.logger = logger
        self.cooldown_seconds = cooldown_seconds

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: Dict[str, Any] | None = None,
    ) -> str:
        if not self.providers:
            raise ProviderCascadeError("AI provider cascade has no configured slots")

        # Separate primary rotating slots (e.g. google-1..N) from fallback slots (e.g. openrouter)
        primary_slots = [
            s
            for s in self.providers
            if not s[0].lower().startswith("openrouter") and not s[0].lower().startswith("fallback")
        ]
        fallback_slots = [
            s
            for s in self.providers
            if s[0].lower().startswith("openrouter") or s[0].lower().startswith("fallback")
        ]
        if not primary_slots:
            primary_slots = list(self.providers)
            fallback_slots = []

        # Rotate primary starting slot via Global Round-Robin across all calls
        if len(primary_slots) > 1:
            start_idx = ProviderCascade._global_round_robin_index % len(primary_slots)
            ProviderCascade._global_round_robin_index += 1
            ordered_primary = primary_slots[start_idx:] + primary_slots[:start_idx]
        else:
            ordered_primary = primary_slots

        # OpenRouter / Fallback slots are strictly placed at the end (only called if all primary slots fail)
        candidates = ordered_primary + fallback_slots

        # Filter out slots in active cooldown (using global cross-component cooldown state)
        now = time.monotonic()
        available_slots: list[tuple[str, AIProvider, str | None]] = []
        cooldown_skipped: list[str] = []
        for slot in candidates:
            label = slot[0]
            cooldown_until = ProviderCascade._global_slot_cooldowns.get(label, 0.0)
            if cooldown_until > now:
                remaining = int(cooldown_until - now)
                cooldown_skipped.append(f"{label} ({remaining}s remaining)")
                continue
            available_slots.append(slot)

        if cooldown_skipped:
            self.logger.debug(
                "Skipping AI provider slots in cooldown: %s", ", ".join(cooldown_skipped)
            )

        # Fail-safe: if all slots are in cooldown, retry all candidates rather than aborting
        if not available_slots and candidates:
            self.logger.warning(
                "All AI provider slots are currently in cooldown (%s); attempting all slots as failover",
                ", ".join(cooldown_skipped),
            )
            available_slots = list(candidates)

        slot_failures: list[ProviderSlotFailure] = []
        failures: list[str] = []
        failure_kinds: list[str] = []
        failure_labels: list[str] = []
        for slot_index, (label, provider, model_override) in enumerate(available_slots):
            selected_model = model_override or model
            try:
                self.logger.info("Trying AI provider slot %s (model=%s)", label, selected_model)
                response = await provider.chat_completion(
                    messages=messages,
                    model=selected_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    thinking=thinking,
                    response_format=response_format,
                )
                if not isinstance(response, str) or not response.strip():
                    raise RuntimeError("provider returned an empty response")
                # Clear any global cooldown upon a successful response for this slot
                ProviderCascade._global_slot_cooldowns.pop(label, None)
                return response
            except Exception as exc:  # every provider error is eligible for failover
                # Do not propagate provider exception text: SDK errors can contain request
                # metadata or credentials. Slot names and exception classes are sufficient
                # for diagnostics while keeping the aggregate error safe to log.
                exc_type = type(exc).__name__
                kind = _classify_provider_failure(exc)
                if kind == "quota":
                    ProviderCascade._global_slot_cooldowns[label] = (
                        time.monotonic() + self.cooldown_seconds
                    )
                    self.logger.warning(
                        "AI provider slot %s quota exceeded (429); placed in global cooldown for %ds",
                        label,
                        int(self.cooldown_seconds),
                    )
                slot_failures.append(
                    ProviderSlotFailure(slot=label, kind=kind, exception_type=exc_type)
                )
                failures.append(f"{label} ({exc_type})")
                failure_labels.append(label)
                failure_kinds.append(kind)
                if slot_index < len(available_slots) - 1:
                    self.logger.warning("AI provider slot %s failed; switching to next slot", label)
                else:
                    self.logger.warning("AI provider slot %s failed; no slots remain", label)

        if not failures:
            raise ProviderCascadeError("AI provider cascade has no configured slots")
        raise ProviderCascadeError(
            "All AI provider slots failed: " + "; ".join(failures),
            failure_kinds=failure_kinds,
            failure_labels=failure_labels,
            slot_failures=slot_failures,
        )


def _extract_chat_completion_text(response: Any, logger: logging.Logger, provider: str) -> str:
    """Extract visible text from an OpenAI-compatible chat response."""
    if not response.choices:
        raise RuntimeError(f"{provider} returned no choices in response")

    choice = response.choices[0]
    finish_reason = choice.finish_reason
    refusal = getattr(choice.message, "refusal", None)
    usage = response.usage

    logger.debug(
        "%s response: finish_reason=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
        provider,
        finish_reason,
        usage.prompt_tokens if usage else None,
        usage.completion_tokens if usage else None,
        usage.total_tokens if usage else None,
    )

    if refusal:
        logger.warning("%s model refusal: %s", provider, refusal)

    content = choice.message.content
    text = content.strip() if content else ""
    if not text:
        if finish_reason == "length":
            raise TokenBudgetExhaustedError(
                f"{provider} returned empty content with finish_reason=length — the model "
                f"exhausted its token budget (possibly in reasoning) without producing output. "
                f"Consider increasing max_tokens_per_summary in config.yaml. "
                f"(prompt_tokens={usage.prompt_tokens if usage else 'N/A'}, "
                f"completion_tokens={usage.completion_tokens if usage else 'N/A'})"
            )
        raise RuntimeError(
            f"{provider} returned empty content "
            f"(finish_reason={finish_reason}, "
            f"prompt_tokens={usage.prompt_tokens if usage else 'N/A'}, "
            f"completion_tokens={usage.completion_tokens if usage else 'N/A'})"
        )
    if finish_reason == "length":
        logger.warning(
            "%s response was truncated (finish_reason=length); returning partial content. "
            "Consider increasing max_tokens_per_summary in config.yaml.",
            provider,
        )
    return text


class OpenAIProvider(AIProvider):
    """OpenAI API provider."""

    def __init__(
        self,
        api_key: str,
        logger: logging.Logger,
        timeout: int = 60,
        base_url: str = "",
    ):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
            max_retries=0,
        )
        self.logger = logger
        self.base_url = base_url.lower()

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: Dict[str, Any] | None = None,
    ) -> str:
        is_deepseek = "deepseek" in self.base_url or model.startswith("deepseek-")
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            ("max_tokens" if is_deepseek else "max_completion_tokens"): max_tokens,
        }
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        if reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = reasoning_effort
        if is_deepseek and thinking is not None:
            create_kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if thinking else "disabled"}
            }
        if response_format is not None:
            create_kwargs["response_format"] = response_format

        try:
            response = await self.client.chat.completions.create(**create_kwargs)
        except OpenAIBadRequestError as exc:
            response = await self._handle_bad_request(create_kwargs, exc, reasoning_effort)

        return _extract_chat_completion_text(response, self.logger, "OpenAI")

    async def _handle_bad_request(
        self,
        create_kwargs: Dict[str, Any],
        original_exc: OpenAIBadRequestError,
        reasoning_effort: str | None,
    ):
        """Handle a BadRequestError by retrying with stripped parameters."""
        if reasoning_effort is not None:
            self.logger.debug(
                "reasoning_effort=%r rejected by model, retrying without it: %s",
                reasoning_effort,
                original_exc,
            )
            create_kwargs.pop("reasoning_effort")
            try:
                return await self.client.chat.completions.create(**create_kwargs)
            except OpenAIBadRequestError as exc2:
                self.logger.debug("retry without reasoning_effort also rejected: %s", exc2)
                # fall through to max_tokens fallback
        if "max_completion_tokens" in create_kwargs:
            self.logger.debug(
                "max_completion_tokens rejected by model, retrying with max_tokens: %s",
                original_exc,
            )
            create_kwargs["max_tokens"] = create_kwargs.pop("max_completion_tokens")
            return await self.client.chat.completions.create(**create_kwargs)
        raise original_exc


class GoogleProvider(AIProvider):
    """Google Gemini provider through Google's OpenAI-compatible endpoint."""

    def __init__(
        self,
        api_key: str,
        logger: logging.Logger,
        timeout: int = 45,
        default_reasoning_effort: str = "high",
    ):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=GOOGLE_BASE_URL,
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
            max_retries=0,
        )
        self.logger = logger
        self.default_reasoning_effort = default_reasoning_effort

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float | None = None,  # noqa: ARG002 — Gemini 3 uses model defaults
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,  # noqa: ARG002 — accepted, reasoning_effort used for Gemini
        response_format: Dict[str, Any] | None = None,
    ) -> str:
        """Generate text with Gemini-compatible Chat Completions parameters."""
        output_tokens = min(max_tokens, GOOGLE_MAX_OUTPUT_TOKENS)
        if max_tokens > GOOGLE_MAX_OUTPUT_TOKENS:
            self.logger.debug(
                "Capping Google output budget from %s to Gemini limit %s",
                max_tokens,
                GOOGLE_MAX_OUTPUT_TOKENS,
            )

        effort = reasoning_effort if reasoning_effort is not None else self.default_reasoning_effort
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": output_tokens,
            "reasoning_effort": effort,
        }
        if response_format is not None:
            create_kwargs["response_format"] = response_format

        response = await self.client.chat.completions.create(**create_kwargs)
        return _extract_chat_completion_text(response, self.logger, "Google Gemini")


class OllamaProvider(AIProvider):
    """Ollama local LLM provider."""

    def __init__(self, base_url: str, logger: logging.Logger, timeout: int = 300):
        self.base_url = base_url.rstrip("/")
        self.logger = logger
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,  # noqa: ARG002 — accepted, not used by Ollama
        thinking: bool | None = None,  # noqa: ARG002 — accepted, not used by Ollama
        response_format: (Dict[str, Any] | None) = None,  # noqa: ARG002 — accepted, not used by Ollama
    ) -> str:
        url = f"{self.base_url}/api/chat"
        options: Dict[str, Any] = {"num_predict": max_tokens}
        if temperature is not None:
            options["temperature"] = temperature
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }

        self.logger.debug(
            "Ollama request: url=%s model=%s timeout=%s",
            _redact_url(url),
            model,
            self.timeout.total,
        )

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Ollama API error {resp.status}: {body[:200]}")
                data = await resp.json(content_type=None)
                self.logger.debug(
                    "Ollama response: status=%s content_length=%s",
                    resp.status,
                    resp.content_length,
                )

        resp_model = data.get("model", model)
        eval_count = data.get("eval_count")
        prompt_eval_count = data.get("prompt_eval_count")
        done_reason = data.get("done_reason")
        self.logger.debug(
            "Ollama metadata: model=%s eval_count=%s prompt_eval_count=%s done_reason=%s",
            resp_model,
            eval_count,
            prompt_eval_count,
            done_reason,
        )

        content: str = data.get("message", {}).get("content", "")
        text = content.strip()
        if not text:
            if done_reason == "length":
                raise TokenBudgetExhaustedError(
                    f"Ollama returned empty content with done_reason=length — the model "
                    f"exhausted its token budget without producing output. "
                    f"Consider increasing max_tokens_per_summary in config.yaml. "
                    f"(model={resp_model}, eval_count={eval_count}, "
                    f"prompt_eval_count={prompt_eval_count})"
                )
            raise RuntimeError(
                f"Ollama returned empty content "
                f"(model={resp_model}, eval_count={eval_count}, "
                f"prompt_eval_count={prompt_eval_count})"
            )
        if done_reason == "length":
            self.logger.warning(
                "Ollama response was truncated (done_reason=length); returning partial content. "
                "Consider increasing max_tokens_per_summary in config.yaml."
            )
        return text


class AnthropicProvider(AIProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str, logger: logging.Logger, timeout: int = 60):
        self._api_key = api_key
        self.logger = logger
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,  # noqa: ARG002 — accepted, not used by Anthropic
        thinking: bool | None = None,  # noqa: ARG002 — accepted, not used by Anthropic
        response_format: (Dict[str, Any] | None) = None,  # noqa: ARG002 — accepted, not used by Anthropic
    ) -> str:
        # Extract system message and user messages
        system_text = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                api_messages.append(msg)

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if system_text:
            payload["system"] = system_text
        if temperature is not None:
            payload["temperature"] = temperature

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Anthropic API error {resp.status}: {body[:200]}")
                data = await resp.json(content_type=None)

        stop_reason = data.get("stop_reason")
        usage = data.get("usage") or {}
        self.logger.debug(
            "Anthropic response: stop_reason=%s input_tokens=%s output_tokens=%s",
            stop_reason,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )

        content_blocks = data.get("content") or []
        texts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
        text = "\n".join(texts).strip()
        if not text:
            if stop_reason == "max_tokens":
                raise TokenBudgetExhaustedError(
                    f"Anthropic returned empty content with stop_reason=max_tokens — the model "
                    f"exhausted its token budget without producing output. "
                    f"Consider increasing max_tokens_per_summary in config.yaml. "
                    f"(input_tokens={usage.get('input_tokens', 'N/A')}, "
                    f"output_tokens={usage.get('output_tokens', 'N/A')})"
                )
            raise RuntimeError(
                f"Anthropic returned empty content "
                f"(stop_reason={stop_reason}, "
                f"input_tokens={usage.get('input_tokens', 'N/A')}, "
                f"output_tokens={usage.get('output_tokens', 'N/A')})"
            )
        if stop_reason == "max_tokens":
            self.logger.warning(
                "Anthropic response was truncated (stop_reason=max_tokens); returning partial content. "
                "Consider increasing max_tokens_per_summary in config.yaml."
            )
        return text


def create_provider(  # noqa: C901
    provider_name: str,
    logger: logging.Logger,
    *,
    openai_api_key: str = "",
    openai_base_url: str = "",
    anthropic_api_key: str = "",
    google_api_key: str = "",
    google_api_keys: list[str] | tuple[str, ...] | None = None,
    openrouter_api_key: str = "",
    openrouter_base_url: str = OPENROUTER_BASE_URL,
    openrouter_model: str = "openrouter/free",
    ollama_base_url: str = "http://localhost:11434",
    api_timeout: int = 60,
    reasoning_effort: str | None = None,
) -> AIProvider:
    """
    Factory function to create an AI provider.

    Args:
        provider_name: One of 'openai', 'ollama', 'anthropic', 'google'
        logger: Logger instance
        openai_api_key: OpenAI API key (required for 'openai' provider)
        anthropic_api_key: Anthropic API key (required for 'anthropic' provider)
        google_api_key: Gemini API key (required for 'google' provider)
        google_api_keys: Optional additional Gemini keys used as failover slots
        openrouter_api_key: Optional OpenRouter key used as the final failover slot
        openrouter_base_url: OpenRouter-compatible API base URL
        openrouter_model: Model used by the OpenRouter failover slot
        ollama_base_url: Ollama server URL (for 'ollama' provider)
        api_timeout: HTTP request timeout in seconds
        reasoning_effort: Optional reasoning effort hint for models supporting it

    Returns:
        AIProvider instance

    Raises:
        ValueError: If provider_name is unknown or required keys are missing
    """
    name = provider_name.lower()

    if name == "openai":
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAI provider")
        return OpenAIProvider(
            api_key=openai_api_key,
            logger=logger,
            timeout=api_timeout,
            base_url=openai_base_url,
        )

    if name == "ollama":
        ollama_timeout = max(api_timeout, 120)
        return OllamaProvider(base_url=ollama_base_url, logger=logger, timeout=ollama_timeout)

    if name == "anthropic":
        if not anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for Anthropic provider")
        return AnthropicProvider(api_key=anthropic_api_key, logger=logger, timeout=api_timeout)

    if name == "google":
        keys: list[str] = []
        for key in [google_api_key, *(google_api_keys or [])]:
            if key and key not in keys:
                keys.append(key)
        if not google_api_key:
            raise ValueError("GEMINI_API_KEY is required as the primary Google provider key")

        google_timeout = min(api_timeout, 45)
        slots: list[tuple[str, AIProvider, str]] = [
            (
                f"google-{index}",
                GoogleProvider(
                    api_key=key,
                    logger=logger,
                    timeout=google_timeout,
                    default_reasoning_effort=reasoning_effort or "high",
                ),
                "",
            )
            for index, key in enumerate(keys, start=1)
        ]
        if openrouter_api_key:
            slots.append(
                (
                    "openrouter",
                    OpenAIProvider(
                        api_key=openrouter_api_key,
                        logger=logger,
                        timeout=api_timeout,
                        base_url=openrouter_base_url,
                    ),
                    openrouter_model,
                )
            )
        if len(slots) == 1:
            return slots[0][1]
        return ProviderCascade(slots, logger)

    if name == "openrouter":
        if not openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouter provider")
        return OpenAIProvider(
            api_key=openrouter_api_key,
            logger=logger,
            timeout=api_timeout,
            base_url=openrouter_base_url,
        )

    raise ValueError(
        f"Unknown AI provider: '{provider_name}'. "
        f"Supported providers: openai, ollama, anthropic, google, openrouter"
    )


def ensure_provider_cascade(
    provider: AIProvider,
    *,
    logger: logging.Logger,
    slot_name: str = "primary",
) -> ProviderCascade:
    """Ensure the provider exhibits uniform ProviderCascade error semantics for editorial callers."""
    if isinstance(provider, ProviderCascade):
        return provider
    return ProviderCascade([(slot_name, provider)], logger)
