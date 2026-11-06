import pytest
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from errand_agent.agent.models import ModelNotConfigured, main_model, review_model
from errand_agent.core.config import Settings


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_default_models_need_only_the_anthropic_key() -> None:
    s = settings(ANTHROPIC_API_KEY="sk-ant-test")

    assert main_model(s).model_name == "claude-sonnet-5-5"
    assert review_model(s).model_name == "claude-haiku-5-5"


def test_defaults_are_low_effort_and_prompt_caching() -> None:
    # D5: measured on live turns, low effort halved errand turns and caching cut cost
    # by 55-83% with no loss in what the agent did.
    configured = dict(main_model(settings(ANTHROPIC_API_KEY="sk-ant-test")).settings or {})

    assert configured["anthropic_effort"] == "low"
    assert configured["anthropic_cache_tool_definitions"] is True
    assert configured["anthropic_cache_instructions"] is True


def test_both_can_be_turned_back() -> None:
    s = settings(ANTHROPIC_API_KEY="sk-ant-test", effort=None, prompt_cache=False)

    configured = dict(main_model(s).settings or {})
    assert "anthropic_effort" not in configured  # the API's own default (high) applies
    assert not configured.get("anthropic_cache_tool_definitions")
    assert not configured.get("anthropic_cache_instructions")


def test_effort_and_prompt_caching_reach_both_anthropic_models() -> None:
    s = settings(ANTHROPIC_API_KEY="sk-ant-test", effort="low", prompt_cache=True)

    for m in (main_model(s), review_model(s)):
        configured = dict(m.settings or {})
        assert configured["anthropic_effort"] == "low"
        assert configured["anthropic_cache_tool_definitions"] is True
        assert configured["anthropic_cache_instructions"] is True


def test_default_model_without_a_key_names_the_variable() -> None:
    with pytest.raises(ModelNotConfigured, match="ANTHROPIC_API_KEY"):
        main_model(settings())


def test_anthropic_model_gets_the_key_from_settings() -> None:
    m = main_model(settings(model="anthropic:claude-opus-5-5", ANTHROPIC_API_KEY="sk-ant-test"))

    assert isinstance(m, AnthropicModel)
    assert m.model_name == "claude-opus-5-5"
    assert isinstance(m.client, AsyncAnthropic)
    assert m.client.api_key == "sk-ant-test"


def test_missing_key_names_the_variable() -> None:
    with pytest.raises(ModelNotConfigured, match="ANTHROPIC_API_KEY"):
        main_model(settings(model="anthropic:claude-opus-5-5"))
    with pytest.raises(ModelNotConfigured, match="OPENAI_API_KEY"):
        main_model(settings(model="openai:gpt-6.1-sol"))


@pytest.mark.parametrize(
    ("model", "cls"),
    [
        ("openai:gpt-6.1-sol", OpenAIResponsesModel),
        ("openai-responses:gpt-6.1-sol", OpenAIResponsesModel),
        ("openai-chat:gpt-6.1-sol", OpenAIChatModel),
    ],
)
def test_openai_models(model: str, cls: type) -> None:
    m = main_model(settings(model=model, OPENAI_API_KEY="sk-oa-test"))

    assert isinstance(m, cls)
    assert m.model_name == "gpt-6.1-sol"


def test_gemini_is_priced_but_not_installed() -> None:
    with pytest.raises(ModelNotConfigured, match="google"):
        main_model(settings(model="google:gemini-3.8-flash", GOOGLE_API_KEY="g-test"))


def test_offline_test_model_needs_no_key() -> None:
    assert isinstance(main_model(settings(model="test")), TestModel)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ModelNotConfigured, match="mistral"):
        main_model(settings(model="mistral:large"))


def test_review_model_defaults_to_the_main_model_but_can_differ() -> None:
    same = settings(model="anthropic:claude-opus-5-5", ANTHROPIC_API_KEY="k")
    cheaper = settings(
        model="anthropic:claude-opus-5-5",
        review_model="anthropic:claude-haiku-5-5",
        ANTHROPIC_API_KEY="k",
    )

    assert review_model(same).model_name == "claude-opus-5-5"
    assert review_model(cheaper).model_name == "claude-haiku-5-5"
