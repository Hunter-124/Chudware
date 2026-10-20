"""Public Printables model files via its GraphQL metadata and download-link API."""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlparse

from . import download_path, download_public_cad, provenance_envelope, register

SITE = "printables"
_API = "https://api.printables.com/graphql/"
_PAGE = "https://www.printables.com/model/"
_MODEL_RE = re.compile(r"^/model/(\d+)(?:-[^/]*)?(?:/files)?/?$")
_SEARCH = "query($q:String!){searchPrints2(query:$q,limit:30,ordering:best_match){items{id name slug license{name}}}}"
_DETAIL = "query($id:ID!){print(id:$id){name slug license{name} stls{id name fileSize}}}"
_LINK = ("mutation($id:ID!,$pid:ID!){getDownloadLink(id:$id,printId:$pid,"
         "fileType:stl,source:model_detail,files:[{fileType:stl,ids:[$id]}])"
         "{ok output{link} errors{field messages}}}")
_CAD_EXTS = (".step", ".stp", ".iges", ".igs", ".stl", ".3mf", ".obj", ".zip")


def _query(query: str, variables: dict) -> dict:
    """Ask Printables' public GraphQL API over HTTPS, without loading a browser."""
    payload = json.dumps({"query": query, "variables": variables})
    try:
        completed = subprocess.run(
            ["curl", "-sS", "--max-time", "20", "-H", "Content-Type: application/json",
             "--data-binary", payload, "--write-out", "\n%{http_code}", _API],
            capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "no_cad_link", "detail": f"Printables API unavailable: {exc}"}
    body, _, status = completed.stdout.rpartition("\n")
    if status.isdigit() and int(status) >= 400:
        return {"ok": False, "reason": f"http_{status}",
                "detail": f"Printables API returned HTTP {status}."}
    if completed.returncode or not body:
        return {"ok": False, "reason": "no_cad_link",
                "detail": completed.stderr.strip() or "Printables API returned no data."}
    try:
        answer = json.loads(body)
    except ValueError:
        return {"ok": False, "reason": "no_cad_link",
                "detail": "Printables API returned non-JSON content."}
    return answer if isinstance(answer, dict) else {}


def _download_link(model_id: str, file_id: str) -> dict:
    return _query(_LINK, {"id": file_id, "pid": model_id})


class PrintablesAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        result = _query(_SEARCH, {"q": query})
        if result.get("ok") is False:
            return [result]
        items = (result.get("data") or {}).get("searchPrints2") or {}
        return [{"site": SITE, "title": item["name"],
                 "url": f"{_PAGE}{item['id']}-{item['slug']}",
                 "license": (item.get("license") or {}).get("name")}
                for item in items.get("items") or []
                if isinstance(item, dict) and item.get("id") and item.get("slug")]

    def acquire(self, result: dict) -> dict:
        url = str(result.get("url") or "")
        parsed = urlparse(url)
        match = _MODEL_RE.fullmatch(parsed.path) if parsed.hostname in {
            "printables.com", "www.printables.com"} and parsed.scheme == "https" else None
        if not match:
            return {"ok": False, "reason": "no_cad_link", "detail": "Expected a Printables model page.",
                    "candidates": []}
        model_id = match.group(1)
        response = _query(_DETAIL, {"id": model_id})
        if response.get("ok") is False:
            return response
        model = (response.get("data") or {}).get("print") or {}
        files = [f for f in model.get("stls") or [] if isinstance(f, dict)
                 and str(f.get("name") or "").lower().endswith(_CAD_EXTS)]
        priority = {".step": 0, ".stp": 0, ".iges": 1, ".igs": 1,
                    ".3mf": 2, ".stl": 3, ".obj": 4, ".zip": 5}
        files.sort(key=lambda f: (priority.get("." + str(f["name"]).rsplit(".", 1)[-1].lower(), 10),
                                  str(f["name"])))
        if not files:
            return {"ok": False, "reason": "no_cad_link",
                    "detail": f"No downloadable CAD file is listed at {url}.", "candidates": []}
        chosen = files[0]
        if int(chosen.get("fileSize") or 0) > 200 * 1024 * 1024:
            return {"ok": False, "reason": "too_large", "detail": "Model file exceeds 200 MB.",
                    "candidates": [f["name"] for f in files]}
        link = _download_link(model_id, str(chosen["id"]))
        if link.get("ok") is False and "data" not in link:
            return link
        download = (link.get("data") or {}).get("getDownloadLink") or {}
        direct = (download.get("output") or {}).get("link")
        if not download.get("ok") or not direct:
            return {"ok": False, "reason": "login_required",
                    "detail": f"Printables did not issue a guest download for {url}. Download it there and drop the file in your project.",
                    "candidates": [f["name"] for f in files]}
        if urlparse(direct).scheme != "https" or urlparse(direct).hostname != "files.printables.com":
            return {"ok": False, "reason": "unsupported_format", "detail": "Unexpected Printables download host.",
                    "candidates": [direct]}
        dest, refusal = download_path(
            SITE, direct, filename=f"{model_id}-{chosen['id']}-{chosen['name']}")
        if refusal is not None:
            return refusal
        outcome = download_public_cad(direct, dest)
        if not outcome.get("ok"):
            return outcome
        return provenance_envelope(source_url=url, artifact_path=outcome["dest"],
                                   session_profile_used="anonymous",
                                   license_note=(model.get("license") or {}).get("name") or "Unknown",
                                   raw_url=direct, title=model.get("name", ""))


ADAPTER = register(PrintablesAdapter())
