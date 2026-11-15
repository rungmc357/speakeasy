from speakeasy import home_chat as H


def _e(provider, model):
    return {f"{provider}/{model}": {"id": f"{provider}/{model}", "provider": provider, "model": model}}


def test_claude_default_user_gets_sonnet_over_codex_terra():
    entries = {**_e("openai-codex", "gpt-6.1-terra"), **_e("anthropic", "claude-sonnet-5-5")}
    assert H.pick_auto(entries, prefer="anthropic") == "anthropic/claude-sonnet-5-5"


def test_non_claude_user_gets_codex_first():
    entries = {**_e("openai-codex", "gpt-6.1-terra"), **_e("anthropic", "claude-sonnet-5-5")}
    assert H.pick_auto(entries, prefer="openai-codex") == "openai-codex/gpt-6.1-terra"
    assert H.pick_auto(entries) == "openai-codex/gpt-6.1-terra"


def test_default_on_an_aggregator_never_pulls_from_it():
    entries = {**_e("openai-codex", "gpt-6.1-terra"), **_e("openrouter", "anthropic/claude-sonnet-5-5")}
    assert H.pick_auto(entries, prefer="openrouter") == "openai-codex/gpt-6.1-terra"


def test_default_provider_reads_config():
    assert H._default_provider({"model": {"provider": "anthropic", "default": "x"}}) == "anthropic"
    assert H._default_provider({}) is None
