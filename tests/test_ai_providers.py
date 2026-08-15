"""Tests for ai_providers module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai_providers import (  # isort: skip
    _redact_url,
    AnthropicProvider,
    ProviderCascade,
    ProviderCascadeError,
    create_provider,
    GoogleProvider,
    OllamaProvider,
    OpenAIProvider,
    TokenBudgetExhaustedError,
    is_token_budget_error,
)

# --- Factory tests ---


@pytest.mark.unit
def test_create_provider_openai(mock_logger):
    """Test creating OpenAI provider."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = create_provider(
            provider_name="openai",
            logger=mock_logger,
            openai_api_key="sk-test",
        )
        assert isinstance(provider, OpenAIProvider)


@pytest.mark.unit
def test_create_provider_openai_passes_custom_base_url(mock_logger):
    """OpenAI-compatible providers can target a custom API endpoint."""
    with patch("src.ai_providers.AsyncOpenAI") as mock_client:
        create_provider(
            provider_name="openai",
            logger=mock_logger,
            openai_api_key="sk-test",
            openai_base_url="https://api.deepseek.com/v1",
        )

    assert mock_client.call_args.kwargs["base_url"] == "https://api.deepseek.com/v1"


@pytest.mark.unit
def test_create_provider_ollama(mock_logger):
    """Test creating Ollama provider."""
    provider = create_provider(
        provider_name="ollama",
        logger=mock_logger,
        ollama_base_url="http://localhost:11434",
    )
    assert isinstance(provider, OllamaProvider)


@pytest.mark.unit
def test_create_provider_anthropic(mock_logger):
    """Test creating Anthropic provider."""
    provider = create_provider(
        provider_name="anthropic",
        logger=mock_logger,
        anthropic_api_key="sk-ant-test",
    )
    assert isinstance(provider, AnthropicProvider)


@pytest.mark.unit
def test_create_provider_google(mock_logger):
    """Test creating the Google Gemini provider with its official endpoint."""
    with patch("src.ai_providers.AsyncOpenAI") as mock_client:
        provider = create_provider(
            provider_name="google",
            logger=mock_logger,
            google_api_key="google-test-key",
        )

    assert isinstance(provider, GoogleProvider)
    assert mock_client.call_args.kwargs["api_key"] == "google-test-key"
    assert (
        mock_client.call_args.kwargs["base_url"]
        == "https://generativelanguage.googleapis.com/v1beta/openai/"
    )


