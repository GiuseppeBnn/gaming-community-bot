from __future__ import annotations

import pytest

from config_data.config import Settings


def test_old_dotenv_loads_without_reactivating_retired_routes(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "ALDUINO_PROVIDER=auto\n"
        "AI_ENTERTAINMENT_PROVIDER=groq\n"
        "TWENTYQ_PROVIDER_ORDER=gemini,groq,openrouter\n"
        "ALDUINO_FALLBACK_TO_GROQ=true\n"
        "ALDUINO_GEMINI_MODEL=legacy-model\n"
        "ALDUINO_THINKING_LEVEL=minimal\n"
        "ALDUINO_FREE_TIMEOUT_SECONDS=10\n"
        "AI_ENTERTAINMENT_FREE_TIMEOUT_SECONDS=10\n"
        "ALDUINO_TIMEOUT_SECONDS=15\n"
        "GROQ_JUDGE_MODEL=openai/gpt-oss-120b\n",
        encoding="utf-8",
    )
    configured = Settings(bot_token="x", _env_file=env)
    assert configured.openrouter_judge_model == "openai/gpt-oss-120b"
    assert configured.openrouter_chat_models == "z-ai/glm-5.3-flash"
    for name in ("alduino_provider", "alduino_fallback_to_groq", "twentyq_provider_order"):
        assert not hasattr(configured, name)


def test_uppercase_retired_arguments_are_also_accepted():
    configured = Settings(bot_token="x", _env_file=None, ALDUINO_PROVIDER="gemini")
    assert "alduino_provider" not in type(configured).model_fields


def test_unknown_environment_keys_still_fail_closed(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_JUDJE_MODEL=typo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Settings(bot_token="x", _env_file=env)


def test_non_mapping_validator_input_is_preserved():
    assert Settings.discard_retired_ai_routing(None) is None


def test_legacy_judge_model_is_preserved_unless_new_selection_exists():
    legacy = Settings(bot_token="x", _env_file=None, groq_judge_model="custom/judge")
    explicit = Settings(
        bot_token="x", _env_file=None, groq_judge_model="custom/judge",
        openrouter_judge_model="custom/new",
    )
    assert legacy.openrouter_judge_model == "custom/judge"
    assert explicit.openrouter_judge_model == "custom/new"


@pytest.mark.parametrize("model", ["", "   "])
def test_judge_model_cannot_be_blank(model):
    with pytest.raises(ValueError, match="model cannot be empty"):
        Settings(bot_token="x", _env_file=None, openrouter_judge_model=model)
