"""Model factory: build the configured LLM with its key passed explicitly.

What: `main_model(settings)` and `review_model(settings)` turn ERRAND_MODEL /
      ERRAND_REVIEW_MODEL ("<provider>:<model>") into a PydanticAI model.

Why:  Keys live in Settings (from backend/.env), which pydantic-settings never exports
      to os.environ, where the provider SDKs would look. Passing them explicitly is
      the only way a key in .env reaches the provider; failing early with the exact
      variable to set beats an opaque SDK error mid-conversation.

How:  - anthropic:<model> -> AnthropicModel(AnthropicProvider(api_key=...)).
      - openai:<model> / openai-responses:<model> -> OpenAIResponsesModel;
        openai-chat:<model> -> OpenAIChatModel; both with OpenAIProvider(api_key=...).
      - google:<model> is priced in the cost table but its SDK isn't installed: a clear
        error says so.
      - "test" -> PydanticAI's offline TestModel (no key), for local smoke runs.
      - Which model each role uses (defaults: Sonnet / Haiku; the reader follows an
        explicit ERRAND_MODEL) is decided in Settings; this module only builds it.
"""

from pydantic import SecretStr
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from errand_agent.core.config import Settings


class ModelNotConfigured(Exception):
    """The model can't be built from the current settings; the message says what to set."""


def main_model(settings: Settings) -> Model:
    return build_model(settings.model, settings, setting="ERRAND_MODEL")


def review_model(settings: Settings) -> Model:
    return build_model(
        settings.review_model_spec, settings, setting="ERRAND_REVIEW_MODEL or ERRAND_MODEL"
    )


def build_model(spec: str, settings: Settings, *, setting: str) -> Model:
    if spec == "test":
        return TestModel()
    provider, _, name = spec.partition(":")
    if not name:
        raise ModelNotConfigured(f"{setting}={spec!r} should look like '<provider>:<model>'")
    if provider == "anthropic":
        key = _key(settings.anthropic_api_key, "ANTHROPIC_API_KEY")
        return AnthropicModel(name, provider=AnthropicProvider(api_key=key))
    if provider in ("openai", "openai-responses", "openai-chat"):
        key = _key(settings.openai_api_key, "OPENAI_API_KEY")
        cls = OpenAIChatModel if provider == "openai-chat" else OpenAIResponsesModel
        return cls(name, provider=OpenAIProvider(api_key=key))
    if provider == "google":
        raise ModelNotConfigured(
            "google models are priced but the SDK isn't installed: add the"
            " pydantic-ai-slim[google] extra first"
        )
    raise ModelNotConfigured(f"unsupported model provider {provider!r} in {setting}")


def _key(secret: SecretStr | None, variable: str) -> str:
    if secret is None:
        raise ModelNotConfigured(f"{variable} is not set (add it to backend/.env)")
    return secret.get_secret_value()
