"""Bounded, provenance-aware previews for library parts and product images."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from contextvars import copy_context
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from services.agent.request_context import owner_key

_MAX_BATCH = 32
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_CAD_BYTES = 128 * 1024 * 1024
_MAX_RENDER_FEATURES = 256
_RENDER_VERSION = "parts-preview-v3"
_IMAGE_EXTENSIONS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}
_BROWSING_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_LOGO_COMPONENT = re.compile(r"(?:^|[/_.-])(?:favicons?|logos?|sprites?|placeholders?|icons?|globe)(?:$|[/_.-])", re.I)
_NO_STANDALONE = object()
_TARGET_KEYS = {"docid", "documentid", "targetdoc", "targetdocid",
                "targetdocument", "targetdocumentid", "assemblyid"}
_render_lock = threading.Lock()
_lookup_lock = threading.Lock()
_futures_lock = threading.Lock()
_futures: dict[object, tuple[str, str] | None] = {}
_trim_lock = threading.Lock()
_last_trim = 0.0


def _env_num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return max(lo, min(hi, value))


def batch_budget_s() -> float:
    return _env_num("CHUDWARE_THUMB_BATCH_BUDGET_S", 20.0, 0.05, 300.0)


def _workers() -> int:
    return int(_env_num("CHUDWARE_THUMB_WORKERS", 8, 1, 16))


def _fetch_timeout_s() -> float:
    return _env_num("CHUDWARE_THUMB_FETCH_TIMEOUT_S", 4.0, 0.05, 60.0)


def _cache_dir() -> Path:
    from services import paths
    directory = paths.ROOT / "parts_thumbs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cache_paths(item_id: str, image_url: str, source: str, identity: object = "") -> tuple[Path, Path]:
    key = json.dumps([_RENDER_VERSION, item_id, image_url, source, identity],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    directory = _cache_dir()
    return directory / (digest + ".img"), directory / (digest + ".miss")


def _image_mime(data: bytes, *, product: bool = False) -> str:
    if not data or len(data) > _MAX_IMAGE_BYTES:
        return ""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            mime = _IMAGE_EXTENSIONS.get(image.format or "", "")
            if (not mime or image.width * image.height > 16_000_000
                    or image.width <= 0 or image.height <= 0
                    or (product and min(image.width, image.height) < 24)):
                return ""
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return mime
    except Exception:  # noqa: BLE001 — corrupt and unrecognized responses are not previews
        return ""


def _cached_image(path: Path, *, product: bool = False) -> tuple[bytes, str] | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_IMAGE_BYTES + 1)
        mime = _image_mime(data, product=product)
        if mime:
            return data, mime
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _cached_miss(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        transient = data.get("transient") is True
        ttl = (_env_num("CHUDWARE_THUMB_TRANSIENT_TTL_S", 60, 1, 3600) if transient else
               _env_num("CHUDWARE_THUMB_MISS_TTL_S", 86400, 1, 365 * 86400))
        if time.time() - path.stat().st_mtime < ttl:
            return str(data.get("reason") or "no_preview")
        path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return ""


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".thumb-", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _store_image(path: Path, miss: Path, data: bytes) -> None:
    _atomic_write(path, data)
    try:
        miss.unlink(missing_ok=True)
    except OSError:
        pass


def _store_miss(path: Path, reason: str, *, transient: bool) -> None:
    _atomic_write(path, json.dumps({"reason": reason, "transient": transient}).encode())
    _trim_misses()


def _trim_misses() -> None:
    global _last_trim
    now = time.monotonic()
    with _trim_lock:
        if _last_trim and now - _last_trim < 600:
            return
        _last_trim = now
    try:
        entries = list(_cache_dir().iterdir())
        if len(entries) <= 20_000:
            return
        misses = [p for p in entries if p.suffix == ".miss"]
        misses.sort(key=lambda p: p.stat().st_mtime)
        for path in misses[:max(1, len(misses) // 5)]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_logo_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() not in ("http", "https") or not host:
            return True
        if host == "icon.horse" or host.endswith(".icon.horse"):
            return True
        if host in ("icons.duckduckgo.com", "t3.gstatic.com"):
            return True
        return bool(_LOGO_COMPONENT.search(parsed.path))
    except ValueError:
        return True


def _gstatic_favicon_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not (host == "google.com" or host.endswith(".google.com")) or not parsed.path.startswith("/s2/favicons"):
            return ""
        query = parse_qs(parsed.query)
        domain = (query.get("domain") or [""])[0].strip()
        if not domain:
            domain = urlparse((query.get("url") or [""])[0].strip()).hostname or ""
        if not domain:
            return ""
        size = (query.get("sz") or ["64"])[0].strip() or "64"
        return ("https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
                "&fallback_opts=TYPE,SIZE,URL"
                f"&url=https://{quote(domain, safe='')}&size={size}")
    except ValueError:
        return ""


def _network_allowed() -> bool:
    try:
        from services.agent.config import local_only_enabled
        from services.mcp.tool_registry import RISK_EXTERNAL, is_risk_denied
        return not local_only_enabled() and not is_risk_denied(RISK_EXTERNAL)
    except Exception:  # noqa: BLE001 — inability to establish policy must not enable egress
        return False


def _http_image_get(url: str, headers: dict, deadline: float, *, product: bool) -> tuple[bytes | None, int]:
    from services.net.safe_http import get
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, 0
    try:
        response = get(url, headers=headers, max_bytes=_MAX_IMAGE_BYTES,
                       idle_timeout=min(_fetch_timeout_s(), remaining))
        if product and _is_logo_url(response.url):
            return None, 404
        body = response.content
        if _image_mime(body, product=product):
            return body, response.status_code
        return None, response.status_code
    except Exception:  # noqa: BLE001 — safe_http is the only network path
        return None, 0


def _fetch_image_bytes(url: str, item_id: str, deadline: float, *, logo: bool = False) -> tuple[bytes | None, int]:
    referer = ""
    if "::" in item_id:
        product_url = item_id.split("::", 1)[1]
        if product_url.startswith(("http://", "https://")):
            referer = product_url
    if not referer:
        try:
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
        except ValueError:
            pass
    headers = {"User-Agent": _BROWSING_UA,
               "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    data, status = _http_image_get(url, headers, deadline, product=not logo)
    if status == 200 and data:
        return data, status
    if logo and status == 404:
        alternate = _gstatic_favicon_url(url)
        if alternate:
            other, other_status = _http_image_get(alternate, headers, deadline, product=False)
            if other and other_status == 200:
                return other, other_status
            data = data or other
        if data:
            return data, status  # vendor footer may use the provider's globe
    return None, status


def _record_for_part(item_id: str) -> dict | None:
    from services import parts
    # Serialize lazy hub bootstrap; concurrent first lookups must not observe a
    # half-registered index. Live catalog providers are never consulted here.
    with _lookup_lock:
        record = parts.get(item_id, include_live=False, acquire=False)
        if isinstance(record, dict):
            return record
        for provider in parts._get_hub()._registry.providers():
            if getattr(provider, "provider_id", "") == "reveng_results":
                record = provider.get(item_id)
                if isinstance(record, dict):
                    return record
    if item_id.startswith("mech_family:"):
        family, sep, size = item_id[len("mech_family:"):].partition(":")
        if sep and family and size:
            return {"part_id": item_id, "geometry_ref": {"kind": "core_family", "family": family, "size": size}}
    return None


def _geometry_identity(record: dict) -> dict | None:
    gref = record.get("geometry_ref") or {}
    if not isinstance(gref, dict):
        return None
    kind = gref.get("kind")
    if kind == "core_family":
        if not gref.get("family") or not gref.get("size"):
            return None
        return {"geometry_ref": gref}
    if kind == "import_file":
        from services.agent.request_context import cell_path
        try:
            path = cell_path(str(gref.get("path") or ""))
            if path.suffix.lower() not in {".step", ".stp", ".iges", ".igs", ".stl"}:
                return None
            stat = path.stat()
            if not path.is_file() or not 0 < stat.st_size <= _MAX_CAD_BYTES:
                return None
            return {"geometry_ref": {**gref, "path": str(path)},
                    "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except (OSError, ValueError):
            return None
    if not isinstance(kind, str) or not kind:
        return None
    params = record.get("params") or {}
    if not isinstance(params, dict) or not isinstance(gref.get("spec") or {}, dict):
        return None
    if kind.startswith("parametric_"):
        from services.mcp.parts_tools import _NO_STANDALONE_GEOMETRY, _PARAMETRIC_BUILDERS
        if kind in _NO_STANDALONE_GEOMETRY and kind not in _PARAMETRIC_BUILDERS:
            return None
        if kind == "parametric_heatsink":
            spec = {**params, **(gref.get("spec") or {})}
            try:
                fins = float(spec.get("n_fins"))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(fins) or not 1 <= fins < _MAX_RENDER_FEATURES:
                return None
    return {"geometry_ref": gref, "params": params}


def _render_local_part_png(record: dict, identity: dict, out_path: str):
    """Render a new native placement, never a caller's existing document."""
    from services.mcp import core_bridge as cb
    gref = identity["geometry_ref"]
    kind = gref["kind"]
    doc_id = ""
    generated_path = None
    generated_disposable = False
    try:
        if kind == "core_family":
            params = {"family": gref["family"], "size": gref["size"]}
            if gref.get("length_mm") is not None:
                params["length_mm"] = gref["length_mm"]
            created = cb.call("parts_make", params)
        elif kind == "import_file":
            created = cb.call("part_import_file", {"path": gref["path"],
                              "as_solid": Path(gref["path"]).suffix.lower() == ".stl"})
        else:
            from services.mcp.parts_tools import _PARAMETRIC_BUILDERS
            builder = _PARAMETRIC_BUILDERS.get(kind)
            if builder is None:
                # Context-free native placement may create a standalone part, or
                # may only describe an element in a piping/structural/ECAD doc.
                # Never let record parameters nominate an existing document.
                open_docs = cb.call("doc_list", {})
                if not isinstance(open_docs, dict) or not isinstance(open_docs.get("docs"), list):
                    return None
                existing = {str(row.get("doc_id")) for row in open_docs["docs"]
                            if isinstance(row, dict) and row.get("doc_id")}
                clean_params = {key: value for key, value in (record.get("params") or {}).items()
                                if str(key).lower().replace("_", "").replace("-", "") not in _TARGET_KEYS}
                clean_record = {**record, "geometry_ref": gref, "params": clean_params}
                created = cb.call("part_place", {"part_id": str(record.get("part_id") or ""),
                                                "record": clean_record})
                if not isinstance(created, dict):
                    return None
                if kind == "generate_model":
                    detail = created.get("detail") or {}
                    raw_path = detail.get("step_path") if isinstance(detail, dict) else None
                    if isinstance(raw_path, str) and raw_path:
                        path = Path(raw_path).resolve()
                        if (path.parent == Path(tempfile.gettempdir()).resolve()
                                and re.fullmatch(r"chudware-spec-model-\d+\.step", path.name)):
                            generated_path = path
                if created.get("ok") is not True:
                    generated_disposable = not created.get("doc_id")
                    reason = str(created.get("reason") or "").lower()
                    if "doc provided" in reason or "unknown geometry_ref kind" in reason:
                        return _NO_STANDALONE
                    return None
                candidate = str(created.get("doc_id") or "")
                if (not candidate or candidate in existing or created.get("editable") is not True or
                        str(created.get("entity_id") or "") != candidate or "doc" in created):
                    return _NO_STANDALONE
                generated_disposable = True
            else:
                spec = {**(record.get("params") or {}), **(gref.get("spec") or {})}
                name, ops, _detail = builder(spec)
                if not isinstance(ops, list) or len(ops) > _MAX_RENDER_FEATURES:
                    return _NO_STANDALONE
                created = cb.call("doc_new", {"kind": "part", "name": name})
                if isinstance(created, dict) and created.get("doc_id"):
                    doc_id = str(created["doc_id"])
                    applied = cb.call("doc_apply_ops", {"doc_id": doc_id,
                                      "base_revision": created.get("revision", 0),
                                      "with_tess": False, "ops": ops})
                    if (not isinstance(applied, dict) or applied.get("ok") is False
                            or "rejected_tree" in applied):
                        return None
        if isinstance(created, dict):
            doc_id = str(created.get("doc_id") or "")
        if not isinstance(created, dict) or created.get("ok") is False or not doc_id:
            return None
        if kind == "import_file":
            detail = created.get("detail") or {}
            count = detail.get("body_count") if isinstance(detail, dict) else None
            if isinstance(count, int) and count <= 0:
                return None
        result = cb.call("doc_render_png", {"doc_id": doc_id, "out_path": out_path, "annotate": False})
        if not isinstance(result, dict) or result.get("ok") is False:
            return None
        with open(out_path, "rb") as stream:
            data = stream.read(_MAX_IMAGE_BYTES + 1)
        return data if _image_mime(data) else None
    except Exception:  # noqa: BLE001 — native service unavailable or model could not render
        return None
    finally:
        closed = not doc_id
        if doc_id:
            try:
                closed = bool(cb.call("doc_close", {"doc_id": doc_id}).get("closed"))
            except Exception:  # noqa: BLE001
                pass
        if generated_disposable and generated_path is not None and closed:
            try:
                generated_path.unlink(missing_ok=True)
            except OSError:
                pass


