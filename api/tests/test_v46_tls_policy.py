"""docs/35 M4: /v1/scrape must not escalate past an invalid TLS certificate.

The end-to-end cases use a REAL local HTTPS server with a self-signed
certificate (no external network); crawl4ai / Jina are spies.
"""

from __future__ import annotations

import datetime as dt
import http.server
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.extraction import scraper as scraper_mod
from app.extraction.scraper import PageContent, PageFailure, Scraper
from app.extraction.tls_policy import (
    TLS_VERIFY_FAILED,
    TLSVerificationFailed,
    is_tls_verification_error,
)
from app.main import create_app
from tests.test_scraper import HTML_PAGE, make_settings

# --------------------------------------------------------------------------------------
# Pure classifier
# --------------------------------------------------------------------------------------


def _wrapped(inner: BaseException, msg: str = "boom") -> httpx.ConnectError:
    try:
        try:
            raise inner
        except BaseException as e:
            raise httpx.ConnectError(msg) from e
    except httpx.ConnectError as outer:
        return outer


def test_cert_verification_error_in_cause_chain() -> None:
    inner = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed")
    assert is_tls_verification_error(_wrapped(inner)) is True


def test_marker_string_fallback() -> None:
    exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert is_tls_verification_error(exc) is True


def test_eof_is_a_network_drop_not_a_cert_error() -> None:
    eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:1000)")
    assert is_tls_verification_error(_wrapped(eof)) is False
    assert (
        is_tls_verification_error(httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]"))
        is False
    )
    assert is_tls_verification_error(httpx.ConnectError("connection refused")) is False
    assert is_tls_verification_error(None) is False


def test_cyclic_chain_terminates() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert is_tls_verification_error(a) is False


def test_exception_carries_url() -> None:
    assert TLSVerificationFailed("https://x.test/").url == "https://x.test/"


# --------------------------------------------------------------------------------------
# Real local HTTPS server with a self-signed certificate
# --------------------------------------------------------------------------------------


def _self_signed(tmp: Path, host: str) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_p, key_p = tmp / "cert.pem", tmp / "key.pem"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_p, key_p


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = HTML_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep test output clean
        return


HOST = "selfsigned.test"


@pytest.fixture
def https_server(tmp_path: Path) -> Iterator[int]:
    cert_p, key_p = _self_signed(tmp_path, HOST)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_p, key_p)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


class _Spies:
    def __init__(self) -> None:
        self.crawl4ai = 0
        self.jina = 0


@pytest.fixture
def tls_scraper(monkeypatch: pytest.MonkeyPatch) -> tuple[Scraper, _Spies]:
    async def loopback(url: str) -> frozenset[str]:
        return frozenset({"127.0.0.1"})

    monkeypatch.setattr(scraper_mod.ssrf, "resolve_and_check", loopback)
    # Real TLS verification (system/certifi trust store): the self-signed cert must fail.
    client = httpx.AsyncClient(verify=True)
    s = Scraper(make_settings(), client=client)
    spies = _Spies()

    async def allowed(*a: object, **k: object) -> bool:
        return True

    async def fake_crawl4ai(url: str, timeout_s: float, *, original_url: str) -> PageContent:
        spies.crawl4ai += 1
        return PageContent(
            url=original_url,
            final_url=url,
            title="via crawl4ai",
            markdown="# escalated content\n\n" + "body " * 50,
            extractor_used="crawl4ai",
            fetched_at=dt.datetime.now(dt.UTC),
        )

    async def fake_jina(url: str, timeout_s: float, *, original_url: str) -> PageContent | None:
        spies.jina += 1
        return None

    monkeypatch.setattr(s._robots, "allowed", allowed)
    monkeypatch.setattr(s, "_crawl4ai", fake_crawl4ai)
    monkeypatch.setattr(s, "_jina", fake_jina)
    return s, spies


async def test_self_signed_is_refused_and_never_escalated(https_server, tls_scraper) -> None:
    s, spies = tls_scraper
    res = await s.scrape(f"https://{HOST}:{https_server}/page", timeout_s=5)
    assert isinstance(res, PageFailure)
    assert res.error == TLS_VERIFY_FAILED
    assert spies.crawl4ai == 0 and spies.jina == 0
    # Not negative-cached: an explicit opt-in right after must still be honoured.
    res2 = await s.scrape(
        f"https://{HOST}:{https_server}/page", timeout_s=5, allow_insecure_escalation=True
    )
    assert isinstance(res2, PageContent)
    assert res2.insecure_escalation is True
    assert spies.crawl4ai == 1