@pytest.mark.unit
def test_create_provider_google_missing_key(mock_logger):
    """Test that Google requires its own API key."""
    with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
        create_provider(provider_name="google", logger=mock_logger)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_cascade_switches_after_primary_error(mock_logger):
    """A quota or transport failure moves the request to the next provider slot."""
    primary = MagicMock()
    primary.chat_completion = AsyncMock(side_effect=RuntimeError("quota exceeded"))
    backup = MagicMock()
    backup.chat_completion = AsyncMock(return_value="backup response")
    cascade = ProviderCascade([("google-primary", primary), ("google-backup", backup)], mock_logger)

    result = await cascade.chat_completion(
        messages=[], model="gemini-3.6-flash", temperature=0.2, max_tokens=100
    )

    assert result == "backup response"
    primary.chat_completion.assert_awaited_once()
    backup.chat_completion.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_cascade_reports_all_slot_failures(mock_logger):
    """Exhausting the cascade raises one actionable error with provider names."""
    primary = MagicMock()
    primary.chat_completion = AsyncMock(side_effect=RuntimeError("quota exceeded"))
    backup = MagicMock()
    backup.chat_completion = AsyncMock(side_effect=RuntimeError("timeout"))
    cascade = ProviderCascade(
        [("google-primary", primary), ("openrouter-free", backup)], mock_logger
    )

    with pytest.raises(ProviderCascadeError, match="google-primary.*openrouter-free"):
        await cascade.chat_completion(
            messages=[], model="gemini-3.6-flash", temperature=0.2, max_tokens=100
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_cascade_classifies_token_budget_exhaustion(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=TokenBudgetExhaustedError("secret provider details")
    )
    cascade = ProviderCascade([("primary", provider)], mock_logger)

    with pytest.raises(ProviderCascadeError) as error:
        await cascade.chat_completion(
            messages=[], model="deepseek-v4-flash", temperature=0.2, max_tokens=100
        )

    assert error.value.failure_kinds == ("token_budget",)
    assert "secret provider details" not in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_cascade_does_not_expose_provider_exception_text(mock_logger):
    """Aggregate failover errors remain safe even if an SDK error contains a secret."""
    provider = MagicMock()
    provider.chat_completion = AsyncMock(side_effect=RuntimeError("request key=sk-secret-value"))
    cascade = ProviderCascade([("google-primary", provider)], mock_logger)

    with pytest.raises(ProviderCascadeError) as exc_info:
        await cascade.chat_completion(
            messages=[], model="gemini-3.6-flash", temperature=0.2, max_tokens=100
        )

    assert "sk-secret-value" not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_cascade_classifies_context_failures_without_secrets(mock_logger):
    """Context-only failures are distinguishable from quota and transport errors."""
    first = MagicMock()
    first.chat_completion = AsyncMock(
        side_effect=RuntimeError("context_length_exceeded key=secret")
    )
    second = MagicMock()
    second.chat_completion = AsyncMock(side_effect=ValueError("maximum context window exceeded"))
    cascade = ProviderCascade([("google-1", first), ("google-2", second)], mock_logger)

    with pytest.raises(ProviderCascadeError) as exc_info:
        await cascade.chat_completion([], "model", 0.2, 10)

    error = exc_info.value
    assert error.context_only is True
    assert error.failure_kinds == ("context_size", "context_size")
    assert "secret" not in str(error)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_cascade_context_only_is_false_for_mixed_failures(mock_logger):
    """A quota or timeout failure prevents accidental context batching."""
    first = MagicMock()
    first.chat_completion = AsyncMock(side_effect=RuntimeError("context length exceeded"))
    second = MagicMock()
    second.chat_completion = AsyncMock(side_effect=RuntimeError("quota exceeded"))
    cascade = ProviderCascade([("google-1", first), ("google-2", second)], mock_logger)

    with pytest.raises(ProviderCascadeError) as exc_info:
        await cascade.chat_completion([], "model", 0.2, 10)

    assert exc_info.value.context_only is False
    assert exc_info.value.failure_kinds == ("context_size", "quota")


@pytest.mark.unit
def test_create_google_provider_builds_google_and_openrouter_fallback_slots(mock_logger):
    """The Google factory keeps the configured order and appends OpenRouter last."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = create_provider(
            provider_name="google",
            logger=mock_logger,
            google_api_key="google-1",
            google_api_keys=["google-2", "google-3"],
            openrouter_api_key="openrouter-key",
            openrouter_model="openrouter/free",
        )

    assert isinstance(provider, ProviderCascade)
    assert [slot[0] for slot in provider.providers] == [
        "google-1",
        "google-2",
        "google-3",
        "openrouter-free",
    ]
    assert provider.providers[-1][2] == "openrouter/free"


@pytest.mark.unit
def test_create_provider_unknown(mock_logger):
    """Test error for unknown provider."""
    with pytest.raises(ValueError, match="Unknown AI provider"):
        create_provider(provider_name="unknown", logger=mock_logger)


@pytest.mark.unit
def test_create_provider_openai_missing_key(mock_logger):
    """Test error when OpenAI key is missing."""
    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        create_provider(provider_name="openai", logger=mock_logger, openai_api_key="")


@pytest.mark.unit
def test_create_provider_anthropic_missing_key(mock_logger):
    """Test error when Anthropic key is missing."""
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
        create_provider(provider_name="anthropic", logger=mock_logger, anthropic_api_key="")


@pytest.mark.unit
def test_create_provider_case_insensitive(mock_logger):
    """Test that provider name is case-insensitive."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = create_provider(
            provider_name="OpenAI",
            logger=mock_logger,
            openai_api_key="sk-test",
        )
        assert isinstance(provider, OpenAIProvider)


