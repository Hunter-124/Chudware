"""MakerWorld model discovery and session-backed downloads."""
from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from services.browser.cad_validate import classify_artifact, extract_cad, validate_cad

from .. import challenge, profiles, session
from . import (abs_url, download_path, fail, login_required, make_hit,
               model_selection_question, provenance_envelope, quote_query, register, source_failure)
from ._retail import card_image

SITE = "makerworld"
BASE_URL = "https://makerworld.com"
# MakerWorld offers sign-in from the header of its home page.
LOGIN_URL = BASE_URL + "/en"
_MODEL = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)(?:-[^/]*)?/?$", re.I)
_CARD = re.compile(r"<a\b[^>]*\bhref=[\"'](?P<href>[^\"']+)[\"'][^>]*>.*?</a\s*>", re.I | re.S)
_TITLE = re.compile(r"\b(?:title|aria-label|alt)=[\"']([^\"']+)[\"']", re.I)
_AVATAR_IMAGE = re.compile(
    r'<img\b(?=[^>]*(?:\bclass\s*=\s*["\'][^"\']*\bavatar\b|/avatars?/))[^>]*>', re.I)
_FORMATS = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges",
            ".stl": "stl", ".3mf": "3mf", ".obj": "obj", ".zip": "zip"}
_ALIASES = {"stp": "step", "igs": "iges"}


class _SearchCards(HTMLParser):
    """Keep each MakerWorld card together when its image and title are siblings."""
    _void = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.cards: list[str] = []
        self._parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if not self._depth:
            classes = dict(attrs).get("class") or ""
            if "js-design-card" not in classes.split():
                return
        self._parts.append(self.get_starttag_text())
        if tag.lower() not in self._void:
            self._depth += 1

    def handle_startendtag(self, tag, attrs):
        if self._depth:
            self._parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self._depth:
            return
        self._parts.append(f"</{tag}>")
        if tag.lower() not in self._void:
            self._depth -= 1
        if not self._depth:
            self.cards.append("".join(self._parts))
            self._parts = []

    def handle_data(self, data):
        if self._depth:
            self._parts.append(data)

    def handle_entityref(self, name):
        if self._depth:
            self._parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._depth:
            self._parts.append(f"&#{name};")


def _format(value: str) -> str:
    value = str(value or "").strip().lower().lstrip(".")
    return _ALIASES.get(value, value)


def _json_content(content: str) -> dict | None:
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        match = re.search(r"<pre\b[^>]*>(.*?)</pre\s*>", content or "", re.I | re.S)
        if not match:
            return None
        try:
            value = json.loads(html.unescape(match.group(1)))
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _login_failure(url: str, response: dict | None = None) -> dict:
    prompt = login_required(SITE, url)
    if response:
        return {**response, "ask_user": response.get("ask_user") or prompt["ask_user"]}
    return fail(SITE, "acquire", "login_required", prompt["hint"], url=url,
                ask_user=prompt["ask_user"])


def _download_host_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and parsed.username is None and parsed.password is None
                and (host in {"makerworld.com", "www.makerworld.com"}
                     or any(host.endswith("." + domain) for domain in
                            ("bblmw.com", "bambulab.com", "amazonaws.com"))))
    except ValueError:
        return False


