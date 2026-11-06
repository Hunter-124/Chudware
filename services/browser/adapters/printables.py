"""Printables search and model acquisition via its public GraphQL API."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from services.browser import profiles, ratelimit, session
from services.browser.cad_validate import classify_artifact, extract_cad
from services.net import safe_http

from . import (abs_url, download_path, download_public_cad, fail, login_required,
               make_hit, model_selection_question, provenance_envelope, register, source_failure)

SITE = "printables"
BASE_URL = "https://www.printables.com"
_API = "https://api.printables.com/graphql/"
_PAGE = BASE_URL + "/model/"
_MEDIA = "https://files.printables.com/"
_MODEL_RE = re.compile(r"^/model/(\d+)(?:-[^/]*)?(?:/files)?/?$")
_SEARCH = ("query($q:String!){searchPrints2(query:$q,limit:30,ordering:best_match)"
           "{items{id name slug license{name} image{filePath}}}}")
_DETAIL = ("query($id:ID!){print(id:$id){name slug license{name} "
           "stls{id name fileSize folder note} otherFiles{id name fileSize folder note}}}")
_LINK = ("mutation($id:ID!,$pid:ID!){getDownloadLink(id:$id,printId:$pid,"
         "fileType:stl,source:model_detail,files:[{fileType:stl,ids:[$id]}])"
         "{ok output{link} errors{field messages}}}")
_FORMATS = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges",
            ".stl": "stl", ".3mf": "3mf", ".obj": "obj", ".zip": "zip"}
_ALIASES = {"stp": "step", "igs": "iges"}


def _paced(op: str, url: str, request) -> tuple[dict | None, dict | None]:
    """Apply the browser's request ceiling, cooldown and per-site serialization."""
    profiles.ensure_profile(SITE)
    lock = profiles.profile_lock(SITE)
    if not lock.acquire(blocking=False):
        return None, fail(SITE, op, "policy_refused", "Printables profile is busy.", url=url)
    try:
        return session._request(SITE, url, op, request,
                                min_interval=ratelimit.DEFAULT_MIN_INTERVAL_S, retry=False)
    finally:
        lock.release()


def _download_archive(url: str, dest: Path) -> dict:
    """Download a requested ZIP as an archive, not as a chosen CAD member."""
    try:
        response = safe_http.download(url, dest, max_bytes=200 * 1024 * 1024,
                                      idle_timeout=30, max_redirects=6, schemes=("https",))
    except (safe_http.OutboundPolicyError, safe_http.ResponseTooLarge) as exc:
        return {"ok": False, "reason": "policy_refused", "hint": str(exc)}
    return {"ok": response.status_code < 400, "status": response.status_code,
            "final_url": response.url, "headers": response.headers, "dest": str(dest),
            "html": response.text if "html" in response.headers.get("content-type", "").lower() else ""}