# --- OpenAI provider tests ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_chat_completion(mock_logger):
    """Test OpenAI provider chat completion."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Test response"))]
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-5-nano",
            temperature=0.7,
            max_tokens=500,
        )

        assert result == "Test response"
        # Verify max_completion_tokens is used instead of max_tokens
        call_kwargs = provider.client.chat.completions.create.call_args[1]
        assert "max_completion_tokens" in call_kwargs
        assert "max_tokens" not in call_kwargs
        assert call_kwargs["max_completion_tokens"] == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_google_provider_request_uses_gemini_compatible_parameters(mock_logger):
    """Google requests omit sampling and DeepSeek-only thinking parameters."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = GoogleProvider(api_key="google-test-key", logger=mock_logger)

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="Test response"), finish_reason="stop")
        ]
        mock_response.usage = None
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gemini-3.6-flash",
            temperature=0.7,
            max_tokens=96_000,
            reasoning_effort="high",
            thinking=True,
        )

    assert result == "Test response"
    call_kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gemini-3.6-flash"
    assert call_kwargs["max_completion_tokens"] == 65_536
    assert call_kwargs["reasoning_effort"] == "high"
    assert "temperature" not in call_kwargs
    assert "extra_body" not in call_kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_none_content_raises(mock_logger):
    """Test OpenAI provider raises RuntimeError when content is None."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "length"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 1234
        mock_response.usage.completion_tokens = 0
        mock_response.usage.total_tokens = 1234
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="gpt-5-nano",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_empty_string_content_raises(mock_logger):
    """Test OpenAI provider raises RuntimeError when content is empty string."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "   "
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 1
        mock_response.usage.total_tokens = 101
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="gpt-5-nano",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_refusal_raises(mock_logger):
    """Test OpenAI provider raises RuntimeError when model refuses."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.refusal = "I cannot process this request"
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 10
        mock_response.usage.total_tokens = 110
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="gpt-5-nano",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_logs_response_metadata(mock_logger):
    """Test OpenAI provider logs finish_reason and token usage."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "Valid response"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 500
        mock_response.usage.completion_tokens = 100
        mock_response.usage.total_tokens = 600
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-5-nano",
            temperature=0.7,
            max_tokens=500,
        )

        assert result == "Valid response"

        # Verify debug logging was called with response metadata
        debug_messages = [str(call) for call in mock_logger.debug.call_args_list]
        debug_text = " ".join(debug_messages)
        assert "finish_reason" in debug_text
        assert "stop" in debug_text
        assert "prompt_tokens" in debug_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_length_with_content_returns_truncated(mock_logger):
    """Test OpenAI returns truncated content and warns when finish_reason=length."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "Partial summary that was cut off mid-sentence"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "length"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 6617
        mock_response.usage.completion_tokens = 500
        mock_response.usage.total_tokens = 7117
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-5-nano",
            temperature=0.7,
            max_tokens=500,
        )

        assert result == "Partial summary that was cut off mid-sentence"
        warning_messages = [str(call) for call in mock_logger.warning.call_args_list]
        warning_text = " ".join(warning_messages)
        assert "truncat" in warning_text.lower() or "max_tokens_per_summary" in warning_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_length_empty_content_raises_with_guidance(mock_logger):
    """Test OpenAI raises with actionable guidance when finish_reason=length and content empty."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "length"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 6617
        mock_response.usage.completion_tokens = 500
        mock_response.usage.total_tokens = 7117
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(TokenBudgetExhaustedError) as exc_info:
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="gpt-5-nano",
                temperature=0.7,
                max_tokens=500,
            )

        assert "max_tokens_per_summary" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_passes_reasoning_effort_when_provided(mock_logger):
    """OpenAI provider passes reasoning_effort to the API when a value is given."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "Summary"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-5-nano",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
        )

        call_kwargs = provider.client.chat.completions.create.call_args[1]
        assert call_kwargs.get("reasoning_effort") == "low"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_omits_reasoning_effort_when_none(mock_logger):
    """OpenAI provider does NOT include reasoning_effort in the API call when it is None."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "response"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-5-nano",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort=None,
        )

        call_kwargs = provider.client.chat.completions.create.call_args[1]
        assert "reasoning_effort" not in call_kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deepseek_provider_uses_thinking_and_native_max_tokens(mock_logger):
    """DeepSeek receives explicit thinking controls and its native token field."""
    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(
            api_key="sk-test",
            logger=mock_logger,
            base_url="https://api.deepseek.com",
        )
        mock_choice = MagicMock()
        mock_choice.message.content = '{"items": []}'
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.chat_completion(
            messages=[{"role": "user", "content": "Extract"}],
            model="deepseek-v4-flash",
            temperature=0.1,
            max_tokens=500,
            reasoning_effort=None,
            thinking=False,
            response_format={"type": "json_object"},
        )

        call_kwargs = provider.client.chat.completions.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 500
        assert "max_completion_tokens" not in call_kwargs
        assert call_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert call_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_falls_back_when_reasoning_effort_rejected(mock_logger):
    """When the API rejects reasoning_effort (BadRequestError), the provider retries without it."""
    import httpx
    from openai import BadRequestError

    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "Summary without reasoning"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150

        bad_request = BadRequestError(
            message="Unsupported parameter: reasoning_effort",
            response=httpx.Response(
                400,
                json={"error": {"message": "Unsupported parameter: reasoning_effort"}},
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            ),
            body={"error": {"message": "Unsupported parameter: reasoning_effort"}},
        )

        provider.client.chat.completions.create = AsyncMock(
            side_effect=[bad_request, mock_response]
        )

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-5-nano",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
        )

        assert result == "Summary without reasoning"
        assert provider.client.chat.completions.create.call_count == 2

        second_call_kwargs = provider.client.chat.completions.create.call_args_list[1][1]
        assert "reasoning_effort" not in second_call_kwargs

        debug_calls = " ".join(str(c) for c in mock_logger.debug.call_args_list)
        assert "reasoning_effort" in debug_calls.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_falls_back_to_max_tokens_without_reasoning_effort(mock_logger):
    """When max_completion_tokens is rejected and reasoning_effort is None, retries with max_tokens."""
    import httpx
    from openai import BadRequestError

    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "Summary via max_tokens"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150

        bad_request = BadRequestError(
            message="Unsupported parameter: max_completion_tokens",
            response=httpx.Response(
                400,
                json={"error": {"message": "Unsupported parameter: max_completion_tokens"}},
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            ),
            body={"error": {"message": "Unsupported parameter: max_completion_tokens"}},
        )

        provider.client.chat.completions.create = AsyncMock(
            side_effect=[bad_request, mock_response]
        )

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-3.5-turbo",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort=None,
        )

        assert result == "Summary via max_tokens"
        assert provider.client.chat.completions.create.call_count == 2

        second_call_kwargs = provider.client.chat.completions.create.call_args_list[1][1]
        assert "max_tokens" in second_call_kwargs
        assert "max_completion_tokens" not in second_call_kwargs
        assert second_call_kwargs["max_tokens"] == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_provider_falls_back_to_max_tokens_after_reasoning_effort_retry(mock_logger):
    """When both reasoning_effort retry and max_completion_tokens fail, falls back to max_tokens."""
    import httpx
    from openai import BadRequestError

    with patch("src.ai_providers.AsyncOpenAI"):
        provider = OpenAIProvider(api_key="sk-test", logger=mock_logger)

        mock_choice = MagicMock()
        mock_choice.message.content = "Summary via max_tokens fallback"
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150

        bad_request = BadRequestError(
            message="Unsupported parameter",
            response=httpx.Response(
                400,
                json={"error": {"message": "Unsupported parameter"}},
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            ),
            body={"error": {"message": "Unsupported parameter"}},
        )

        provider.client.chat.completions.create = AsyncMock(
            side_effect=[bad_request, bad_request, mock_response]
        )

        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-3.5-turbo",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
        )

        assert result == "Summary via max_tokens fallback"
        assert provider.client.chat.completions.create.call_count == 3

        third_call_kwargs = provider.client.chat.completions.create.call_args_list[2][1]
        assert "max_tokens" in third_call_kwargs
        assert "max_completion_tokens" not in third_call_kwargs
        assert "reasoning_effort" not in third_call_kwargs
        assert third_call_kwargs["max_tokens"] == 500


