from __future__ import annotations

from app.config import Settings, get_settings


def test_defaults_are_giants_first() -> None:
    settings = Settings(_env_file=None)
    assert settings.searxng_url.startswith("http")
    assert settings.reranker == "heuristic"
    assert settings.embedding_l2 == "off"
    assert settings.llm_provider == "none"


def test_giants_snapshot_exposes_giant_endpoints() -> None:
    giants = Settings(_env_file=None).giants()
    assert set(giants) >= {"searxng_url", "crawl4ai_url", "reranker", "embedding_l2"}


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_test_environment_flag() -> None:
    settings = get_settings()
    assert settings.is_test is True
    assert settings.is_prod is False