def _query(query: str, variables: dict) -> dict:
    """Use the pinned, bounded HTTP transport and the browser's site pacing."""
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")

    def request() -> dict:
        try:
            response = safe_http.request(
                _API, method="POST", body=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                max_bytes=4 * 1024 * 1024, idle_timeout=20, max_redirects=3,
                schemes=("https",))
        except safe_http.OutboundPolicyError as exc:
            return {"ok": False, "reason": "policy_refused", "hint": str(exc)}
        except safe_http.ResponseTooLarge as exc:
            return {"ok": False, "reason": "unsupported", "hint": str(exc)}
        return {"ok": response.status_code < 400, "status": response.status_code,
                "final_url": response.url, "headers": response.headers,
                "html": response.text if "html" in response.headers.get("content-type", "").lower() else "",
                "content": response.content}

    response, error = _paced("api", _API, request)
    if error:
        return error
    try:
        answer = json.loads(response["content"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return fail(SITE, "api", "parse_failed", "Printables returned invalid JSON.", url=_API)
    if not isinstance(answer, dict):
        return fail(SITE, "api", "parse_failed", "Printables returned an invalid GraphQL response.", url=_API)
    if answer.get("errors"):
        return _graphql_failure(answer["errors"], "api", _API)
    return answer


def _graphql_failure(errors, op: str, url: str) -> dict:
    if not isinstance(errors, list):
        errors = [errors]
    messages = []
    codes = []
    for error in errors:
        if isinstance(error, dict):
            parts = error.get("messages") or []
            if isinstance(parts, str):
                parts = [parts]
            messages.extend(str(m) for m in parts if m)
            if error.get("message"):
                messages.append(str(error["message"]))
            extensions = error.get("extensions") or {}
            if isinstance(extensions, dict) and extensions.get("code"):
                codes.append(str(extensions["code"]))
        elif error:
            messages.append(str(error))
    hint = "; ".join(messages) or "Printables reported a GraphQL error."
    text = " ".join(codes + messages).lower().replace("_", " ")
    if "captcha" in text:
        reason = "captcha"
    elif re.search(r"\b(?:unauthenticated|login|log in|sign in|authentication required)\b", text):
        question = login_required(SITE, url)["ask_user"]
        question.update({
            "prompt": "Open Printables to sign in and continue there?",
            "why": "This Printables API request is anonymous and cannot use a saved browser sign-in.",
            "proposal": "Open Printables so you can sign in and continue on the website.",
        })
        return fail(SITE, op, "login_required", hint, url=url, ask_user=question)
    elif re.search(r"\b(?:forbidden|blocked|access denied)\b", text):
        reason = "blocked"
    elif re.search(r"\b(?:rate limit|too many requests)\b", text):
        reason = "rate_limited"
    elif re.search(r"\bnot found\b", text):
        reason = "not_found"
    else:
        reason = "parse_failed"
    return fail(SITE, op, reason, hint, url=url)


def _download_link(model_id: str, file_id: str, file_type: str = "stl") -> dict:
    query = _LINK if file_type == "stl" else _LINK.replace("fileType:stl", "fileType:other")
    return _query(query, {"id": file_id, "pid": model_id})


def _format(value: str) -> str:
    value = str(value or "").strip().lower().lstrip(".")
    return _ALIASES.get(value, value)


class PrintablesAdapter:
    key = SITE
    hosts = ("printables.com",)
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = False
    api = True

    def search(self, query: str) -> list[dict]:
        result = _query(_SEARCH, {"q": query})
        if result.get("ok") is False:
            return [source_failure(SITE, "search", result, url=_API)]
        if result.get("errors"):
            return [_graphql_failure(result["errors"], "search", _API)]
        data = result.get("data")
        search = data.get("searchPrints2") if isinstance(data, dict) else None
        if not isinstance(search, dict) or not isinstance(search.get("items"), list):
            return [fail(SITE, "search", "parse_failed", "Missing Printables search results.", url=_API)]
        hits = []
        for item in search["items"]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("slug") or not item.get("name"):
                continue
            image = item.get("image")
            image = image.get("filePath") if isinstance(image, dict) else None
            if not isinstance(image, str):
                image = None
            license_data = item.get("license")
            license_name = license_data.get("name") if isinstance(license_data, dict) else None
            url = f"{_PAGE}{item['id']}-{item['slug']}"
            hits.append(make_hit(SITE, title=str(item["name"]), url=url, kind="model",
                                 thumbnail=abs_url(_MEDIA, image), license=license_name,
                                 extra={"model_id": str(item["id"])}))
        if search["items"] and not hits:
            return [fail(SITE, "search", "parse_failed", "No recognizable Printables models.", url=_API)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = str(result.get("url") or "")
        try:
            parsed = urlparse(url)
            match = _MODEL_RE.fullmatch(parsed.path) if parsed.hostname in {
                "printables.com", "www.printables.com"} and parsed.scheme == "https" and parsed.username is None and parsed.password is None else None
        except ValueError:
            match = None
        if not match:
            return fail(SITE, "acquire", "unsupported", "Expected a Printables model page.", url=url)
        model_id = match.group(1)
        response = _query(_DETAIL, {"id": model_id})
        if response.get("ok") is False:
            return source_failure(SITE, "acquire", response, url=url)
        if response.get("errors"):
            return _graphql_failure(response["errors"], "acquire", url)
        data = response.get("data")
        if not isinstance(data, dict) or "print" not in data:
            return fail(SITE, "acquire", "parse_failed", "Missing Printables model metadata.", url=url)
        model = data["print"]
        if model is None:
            return fail(SITE, "acquire", "not_found", "Printables model was not found.", url=url)
        if not isinstance(model, dict):
            return fail(SITE, "acquire", "parse_failed", "Invalid Printables model metadata.", url=url)
        files = []
        for field, file_type in (("stls", "stl"), ("otherFiles", "other")):
            entries = model.get(field) or []
            if not isinstance(entries, list):
                return fail(SITE, "acquire", "parse_failed", "Invalid Printables file list.", url=url)
            for item in entries:
                if isinstance(item, dict) and item.get("id") and _FORMATS.get(Path(str(item.get("name") or "")).suffix.lower()):
                    files.append((item, file_type))
        requested = _format(result.get("format") or result.get("requested_format") or "")
        file_id = str(result.get("file_id") or "")
        file_name = str(result.get("file_name") or "")
        archive_member = result.get("archive_member")
        if archive_member is not None and (not isinstance(archive_member, str) or not archive_member):
            return fail(SITE, "acquire", "unsupported", "Specify a valid archive member.", url=url)
        if requested == "zip" and archive_member:
            return fail(SITE, "acquire", "unsupported", "Choose either an archive or a specific member.", url=url)
        candidates = [dict(item, file_type=file_type) for item, file_type in files]
        selected = [(item, file_type) for item, file_type in files
                    if (not file_id or str(item["id"]) == file_id)
                    and (not file_name or item["name"] == file_name)
                    and (not requested or _FORMATS[Path(item["name"]).suffix.lower()] in {requested, "zip"})
                    and (not archive_member or Path(item["name"]).suffix.lower() == ".zip")]
        if requested and not file_id and not file_name and not archive_member:
            exact = [(item, file_type) for item, file_type in selected
                     if _FORMATS[Path(item["name"]).suffix.lower()] == requested]
            if exact:
                selected = exact
        if not selected:
            return {**fail(SITE, "acquire", "not_found", "No matching downloadable CAD file is listed.", url=url),
                    "candidates": candidates}
        if len(selected) != 1:
            identities = [str(item["id"]) for item, _ in selected]
            field = "file_id"
            if len(set(identities)) != len(identities) or "cancel" in identities:
                identities = [str(item["name"]) for item, _ in selected]
                field = "file_name"
            question = None
            download_keys = {(str(item["id"]), file_type) for item, file_type in selected}
            if (len(download_keys) == len(selected) and len(set(identities)) == len(identities)
                    and "cancel" not in identities):
                question = model_selection_question(SITE, url, identities)
                for option, (item, _) in zip(question["options"][1:], selected):
                    option["label"] = f"{item['name']} (file ID {item['id']})"
            hint = ("Select a specific model file before downloading." if question else
                    "The listed model files cannot be uniquely identified by ID or name.")
            failure = fail(SITE, "acquire", "unsupported", hint, url=url, ask_user=question)
            failure["candidates"] = [dict(item, file_type=file_type) for item, file_type in selected]
            if question:
                failure["selection_field"] = field
            return failure
        chosen, file_type = selected[0]
        size = chosen.get("fileSize")
        if isinstance(size, (int, float)) and size > 200 * 1024 * 1024:
            return {**fail(SITE, "acquire", "unsupported", "Model file exceeds 200 MB.", url=url),
                    "candidates": candidates}
        link = (_download_link(model_id, str(chosen["id"])) if file_type == "stl" else
                _download_link(model_id, str(chosen["id"]), file_type))
        if link.get("ok") is False:
            return source_failure(SITE, "acquire", link, url=url)
        if link.get("errors"):
            return _graphql_failure(link["errors"], "acquire", url)
        link_data = link.get("data")
        download = link_data.get("getDownloadLink") if isinstance(link_data, dict) else None
        if not isinstance(download, dict):
            return fail(SITE, "acquire", "parse_failed", "Missing Printables download response.", url=url)
        output = download.get("output")
        direct = output.get("link") if isinstance(output, dict) else None
        if not download.get("ok") or not direct:
            errors = download.get("errors")
            return (_graphql_failure(errors, "acquire", url) if errors else
                    fail(SITE, "acquire", "parse_failed", "Printables did not issue a download link.", url=url))
        try:
            parsed_direct = urlparse(direct)
            allowed = (parsed_direct.scheme == "https" and parsed_direct.hostname == "files.printables.com"
                       and parsed_direct.username is None and parsed_direct.password is None)
        except (ValueError, TypeError):
            allowed = False
        if not allowed:
            return fail(SITE, "acquire", "policy_refused", "Unexpected Printables download host.", url=url)
        dest, refusal = download_path(SITE, direct, filename=f"{model_id}-{chosen['id']}-{chosen['name']}")
        if refusal:
            return {**source_failure(SITE, "acquire", refusal, url=url), "reason": "policy_refused",
                    "original_reason": refusal.get("reason")}
        staged = None
        extracted = None
        try:
            with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".printables-",
                                             suffix=dest.suffix, delete=False) as temp:
                staged = Path(temp.name)
            download_options = {}
            if staged.suffix.lower() == ".zip" and requested and requested != "zip":
                download_options["requested_format"] = requested
            if archive_member:
                download_options["requested_member"] = archive_member
            if requested == "zip":
                request = lambda: _download_archive(direct, staged)
            else:
                request = lambda: download_public_cad(direct, staged, **download_options)
            outcome, error = _paced("acquire", direct, request)
            if error:
                failure = source_failure(SITE, "acquire", error, url=direct)
                if failure.get("candidates"):
                    failure["ask_user"] = model_selection_question(SITE, url, failure["candidates"])
                    failure["selection_field"] = "archive_member"
                return failure
            if requested == "zip":
                checked = classify_artifact(outcome["dest"], kind="archive")
            else:
                checked = extract_cad(outcome["dest"], requested_format=requested or None,
                                      requested_member=archive_member)
            if not checked["ok"]:
                candidates = checked.get("candidates") or []
                question = model_selection_question(SITE, url, candidates) if candidates else None
                failure = fail(SITE, "acquire", "unsupported", checked["reason"], url=direct, ask_user=question)
                if candidates:
                    failure["candidates"] = candidates
                    failure["selection_field"] = "archive_member"
                return failure
            if requested != "zip" and checked.get("member"):
                extracted = Path(checked["artifact_path"])
            os.replace(staged, dest)
            license_data = model.get("license")
            license_name = (license_data.get("name") if isinstance(license_data, dict) else None) or result.get("license")
            return provenance_envelope(source_url=url, artifact_path=str(extracted or dest),
                                       artifact_kind="archive" if requested == "zip" else "cad",
                                       format="zip" if requested == "zip" else checked["format"],
                                       archive_path=str(dest) if extracted else None,
                                       member=checked.get("member"),
                                       session_profile_used="anonymous", license_note=license_name or "Unknown",
                                       raw_url=direct, title=model.get("name", ""), model_id=model_id,
                                       file=dict(chosen, file_type=file_type), files=candidates)
        except OSError as exc:
            if extracted is not None:
                extracted.unlink(missing_ok=True)
            return fail(SITE, "acquire", "transport_failed", str(exc), url=direct)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)


ADAPTER = register(PrintablesAdapter())
