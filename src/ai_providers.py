"""AI provider abstraction for multiple LLM backends."""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import aiohttp
import httpx
from openai import AsyncOpenAI
from openai import BadRequestError as OpenAIBadRequestError

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GOOGLE_MAX_OUTPUT_TOKENS = 65_536


def _redact_url(url: str) -> str:
    """Redact credentials from a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        redacted_netloc = f"***@{parsed.hostname}"
        if parsed.port:
            redacted_netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=redacted_netloc))
    return url


class TokenBudgetExhaustedError(RuntimeError):
    """Raised when a provider exhausts its token budget without producing visible output."""


class AIProvider(ABC):
    """Abstract base class for AI providers."""

    @abstractmethod
    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: Dict[str, Any] | None = None,
    ) -> str:
        """
        Generate a chat completion.

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Model name
            temperature: Sampling temperature
            max_tokens: Maximum tokens in response
            reasoning_effort: Optional reasoning effort hint passed to the API when not None.
                Supported by some providers (e.g. OpenAI). Ignored by others.
            thinking: Optional DeepSeek thinking-mode toggle.
            response_format: Optional structured-output format for compatible providers.

        Returns:
            Generated text content
        """


def _extract_chat_completion_text(response: Any, logger: logging.Logger, provider: str) -> str:
    """Extract visible text from an OpenAI-compatible chat response."""
    if not response.choices:
        raise RuntimeError(f"{provider} returned no choices in response")

    choice = response.choices[0]
    finish_reason = choice.finish_reason
    refusal = getattr(choice.message, "refusal", None)
    usage = response.usage

    logger.debug(
        "%s response: finish_reason=%s prompt_tokens=%s " "completion_tokens=%s total_tokens=%s",
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
        client_kwargs = {
            "api_key": api_key,
            "timeout": httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**client_kwargs)
        self.logger = logger
        self.base_url = base_url.lower()

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: Dict[str, Any] | None = None,
    ) -> str:
        is_deepseek = "deepseek" in self.base_url or model.startswith("deepseek-")
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            ("max_tokens" if is_deepseek else "max_completion_tokens"): max_tokens,
        }
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

    def __init__(self, api_key: str, logger: logging.Logger, timeout: int = 60):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=GOOGLE_BASE_URL,
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
        )
        self.logger = logger

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,  # noqa: ARG002 — Gemini 3 uses model defaults
        max_tokens: int,
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

        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": output_tokens,
            "reasoning_effort": reasoning_effort if reasoning_effort is not None else "high",
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
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,  # noqa: ARG002 — accepted, not used by Ollama
        thinking: bool | None = None,  # noqa: ARG002 — accepted, not used by Ollama
        response_format: (
            Dict[str, Any] | None
        ) = None,  # noqa: ARG002 — accepted, not used by Ollama
    ) -> str:
        url = f"{self.base_url}/api/chat"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
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

        resp_model = data.get("model", "unknown")
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
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,  # noqa: ARG002 — accepted, not used by Anthropic
        thinking: bool | None = None,  # noqa: ARG002 — accepted, not used by Anthropic
        response_format: (
            Dict[str, Any] | None
        ) = None,  # noqa: ARG002 — accepted, not used by Anthropic
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


def create_provider(
    provider_name: str,
    logger: logging.Logger,
    *,
    openai_api_key: str = "",
    openai_base_url: str = "",
    anthropic_api_key: str = "",
    google_api_key: str = "",
    ollama_base_url: str = "http://localhost:11434",
    api_timeout: int = 60,
) -> AIProvider:
    """
    Factory function to create an AI provider.

    Args:
        provider_name: One of 'openai', 'ollama', 'anthropic', 'google'
        logger: Logger instance
        openai_api_key: OpenAI API key (required for 'openai' provider)
        anthropic_api_key: Anthropic API key (required for 'anthropic' provider)
        google_api_key: Gemini API key (required for 'google' provider)
        ollama_base_url: Ollama server URL (for 'ollama' provider)
        api_timeout: HTTP request timeout in seconds

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
        if not google_api_key:
            raise ValueError("GEMINI_API_KEY is required for Google provider")
        return GoogleProvider(api_key=google_api_key, logger=logger, timeout=api_timeout)

    raise ValueError(
        f"Unknown AI provider: '{provider_name}'. "
        f"Supported providers: openai, ollama, anthropic, google"
    )