# --- Ollama provider tests ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_chat_completion(mock_logger):
    """Test Ollama provider chat completion."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"message": {"content": "Ollama response"}})

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="llama3",
            temperature=0.7,
            max_tokens=500,
        )

    assert result == "Ollama response"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_error(mock_logger):
    """Test Ollama provider error handling."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.text = AsyncMock(return_value="Internal Server Error")

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError, match="Ollama API error 500"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="llama3",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_error_body_truncated(mock_logger):
    """Test that long error bodies are truncated in the exception message."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    long_body = "x" * 500
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.text = AsyncMock(return_value=long_body)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError) as exc_info:
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="llama3",
                temperature=0.7,
                max_tokens=500,
            )

    # Error message should contain at most 200 chars of the body
    error_msg = str(exc_info.value)
    assert "Ollama API error 500" in error_msg
    assert len(error_msg) < 250  # status prefix + 200 chars of body


@pytest.mark.unit
def test_ollama_provider_url_trailing_slash(mock_logger):
    """Test Ollama provider strips trailing slash from URL."""
    provider = OllamaProvider(base_url="http://localhost:11434/", logger=mock_logger)
    assert provider.base_url == "http://localhost:11434"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_ndjson_content_type(mock_logger):
    """Test Ollama provider handles application/x-ndjson content type.

    Ollama returns application/x-ndjson even with stream: false.
    aiohttp's resp.json() rejects non-application/json by default.
    """
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    response_data = {"message": {"content": "Ollama ndjson response"}}

    mock_response = MagicMock()
    mock_response.status = 200
    # Simulate the real behavior: json() with strict content_type raises ContentTypeError
    # when Content-Type is application/x-ndjson
    mock_response.headers = {"Content-Type": "application/x-ndjson"}
    mock_response.json = AsyncMock(return_value=response_data)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="llama3",
            temperature=0.7,
            max_tokens=500,
        )

    assert result == "Ollama ndjson response"
    # Verify json() was called with content_type=None to bypass strict checking
    mock_response.json.assert_called_once_with(content_type=None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_debug_logging(mock_logger):
    """Test that OllamaProvider emits debug logs before and after the HTTP call."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_length = 42
    mock_response.json = AsyncMock(return_value={"message": {"content": "ok"}})

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="llama3",
            temperature=0.7,
            max_tokens=500,
        )

    debug_calls = [call for call in mock_logger.debug.call_args_list]
    assert len(debug_calls) == 3

    # First call: request log
    assert "Ollama request" in debug_calls[0][0][0]
    assert debug_calls[0][0][1] == "http://localhost:11434/api/chat"
    assert debug_calls[0][0][2] == "llama3"

    # Second call: response log
    assert "Ollama response" in debug_calls[1][0][0]
    assert debug_calls[1][0][1] == 200
    assert debug_calls[1][0][2] == 42

    # Third call: metadata log
    assert "Ollama metadata" in debug_calls[2][0][0]


