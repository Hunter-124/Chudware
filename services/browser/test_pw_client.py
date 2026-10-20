"""Unit coverage for the CEF-compatible Playwright headless client."""
from __future__ import annotations

import inspect
from pathlib import Path

from services.browser import cef_client, pw_client, stealth


class _Response:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def body(self) -> bytes:
        return self._body


class _Page:
    def __init__(self, html: str, status: int, url: str) -> None:
        self._html = html
        self._status = status
        self.url = url
        self.waited: list[int] = []

    def goto(self, url: str, **_kwargs):
        self.url = url
        return _Response(self._status)

    def wait_for_timeout(self, timeout: int) -> None:
        self.waited.append(timeout)

    def content(self) -> str:
        return self._html


class _Cdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def send(self, method: str, params: dict) -> None:
        self.calls.append((method, params))


class _Request:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class _Context:
    def __init__(self, html: str, status: int, url: str,
                 download: _Response | None = None) -> None:
        self.pages: list[_Page] = []
        self.page = _Page(html, status, url)
        self.cdp = _Cdp()
        self.init_scripts: list[str] = []
        self.request = _Request(download or _Response(200, b"cad-data"))
        self.closed = False

    def new_page(self) -> _Page:
        self.pages.append(self.page)
        return self.page

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def new_cdp_session(self, _page: _Page) -> _Cdp:
        return self.cdp

    def close(self) -> None:
        self.closed = True

    def cookies(self, urls):
        return [{"name": "session", "value": "fixture"}]


class _VersionBrowser:
    version = "149.0.7827.55"

    def close(self) -> None:
        pass


class _Chromium:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.launch_kwargs: dict | None = None

    def launch(self, **_kwargs) -> _VersionBrowser:
        return _VersionBrowser()

    def launch_persistent_context(self, **kwargs) -> _Context:
        self.launch_kwargs = kwargs
        return self.context


class _Playwright:
    def __init__(self, context: _Context) -> None:
        self.chromium = _Chromium(context)


class _FactoryContext:
    def __init__(self, playwright: _Playwright) -> None:
        self.playwright = playwright

    def __enter__(self) -> _Playwright:
        return self.playwright

    def __exit__(self, *_exc) -> bool:
        return False


def _factory(context: _Context):
    playwright = _Playwright(context)

    def start() -> _FactoryContext:
        return _FactoryContext(playwright)

    start.playwright = playwright
    return start


def _isolated_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pw_client.profiles, "ensure_profile", lambda _site: tmp_path)


def test_public_method_signatures_match_cef_client_exactly():
    assert inspect.signature(pw_client.PlaywrightClient.fetch) == inspect.signature(
        cef_client.CefDaemon.fetch)
    assert inspect.signature(pw_client.PlaywrightClient.download) == inspect.signature(
        cef_client.CefDaemon.download)


def test_fetch_returns_cef_shape_and_applies_existing_stealth(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("<html><title>Tolerant</title></html>", 200, "https://example.com/")
    factory = _factory(context)
    result = pw_client.PlaywrightClient("example", sync_playwright=factory).fetch(
        "https://example.com/", settle_ms=25)

    assert result == {
        "ok": True,
        "status": 200,
        "final_url": "https://example.com/",
        "html": "<html><title>Tolerant</title></html>",
    }
    assert context.init_scripts == [stealth.INIT_SCRIPT]
    assert context.cdp.calls[0][0] == "Network.setUserAgentOverride"
    assert "--disable-blink-features=AutomationControlled" in (
        factory.playwright.chromium.launch_kwargs or {})["args"]
    assert context.page.waited == [25]
    assert context.closed is True


def test_fetch_challenge_is_clean_bot_wall(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("<html><title>Just a moment...</title></html>", 200,
                       "https://vendor.example/challenge")
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).fetch(
            "https://vendor.example/challenge", settle_ms=0)

    assert result == {
        "ok": False,
        "reason": "bot_walled",
        "status": 200,
        "final_url": "https://vendor.example/challenge",
    }
    assert context.closed is True


def test_download_uses_context_cookies_and_writes_bytes(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("", 200, "https://vendor.example/",
                       download=_Response(200, b"STEP payload"))
    dest = tmp_path / "part.step"
    from services.net import safe_http
    calls = []

    def download(url, destination, **kwargs):
        calls.append((url, kwargs["headers_for_url"](url)))
        destination.write_bytes(b"STEP payload")
        return safe_http.Response(200, {}, url, bytes_written=len(b"STEP payload"))

    monkeypatch.setattr(safe_http, "download", download)
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).download(
            "https://vendor.example/part.step", str(dest))

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["bytes"] == len(b"STEP payload")
    assert dest.read_bytes() == b"STEP payload"
    assert calls == [("https://vendor.example/part.step", {"Cookie": "session=fixture"})]
    assert context.request.calls == []


def test_download_bot_wall_does_not_write_partial_file(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("", 403, "https://vendor.example/",
                       download=_Response(403, b"Access Denied"))
    dest = tmp_path / "blocked.step"
    from services.net import safe_http
    monkeypatch.setattr(safe_http, "download",
                        lambda url, *_args, **_kwargs: safe_http.Response(403, {}, url))
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).download(
            "https://vendor.example/blocked.step", str(dest))

    assert result == {"ok": False, "reason": "bot_walled", "status": 403}
    assert not dest.exists()
