"""
tests/test_config.py
--------------------
Unit tests for the MythosConfig defaults (Claude/Anthropic backend).
"""
import os
import pytest
from mythos.config import MythosConfig


class TestMythosConfigDefaults:
    def test_default_provider_is_anthropic(self):
        config = MythosConfig()
        assert config.llm_provider == "anthropic"

    def test_default_model_is_claude(self):
        config = MythosConfig()
        assert "claude" in config.llm_model.lower()

    def test_from_env_defaults_to_anthropic(self, monkeypatch):
        monkeypatch.delenv("MYTHOS_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("MYTHOS_LLM_MODEL", raising=False)
        config = MythosConfig.from_env()
        assert config.llm_provider == "anthropic"
        assert "claude" in config.llm_model.lower()

    def test_from_env_reads_anthropic_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        monkeypatch.delenv("MYTHOS_API_KEY", raising=False)
        config = MythosConfig.from_env()
        assert config.llm_api_key == "test-key-123"

    def test_mythos_api_key_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_API_KEY", "mythos-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        config = MythosConfig.from_env()
        assert config.llm_api_key == "mythos-key"

    def test_from_env_overrides_provider(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MYTHOS_LLM_MODEL", "gpt-4o")
        config = MythosConfig.from_env()
        assert config.llm_provider == "openai"
        assert config.llm_model == "gpt-4o"

    def test_from_env_stub_provider(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_LLM_PROVIDER", "stub")
        config = MythosConfig.from_env()
        assert config.llm_provider == "stub"

    def test_default_api_key_reads_anthropic_env(self, monkeypatch):
        monkeypatch.delenv("MYTHOS_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        # Default factory uses ANTHROPIC_API_KEY
        config = MythosConfig()
        # The default_factory is evaluated at instantiation time
        assert config.llm_api_key == "sk-ant-test"