async def test_opt_in_escalates_and_is_flagged(https_server, tls_scraper) -> None:
    s, _spies = tls_scraper
    url = f"https://{HOST}:{https_server}/page"
    res = await s.scrape(url, timeout_s=5, allow_insecure_escalation=True)
    assert isinstance(res, PageContent)
    assert res.insecure_escalation is True and res.extractor_used == "crawl4ai"
    # Insecure content is never served to a later default (verified-only) caller.
    again = await s.scrape(url, timeout_s=5)
    assert isinstance(again, PageFailure) and again.error == TLS_VERIFY_FAILED


async def test_eof_drop_still_escalates() -> None:
    """Regression: anti-bot middleboxes that reset the TLS handshake (EOF)
    keep the crawl4ai/Jina fallback."""
    calls = {"jina": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        raise httpx.ConnectError("EOF occurred in violation of protocol") from eof

    s = Scraper(make_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def fake_crawl4ai(url, timeout_s, *, original_url):
        return None

    async def fake_jina(url, timeout_s, *, original_url):
        calls["jina"] += 1
        return PageContent(
            url=original_url,
            final_url=url,
            title="via jina",
            markdown="# jina\n\n" + "words " * 60,
            extractor_used="jina",
            fetched_at=dt.datetime.now(dt.UTC),
        )

    s._crawl4ai = fake_crawl4ai  # type: ignore[method-assign]
    s._jina = fake_jina  # type: ignore[method-assign]
    res = await s.scrape("https://botwall.example.com/a", timeout_s=5)
    assert isinstance(res, PageContent)
    assert res.extractor_used == "jina" and res.insecure_escalation is False
    assert calls["jina"] == 1


# --------------------------------------------------------------------------------------
# /v1/scrape contract
# --------------------------------------------------------------------------------------


class _FakeScraper:
    def __init__(self, mapping: dict[str, PageContent | PageFailure]):
        self.mapping = mapping
        self.flags: list[bool] = []

    async def scrape_many(self, urls, *, timeout_s=None, allow_insecure_escalation=False):
        self.flags.append(allow_insecure_escalation)
        return [self.mapping[u] for u in urls]


def _ok(url: str, insecure: bool = False) -> PageContent:
    return PageContent(
        url=url,
        final_url=url,
        title="t",
        markdown="m",
        extractor_used="jina" if insecure else "trafilatura",
        fetched_at=dt.datetime.now(dt.UTC),
        insecure_escalation=insecure,
    )


def test_route_all_refused_is_502(monkeypatch) -> None:
    bad = "https://expired.example/"
    fake = _FakeScraper({bad: PageFailure(url=bad, status="failed", error=TLS_VERIFY_FAILED)})
    monkeypatch.setattr(deps, "get_scraper", lambda: fake)
    resp = TestClient(create_app()).post("/v1/scrape", json={"url": bad})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "tls_verify_failed"
    assert fake.flags == [False]  # default call omits the opt-in kwarg


def test_route_mixed_batch_is_partial_and_flags_tls(monkeypatch) -> None:
    bad, good, plain, esc = (
        "https://expired.example/",
        "https://ok.example/",
        "http://plain.example/",
        "https://esc.example/",
    )
    fake = _FakeScraper(
        {
            bad: PageFailure(url=bad, status="failed", error=TLS_VERIFY_FAILED),
            good: _ok(good),
            plain: _ok(plain),
            esc: _ok(esc, insecure=True),
        }
    )
    monkeypatch.setattr(deps, "get_scraper", lambda: fake)
    resp = TestClient(create_app()).post(
        "/v1/scrape", json={"urls": [bad, good, plain, esc], "allow_insecure_escalation": True}
    )
    assert resp.status_code == 200, resp.text
    by = {i["url"]: i for i in resp.json()}
    assert by[bad]["error"] == "tls_verify_failed" and "tls_verified" not in by[bad]
    assert by[good]["tls_verified"] is True
    assert by[plain]["tls_verified"] is False
    assert by[esc]["tls_verified"] is False
    assert fake.flags == [True]