@pytest.mark.unit
def test_ollama_provider_minimum_timeout(mock_logger):
    """Test that Ollama provider gets at least 120s timeout even when lower value is configured."""
    provider = create_provider(
        provider_name="ollama",
        logger=mock_logger,
        ollama_base_url="http://localhost:11434",
        api_timeout=30,
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.timeout.total >= 120


@pytest.mark.unit
def test_ollama_provider_respects_higher_timeout(mock_logger):
    """Test that Ollama provider uses the configured timeout when it exceeds the minimum."""
    provider = create_provider(
        provider_name="ollama",
        logger=mock_logger,
        ollama_base_url="http://localhost:11434",
        api_timeout=300,
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.timeout.total == 300


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_empty_content_raises(mock_logger):
    """Test Ollama provider raises RuntimeError when content is empty."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_length = 42
    mock_response.json = AsyncMock(
        return_value={
            "message": {"content": ""},
            "model": "llama3",
            "eval_count": 0,
            "prompt_eval_count": 100,
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="llama3",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_missing_message_key_raises(mock_logger):
    """Test Ollama provider raises RuntimeError when message key is missing."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_length = 10
    mock_response.json = AsyncMock(
        return_value={
            "model": "llama3",
            "eval_count": 0,
            "prompt_eval_count": 50,
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="llama3",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_length_with_content_returns_truncated(mock_logger):
    """Test Ollama returns truncated content and warns when done_reason=length."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_length = 80
    mock_response.json = AsyncMock(
        return_value={
            "message": {"content": "Partial summary cut off mid-sentence"},
            "model": "llama3",
            "done_reason": "length",
            "eval_count": 500,
            "prompt_eval_count": 100,
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="llama3",
            temperature=0.7,
            max_tokens=500,
        )

    assert result == "Partial summary cut off mid-sentence"
    warning_messages = [str(call) for call in mock_logger.warning.call_args_list]
    warning_text = " ".join(warning_messages)
    assert "truncat" in warning_text.lower() or "max_tokens_per_summary" in warning_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_length_empty_content_raises_with_guidance(mock_logger):
    """Test Ollama raises with actionable guidance when done_reason=length and content empty."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_length = 20
    mock_response.json = AsyncMock(
        return_value={
            "message": {"content": ""},
            "model": "llama3",
            "done_reason": "length",
            "eval_count": 500,
            "prompt_eval_count": 100,
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(TokenBudgetExhaustedError) as exc_info:
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="llama3",
                temperature=0.7,
                max_tokens=500,
            )

    assert "max_tokens_per_summary" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ollama_provider_accepts_reasoning_effort_param(mock_logger):
    """Ollama provider silently accepts and ignores reasoning_effort."""
    provider = OllamaProvider(base_url="http://localhost:11434", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_length = 42
    mock_response.json = AsyncMock(return_value={"message": {"content": "response"}})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="llama3",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
        )

    assert result == "response"


# --- Anthropic provider tests ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_chat_completion(mock_logger):
    """Test Anthropic provider chat completion."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={"content": [{"type": "text", "text": "Anthropic response"}]}
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
            model="claude-sonnet-4-5-20250929",
            temperature=0.7,
            max_tokens=500,
        )

    assert result == "Anthropic response"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_error_body_truncated(mock_logger):
    """Test that long Anthropic error bodies are truncated in the exception message."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    long_body = "y" * 500
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.text = AsyncMock(return_value=long_body)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError) as exc_info:
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-5-20250929",
                temperature=0.7,
                max_tokens=500,
            )

    error_msg = str(exc_info.value)
    assert "Anthropic API error 500" in error_msg
    assert len(error_msg) < 250


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_error(mock_logger):
    """Test Anthropic provider error handling."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.text = AsyncMock(return_value="Unauthorized")

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError, match="Anthropic API error 401"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-5-20250929",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_empty_content_array_raises(mock_logger):
    """Test Anthropic provider raises RuntimeError when content array is empty."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 0},
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-5-20250929",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_no_text_blocks_raises(mock_logger):
    """Test Anthropic provider raises RuntimeError when content has no text blocks."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "content": [{"type": "tool_use", "id": "123", "name": "test"}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 200, "output_tokens": 50},
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(RuntimeError, match="empty content"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-5-20250929",
                temperature=0.7,
                max_tokens=500,
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_logs_response_metadata(mock_logger):
    """Test Anthropic provider logs stop_reason and usage."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "Valid response"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 300, "output_tokens": 75},
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-5-20250929",
            temperature=0.7,
            max_tokens=500,
        )

    assert result == "Valid response"

    debug_messages = [str(call) for call in mock_logger.debug.call_args_list]
    debug_text = " ".join(debug_messages)
    assert "stop_reason" in debug_text
    assert "end_turn" in debug_text
    assert "input_tokens" in debug_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_max_tokens_with_content_returns_truncated(mock_logger):
    """Test Anthropic returns truncated content and warns when stop_reason=max_tokens."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "Partial summary that was cut off"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 3834, "output_tokens": 500},
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-5-20250929",
            temperature=0.7,
            max_tokens=500,
        )

    assert result == "Partial summary that was cut off"
    warning_messages = [str(call) for call in mock_logger.warning.call_args_list]
    warning_text = " ".join(warning_messages)
    assert "truncat" in warning_text.lower() or "max_tokens_per_summary" in warning_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_max_tokens_empty_content_raises_with_guidance(mock_logger):
    """Test Anthropic raises with actionable guidance when stop_reason=max_tokens, content empty."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "content": [],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 3834, "output_tokens": 500},
        }
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))
    mock_session.close = AsyncMock()

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        with pytest.raises(TokenBudgetExhaustedError) as exc_info:
            await provider.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-5-20250929",
                temperature=0.7,
                max_tokens=500,
            )

    assert "max_tokens_per_summary" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_provider_accepts_reasoning_effort_param(mock_logger):
    """Anthropic provider silently accepts and ignores reasoning_effort."""
    provider = AnthropicProvider(api_key="sk-ant-test", logger=mock_logger)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "response"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=AsyncContextManager(mock_response))

    with patch("src.ai_providers.aiohttp.ClientSession") as mock_cs:
        mock_cs.return_value = AsyncContextManager(mock_session)
        result = await provider.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-6",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
        )

    assert result == "response"


