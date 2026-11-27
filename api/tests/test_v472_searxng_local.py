"""docs/36 §21: native local SearXNG settings builder (scripts/windows/...settings.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "searxng_local_settings", REPO / "scripts" / "windows" / "searxng_local_settings.py"
)
assert _spec and _spec.loader
sls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sls)

SOURCE = (REPO / "deploy" / "searxng" / "settings.yml").read_text(encoding="utf-8")


def _build(**kw):
    args = {"secret": "a" * 64, "port": 8888, "disable": [], "inactive": []}
    args.update(kw)
    return yaml.safe_load(sls.build(SOURCE, **args))


def _engine(cfg, name):
    return next(e for e in cfg["engines"] if e["name"] == name)


def test_local_server_binds_loopback_with_json_and_no_limiter():
    cfg = _build()
    srv = cfg["server"]
    assert srv["bind_address"] == "127.0.0.1" and srv["port"] == 8888
    assert srv["limiter"] is False and srv["secret_key"] == "a" * 64
    assert srv["base_url"] == "http://127.0.0.1:8888/"
    assert "json" in cfg["search"]["formats"]


def test_disable_and_inactive_engines():
    cfg = _build(disable=["google", "qwant"], inactive=["wikipedia", "ahmia"])
    assert _engine(cfg, "google")["disabled"] is True
    assert _engine(cfg, "qwant")["disabled"] is True  # line carries a trailing comment
    wiki = _engine(cfg, "wikipedia")
    assert wiki["disabled"] is True and wiki["inactive"] is True
    assert _engine(cfg, "ahmia")["inactive"] is True  # not in the source list: appended
    assert _engine(cfg, "bing")["disabled"] is False
    names = [e["name"] for e in cfg["engines"]]
    assert len(names) == len(set(names))  # no duplicated engine entries


def test_no_contact_data_and_secret_is_kept(tmp_path):
    text = sls.build(SOURCE, secret="b" * 64, port=8888, disable=[], inactive=[])
    assert "contact_url" not in text and "@" not in text.split("engines:")[0]
    target = tmp_path / "settings.yml"
    target.write_text(text, encoding="utf-8")
    assert sls._existing_secret(target) == "b" * 64


def test_fetch_rejects_non_commit_refs(tmp_path):
    with pytest.raises(SystemExit):
        sls.fetch("main", tmp_path / "src")
