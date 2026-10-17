"""A subscription token is no API key: the API backend neither accepts nor sends it."""

from pipeline import config, llm, runner


def test_api_backend_takes_the_key_and_never_a_subscription_token(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-invented")
    assert runner.missing_credentials() is not None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-invented")
    monkeypatch.setattr(llm, "_client", None)
    client = llm.client()
    assert client.api_key == "sk-ant-api-invented" and client.auth_token is None