def _result(item_id: str, *, data: bytes | None = None, mime: str = "",
            provenance: str = "", reason: str = "") -> dict:
    return {"item_id": item_id, "ok": data is not None, "data": data,
            "mime": mime, "provenance": provenance, "reason": reason}


def _resolve_one(item_id: str, image_url: str, deadline: float, network: bool,
                 logo: bool, native_only: bool = False) -> dict:
    if not item_id:
        return _result(item_id, reason="item_id required")
    if item_id.startswith("vendorlogo:") and not logo:
        return _result(item_id, reason="no_preview")
    product_url = bool(image_url) and not native_only and (logo or not _is_logo_url(image_url))
    fetch_failure = ""
    if product_url:
        source = "logo" if logo else "product"
        img, miss = _cache_paths(item_id, image_url, source)
        hit = _cached_image(img, product=not logo)
        if hit:
            return _result(item_id, data=hit[0], mime=hit[1], provenance="" if logo else "product_image")
        fetch_failure = _cached_miss(miss)
        if not fetch_failure and network:
            data, status = _fetch_image_bytes(image_url, item_id, deadline, logo=logo)
            mime = _image_mime(data or b"", product=not logo)
            if data and mime and (status == 200 or logo):
                _store_image(img, miss, data)
                return _result(item_id, data=data, mime=mime, provenance="" if logo else "product_image")
            transient = status == 0 or status in (408, 425, 429) or status >= 500
            fetch_failure = "transport_failed" if transient else "no_preview"
            _store_miss(miss, fetch_failure, transient=transient)
        elif not fetch_failure:
            fetch_failure = "network_disabled"
    if logo:
        return _result(item_id, reason=fetch_failure or "no_preview")
    if time.monotonic() >= deadline:
        return _result(item_id, reason="timeout")
    try:
        record = _record_for_part(item_id)
        identity = _geometry_identity(record) if record else None
    except Exception:  # noqa: BLE001 — hub temporarily unavailable is not a definitive absence
        return _result(item_id, reason=fetch_failure or
                       ("no_preview" if image_url and not product_url and not native_only
                        else "render_unavailable"))
    if identity is None:
        return _result(item_id, reason=fetch_failure or "no_preview")
    img, miss = _cache_paths(item_id, image_url, "cad", identity)
    hit = _cached_image(img)
    if hit:
        return _result(item_id, data=hit[0], mime=hit[1], provenance="cad_render")
    failure = _cached_miss(miss)
    if failure:
        return _result(item_id, reason=fetch_failure if fetch_failure not in ("", "no_preview") else failure)
    wait = max(0.0, deadline - time.monotonic())
    if not _render_lock.acquire(timeout=wait):
        return _result(item_id, reason="timeout")
    tmp = None
    try:
        # Recheck after the lock: another concurrent request may have warmed this key.
        hit = _cached_image(img)
        if hit:
            return _result(item_id, data=hit[0], mime=hit[1], provenance="cad_render")
        with tempfile.NamedTemporaryFile(dir=img.parent, prefix=".render-", suffix=".png", delete=False) as stream:
            tmp = Path(stream.name)
        data = _render_local_part_png(record, identity, str(tmp))
    finally:
        _render_lock.release()
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    if data is _NO_STANDALONE:
        return _result(item_id, reason=fetch_failure or "no_preview")
    mime = _image_mime(data or b"")
    if data and mime:
        _store_image(img, miss, data)
        return _result(item_id, data=data, mime=mime, provenance="cad_render")
    _store_miss(miss, "render_unavailable", transient=True)
    return _result(item_id, reason=fetch_failure if fetch_failure not in ("", "no_preview") else "render_unavailable")


