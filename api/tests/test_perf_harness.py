"""Verification tests for WP-00: Safe and repeatable performance harness."""

from __future__ import annotations

import _socket
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.perf.compare import main as compare_main
from scripts.perf_load import (
    assert_port_available,
    wait_ready,
)


@pytest.fixture(autouse=True)
def restore_real_dns(monkeypatch):
    """Restore real DNS resolution for local loopback in harness tests."""
    monkeypatch.setattr(socket, "getaddrinfo", _socket.getaddrinfo)


class ForeignServiceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path.startswith("/healthz"):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        elif self.path.startswith("/v1/meta"):
            # Foreign service: name is NOT 'MetaSearch AI'
            body = b'{"name": "Foreign Service", "version": "1.0.0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.flush()

    def log_message(self, *args) -> None:
        pass


def test_perf_load_refuses_busy_port():
    """perf_load must immediately abort if the target port is already in use."""
    # Open a real listening socket to simulate an existing service (another local system)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]

        with pytest.raises(RuntimeError, match=r"already in use|occupied|busy"):
            assert_port_available("127.0.0.1", busy_port)


def test_wait_ready_rejects_foreign_service():
    """wait_ready must verify /v1/meta name == 'MetaSearch AI', rejecting foreign services."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ForeignServiceHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        api_url = f"http://127.0.0.1:{port}"
        with pytest.raises(RuntimeError, match=r"Foreign service|foreign service|not MetaSearch"):
            wait_ready(api_url, max_attempts=5, delay_s=0.05)
    finally:
        server.shutdown()
        server.server_close()


def test_perf_compare_flags_regression(tmp_path, monkeypatch):
    """compare.py exits with 1 on regression > max_regress and 0 when within budget."""
    base_file = tmp_path / "baseline.json"
    curr_file_ok = tmp_path / "curr_ok.json"
    curr_file_bad = tmp_path / "curr_bad.json"

    base_data = {
        "profile": "memory",
        "scenarios": [
            {"label": "search miss, 5 engines", "concurrency": 1, "p95_ms": 100.0, "rps": 10.0}
        ],
    }
    # 5% increase (within 10% threshold)
    curr_ok_data = {
        "profile": "memory",
        "scenarios": [
            {"label": "search miss, 5 engines", "concurrency": 1, "p95_ms": 105.0, "rps": 9.8}
        ],
    }
    # 25% latency increase (exceeds 10% threshold)
    curr_bad_data = {
        "profile": "memory",
        "scenarios": [
            {"label": "search miss, 5 engines", "concurrency": 1, "p95_ms": 130.0, "rps": 7.0}
        ],
    }

    base_file.write_text(json.dumps(base_data), encoding="utf-8")
    curr_file_ok.write_text(json.dumps(curr_ok_data), encoding="utf-8")
    curr_file_bad.write_text(json.dumps(curr_bad_data), encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["compare.py", str(base_file), str(curr_file_ok), "--max-regress", "10"],
    )
    assert compare_main() == 0

    monkeypatch.setattr(
        "sys.argv",
        ["compare.py", str(base_file), str(curr_file_bad), "--max-regress", "10"],
    )
    assert compare_main() == 1
