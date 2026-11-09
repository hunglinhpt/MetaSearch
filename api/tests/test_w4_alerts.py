"""Alert rules point at series that exist, and the persist queue exports what they need."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml

from app.core.metrics import REGISTRY
from app.main import app
from app.orchestration.persist_queue import PersistItem, PersistQueue

ALERTS = Path(__file__).resolve().parents[2] / "deploy" / "prometheus" / "alerts.yml"


def _rules() -> list[dict]:
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g["rules"]]


def _sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return value or 0.0


def test_every_path_label_in_alerts_is_emitted():
    # The label is whatever the middleware records for a real request, so send one.
    from fastapi.testclient import TestClient

    client = TestClient(app)
    for rule in _rules():
        for path in re.findall(r'path="([^"]+)"', rule["expr"]):
            client.post(f"/v1{path}", json={})
            client.get(f"/v1{path}")
            labels = {
                s.labels.get("path")
                for m in REGISTRY.collect()
                if m.name == "metasearch_http_request_duration_seconds"
                for s in m.samples
            }
            assert path in labels, f"{rule['alert']}: no series with path={path}"


def test_every_metric_in_alerts_is_exported():
    exported = set()
    for metric in REGISTRY.collect():
        for suffix in ("", "_total", "_bucket", "_count", "_sum"):
            exported.add(metric.name + suffix)
    for rule in _rules():
        for name in re.findall(r"(metasearch_[a-z_]+)", rule["expr"]):
            assert name in exported, f"{rule['alert']}: {name} is not exported"


def test_persist_alerts_exist():
    names = {r["alert"] for r in _rules()}
    assert {"PersistQueueBacklog", "PersistWritesDropped", "PersistWritesFailing"} <= names


async def _noop():
    return None


async def test_depth_gauge_falls_as_the_consumer_drains():
    q = PersistQueue(maxsize=50)
    for _ in range(20):
        q.enqueue(PersistItem("t", _noop))
    assert _sample("metasearch_background_queue_depth", queue="persist") == 20
    await q.start()
    for _ in range(50):
        await asyncio.sleep(0.05)
        if _sample("metasearch_background_queue_depth", queue="persist") == 0:
            break
    await q.stop()
    assert _sample("metasearch_background_queue_depth", queue="persist") == 0


async def test_dropped_and_failed_writes_are_counted():
    dropped = _sample("metasearch_persist_dropped_total")
    failed = _sample("metasearch_persist_failed_total")
    q = PersistQueue(maxsize=2)
    for _ in range(5):
        q.enqueue(PersistItem("t", _noop))
    assert _sample("metasearch_persist_dropped_total") == dropped + 3

    async def boom():
        raise RuntimeError("db down")

    q2 = PersistQueue(maxsize=5)
    q2.enqueue(PersistItem("t", boom))
    await q2.drain(1.0)
    q2.enqueue(PersistItem("t", boom))
    await q2.start()
    for _ in range(40):
        await asyncio.sleep(0.05)
        if _sample("metasearch_persist_failed_total") == failed + 2:
            break
    await q2.stop()
    assert _sample("metasearch_persist_failed_total") == failed + 2
