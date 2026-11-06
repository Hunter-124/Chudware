"""Offline PCBWay Community project search fixtures."""
import pytest

from services.browser.adapters import pcbway


# Trimmed from the 2026-11-04 public search response. The graphic belongs to
# the project; the portrait in the same row belongs to its author, not the part.
LIVE_PROJECT_CARD = '''<div class="result-row"><div class="info-msg"><div class="result-head">
<div class="answer-info"><a href="/project/shareproject/Give_Your_iPhone_a_Pair_of_Robot_Hands_a_7_XIAO_ESP32_S3_Lets_an_AI_Agent_Use_faae0bbe.html" target="_blank" class="info-title">
Give Your iPhone a Pair of Robot Hands: a $7 XIAO <span class="tag">ESP32</span>-S3 Lets an AI Agent Use Your Phone</a>
<div class="person-row"><img class="portrait" src="/project/img/images/avatar/user-pic.png"></div></div></div></div>
<a href="/project/shareproject/Give_Your_iPhone_a_Pair_of_Robot_Hands_a_7_XIAO_ESP32_S3_Lets_an_AI_Agent_Use_faae0bbe.html" target="_blank" class="info-graphic">
<span class="graphic-pic" style="background-image: url(https://pcb-files.s3.us-west-2.amazonaws.com/web/26/11/04/022900900baa543c7944544b4821c392baeb8aba80406.png)" alt="" /></a></div>'''


def test_live_project_card_uses_project_graphic_not_author(monkeypatch):
    requested = []
    monkeypatch.setattr(pcbway._session, "browser_fetch", lambda url, site: (
        requested.append((url, site)) or {"ok": True, "content": LIVE_PROJECT_CARD}))
    hit = pcbway.ADAPTER.search("C++ & /#")[0]
    assert requested == [("https://www.pcbway.com/project/projectsearch?q=C%2B%2B+%26+%2F%23&t=1", "pcbway")]
    assert hit["kind"] == "project" and "ESP32-S3" in hit["title"]
    assert hit["sku"] is None and hit["price"] is None and hit["stock"] is None
    assert hit["thumbnail"] == "https://pcb-files.s3.us-west-2.amazonaws.com/web/26/11/04/022900900baa543c7944544b4821c392baeb8aba80406.png"


def test_author_portrait_is_not_project_thumbnail(monkeypatch):
    content = '''<div class="result-row"><a href="/project/shareproject/A.html" class="info-title">A</a>
    <img class="portrait" src="/avatar.png"></div>'''
    monkeypatch.setattr(pcbway._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    assert pcbway.ADAPTER.search("a")[0]["thumbnail"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ("Access Denied", "blocked"), ("Verify you are human", "captcha"),
    ('<div id="login-email"></div>', "login_required"), ("<main>changed markup</main>", "parse_failed"),
    ('<div style="display: none" class="empty-data">No Results Found</div><main>changed markup</main>', "parse_failed"),
])
def test_search_failure_is_not_empty(monkeypatch, content, reason):
    monkeypatch.setattr(pcbway._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    out = pcbway.ADAPTER.search("project")[0]
    assert (out["ok"], out["site"], out["op"], out["reason"]) == (False, "pcbway", "search", reason)


def test_no_results_and_no_project_file_acquisition(monkeypatch):
    # Trimmed public empty response, 2026-11-04.
    empty = '<div class="result-lists"><div class="empty-data">No results found. Please try different keywords.</div></div>'
    monkeypatch.setattr(pcbway._session, "browser_fetch", lambda *_: {"ok": True, "content": empty})
    assert pcbway.ADAPTER.search("nothing") == []
    monkeypatch.setattr(pcbway._session, "browser_fetch", lambda *_: {"ok": True, "content": "<html>project</html>"})
    result = pcbway.ADAPTER.acquire({"url": "https://www.pcbway.com/project/shareproject/A.html"})
    assert result["ok"] is False and result["reason"] == "unsupported" and not result.get("artifact_path")
    monkeypatch.setattr(pcbway._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<div class="order-terms">Accept the terms</div>'})
    assert pcbway.ADAPTER.acquire({"url": "https://www.pcbway.com/project/shareproject/A.html"})["reason"] == "terms_required"
