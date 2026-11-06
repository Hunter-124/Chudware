"""services/browser/adapters/parts_provider.py — CatalogProvider wrapper.

Presents a :class:`services.browser.adapters.SourceAdapter` (search/
acquire) as a ``services.parts.hub`` ``CatalogProvider``
(``contracts/parts.md`` §3): live-search-only (``count``/``iter_records``
raise ``NotImplementedError``), domain
``"catalog"`` — the seam ``contracts/parts.md`` §6.3 reserved for "paid
catalog providers" (TraceParts/Werk24). This is the user-authenticated
browser-session mechanism for the same catalogs instead of a paid API; both
can coexist behind the same ``CatalogProvider`` protocol.

Anonymous searches are allowed; sources that require credentials, a sign-in,
or human action return their structured failure. Search preserves the hit for
later explicit acquisition rather than downloading a model while browsing.
"""
from __future__ import annotations

from typing import Iterator

from services.browser.cad_validate import classify_artifact, extract_cad

from . import SourceAdapter, abs_url, fail, make_hit, model_selection_question, source_failure
from ._retail import _price, _stock


class BrowserCatalogAdapter:
    """``CatalogProvider`` wrapping one ``SourceAdapter`` instance.

    ``part_id`` scheme: ``"catalog:<site>::<url>"``. Colons inside a
    ``catalog_id`` are allowed per ``contracts/parts.md`` §0 (split on the
    FIRST colon only) so ``catalog_id`` is simply ``"<site>::<url>"``.

    Search results are cached in-memory (``part_id`` -> the adapter's result
    stub) so a subsequent :meth:`get` in the same process can re-run
    ``acquire()`` without re-searching. A ``get`` for a ``part_id`` from a
    prior process (cache miss) still works — the URL is recovered from the
    ``part_id`` itself, just with an empty ``title`` until acquired.
    """

    domain = "catalog"

    def __init__(self, source_adapter: SourceAdapter):
        self._adapter = source_adapter
        self._site = source_adapter.key
        #: Public vendor site key (e.g. "digikey", "mcmaster") — lets the hub
        #: restrict a routed search to only the vendors that fit a category
        #: without reaching into a private attribute.
        self.site = source_adapter.key
        self.provider_id = f"browser_{self._site}"
        self._cache: dict[str, dict] = {}

    def _part_id(self, result: dict) -> str:
        return f"catalog:{self._site}::{result.get('url', '')}"

    def _params_from_hit(self, hit: dict) -> dict:
        params: dict = {"site": self._site, "url": hit.get("url", "")}
        model = hit.get("model") if isinstance(hit.get("model"), dict) else {}
        values = {
            "sku": hit.get("sku"),
            "part_number": hit.get("part_number"),
            "mpn": hit.get("mpn"),
            "manufacturer": hit.get("manufacturer") or hit.get("brand"),
            "stock": _stock(hit.get("stock")),
            "material": hit.get("material"),
            "kind": hit.get("kind"),
            "model_formats": hit.get("model_formats") or model.get("formats"),
            "model_available": hit.get("model_available", model.get("available")),
            "datasheet_url": abs_url(hit.get("url", ""), hit.get("datasheet_url") or hit.get("datasheet")),
            "license": hit.get("license"),
        }
        thumb = hit.get("thumbnail") or hit.get("image_url") or hit.get("image")
        if isinstance(thumb, str):
            values["thumbnail"] = make_hit(self._site, title="", url=hit.get("url", ""),
                                            thumbnail=thumb)["thumbnail"]
        params.update({key: value for key, value in values.items()
                       if value is not None and value != "" and value != []})
        return params

    def _sourcing_from_hit(self, hit: dict) -> list[dict]:
        price = _price(hit.get("price"))
        if price is None:
            return []
        currency = hit.get("currency")
        return [{"vendor": getattr(self._adapter, "label", None) or self._site.replace("_", " ").title(),
                 "price": price, "currency": currency or "", "stock": _stock(hit.get("stock")),
                 "url": hit.get("url", "")}]

    def record_from_hit(self, hit: dict) -> dict:
        """Convert a hit to a PartRecord, retaining it for later acquisition."""
        if hit.get("ok") is False:
            return source_failure(self._site, "search", hit, url=hit.get("url"))
        part_id = self._part_id(hit)
        self._cache[part_id] = hit
        return {
            "part_id": part_id, "domain": self.domain, "title": hit.get("title", ""),
            "standard": None, "designation": None, "params": self._params_from_hit(hit),
            "geometry_ref": None, "sourcing": self._sourcing_from_hit(hit),
            "provenance": f"{self._site} search result via the user's own browser session "
                          f"(not yet downloaded — acquire explicitly when needed)",
            "license": hit.get("license") or "user-supplied", "source": "live",
        }

    def search(self, query: str, *, domain: str | list[str] | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and (self.domain not in domain if not isinstance(domain, str)
                       else domain != self.domain):
            return []
        return [self.record_from_hit(hit) for hit in self._adapter.search(query)]

    def get(self, part_id: str) -> dict | None:
        prefix = f"catalog:{self._site}::"
        if not part_id.startswith(prefix):
            return None
        result = self._cache.get(part_id)
        if result is None:
            result = {"title": "", "url": part_id[len(prefix):], "site": self._site}
        outcome = self._adapter.acquire(result)
        if not outcome.get("ok"):
            enriched = source_failure(self._site, "acquire", outcome, url=result.get("url"))
            enriched.setdefault("part_id", part_id)
            enriched.setdefault("source_url", result.get("url", ""))
            enriched.setdefault("title", result.get("title", ""))
            return enriched
        params = self._params_from_hit(result)
        params["fetched_ts"] = outcome.get("fetched_ts")
        if isinstance(outcome.get("params"), dict):
            params.update({key: value for key, value in outcome["params"].items() if value is not None})
        envelope_keys = {"ok", "source_url", "fetched_ts", "session_profile_used", "license_note",
                         "artifact_path", "title", "params"}
        for key, value in outcome.items():
            if key not in envelope_keys and value is not None:
                params[key] = value
        artifact = outcome.get("artifact_path")
        geometry_ref = None
        if artifact:
            selected_format = outcome.get("format")
            if selected_format == "zip":
                selected_format = None
            selected_member = outcome.get("member") if str(artifact).lower().endswith(".zip") else None
            classified = classify_artifact(artifact, outcome.get("artifact_kind"),
                                           requested_format=selected_format, requested_member=selected_member)
            if classified.get("candidates"):
                candidates = classified["candidates"]
                url = result.get("url") or ""
                return {**fail(self._site, "acquire", "unsupported", classified["reason"], url=url,
                               ask_user=model_selection_question(self._site, url, candidates)),
                        "part_id": part_id, "artifact_path": str(artifact), "candidates": candidates,
                        "selection_field": "archive_member"}
            if not classified["ok"]:
                return {**fail(self._site, "acquire", "unsupported",
                               classified["reason"], url=result.get("url")),
                        "part_id": part_id, "artifact_path": str(artifact)}
            params["artifact_kind"] = classified["artifact_kind"]
            params["format"] = classified["format"]
            if classified["artifact_kind"] == "cad" and classified.get("cad_validated", True):
                extracted = extract_cad(artifact, requested_format=selected_format,
                                        requested_member=selected_member)
                if not extracted["ok"]:
                    candidates = extracted.get("candidates") or []
                    url = result.get("url") or ""
                    return {**fail(self._site, "acquire", "unsupported", extracted["reason"], url=url,
                                   ask_user=model_selection_question(self._site, url, candidates) if candidates else None),
                            "part_id": part_id, "artifact_path": str(artifact), "candidates": candidates,
                            **({"selection_field": "archive_member"} if candidates else {})}
                if extracted["format"] == "fcstd":
                    params["artifact_path"] = extracted["artifact_path"]
                    params["import_supported"] = False
                    params["artifact_notice"] = "FreeCAD documents cannot currently be imported natively."
                else:
                    geometry_ref = {"kind": "import_file", "path": extracted["artifact_path"]}
                if extracted.get("archive_path"):
                    params["archive_path"] = extracted["archive_path"]
            elif classified["artifact_kind"] == "cad":
                params["artifact_path"] = str(artifact)
                params["cad_validated"] = False
            elif classified["artifact_kind"] == "document":
                params["datasheet_path"] = str(artifact)
            else:
                params["artifact_path"] = str(artifact)
        sourcing_hit = {**result, **{key: value for key, value in outcome.items() if value is not None}}
        return {
            "part_id": part_id, "domain": self.domain,
            "title": result.get("title") or outcome.get("title") or "",
            "standard": None, "designation": None, "params": params,
            "geometry_ref": geometry_ref, "sourcing": self._sourcing_from_hit(sourcing_hit),
            "provenance": f"Acquired via {self._site} under the user's own browser session "
                          f"({outcome.get('source_url', result.get('url', ''))}, "
                          f"fetched {outcome.get('fetched_ts', '')})",
            "license": result.get("license") or "user-supplied",
            "license_note": outcome.get("license_note", ""), "source": "live",
        }

    def count(self) -> int:
        raise NotImplementedError(f"{self.provider_id} is a live-search-only provider")

    def iter_records(self) -> Iterator[dict]:
        raise NotImplementedError(f"{self.provider_id} is a live-search-only provider")