def _future_finished(future) -> None:
    with _futures_lock:
        _futures.pop(future, None)


def active_future_count(owner: tuple[str, str] | None) -> int:
    from services.worker_activity import registry_lock
    with registry_lock(_futures_lock):
        return sum(stored == owner and not future.done() for future, stored in _futures.items())


def _resolve(items: list[dict], *, budget_s: float, allow_network: bool, logos: bool,
             native_only: bool = False) -> list[dict]:
    try:
        budget = float(budget_s)
        if not math.isfinite(budget):
            budget = 0.0
    except (TypeError, ValueError):
        budget = 0.0
    deadline = time.monotonic() + max(0.0, min(budget, 300.0))
    network = bool(allow_network) and _network_allowed()
    owner = owner_key()
    results: list[dict] = []
    for start in range(0, len(items), _MAX_BATCH):
        chunk = items[start:start + _MAX_BATCH]
        requests = [(str(ent.get("item_id") or "").strip(), str(ent.get("image_url") or "").strip())
                    if isinstance(ent, dict) else ("", "") for ent in chunk]
        answers: list[dict | None] = [None] * len(requests)
        remaining = deadline - time.monotonic()
        if remaining > 0 and requests:
            executor = ThreadPoolExecutor(max_workers=min(_workers(), len(requests)),
                                          thread_name_prefix="parts-thumb")
            try:
                pending = {}
                for index, (item_id, url) in enumerate(requests):
                    context = copy_context()
                    future = executor.submit(context.run, _resolve_one, item_id, url, deadline,
                                             network, logos, native_only)
                    with _futures_lock:
                        _futures[future] = owner
                    future.add_done_callback(_future_finished)
                    pending[future] = index
                try:
                    for future in as_completed(pending, timeout=max(0.0, deadline - time.monotonic())):
                        index = pending[future]
                        try:
                            answers[index] = future.result()
                        except Exception:  # noqa: BLE001 — one failed item must not fail the batch
                            answers[index] = _result(requests[index][0], reason="thumbnail_failed")
                except FutureTimeout:
                    pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        results.extend(answer or _result(requests[i][0], reason="timeout")
                       for i, answer in enumerate(answers))
    return results


def resolve_previews(items: list[dict], *, budget_s: float = 20.0, allow_network: bool = True) -> list[dict]:
    """Return one ordered part preview per input; vendor logos are never part previews."""
    return _resolve(items, budget_s=budget_s, allow_network=allow_network, logos=False)


def _warm_native_previews(items: list[dict], *, budget_s: float = 20.0) -> list[dict]:
    """Warm CAD entries even if a product photo is already cached for the same URL."""
    return _resolve(items, budget_s=budget_s, allow_network=False, logos=False, native_only=True)


def resolve_vendor_logos(items: list[dict], *, budget_s: float = 20.0, allow_network: bool = True) -> list[dict]:
    """Resolve the GUI vendor footer's separate favicon requests, not part previews."""
    return _resolve(items, budget_s=budget_s, allow_network=allow_network, logos=True)
