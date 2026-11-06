"""Unit coverage for the CEF-compatible Playwright headless client."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.browser import cef_client, pw_client, stealth


class _Response:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def body(self) -> bytes:
        return self._body


class _Page:
    def __init__(self, html: str, status: int, url: str) -> None:
        self._html = html
        self._status = status
        self.url = url
        self.headers: dict[str, str] = {}
        self.waited: list[int] = []

    def goto(self, url: str, **_kwargs):
        self.url = url
        return _Response(self._status, headers=self.headers)

    def wait_for_timeout(self, timeout: int) -> None:
        self.waited.append(timeout)

    def content(self) -> str:
        return self._html


class _Cdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.detached = False

    def send(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return {}

    def detach(self) -> None:
        self.detached = True


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
    monkeypatch.setattr(pw_client.profiles, "_profile_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(pw_client.profiles, "ensure_profile", lambda _site: tmp_path)


def test_public_method_signatures_match_cef_client_exactly():
    assert inspect.signature(pw_client.PlaywrightClient.fetch) == inspect.signature(
        cef_client.CefDaemon.fetch)
    assert inspect.signature(pw_client.PlaywrightClient.interact) == inspect.signature(
        cef_client.CefDaemon.interact)
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
        "retry_after_s": None,
    }
    assert context.init_scripts == [stealth.INIT_SCRIPT]
    assert context.cdp.calls[0][0] == "Network.setUserAgentOverride"
    assert "--disable-blink-features=AutomationControlled" in (
        factory.playwright.chromium.launch_kwargs or {})["args"]
    assert context.page.waited == [25]
    assert context.closed is True


@pytest.mark.parametrize(("bound_profile", "override_profile", "selected_profile"), [
    ("vendor", "", "vendor"),
    ("bound-vendor", "vendor", "vendor"),
    ("", "", "playwright-headless"),
])
def test_fetch_applies_gui_cookies_before_navigation_for_selected_profile(
        monkeypatch, tmp_path, bound_profile, override_profile, selected_profile):
    _isolated_profile(monkeypatch, tmp_path)
    from services.browser import embed

    imported = embed.import_session_cookies(selected_profile, [
        {"name": "gui_session", "value": "fresh", "domain": ".vendor.example",
         "secure": True, "httponly": True},
    ], verified=False)
    assert imported["ok"] is True
    other_cookie = {"name": "other", "value": "untouched", "domain": "other.example",
                    "path": "/"}
    other_site = bound_profile if bound_profile and bound_profile != selected_profile else "unrelated"
    pw_client.profiles.save_session_cookies(other_site, [other_cookie])
    chosen_profiles = []
    monkeypatch.setattr(
        pw_client.profiles, "ensure_profile",
        lambda site: chosen_profiles.append(site) or tmp_path / site,
    )
    context = _Context("<html>signed in</html>", 200, "https://vendor.example/")
    existing = {"name": "existing", "value": "keep", "domain": ".vendor.example"}
    cookies = [existing]
    events = []
    original_goto = context.page.goto

    def add_cookies(jar):
        events.append("add_cookies")
        cookies.extend(jar)

    def goto(url, **kwargs):
        events.append("navigate")
        assert cookies == [existing, {
            "name": "gui_session", "value": "fresh", "domain": ".vendor.example",
            "path": "/", "secure": True, "httpOnly": True, "expires": -1,
        }]
        return original_goto(url, **kwargs)

    context.add_cookies = add_cookies
    context.page.goto = goto
    factory = _factory(context)
    result = pw_client.PlaywrightClient(bound_profile, sync_playwright=factory).fetch(
        "https://vendor.example/", settle_ms=0, profile=override_profile)

    assert result["ok"] is True
    assert events == ["add_cookies", "navigate"]
    assert chosen_profiles == [selected_profile]
    assert factory.playwright.chromium.launch_kwargs["user_data_dir"] == str(
        tmp_path / selected_profile)
    assert not pw_client.profiles.has_pending_session_cookies(selected_profile)
    assert pw_client.profiles.consume_session_cookies(other_site) == [other_cookie]
    assert context.closed is True


@pytest.mark.parametrize(("status", "html", "reason", "kind"), [
    (200, "<html>Just a moment...</html>", "captcha", "captcha"),
    (403, "<html>verify you are human</html>", "captcha", "captcha"),
    (200, "<html>two-factor authentication</html>", "blocked", "2fa"),
    (401, "<html>Unauthorized</html>", "login_required", None),
    (403, "<html>Access Denied</html>", "blocked", None),
    (404, "<html>Not Found</html>", "not_found", None),
    (429, "<html>Too Many Requests</html>", "rate_limited", None),
    (503, "<html>Service Unavailable</html>", "transport_failed", None),
])
def test_fetch_preserves_failure_type_and_retry_after(
        monkeypatch, tmp_path, status, html, reason, kind):
    _isolated_profile(monkeypatch, tmp_path)
    url = "https://vendor.example/product"
    context = _Context(html, status, url)
    context.page.headers = {"ReTrY-AfTeR": "17"}
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).fetch(url, settle_ms=0)

    assert result["ok"] is False
    assert result["reason"] == reason
    assert result.get("challenge_kind") == kind
    assert result["status"] == status
    assert result["final_url"] == url
    assert result["retry_after_s"] == 17.0
    if kind == "2fa":
        assert "Two-factor" in result["hint"]
    assert "html" not in result
    assert context.closed is True


@pytest.mark.parametrize(("header", "expected"), [
    ("not a date", None),
    ("-1", None),
    ("Wed, 01 Jan 2020 00:00:00 GMT", 0.0),
])
def test_fetch_retry_after_invalid_or_expired(monkeypatch, tmp_path, header, expected):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("<html>Too Many Requests</html>", 429, "https://vendor.example/")
    context.page.headers = {"retry-after": header}
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).fetch(
            "https://vendor.example/", settle_ms=0)
    assert result["reason"] == "rate_limited"
    assert result["retry_after_s"] == expected


@pytest.mark.parametrize(("bound_profile", "override_profile", "selected_profile"), [
    ("vendor", "", "vendor"),
    ("bound-vendor", "vendor", "vendor"),
    ("", "", "playwright-headless"),
])
def test_interact_uses_staged_selected_profile_and_returns_sentinel_dom(
        monkeypatch, tmp_path, bound_profile, override_profile, selected_profile):
    _isolated_profile(monkeypatch, tmp_path)
    jar = [{"name": "gui_session", "value": "fresh", "domain": ".vendor.example", "path": "/"}]
    pw_client.profiles.save_session_cookies(selected_profile, jar)
    other_site = bound_profile if bound_profile and bound_profile != selected_profile else "unrelated"
    pw_client.profiles.save_session_cookies(other_site, [
        {"name": "other", "value": "keep", "domain": "other.example", "path": "/"}])
    chosen_profiles = []
    monkeypatch.setattr(
        pw_client.profiles, "ensure_profile",
        lambda site: chosen_profiles.append(site) or tmp_path / site,
    )
    url = "https://vendor.example/start"
    final_url = "https://vendor.example/product"
    sentinel = "capture:vendor"
    script = "fetch('/api', {credentials:'include'}).then(() => {});"
    final_html = '<html><div id="capture:vendor">real response</div></html>'
    context = _Context("<html>product</html>", 200, url)
    context.page.headers = {"retry-after": "7"}
    existing = {"name": "existing", "value": "keep", "domain": ".vendor.example"}
    cookies = [existing]
    events = []
    original_goto = context.page.goto
    original_settle = context.page.wait_for_timeout
    original_send = context.cdp.send

    def add_cookies(staged):
        events.append("cookies")
        cookies.extend(staged)

    def goto(requested_url, **kwargs):
        events.append("navigate")
        assert cookies == [existing, *jar]
        assert kwargs["wait_until"] == "domcontentloaded"
        response = original_goto(requested_url, **kwargs)
        context.page.url = final_url
        return response

    def settle(ms):
        events.append("settle")
        original_settle(ms)

    def send(method, params):
        response = original_send(method, params)
        if method == "Runtime.evaluate":
            events.append("script")
            assert params["expression"] == script
            assert params["awaitPromise"] is False
            assert params["timeout"] == 1500
        return response

    def wait_for_function(expression, *, arg, timeout):
        events.append("sentinel")
        assert "document.getElementById" in expression
        assert arg == sentinel
        assert timeout == 1500
        context.page._html = final_html

    context.add_cookies = add_cookies
    context.page.goto = goto
    context.page.wait_for_timeout = settle
    context.page.wait_for_function = wait_for_function
    context.cdp.send = send
    factory = _factory(context)
    result = pw_client.PlaywrightClient(bound_profile, sync_playwright=factory).interact(
        url, script, sentinel=sentinel, settle_ms=40, timeout_ms=1500, profile=override_profile)

    assert result == {"ok": True, "status": 200, "final_url": final_url,
                      "html": final_html, "retry_after_s": 7.0}
    assert events == ["cookies", "navigate", "settle", "script", "sentinel"]
    assert context.page.waited == [40]
    assert context.init_scripts == [stealth.INIT_SCRIPT]
    assert context.cdp.calls[0][0] == "Network.setUserAgentOverride"
    assert context.cdp.detached is True and context.closed is True
    assert chosen_profiles == [selected_profile]
    assert factory.playwright.chromium.launch_kwargs["user_data_dir"] == str(
        tmp_path / selected_profile)
    assert not pw_client.profiles.has_pending_session_cookies(selected_profile)
    assert pw_client.profiles.has_pending_session_cookies(other_site)


@pytest.mark.parametrize(("status", "html", "reason", "kind"), [
    (200, "<html>Just a moment...</html>", "captcha", "captcha"),
    (200, "<html>two-factor authentication</html>", "blocked", "2fa"),
    (401, "<html>Unauthorized</html>", "login_required", None),
    (403, "<html>Access Denied</html>", "blocked", None),
    (429, "<html>Too Many Requests</html>", "rate_limited", None),
    (503, "<html>Service Unavailable</html>", "transport_failed", None),
])
def test_interact_never_runs_script_on_challenge_or_http_error(
        monkeypatch, tmp_path, status, html, reason, kind):
    _isolated_profile(monkeypatch, tmp_path)
    url = "https://vendor.example/product"
    context = _Context(html, status, url)
    context.page.headers = {"Retry-After": "11"}
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).interact(
            url, "window.should_not_run = true;", settle_ms=0)
    assert result["ok"] is False
    assert result["reason"] == reason
    assert result.get("challenge_kind") == kind
    assert result["status"] == status and result["final_url"] == url
    assert result["retry_after_s"] == 11.0
    assert all(method != "Runtime.evaluate" for method, _params in context.cdp.calls)
    assert context.closed is True


@pytest.mark.parametrize(("timeout_ms", "expected"), [(1250, 1250), (0, 12000), (-1, 12000)])
def test_interact_sentinel_timeout_returns_real_current_dom(monkeypatch, tmp_path, timeout_ms, expected):
    _isolated_profile(monkeypatch, tmp_path)
    html = "<html><body>no result yet</body></html>"
    context = _Context(html, 200, "https://vendor.example/")
    waits = []

    def no_sentinel(expression, *, arg, timeout):
        waits.append((expression, arg, timeout))
        raise TimeoutError("sentinel wait timed out")

    context.page.wait_for_function = no_sentinel
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).interact(
            "https://vendor.example/", "run();", sentinel="capture",
            settle_ms=0, timeout_ms=timeout_ms)
    assert result["ok"] is True
    assert result["html"] == html
    assert waits == [("(id) => id !== '' && !!document.getElementById(id)", "capture", expected)]
    assert context.cdp.detached is True and context.closed is True


def test_interact_navigation_timeout_never_runs_script(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("", 200, "https://vendor.example/")

    def timeout(*_args, **_kwargs):
        raise TimeoutError("navigation timed out")

    context.page.goto = timeout
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).interact(
            "https://vendor.example/", "run();", settle_ms=0)
    assert result["ok"] is False and result["reason"] == "timeout"
    assert result["status"] is None and result["final_url"] == "https://vendor.example/"
    assert all(method != "Runtime.evaluate" for method, _params in context.cdp.calls)
    assert context.closed is True


def test_interact_transport_budget_caps_sentinel_wait(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("<html>product</html>", 200, "https://vendor.example/")

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

    monkeypatch.setattr(pw_client, "time", Clock)

    def no_sentinel(_expression, *, arg, timeout):
        assert arg == "__aicad_capture" and timeout == 2000
        Clock.now = 2.0
        raise TimeoutError("sentinel wait timed out")

    context.page.wait_for_function = no_sentinel
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).interact(
            "https://vendor.example/", "run();", settle_ms=0,
            timeout_ms=12000, timeout_s=2.0)
    assert result["ok"] is False and result["reason"] == "timeout"
    assert result["status"] == 200 and result["final_url"] == "https://vendor.example/"
    assert context.cdp.detached is True and context.closed is True


def test_interact_rejects_expired_transport_budget_without_launch(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)

    def unexpected_factory():
        pytest.fail("an expired interaction must not start a browser")

    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=unexpected_factory).interact(
            "https://vendor.example/", "run();", timeout_s=0)
    assert result["ok"] is False and result["reason"] == "timeout"


def test_interact_waits_for_async_result_after_script_error(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("<html>product</html>", 200, "https://vendor.example/")
    original_send = context.cdp.send

    def send(method, params):
        response = original_send(method, params)
        if method == "Runtime.evaluate":
            return {"exceptionDetails": {"exception": {"description": "ReferenceError: missing"}}}
        return response

    def publish_sentinel(_expression, *, arg, timeout):
        assert arg == "__aicad_capture" and timeout == 12000
        context.page._html = '<html><div id="__aicad_capture">result</div></html>'

    context.page.wait_for_function = publish_sentinel
    context.cdp.send = send
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).interact(
            "https://vendor.example/", "bad_script();", settle_ms=0)
    assert result["ok"] is True
    assert result["html"] == '<html><div id="__aicad_capture">result</div></html>'
    assert result["script_error"] == "ReferenceError: missing"
    assert context.cdp.detached is True and context.closed is True


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
    assert result["final_url"] == "https://vendor.example/part.step"
    assert result["retry_after_s"] is None
    assert result["bytes"] == len(b"STEP payload")
    assert dest.read_bytes() == b"STEP payload"
    assert calls == [("https://vendor.example/part.step", {"Cookie": "session=fixture"})]
    assert context.request.calls == []


@pytest.mark.parametrize(("status", "body", "reason", "kind"), [
    (401, b"Unauthorized", "login_required", None),
    (403, b"Access Denied", "blocked", None),
    (403, b"verify you are human", "captcha", "captcha"),
    (403, b"two-factor authentication", "blocked", "2fa"),
    (404, b"Not Found", "not_found", None),
    (429, b"Too Many Requests", "rate_limited", None),
    (503, b"Service Unavailable", "transport_failed", None),
])
def test_download_preserves_failure_details_and_existing_destination(
        monkeypatch, tmp_path, status, body, reason, kind):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("", status, "https://vendor.example/")
    dest = tmp_path / "part.step"
    dest.write_bytes(b"existing model")
    final_url = "https://cdn.vendor.example/redirected.step"
    from services.net import safe_http
    monkeypatch.setattr(
        safe_http, "download",
        lambda *_args, **_kwargs: safe_http.Response(
            status, {"retry-after": "25"}, final_url, content=body),
    )
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).download(
            "https://vendor.example/part.step", str(dest))

    assert result["ok"] is False
    assert result["reason"] == reason
    assert result.get("challenge_kind") == kind
    assert result["status"] == status
    assert result["final_url"] == final_url
    assert result["retry_after_s"] == 25.0
    if kind == "2fa":
        assert "Two-factor" in result["hint"]
    assert dest.read_bytes() == b"existing model"
    assert context.closed is True


def test_download_http_date_retry_after(monkeypatch, tmp_path):
    _isolated_profile(monkeypatch, tmp_path)
    context = _Context("", 503, "https://vendor.example/")
    from services.net import safe_http

    class FrozenDatetime:
        @staticmethod
        def now(tz):
            assert tz == timezone.utc
            return datetime(2020, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(pw_client, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        safe_http, "download",
        lambda *_args, **_kwargs: safe_http.Response(
            503, {"Retry-After": "Wed, 01 Jan 2020 00:02:00 GMT"},
            "https://cdn.vendor.example/file"),
    )
    result = pw_client.PlaywrightClient(
        "vendor", sync_playwright=_factory(context)).download(
            "https://vendor.example/file", str(tmp_path / "file"))
    assert result["reason"] == "transport_failed"
    assert result["status"] == 503
    assert result["final_url"] == "https://cdn.vendor.example/file"
    assert result["retry_after_s"] == 120.0
    assert not (tmp_path / "file").exists()