# --- URL redaction tests ---


@pytest.mark.unit
def test_redact_url_no_credentials():
    """Test that URLs without credentials are returned unchanged."""
    url = "http://localhost:11434/api/chat"
    assert _redact_url(url) == url


@pytest.mark.unit
def test_redact_url_with_credentials():
    """Test that URLs with embedded credentials are redacted."""
    url = "http://user:secret@myhost:11434/api/chat"
    redacted = _redact_url(url)
    assert "user" not in redacted
    assert "secret" not in redacted
    assert "myhost" in redacted
    assert "11434" in redacted
    assert "***@" in redacted


@pytest.mark.unit
def test_redact_url_with_username_only():
    """Test that URLs with only a username are redacted."""
    url = "http://admin@myhost:11434/api/chat"
    redacted = _redact_url(url)
    assert "admin" not in redacted
    assert "***@" in redacted


# --- Helper for async context managers ---


class AsyncContextManager:
    """Helper to mock async context managers."""

    def __init__(self, return_value):
        self.return_value = return_value

    async def __aenter__(self):
        return self.return_value

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.unit
def test_is_token_budget_error():
    """Token budget classifier identifies direct and cascade token budget exhaustion."""
    import asyncio

    assert is_token_budget_error(TokenBudgetExhaustedError("exceeded")) is True
    assert (
        is_token_budget_error(
            ProviderCascadeError("cascade failed", failure_kinds=("token_budget",))
        )
        is True
    )
    assert (
        is_token_budget_error(
            ProviderCascadeError("cascade failed", failure_kinds=("token_budget", "timeout"))
        )
        is True
    )
    assert (
        is_token_budget_error(
            ProviderCascadeError("cascade failed", failure_kinds=("timeout", "quota"))
        )
        is False
    )
    assert is_token_budget_error(asyncio.TimeoutError()) is False
    assert is_token_budget_error(ValueError("invalid")) is False
    assert is_token_budget_error(RuntimeError("generic")) is False