def _instance_download_url(url: str, instance_id: str) -> tuple[str | None, dict | None]:
    """Ask MakerWorld's web endpoint in the signed-in page; never solve its challenges."""
    if not re.fullmatch(r"\d+", instance_id):
        return None, fail(SITE, "acquire", "parse_failed", "Invalid MakerWorld instance ID.", url=url)
    endpoint = f"/api/v1/design-service/instance/{instance_id}/f3mf?type=download"
    script = (
        "(function(){function publish(x){var d=document.createElement('div');"
        "d.id='__makerworld_capture';d.textContent=JSON.stringify(x);"
        "document.documentElement.appendChild(d);}"
        "fetch(" + json.dumps(endpoint) + ",{credentials:'include',headers:{"
        "'X-BBL-Client-Type':'web','X-BBL-Client-Version':'00.00.00.01',"
        "'X-BBL-App-Source':'makerworld','X-BBL-Client-Name':'MakerWorld'}})"
        ".then(async function(r){var data;try{data=await r.json();}catch(e){data=null;}"
        "publish({status:r.status,data:data});})"
        ".catch(function(e){publish({error:String(e)});});})();"
    )
    response = session.browser_interact(url, SITE, script, sentinel="__makerworld_capture")
    if not response.get("ok"):
        if response.get("reason") == "login_required":
            return None, _login_failure(url, response)
        return None, response
    capture = re.search(r'<div\b[^>]*\bid=["\']__makerworld_capture["\'][^>]*>(.*?)</div\s*>',
                        response.get("content") or "", re.I | re.S)
    if not capture:
        if challenge.is_login_page(response.get("content"), response.get("final_url") or url):
            return None, _login_failure(url)
        return None, fail(SITE, "acquire", "parse_failed", "Missing MakerWorld download response.", url=url)
    data = _json_content(html.unescape(capture.group(1)))
    if not data:
        return None, fail(SITE, "acquire", "parse_failed", "Invalid MakerWorld download response.", url=url)
    if data.get("error"):
        return None, fail(SITE, "acquire", "transport_failed", str(data["error"]), url=url)
    status = data.get("status")
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    message = str(body.get("error") or body.get("message") or "")
    if status == 418 or body.get("captchaId"):
        return None, fail(SITE, "acquire", "captcha", "MakerWorld requires a captcha.", url=url, status=status)
    if status == 401:
        return None, _login_failure(url, fail(SITE, "acquire", "login_required",
                                               "MakerWorld requires sign-in.", url=url, status=status))
    if status == 403:
        return None, fail(SITE, "acquire", "blocked", message or "MakerWorld blocked the download.", url=url, status=status)
    if status == 429:
        return None, fail(SITE, "acquire", "rate_limited", message or "MakerWorld rate-limited the download.", url=url, status=status)
    if status in (404, 410):
        return None, fail(SITE, "acquire", "not_found", message or "MakerWorld profile not found.", url=url, status=status)
    if not isinstance(status, int) or status < 200 or status >= 300:
        return None, fail(SITE, "acquire", "transport_failed", message or "MakerWorld download request failed.", url=url, status=status)
    if re.search(r"\b(?:log ?in|sign ?in|unauthenticated)\b", message, re.I):
        if not profiles.has_login(SITE):
            return None, _login_failure(url)
        return None, fail(SITE, "acquire", "blocked", message, url=url)
    direct = body.get("url")
    if not isinstance(direct, str) or not direct:
        return None, fail(SITE, "acquire", "parse_failed", message or "MakerWorld did not return a download URL.", url=url)
    return direct, None


def _model_files(entries):
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("isDir"):
            yield from _model_files(item.get("children"))
        elif item.get("modelName"):
            yield item


