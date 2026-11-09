"""An -EnvFile given to the Windows service script is parsed exactly like api/.env."""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "windows" / "metasearch-lite.ps1"


def test_script_hands_the_file_to_the_app_instead_of_parsing_it():
    text = SCRIPT.read_text(encoding="ascii")
    assert "$env:METASEARCH_ENV_FILE" in text
    # Setting variables one by one turned comments into values and deleted empty ones.
    assert "SetEnvironmentVariable" not in text


def test_extra_env_file_keeps_empty_values_and_drops_comments(tmp_path, monkeypatch):
    env = tmp_path / "lite.env"
    env.write_text(
        "REDIS_URL=\n"
        "CRAWL4AI_URL=http://127.0.0.1:1      # not there, tier skipped\n"
        "JINA_API_KEY=                        # fill in for a higher rate ceiling\n"
        "SCRAPE_CONCURRENCY=3\n",
        encoding="utf-8",
    )
    for name in ("REDIS_URL", "CRAWL4AI_URL", "JINA_API_KEY", "SCRAPE_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no api/.env in the way
    monkeypatch.setenv("METASEARCH_ENV_FILE", str(env))
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.redis_url == ""  # not the built-in redis://redis:6379/0
        assert s.crawl4ai_url == "http://127.0.0.1:1"
        assert not s.jina_api_key
        assert s.scrape_concurrency == 3
    finally:
        get_settings.cache_clear()


def test_real_environment_still_wins_over_the_file(tmp_path, monkeypatch):
    env = tmp_path / "lite.env"
    env.write_text("SCRAPE_CONCURRENCY=3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METASEARCH_ENV_FILE", str(env))
    monkeypatch.setenv("SCRAPE_CONCURRENCY", "7")
    get_settings.cache_clear()
    try:
        assert get_settings().scrape_concurrency == 7
    finally:
        get_settings.cache_clear()


def test_measure_script_measures_the_server_not_the_launcher():
    text = (REPO / "scripts" / "windows" / "measure-lite.ps1").read_text(encoding="ascii")
    assert "Get-ServerProcess" in text and "ParentProcessId" in text
    assert "Stop-Process -Id $server.Id" in text
    assert "Stop-Process -Id $procId" not in text
