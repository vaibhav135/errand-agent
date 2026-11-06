import pytest
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from errand_agent.agent.models import ModelNotConfigured, main_model, review_model
from errand_agent.core.config import Settings


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_no_model_configured_names_the_setting() -> None:
    with pytest.raises(ModelNotConfigured, match="ERRAND_MODEL"):
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