class MakerWorldAdapter:
    key = SITE
    hosts = ("makerworld.com",)
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = True
    api = True

    def search(self, query: str) -> list[dict]:
        url = BASE_URL + "/en/search/models?keyword=" + quote_query(query)
        response = session.browser_fetch(url, SITE)
        if not response.get("ok"):
            return [response]
        content = response.get("content") or ""
        cards = _SearchCards()
        cards.feed(content)
        hits: dict[str, dict] = {}
        for fragment in cards.cards or [content]:
            # Creator avatars are not previews of the model.
            image_html = _AVATAR_IMAGE.sub("", fragment)
            card_thumbnail = card_image(image_html, response.get("final_url") or BASE_URL)
            for match in _CARD.finditer(fragment):
                href = abs_url(BASE_URL, html.unescape(match.group("href")))
                if not href:
                    continue
                parsed = urlparse(href)
                model = _MODEL.fullmatch(parsed.path) if parsed.hostname in {"makerworld.com", "www.makerworld.com"} else None
                if not model:
                    continue
                card = match.group(0)
                image = card_image(_AVATAR_IMAGE.sub("", card), response.get("final_url") or BASE_URL)
                if not image and cards.cards:
                    image = card_thumbnail
                text = html.unescape(re.sub(r"<[^>]+>", " ", card))
                text = " ".join(text.split())
                title_match = _TITLE.search(card)
                title = (html.unescape(title_match.group(1)).strip() if title_match else text)
                if not title:
                    title = parsed.path.rsplit("/", 1)[-1].replace("-", " ")
                model_id = model.group(1)
                if model_id not in hits:
                    hits[model_id] = make_hit(SITE, title=title, url=href, kind="model",
                                              thumbnail=image, extra={"model_id": model_id})
                elif image and not hits[model_id]["thumbnail"]:
                    hits[model_id]["thumbnail"] = image
        if hits:
            return list(hits.values())
        if challenge.is_login_page("", response.get("final_url") or ""):
            prompt = login_required(SITE, url)
            return [fail(SITE, "search", "login_required", prompt["hint"], url=url,
                         ask_user=prompt["ask_user"])]
        if re.search(r"\b(?:no results|no models found|0 results)\b", content, re.I):
            return []
        return [fail(SITE, "search", "parse_failed", "No recognizable MakerWorld model cards.", url=url)]

    def acquire(self, result: dict) -> dict:
        url = str(result.get("url") or "")
        try:
            parsed = urlparse(url)
            match = _MODEL.fullmatch(parsed.path) if parsed.scheme == "https" and parsed.hostname in {
                "makerworld.com", "www.makerworld.com"} and parsed.username is None and parsed.password is None else None
        except ValueError:
            match = None
        if not match:
            return fail(SITE, "acquire", "unsupported", "Expected a MakerWorld model page.", url=url)
        model_id = match.group(1)
        metadata_url = f"{BASE_URL}/api/v1/design-service/design/{model_id}"
        response = session.browser_fetch(metadata_url, SITE, no_cache=True)
        if not response.get("ok"):
            return _login_failure(url, response) if response.get("reason") == "login_required" else response
        metadata = _json_content(response.get("content") or "")
        if not metadata:
            if challenge.is_login_page(response.get("content"), response.get("final_url") or metadata_url):
                return _login_failure(url)
            return fail(SITE, "acquire", "parse_failed", "Missing MakerWorld design metadata.", url=metadata_url)
        if str(metadata.get("id") or "") != model_id:
            return fail(SITE, "acquire", "parse_failed", "MakerWorld returned a different design.", url=metadata_url)

        requested = _format(result.get("format") or result.get("requested_format") or "")
        file_id = str(result.get("file_id") or "")
        file_name = str(result.get("file_name") or "")
        archive_member = result.get("archive_member")
        if archive_member is not None and (not isinstance(archive_member, str) or not archive_member):
            return fail(SITE, "acquire", "unsupported", "Specify a valid archive member.", url=url)
        if requested == "zip" and archive_member:
            return fail(SITE, "acquire", "unsupported", "Choose either an archive or a specific member.", url=url)
        instance_id = str(result.get("instance_id") or "")
        profile_id = str(result.get("profile_id") or "")
        fragment = re.fullmatch(r"profileId-(\d+)", parsed.fragment, re.I)
        fragment_id = fragment.group(1) if fragment else ""
        if (profile_id or instance_id or fragment_id) and (file_id or file_name or archive_member or (requested and requested != "3mf")):
            return fail(SITE, "acquire", "unsupported", "Choose either a print profile or a raw model file.", url=url)
        extension = metadata.get("designExtension") or {}
        file_list = list(_model_files(extension.get("model_files") if isinstance(extension, dict) else []))
        raw_candidates = [item for item in file_list
                          if _FORMATS.get(Path(str(item.get("modelName") or "")).suffix.lower())]
        selected = [item for item in raw_candidates
                    if (not file_id or str(item.get("unikey") or item.get("id") or "") == file_id)
                    and (not file_name or item.get("modelName") == file_name)
                    and (not requested or _FORMATS[Path(item["modelName"]).suffix.lower()] in {requested, "zip"})
                    and (not archive_member or Path(item["modelName"]).suffix.lower() == ".zip")]
        if requested and not file_id and not file_name and not archive_member:
            exact = [item for item in selected
                     if _FORMATS[Path(item["modelName"]).suffix.lower()] == requested]
            if exact:
                selected = exact
        direct = None
        chosen = None
        if file_id or file_name or archive_member or (requested and requested != "3mf"):
            if not selected:
                return {**fail(SITE, "acquire", "not_found", "Requested model file or format was not listed.", url=url),
                        "candidates": raw_candidates}
            if len(selected) != 1:
                identities = [str(item.get("unikey") or item.get("id") or "") for item in selected]
                field = "file_id"
                if not all(identities) or len(set(identities)) != len(identities) or "cancel" in identities:
                    identities = [str(item["modelName"]) for item in selected]
                    field = "file_name"
                question = None
                if len(set(identities)) == len(identities) and "cancel" not in identities:
                    question = model_selection_question(SITE, url, identities)
                    for option, item in zip(question["options"][1:], selected):
                        option["label"] = f"{item['modelName']} ({option['value']})"
                hint = ("Select a specific model file before downloading." if question else
                        "The listed model files cannot be uniquely identified by ID or name.")
                failure = fail(SITE, "acquire", "unsupported", hint, url=url, ask_user=question)
                failure["candidates"] = selected
                if question:
                    failure["selection_field"] = field
                return failure
            chosen = selected[0]
            direct = chosen.get("modelUrl")
            if not direct:
                return {**fail(SITE, "acquire", "unsupported", "MakerWorld did not provide a direct URL for the selected raw file.", url=url),
                        "candidates": selected}
            filename = f"{model_id}-{chosen['modelName']}"
        else:
            instances = metadata.get("instances")
            if not isinstance(instances, list):
                return fail(SITE, "acquire", "parse_failed", "Missing MakerWorld print profiles.", url=metadata_url)
            candidates = [item for item in instances if isinstance(item, dict) and item.get("id")]
            selected_instances = [item for item in candidates
                                  if (not instance_id or str(item.get("id")) == instance_id)
                                  and (not profile_id or profile_id in {str(item.get("id")), str(item.get("profileId"))})
                                  and (not fragment_id or fragment_id in {str(item.get("id")), str(item.get("profileId"))})]
            if not selected_instances:
                return {**fail(SITE, "acquire", "not_found", "No matching MakerWorld print profile.", url=url),
                        "candidates": candidates}
            if len(selected_instances) != 1:
                identities = [str(item["id"]) for item in selected_instances]
                question = None
                if (len(set(identities)) == len(identities) and
                        all(re.fullmatch(r"\d+", identity) for identity in identities)):
                    question = model_selection_question(SITE, url, identities)
                    for option, item in zip(question["options"][1:], selected_instances):
                        option["label"] = f"{item.get('title') or 'Print profile'} (instance {item['id']})"
                hint = ("Select a specific MakerWorld print profile." if question else
                        "The listed print profiles cannot be uniquely identified by instance ID.")
                failure = fail(SITE, "acquire", "unsupported", hint, url=url, ask_user=question)
                failure["candidates"] = selected_instances
                if question:
                    failure["selection_field"] = "instance_id"
                return failure
            chosen = selected_instances[0]
            instance_id = str(chosen["id"])
            direct, error = _instance_download_url(url, instance_id)
            if error:
                return error
            filename = f"{model_id}-{instance_id}.3mf"
        if not isinstance(direct, str) or not _download_host_allowed(direct):
            return fail(SITE, "acquire", "policy_refused", "Unexpected MakerWorld download host.", url=url)
        dest, refusal = download_path(SITE, direct, filename=filename)
        if refusal:
            return {**source_failure(SITE, "acquire", refusal, url=url), "reason": "policy_refused",
                    "original_reason": refusal.get("reason")}
        suffix = Path(filename).suffix.lower()
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".makerworld-", suffix=suffix, delete=False) as temp:
                staged = Path(temp.name)
            outcome = session.browser_download(direct, SITE, staged, public_only=True)
            if not outcome.get("ok"):
                return _login_failure(url, outcome) if outcome.get("reason") == "login_required" else outcome
            if requested == "zip":
                checked = classify_artifact(staged, kind="archive")
            else:
                checked = validate_cad(staged, requested_format=requested or None,
                                       requested_member=archive_member)
            if not checked["ok"]:
                candidates = checked.get("candidates") or []
                question = model_selection_question(SITE, url, candidates) if candidates else None
                failure = fail(SITE, "acquire", "unsupported", checked["reason"], url=direct, ask_user=question)
                if candidates:
                    failure["candidates"] = candidates
                    failure["selection_field"] = "archive_member"
                return failure
            if requested and suffix != ".zip" and checked["format"] != requested:
                return fail(SITE, "acquire", "unsupported", "Requested CAD format is unavailable.", url=url)
            os.replace(staged, dest)
            if requested == "zip":
                checked = {"ok": True, "artifact_kind": "archive", "format": "zip",
                           "artifact_path": str(dest), "member": checked.get("member")}
            else:
                checked = extract_cad(dest, requested_format=requested or None,
                                      requested_member=archive_member)
                if not checked["ok"]:
                    candidates = checked.get("candidates") or []
                    question = model_selection_question(SITE, url, candidates) if candidates else None
                    failure = fail(SITE, "acquire", "unsupported", checked["reason"], url=direct, ask_user=question)
                    if candidates:
                        failure["candidates"] = candidates
                        failure["selection_field"] = "archive_member"
                    return failure
            return provenance_envelope(source_url=url, artifact_path=checked["artifact_path"],
                                       artifact_kind=checked["artifact_kind"], format=checked["format"],
                                       archive_path=checked.get("archive_path"), member=checked.get("member"),
                                       session_profile_used=outcome.get("session_profile_used", SITE),
                                       license_note=metadata.get("license") or "Unknown",
                                       title=metadata.get("title") or result.get("title", ""),
                                       raw_url=direct, model_id=model_id, file=chosen)
        except OSError as exc:
            return fail(SITE, "acquire", "transport_failed", str(exc), url=direct)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)


ADAPTER = register(MakerWorldAdapter())
