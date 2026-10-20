"""Public marketplace, buyer library, and seller listing studio."""
from __future__ import annotations

from collections.abc import Iterator
import asyncio
import hashlib
import logging
import math
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import psycopg

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from config import settings
from db import tx
from limits import SMALL_REQUEST_BYTES, api_writes, bounded_form, uploads
from marketplace_authz import (
    MarketplaceError,
    marketplace_enabled_or_404,
    optional_user,
    require_seller,
    require_user,
)
from marketplace_schema import (
    ALWAYS_GATED_KINDS,
    ALWAYS_UNGATED_KINDS,
    ASSET_KINDS,
    ASSET_ROLES,
    ATTESTATION_NOTE_MAX,
    ATTESTATION_NOTE_MIN,
    CursorError,
    DESCRIPTION_MAX,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    LISTING_STATUSES,
    MAX_CASH_PRICE_CENTS,
    MAX_CREDIT_PRICE,
    MIN_CASH_PRICE_CENTS,
    MIN_CREDIT_PRICE,
    ORIGINS,
    ORIGIN_LABELS,
    PRICING_KINDS,
    REPORT_REASONS,
    SHELVES,
    SORT_ORDERS,
    SOURCE_EXTENSIONS,
    SUMMARY_MAX,
    TITLE_MAX,
    TITLE_MIN,
    clamp_limit,
    decode_cursor,
    derive_open_source as schema_derive_open_source,
    encode_cursor,
    handleify,
    normalize_tags,
    slugify,
    scrub_text,
    to_tsquery_text,
    content_disposition,
    safe_media_type,
)

log = logging.getLogger("chudware.gateway.marketplace")
router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])
studio_router = APIRouter(prefix="/api/studio", tags=["marketplace-studio"])

_LISTING_JOIN = """ FROM mk_listings l
 JOIN mk_sellers s ON s.email=l.seller_email
 JOIN mk_licenses lic ON lic.id=l.license_id
 LEFT JOIN mk_sources src ON src.id=l.source_id """
_COUNTERS = frozenset({"views", "downloads", "favorites", "sales"})
_RESERVED_HANDLES = frozenset({
    "admin", "api", "static", "studio", "marketplace", "support", "help",
    "about", "legal", "login", "signin", "signup", "chudware", "www",
})


def _uuid(value: object, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MarketplaceError(400, "invalid_input", f"{name} must be a UUID") from exc


def _text(value: object, name: str, maximum: int, minimum: int = 0) -> str:
    # scrub_text before length checks, not after: a NUL reaching a bound
    # parameter makes psycopg raise DataError before the statement is sent, so
    # every text body field -- listing title, collection name, report detail --
    # returned 500 instead of a validation error.
    out = scrub_text(value).strip()
    if len(out) < minimum or len(out) > maximum:
        raise MarketplaceError(
            400, "invalid_input",
            f"{name} must be between {minimum} and {maximum} characters",
        )
    return out


def _int(
    value: object, name: str, minimum: int = 0, maximum: int | None = None
) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketplaceError(400, "invalid_input", f"{name} must be an integer") from exc
    if out < minimum or (maximum is not None and out > maximum):
        raise MarketplaceError(400, "invalid_input", f"{name} is outside its allowed range")
    return out


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise MarketplaceError(400, "invalid_input", "expected a boolean")


def _seller_json(row: Any) -> dict[str, Any]:
    return {
        "email": row.get("email") or row.get("seller_email"),
        "handle": row.get("handle") or row.get("seller_handle") or "",
        "display_name": row.get("display_name") or row.get("seller_display_name") or "",
        "bio": row.get("bio") or row.get("seller_bio") or "",
        "website": row.get("website") or row.get("seller_website") or "",
        "country": row.get("country") or row.get("seller_country") or "",
        "org_id": row.get("org_id"),
        "verified": bool(row.get("verified", row.get("seller_verified", False))),
        "featured": bool(row.get("featured", row.get("seller_featured", False))),
        "publishing_blocked": bool(row.get("publishing_blocked", False)),
        "blocked_reason": row.get("blocked_reason") or "",
        "connect_charges_enabled": bool(row.get("connect_charges_enabled", False)),
        "connect_payouts_enabled": bool(row.get("connect_payouts_enabled", False)),
        "connect_details_submitted": bool(row.get("connect_details_submitted", False)),
    }


def get_seller(email: str) -> dict[str, Any] | None:
    with tx() as cur:
        cur.execute("SELECT * FROM mk_sellers WHERE email=%s", (email.lower(),))
        row = cur.fetchone()
    return dict(row) if row else None


def derive_open_source(license_id: str, pricing_kind: str) -> bool:
    with tx() as cur:
        cur.execute(
            "SELECT is_open_source FROM mk_licenses WHERE id=%s AND active",
            (license_id,),
        )
        license_row = cur.fetchone()
    if license_row is None:
        raise MarketplaceError(400, "invalid_license", "choose an active license")
    return schema_derive_open_source(
        license_is_open_source=bool(license_row["is_open_source"]),
        pricing_kind=pricing_kind,
    )


def normalize_listing_input(
    payload: dict[str, Any], *, viewer_email: str | None = None
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MarketplaceError(400, "invalid_input", "listing body must be an object")
    title = _text(payload.get("title"), "title", TITLE_MAX, TITLE_MIN)
    pricing = str(payload.get("pricing_kind") or "free").strip().lower()
    origin = str(payload.get("origin") or "").strip().lower()
    if pricing not in PRICING_KINDS:
        raise MarketplaceError(400, "invalid_pricing", "unknown pricing kind")
    if origin not in ORIGINS:
        raise MarketplaceError(400, "invalid_origin", "disclose how the model was made")
    cents = _int(
        payload.get("price_cents", 0),
        "price_cents",
        maximum=MAX_CASH_PRICE_CENTS,
    )
    credits = _int(
        payload.get("price_credits", 0),
        "price_credits",
        maximum=MAX_CREDIT_PRICE,
    )
    valid_price = (
        (pricing == "free" and cents == 0 and credits == 0)
        or (pricing == "cash" and cents >= MIN_CASH_PRICE_CENTS and credits == 0)
        or (pricing == "credits" and cents == 0 and credits >= MIN_CREDIT_PRICE)
        or (
            pricing == "cash_or_credits"
            and cents >= MIN_CASH_PRICE_CENTS
            and credits >= MIN_CREDIT_PRICE
        )
    )
    if not valid_price:
        raise MarketplaceError(
            400, "invalid_pricing", "prices do not match the pricing kind"
        )
    license_id = str(payload.get("license_id") or "").strip()
    category_id = str(payload.get("category_id") or "").strip() or None
    source_id = str(payload.get("source_id") or "").strip() or None
    forked_from = (
        _uuid(payload["forked_from"], "forked_from")
        if payload.get("forked_from")
        else None
    )
    with tx() as cur:
        cur.execute(
            "SELECT is_open_source,sellable FROM mk_licenses WHERE id=%s AND active",
            (license_id,),
        )
        license_row = cur.fetchone()
        if license_row is None:
            raise MarketplaceError(400, "invalid_license", "choose an active license")
        if pricing != "free" and not license_row["sellable"]:
            raise MarketplaceError(
                400,
                "license_not_sellable",
                "this license cannot be used for a paid listing",
            )
        if category_id:
            cur.execute(
                "SELECT 1 FROM mk_categories WHERE id=%s AND active", (category_id,)
            )
            if cur.fetchone() is None:
                raise MarketplaceError(
                    400, "invalid_category", "choose an active category"
                )
        if source_id:
            cur.execute(
                "SELECT 1 FROM mk_sources WHERE id=%s AND enabled", (source_id,)
            )
            if cur.fetchone() is None:
                raise MarketplaceError(
                    400, "invalid_source", "choose an enabled source"
                )
        if forked_from:
            cur.execute("SELECT * FROM mk_listings WHERE id=%s", (forked_from,))
            upstream = cur.fetchone()
            if upstream is None or not _visible(upstream, viewer_email, False):
                raise MarketplaceError(
                    400, "invalid_fork", "upstream listing is not available"
                )
    return {
        "title": title,
        "summary": _text(payload.get("summary", ""), "summary", SUMMARY_MAX),
        "description": _text(
            payload.get("description", ""), "description", DESCRIPTION_MAX
        ),
        "category_id": category_id,
        "license_id": license_id,
        "pricing_kind": pricing,
        "price_cents": cents,
        "price_credits": credits,
        "tips_enabled": _bool(payload.get("tips_enabled"), pricing == "free"),
        "origin": origin,
        "origin_note": _text(payload.get("origin_note", ""), "origin_note", 8000),
        "open_source": derive_open_source(license_id, pricing),
        "forked_from": forked_from,
        "source_id": source_id,
        "source_url": _text(payload.get("source_url", ""), "source_url", 4000),
        "source_author": _text(
            payload.get("source_author", ""), "source_author", 200
        ),
        "tags": normalize_tags(payload.get("tags")),
        "units": _text(payload.get("units", "mm"), "units", 16, 1),
    }


def _listing_select() -> str:
    return """SELECT l.*,
 s.handle AS seller_handle,s.display_name AS seller_display_name,
 s.bio AS seller_bio,s.website AS seller_website,s.country AS seller_country,
 s.verified AS seller_verified,s.featured AS seller_featured,
 lic.name AS license_name,lic.url AS license_url,
 lic.is_open_source AS license_is_open_source,
 lic.allows_commercial AS license_allows_commercial,
 lic.allows_derivatives AS license_allows_derivatives,
 lic.requires_attribution AS license_requires_attribution,
 lic.share_alike AS license_share_alike,lic.sellable AS license_sellable,
 src.name AS source_name,src.url AS source_home_url,
 (SELECT array_agg(a.id ORDER BY a.sort_order,a.created_at,a.id)
    FROM mk_assets a
   WHERE a.listing_id=l.id AND a.kind IN ('render','image') AND NOT a.gated)
   AS preview_asset_ids,
 FALSE AS owned,FALSE AS favorited""" + _LISTING_JOIN


def load_listing(listing_id_or_slug: str) -> dict[str, Any] | None:
    # Scrubbed here rather than at each call site: every caller passes a raw path
    # parameter, and a NUL in it makes psycopg raise DataError before the query
    # is sent -- a 500 on an anonymous GET. A scrubbed value simply matches
    # nothing, which is the honest 404 the caller already handles.
    needle = scrub_text(listing_id_or_slug)
    with tx() as cur:
        cur.execute(
            _listing_select()
            + " WHERE l.id::text=%s OR lower(l.slug)=lower(%s) LIMIT 1",
            (needle, needle),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def listing_card(row: Any, viewer_email: str | None = None) -> dict[str, Any]:
    count = int(row.get("rating_count") or 0)
    total = int(row.get("rating_sum") or 0)
    owned = bool(row.get("owned", False))
    favorited = bool(row.get("favorited", False))
    if viewer_email and not row.get("_personalized", False):
        with tx() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM mk_entitlements"
                " WHERE listing_id=%s AND email=%s) owned,"
                " EXISTS(SELECT 1 FROM mk_favorites"
                " WHERE listing_id=%s AND email=%s) favorited",
                (row["id"], viewer_email, row["id"], viewer_email),
            )
            state = cur.fetchone()
        owned = bool(state["owned"])
        favorited = bool(state["favorited"])
    preview_ids = [str(value) for value in (row.get("preview_asset_ids") or [])]
    thumbnail_id = row.get("thumbnail_asset_id")
    source = None
    if row.get("source_id"):

        source = {
            "id": row["source_id"],
            "name": row.get("source_name") or row["source_id"],
        }
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "title": row["title"],
        "summary": row.get("summary") or "",
        "seller": {
            "handle": row.get("seller_handle") or row.get("handle") or "",
            "display_name": (
                row.get("seller_display_name") or row.get("display_name") or ""
            ),
            "verified": bool(
                row.get("seller_verified", row.get("verified", False))
            ),
        },
        "pricing_kind": row["pricing_kind"],
        "price_cents": int(row.get("price_cents") or 0),
        "price_credits": int(row.get("price_credits") or 0),
        "origin": row["origin"],
        "origin_label": ORIGIN_LABELS[row["origin"]],
        "attestation_status": row.get("attestation_status") or "none",
        "human_verified": (
            row.get("origin") == "human"
            and row.get("attestation_status") == "verified"
        ),
        "open_source": bool(row.get("open_source")),
        "featured": bool(row.get("featured")),
        "license": {
            "id": row["license_id"],
            "name": row.get("license_name") or row["license_id"],
            "is_open_source": bool(row.get("license_is_open_source")),
            "allows_commercial": bool(row.get("license_allows_commercial")),
            "share_alike": bool(row.get("license_share_alike")),
        },
        "category_id": row.get("category_id"),
        "tags": list(row.get("tags") or []),
        "thumbnail_url": (
            f"/api/marketplace/assets/{thumbnail_id}" if thumbnail_id else None
        ),
        "preview_urls": [
            f"/api/marketplace/assets/{asset_id}" for asset_id in preview_ids
        ],
        "downloads": int(row.get("downloads") or 0),
        "favorites": int(row.get("favorites") or 0),
        "sales": int(row.get("sales") or 0),
        "rating": {
            "average": round(total / count, 2) if count else None,
            "count": count,
        },
        "published_at": row.get("published_at"),
        "source": source,
        "owned": owned,
        "favorited": favorited,
    }
def _personalize_rows(rows: list[Any], email: str | None) -> None:
    if not email or not rows:
        return
    ids = [row["id"] for row in rows]
    with tx() as cur:
        cur.execute(
            "SELECT listing_id,'owned' kind FROM mk_entitlements"
            " WHERE email=%s AND listing_id=ANY(%s::uuid[])"
            " UNION ALL SELECT listing_id,'favorited' kind FROM mk_favorites"
            " WHERE email=%s AND listing_id=ANY(%s::uuid[])",
            (email, ids, email, ids),
        )
        states = cur.fetchall()
    owned = {value["listing_id"] for value in states if value["kind"] == "owned"}
    favorites = {
        value["listing_id"] for value in states if value["kind"] == "favorited"
    }
    for row in rows:
        row["owned"] = row["id"] in owned
        row["favorited"] = row["id"] in favorites
        row["_personalized"] = True



def asset_path(listing_id: str, rel_path: str) -> Path:
    root = settings.listing_asset_root(str(listing_id)).resolve()
    raw = (rel_path or "").strip().replace("\\", "/")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or (len(raw) > 1 and raw[1] == ":")
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise MarketplaceError(400, "bad_asset_path", "invalid asset path")
    current = root
    for part in raw.split("/"):
        current /= part
        if current.is_symlink():
            raise MarketplaceError(400, "bad_asset_path", "symlinks are not allowed")
    target = current.resolve()
    if target != root and root not in target.parents:
        raise MarketplaceError(
            400, "bad_asset_path", "asset path escapes its listing"
        )
    return target


def asset_visible(
    asset_row: Any,
    listing_row: Any,
    viewer_email: str | None,
    is_admin: bool,
) -> bool:
    if asset_row.get("kind") == "evidence":
        return bool(is_admin)
    if is_admin or (
        viewer_email
        and viewer_email.lower() == listing_row["seller_email"].lower()
    ):
        return True
    if not asset_row.get("gated"):
        return True
    # Free and open-source listings serve their bytes without an entitlement,
    # and this check MUST precede the anonymous guard below.
    #
    # It used to come after it, which meant a signed-in visitor could download a
    # free CC-BY model and a signed-out one could not -- and an anonymous caller
    # can never obtain an entitlement, so the file was simply unreachable for
    # them. That silently broke downloads on the open-source shelf, the one shelf
    # whose entire audience is people who have not signed in yet.
    #
    # The listing's price is authoritative here, not the asset's `gated` flag. A
    # seller may set `gated` and later switch the listing to free, and a stale
    # flag must not outlive the paywall it belonged to. `gated` still does its
    # real job on paid listings, where it separates a free sample from the
    # deliverable.
    if (
        listing_row.get("pricing_kind") == "free"
        or listing_row.get("open_source")
    ):
        return True
    if not viewer_email:
        return False
    if "_entitled" in asset_row:
        return bool(asset_row["_entitled"])
    with tx() as cur:
        cur.execute(
            "SELECT 1 FROM mk_entitlements WHERE listing_id=%s AND email=%s",
            (listing_row["id"], viewer_email),
        )
        return cur.fetchone() is not None


def awaiting_first_paid_review(cur: Any, listing_row: Any) -> bool:
    """Whether this listing is held for a first-paid-listing commercial review.

    A seller's FIRST paid listing is reviewed by a human; later ones publish
    immediately. Free listings are never held for this reason.

    Exported and takes an open cursor because two modules need the same answer
    inside their own transaction: ``studio_submit_listing`` here, deciding where a
    submission lands, and ``admin_marketplace.decide_attestation``, deciding
    whether verifying a provenance claim is enough to release a listing held in
    review. Those two must never disagree -- if they did, verifying an attestation
    could publish a paid listing no moderator had approved, or leave a free
    human-made listing stuck in review forever with nothing left to review. Both
    are invisible until someone tries it, so the predicate has one definition.
    """
    if listing_row["pricing_kind"] == "free":
        return False
    if not settings.marketplace_review_first_paid:
        return False
    cur.execute(
        "SELECT 1 FROM mk_listings WHERE seller_email=%s"
        " AND status='published' AND pricing_kind<>'free' AND id<>%s"
        " LIMIT 1",
        (listing_row["seller_email"].lower(), listing_row["id"]),
    )
    return cur.fetchone() is None


def bump_counter(listing_id: str, column: str, delta: int) -> None:
    if column not in _COUNTERS or not isinstance(delta, int):
        raise ValueError("unsupported counter update")
    with tx() as cur:
        cur.execute(
            f"UPDATE mk_listings SET {column}=GREATEST(0,{column}+%s) WHERE id=%s",
            (delta, listing_id),
        )


def _visible(row: Any, viewer: str | None, admin: bool) -> bool:
    if (
        admin
        or (viewer and viewer.lower() == row["seller_email"].lower())
        or row["status"] == "published"
    ):
        return True
    if not viewer or row["status"] not in {"unlisted", "removed"}:
        return False
    with tx() as cur:
        cur.execute(
            "SELECT 1 FROM mk_entitlements WHERE listing_id=%s AND email=%s",
            (row["id"], viewer),
        )
        return cur.fetchone() is not None


def listing_detail(
    row: Any, viewer_email: str | None = None, is_admin: bool = False
) -> dict[str, Any]:
    with tx() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM mk_entitlements"
            " WHERE listing_id=%s AND email=%s) owned,"
            " EXISTS(SELECT 1 FROM mk_favorites"
            " WHERE listing_id=%s AND email=%s) favorited",
            (row["id"], viewer_email or "", row["id"], viewer_email or ""),
        )
        personal = cur.fetchone()
        enriched = dict(row)
        enriched.update(personal)
        enriched["_personalized"] = True
        cur.execute(
            "SELECT * FROM mk_assets WHERE listing_id=%s AND kind<>'evidence'"
            " ORDER BY sort_order,created_at,id",
            (row["id"],),
        )
        assets = [dict(value) for value in cur.fetchall()]
        cur.execute(
            "SELECT id,label,changelog,is_current,created_at FROM mk_versions"
            " WHERE listing_id=%s ORDER BY created_at DESC",
            (row["id"],),
        )
        versions = cur.fetchall()
        cur.execute(
            "SELECT process_note,tools,hours,ai_tools_note,status,reviewed_at"
            " FROM mk_attestations WHERE listing_id=%s AND status<>'revoked'"
            " ORDER BY submitted_at DESC LIMIT 1",
            (row["id"],),
        )
        attestation = cur.fetchone()
        upstream = None
        if row.get("forked_from"):
            cur.execute(
                "SELECT id,slug,title,seller_email,status FROM mk_listings WHERE id=%s",
                (row["forked_from"],),
            )
            upstream = cur.fetchone()
            if upstream is not None and not _visible(upstream, viewer_email, is_admin):
                upstream = None
        cur.execute(
            "SELECT id,slug,title FROM mk_listings"
            " WHERE forked_from=%s AND status='published'"
            " ORDER BY published_at DESC LIMIT 50",
            (row["id"],),
        )
        forks = cur.fetchall()
        cur.execute(
            "SELECT rating,count(*) AS n FROM mk_reviews"
            " WHERE listing_id=%s AND NOT hidden GROUP BY rating",
            (row["id"],),
        )
        rating_rows = cur.fetchall()
        cur.execute(
            "SELECT count(*) AS n FROM mk_listings"
            " WHERE seller_email=%s AND status='published'",
            (row["seller_email"],),
        )
        seller_count = int(cur.fetchone()["n"])

    entitled = bool(personal["owned"])
    asset_items = []
    downloadable = False
    for asset in assets:
        asset["_entitled"] = entitled
        allowed = asset_visible(asset, row, viewer_email, is_admin)
        if allowed and asset["kind"] in {"source", "export", "document"}:
            downloadable = True
        asset_items.append({
            "id": str(asset["id"]),
            "kind": asset["kind"],
            "role": asset["role"],
            "filename": asset["filename"],
            "format": asset["format"],
            "bytes": int(asset["bytes"]),
            "gated": bool(asset["gated"]),
            "url": (
                (
                    f"/api/marketplace/download/{asset['id']}"
                    if asset["gated"]
                    else f"/api/marketplace/assets/{asset['id']}"
                )
                if allowed
                else None
            ),
            "width": asset.get("width"),
            "height": asset.get("height"),
        })

    card = listing_card(enriched, viewer_email)
    card["license"] = {
        "id": row["license_id"],
        "name": row.get("license_name") or row["license_id"],
        "url": row.get("license_url") or "",
        "is_open_source": bool(row.get("license_is_open_source")),
        "allows_commercial": bool(row.get("license_allows_commercial")),
        "allows_derivatives": bool(row.get("license_allows_derivatives")),
        "requires_attribution": bool(row.get("license_requires_attribution")),
        "share_alike": bool(row.get("license_share_alike")),
        "sellable": bool(row.get("license_sellable")),
    }
    card["seller"] = {
        "handle": row.get("seller_handle") or "",
        "display_name": row.get("seller_display_name") or "",
        "bio": row.get("seller_bio") or "",
        "website": row.get("seller_website") or "",
        "country": row.get("seller_country") or "",
        "verified": bool(row.get("seller_verified")),
        "featured": bool(row.get("seller_featured")),
        "listing_count": seller_count,
    }
    histogram = [0] * 5
    for rating_row in rating_rows:
        histogram[int(rating_row["rating"]) - 1] = int(rating_row["n"])
    rating_count = int(row.get("rating_count") or 0)
    rating_sum = int(row.get("rating_sum") or 0)
    card.update({
        "description": row.get("description") or "",
        "units": row.get("units") or "mm",
        "bbox_mm": row.get("bbox_mm"),
        "part_count": int(row.get("part_count") or 0),
        "triangle_count": int(row.get("triangle_count") or 0),
        "assets": asset_items,
        "versions": [{
            "id": str(version["id"]),
            "label": version["label"],
            "changelog": version["changelog"],
            "is_current": bool(version["is_current"]),
            "created_at": version["created_at"],
        } for version in versions],
        "attestation": dict(attestation) if attestation else None,
        "forked_from": ({
            "id": str(upstream["id"]),
            "slug": upstream["slug"],
            "title": upstream["title"],
        } if upstream else None),
        "forks": [{
            "id": str(fork["id"]),
            "slug": fork["slug"],
            "title": fork["title"],
        } for fork in forks],
        "stats": {
            "views": int(row.get("views") or 0),
            "downloads": int(row.get("downloads") or 0),
            "favorites": int(row.get("favorites") or 0),
            "sales": int(row.get("sales") or 0),
        },
        "reviews": {
            "average": (
                round(rating_sum / rating_count, 2) if rating_count else None
            ),
            "count": rating_count,
            "histogram": histogram,
        },
        "source": ({
            "id": row["source_id"],
            "name": row.get("source_name") or row["source_id"],
            "url": row.get("source_home_url") or "",
            "source_url": row.get("source_url") or "",
            "source_author": row.get("source_author") or "",
        } if row.get("source_id") else None),
        "can": {
            "download": downloadable,
            "buy": bool(
                viewer_email
                and row["status"] == "published"
                and row["pricing_kind"] != "free"
                and not entitled
                and viewer_email.lower() != row["seller_email"].lower()
            ),
            "review": bool(
                viewer_email
                and viewer_email.lower() != row["seller_email"].lower()
            ),
            "edit": bool(
                is_admin
                or (
                    viewer_email
                    and viewer_email.lower() == row["seller_email"].lower()
                )
            ),
        },
    })
    return card


def _browse_state(request: Request) -> dict[str, Any]:
    query = request.query_params
    sort = query.get("sort", "newest")
    search = to_tsquery_text(query.get("q", ""))
    shelf = query.get("shelf", "all")
    if sort not in SORT_ORDERS:
        raise MarketplaceError(400, "invalid_sort", "unknown sort order")
    if shelf not in SHELVES:
        raise MarketplaceError(400, "invalid_shelf", "unknown shelf")
    if sort == "relevance" and not search:
        sort = "newest"

    def optional_number(name: str) -> int | None:
        value = query.get(name)
        return None if value in (None, "") else _int(value, name)

    return {
        "q": search,
        "shelf": shelf,
        # Every string below is bound as a SQL parameter, so each is scrubbed of
        # NUL first: psycopg raises DataError before the statement is sent, which
        # surfaced as a 500 on an anonymous GET rather than a validation error.
        # `tags` needs no scrub -- normalize_tags already strips control bytes,
        # which is exactly why ?tag= survived this bug class while ?q= did not.
        "category": scrub_text(query.get("category")) or None,
        "license": scrub_text(query.get("license")) or None,
        "origin": scrub_text(query.get("origin")) or None,
        "tags": normalize_tags(query.getlist("tag")),
        "source": scrub_text(query.get("source")) or None,
        "min_price_cents": optional_number("min_price_cents"),
        "max_price_cents": optional_number("max_price_cents"),
        "max_credits": optional_number("max_credits"),
        "pricing": scrub_text(query.get("pricing")) or None,
        "verified_only": _bool(query.get("verified_only")),
        "commercial_only": _bool(query.get("commercial_only")),
        "no_share_alike": _bool(query.get("no_share_alike")),
        "featured_only": _bool(query.get("featured_only")),
        "seller": scrub_text(query.get("seller")) or None,
        "sort": sort,
        "cursor": scrub_text(query.get("cursor")) or "",
        "limit": clamp_limit(query.get("limit")),
    }


def _browse_where(
    state: dict[str, Any], omit: str | None = None
) -> tuple[str, list[Any]]:
    clauses = ["l.status='published'"]
    args: list[Any] = []
    shelf_sql = SHELVES[state["shelf"]]
    if shelf_sql:
        clauses.append(shelf_sql.removeprefix("AND "))
    filters = (
        ("q", "l.search_doc @@ websearch_to_tsquery('english',%s)", None),
        ("category", "l.category_id=%s", "category"),
        ("license", "l.license_id=%s", "license"),
        ("origin", "l.origin=%s", "origin"),
        ("source", "l.source_id=%s", None),
        ("min_price_cents", "l.price_cents>=%s", None),
        ("max_price_cents", "l.price_cents<=%s", None),
        ("max_credits", "l.price_credits<=%s", None),
        ("pricing", "l.pricing_kind=%s", None),
        ("seller", "lower(s.handle)=lower(%s)", None),
    )
    for key, sql, dimension in filters:
        value = state.get(key)
        if value not in (None, "") and omit != dimension:
            clauses.append(sql)
            args.append(value)
    if state["pricing"] and state["pricing"] not in PRICING_KINDS:
        raise MarketplaceError(400, "invalid_pricing", "unknown pricing filter")
    if state["tags"] and omit != "tag":
        clauses.append("l.tags && %s::text[]")
        args.append(state["tags"])
    if state["verified_only"]:
        clauses.append("s.verified")
    if state["commercial_only"] and omit != "license":
        clauses.append("lic.allows_commercial")
    if state["no_share_alike"] and omit != "license":
        clauses.append("NOT lic.share_alike")
    if state["featured_only"]:
        clauses.append("l.featured")
    return " AND ".join(clauses), args


def _cursor_clause(sort: str, keys: list[object]) -> tuple[str, list[object]]:
    arity = {
        "newest": 2,
        "oldest": 2,
        "downloads": 2,
        "popular": 2,
        "rating": 3,
        "price_low": 3,
        "price_high": 3,
        "relevance": 3,
    }
    if len(keys) != arity[sort]:
        raise MarketplaceError(400, "invalid_cursor", "malformed cursor")
    if sort == "newest":
        return "(published_at,id)<(%s,%s)", keys
    if sort == "oldest":
        return "(published_at,id)>(%s,%s)", keys
    if sort == "downloads":
        return "(downloads,id)<(%s,%s)", keys
    if sort == "popular":
        return "(popularity,id)<(%s,%s)", keys
    if sort == "rating":
        return "(rating_score,rating_count,id)<(%s,%s,%s)", keys
    if sort == "price_high":
        return "(price_cents,price_credits,id)<(%s,%s,%s)", keys
    if sort == "price_low":
        return (
            "(price_cents>%s OR (price_cents=%s AND"
            " (price_credits>%s OR (price_credits=%s AND id<%s))))",
            [keys[0], keys[0], keys[1], keys[1], keys[2]],
        )
    return "(rank,published_at,id)<(%s,%s,%s)", keys


def _cursor_keys(row: Any, sort: str) -> list[Any]:
    if sort in {"newest", "oldest"}:
        return [row["published_at"], row["id"]]
    if sort == "downloads":
        return [row["downloads"], row["id"]]
    if sort == "popular":
        return [row["popularity"], row["id"]]
    if sort == "rating":
        return [row["rating_score"], row["rating_count"], row["id"]]
    if sort in {"price_low", "price_high"}:
        return [row["price_cents"], row["price_credits"], row["id"]]
    return [row["rank"], row["published_at"], row["id"]]


def _facet_counts(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    queries: list[str] = []
    args: list[Any] = []
    for dimension in ("category", "license", "origin", "tag"):
        where, values = _browse_where(state, dimension)
        args.extend(values)
        if dimension == "category":
            sql = (
                "SELECT 'category' dimension,COALESCE(l.category_id,'') id,"
                " COALESCE(c.name,'Uncategorized') name,count(*) n"
                + _LISTING_JOIN
                + " LEFT JOIN mk_categories c ON c.id=l.category_id WHERE "
                + where
                + " GROUP BY l.category_id,c.name"
            )
        elif dimension == "license":
            sql = (
                "SELECT 'license' dimension,l.license_id id,lic.name,count(*) n"
                + _LISTING_JOIN
                + " WHERE "
                + where
                + " GROUP BY l.license_id,lic.name"
            )
        elif dimension == "origin":
            sql = (
                "SELECT 'origin' dimension,l.origin id,l.origin name,count(*) n"
                + _LISTING_JOIN
                + " WHERE "
                + where
                + " GROUP BY l.origin"
            )
        else:
            sql = (
                "SELECT 'tag' dimension,tag id,tag name,count(*) n"
                + _LISTING_JOIN
                + " CROSS JOIN LATERAL unnest(l.tags) tag WHERE "
                + where
                + " GROUP BY tag ORDER BY n DESC,tag LIMIT 30"
            )
        queries.append("(" + sql + ")")
    with tx() as cur:
        cur.execute(" UNION ALL ".join(queries), args)
        rows = cur.fetchall()
    result: dict[str, list[dict[str, Any]]] = {
        "categories": [],
        "licenses": [],
        "origins": [],
        "tags": [],
    }
    for row in rows:
        if row["dimension"] == "category":
            result["categories"].append({
                "id": row["id"], "name": row["name"], "count": int(row["n"])
            })
        elif row["dimension"] == "license":
            result["licenses"].append({
                "id": row["id"], "name": row["name"], "count": int(row["n"])
            })
        elif row["dimension"] == "origin":
            result["origins"].append({
                "id": row["id"],
                "label": ORIGIN_LABELS[row["id"]],
                "count": int(row["n"]),
            })
        else:
            result["tags"].append({"tag": row["id"], "count": int(row["n"])})
    for key in ("categories", "licenses", "origins"):
        result[key].sort(key=lambda item: -item["count"])
    return result


@router.get("/meta")
def marketplace_meta(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    optional_user(request)
    with tx() as cur:
        cur.execute(
            "SELECT id,parent_id,name,icon,blurb FROM mk_categories"
            " WHERE active ORDER BY sort_order,name"
        )
        categories = cur.fetchall()
        cur.execute(
            "SELECT id,name,url,is_open_source,allows_commercial,"
            " allows_derivatives,requires_attribution,share_alike,sellable"
            " FROM mk_licenses WHERE active ORDER BY sort_order,name"
        )
        licenses = cur.fetchall()
        cur.execute(
            "SELECT id,name,url,kind FROM mk_sources"
            " WHERE enabled ORDER BY sort_order,name"
        )
        sources = cur.fetchall()
    return {
        "categories": [dict(row) for row in categories],
        "licenses": [dict(row) for row in licenses],
        "sources": [dict(row) for row in sources],
        "origins": [{"id": value, "label": ORIGIN_LABELS[value]} for value in ORIGINS],
        "pricing_kinds": list(PRICING_KINDS),
        "shelves": list(SHELVES),
        "sorts": list(SORT_ORDERS),
        "report_reasons": list(REPORT_REASONS),
        "credits_per_dollar": settings.marketplace_credits_per_dollar,
        "fee": {
            "cash_bps": settings.marketplace_fee_bps,
            "credit_bps": settings.marketplace_credit_fee_bps,
            "credits_per_dollar": settings.marketplace_credits_per_dollar,
            "payout_min_credits": settings.marketplace_payout_min_credits,
        },
        "limits": {
            "max_asset_bytes": settings.marketplace_max_asset_bytes,
            "seller_quota_bytes": settings.marketplace_seller_quota_bytes,
            "source_extensions": list(SOURCE_EXTENSIONS),
            "image_extensions": list(IMAGE_EXTENSIONS),
            "document_extensions": list(DOCUMENT_EXTENSIONS),
        },
    }


@router.get("/handle-check")
def handle_check(request: Request, handle: str = "") -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = optional_user(request)
    if user:
        api_writes.enforce(user.email, "handle checks")
    normalized = handleify(handle)
    reason = "ok"
    if not normalized or normalized != handle.strip().lower():
        reason = "invalid"
    elif normalized in _RESERVED_HANDLES:
        reason = "reserved"
    else:
        with tx() as cur:
            cur.execute(
                "SELECT 1 FROM mk_sellers WHERE lower(handle)=lower(%s)", (normalized,)
            )
            if cur.fetchone():
                reason = "taken"
    base = normalized or "maker"
    suggestion = base
    suggestion_needs_variant = reason in {"taken", "reserved"}
    if reason == "invalid":
        with tx() as cur:
            cur.execute(
                "SELECT 1 FROM mk_sellers WHERE lower(handle)=lower(%s)",
                (base,),
            )
            suggestion_needs_variant = (
                base in _RESERVED_HANDLES or cur.fetchone() is not None
            )
    if suggestion_needs_variant:
        with tx() as cur:
            for number in range(2, 10000):
                candidate = f"{base[:35]}-{number}"
                if candidate in _RESERVED_HANDLES:
                    continue
                cur.execute(
                    "SELECT 1 FROM mk_sellers WHERE lower(handle)=lower(%s)",
                    (candidate,),
                )
                if cur.fetchone() is None:
                    suggestion = candidate
                    break
    return {
        "handle": normalized,
        "available": reason == "ok",
        "reason": reason,
        "suggestion": suggestion,
    }


@router.get("/listings")
def browse_listings(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = optional_user(request)
    viewer = user.email.lower() if user else None
    state = _browse_state(request)
    where, where_args = _browse_where(state)
    query = f"""WITH filtered AS (
 SELECT l.*,s.handle AS seller_handle,s.display_name AS seller_display_name,
 s.verified AS seller_verified,s.featured AS seller_featured,
 lic.name AS license_name,lic.is_open_source AS license_is_open_source,
 lic.allows_commercial AS license_allows_commercial,
 lic.share_alike AS license_share_alike,
 src.name AS source_name,src.url AS source_home_url,
 EXISTS(SELECT 1 FROM mk_entitlements e
  WHERE %s::text IS NOT NULL AND e.listing_id=l.id AND e.email=%s) owned,
 EXISTS(SELECT 1 FROM mk_favorites f
  WHERE %s::text IS NOT NULL AND f.listing_id=l.id AND f.email=%s) favorited,
 TRUE AS _personalized,
 CASE WHEN %s='' THEN 0.0 ELSE
  ts_rank_cd(l.search_doc,websearch_to_tsquery('english',%s)) END rank,
 (l.views+l.downloads*8+l.favorites*20) popularity,
 ((l.rating_sum+17.5)/(l.rating_count+5.0)) rating_score,
 (SELECT array_agg(a.id ORDER BY a.sort_order,a.created_at,a.id)
  FROM mk_assets a WHERE a.listing_id=l.id
   AND a.kind IN ('render','image') AND NOT a.gated) preview_asset_ids
 {_LISTING_JOIN} WHERE {where})
 SELECT * FROM filtered"""
    args: list[Any] = [viewer, viewer, viewer, viewer, state["q"], state["q"]]
    args.extend(where_args)
    if state["cursor"]:
        try:
            keys = decode_cursor(state["cursor"], expect_sort=state["sort"])
        except CursorError as exc:
            raise MarketplaceError(
                400, "invalid_cursor", "malformed cursor"
            ) from exc
        cursor_sql, cursor_args = _cursor_clause(state["sort"], keys)
        query += " WHERE " + cursor_sql
        args.extend(cursor_args)
    order_sql = SORT_ORDERS[state["sort"]]
    if state["sort"] == "popular":
        order_sql = "popularity DESC,id DESC"
    elif state["sort"] == "rating":
        order_sql = "rating_score DESC,rating_count DESC,id DESC"
    query += f" ORDER BY {order_sql} LIMIT %s"
    args.append(state["limit"] + 1)
    with tx() as cur:
        cur.execute(query, args)
        rows = cur.fetchall()
    page = rows[: state["limit"]]
    next_cursor = (
        encode_cursor(state["sort"], _cursor_keys(page[-1], state["sort"]))
        if len(rows) > state["limit"] and page
        else None
    )
    return {
        "items": [listing_card(row, viewer) for row in page],
        "next_cursor": next_cursor,
        "facets": _facet_counts(state),
    }


@router.get("/listings/{listing_id}/reviews")
def listing_reviews(
    request: Request, listing_id: str, cursor: str = "", limit: int = 24
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = optional_user(request)
    row = load_listing(listing_id)
    if row is None or not _visible(
        row, user.email if user else None, bool(user and user.role == "admin")
    ):
        raise HTTPException(status_code=404, detail="not found")
    size = clamp_limit(limit)
    where = "r.listing_id=%s AND NOT r.hidden"
    args: list[Any] = [row["id"]]
    if cursor:
        try:
            keys = decode_cursor(cursor, expect_sort="reviews")
        except CursorError as exc:
            raise MarketplaceError(
                400, "invalid_cursor", "malformed cursor"
            ) from exc
        if len(keys) != 2:
            raise MarketplaceError(400, "invalid_cursor", "malformed cursor")
        where += " AND (r.created_at,r.id)<(%s,%s)"
        args.extend(keys)
    args.append(size + 1)
    with tx() as cur:
        cur.execute(
            "SELECT r.id,r.rating,r.body,r.verified_purchase,r.created_at,"
            " COALESCE(a.display,split_part(r.email,'@',1)) author_display"
            " FROM mk_reviews r LEFT JOIN accounts a ON a.email=r.email"
            f" WHERE {where} ORDER BY r.created_at DESC,r.id DESC LIMIT %s",
            args,
        )
        rows = cur.fetchall()
    page = rows[:size]
    return {
        "items": [{
            "id": str(item["id"]),
            "rating": item["rating"],
            "body": item["body"],
            "verified_purchase": bool(item["verified_purchase"]),
            "created_at": item["created_at"],
            "author": {"display": item["author_display"]},
        } for item in page],
        "next_cursor": (
            encode_cursor("reviews", [page[-1]["created_at"], page[-1]["id"]])
            if len(rows) > size
            else None
        ),
    }


@router.get("/listings/{slug_or_id}")
def get_listing_detail(request: Request, slug_or_id: str) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = optional_user(request)
    viewer = user.email.lower() if user else None
    admin = bool(user and user.role == "admin")
    row = load_listing(slug_or_id)
    if row is None or not _visible(row, viewer, admin):
        raise HTTPException(status_code=404, detail="not found")
    bump_counter(str(row["id"]), "views", 1)
    row["views"] = int(row.get("views") or 0) + 1
    return listing_detail(row, viewer, admin)


@router.get("/sellers/{handle}")
def public_seller(request: Request, handle: str) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = optional_user(request)
    viewer = user.email.lower() if user else None
    with tx() as cur:
        cur.execute(
            "SELECT * FROM mk_sellers WHERE lower(handle)=lower(%s)",
            (scrub_text(handle),),
        )
        seller = cur.fetchone()
        if seller is None:
            raise HTTPException(status_code=404, detail="not found")
        cur.execute(
            _listing_select()
            + " WHERE l.seller_email=%s AND l.status='published'"
            " ORDER BY l.published_at DESC LIMIT 96",
            (seller["email"],),
        )
        rows = cur.fetchall()
    _personalize_rows(rows, viewer)
    seller_json = {
        "handle": seller["handle"],
        "display_name": seller["display_name"],
        "bio": seller["bio"],
        "website": seller["website"],
        "country": seller["country"],
        "verified": bool(seller["verified"]),
        "featured": bool(seller["featured"]),
        "listing_count": len(rows),
    }
    return {
        "seller": seller_json,
        "items": [listing_card(row, viewer) for row in rows],
    }


@router.get("/assets/{asset_id}")
def public_asset(request: Request, asset_id: str) -> FileResponse:
    marketplace_enabled_or_404()
    optional_user(request)
    with tx() as cur:
        cur.execute(
            "SELECT a.*,l.status FROM mk_assets a"
            " JOIN mk_listings l ON l.id=a.listing_id WHERE a.id::text=%s",
            (asset_id,),
        )
        asset = cur.fetchone()
    # Evidence bytes exist only on the attestation-scoped admin route.
    if (
        asset is None
        or asset["status"] != "published"
        or asset["gated"]
        or asset["kind"] == "evidence"
    ):
        raise HTTPException(status_code=404, detail="not found")
    target = asset_path(str(asset["listing_id"]), asset["rel_path"])
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type = safe_media_type(asset["filename"])
    return FileResponse(
        str(target),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": content_disposition(asset["filename"], media_type),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/suggest")
def suggest(request: Request, q: str = "") -> dict[str, Any]:
    marketplace_enabled_or_404()
    optional_user(request)
    search = to_tsquery_text(q)
    if len(search) < 2:
        return {"items": []}
    with tx() as cur:
        cur.execute(
            "SELECT slug,title,summary FROM mk_listings"
            " WHERE status='published'"
            " AND (title ILIKE %s OR"
            " search_doc @@ websearch_to_tsquery('english',%s))"
            " ORDER BY CASE WHEN title ILIKE %s THEN 0 ELSE 1 END,"
            " downloads DESC,published_at DESC LIMIT 10",
            (f"%{search}%", search, f"{search}%"),
        )
        rows = cur.fetchall()
    return {"items": [dict(row) for row in rows]}


def _published_listing(listing_id: str) -> dict[str, Any]:
    row = load_listing(listing_id)
    if row is None or row["status"] != "published":
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.post("/listings/{listing_id}/favorite")
def add_favorite(request: Request, listing_id: str) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    listing = _published_listing(listing_id)
    with tx() as cur:
        cur.execute(
            "INSERT INTO mk_favorites(listing_id,email) VALUES(%s,%s)"
            " ON CONFLICT DO NOTHING RETURNING 1",
            (listing["id"], user.email.lower()),
        )
        if cur.fetchone():
            bump_counter(str(listing["id"]), "favorites", 1)
        cur.execute(
            "SELECT favorites FROM mk_listings WHERE id=%s", (listing["id"],)
        )
        count = int(cur.fetchone()["favorites"])
    return {"favorited": True, "favorites": count}


@router.delete("/listings/{listing_id}/favorite")
def remove_favorite(request: Request, listing_id: str) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    listing = _published_listing(listing_id)
    with tx() as cur:
        cur.execute(
            "DELETE FROM mk_favorites WHERE listing_id=%s AND email=%s RETURNING 1",
            (listing["id"], user.email.lower()),
        )
        if cur.fetchone():
            bump_counter(str(listing["id"]), "favorites", -1)
        cur.execute(
            "SELECT favorites FROM mk_listings WHERE id=%s", (listing["id"],)
        )
        count = int(cur.fetchone()["favorites"])
    return {"favorited": False, "favorites": count}


@router.post("/listings/{listing_id}/reviews")
def write_review(
    request: Request, listing_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    email = user.email.lower()
    rating = _int(body.get("rating"), "rating", 1, 5)
    review_body = _text(body.get("body", ""), "body", 4000)
    with tx() as cur:
        cur.execute(
            "SELECT id,seller_email,status FROM mk_listings"
            " WHERE id::text=%s OR lower(slug)=lower(%s) FOR UPDATE",
            (listing_id, listing_id),
        )
        listing = cur.fetchone()
        if listing is None or listing["status"] != "published":
            raise HTTPException(status_code=404, detail="not found")
        if email == listing["seller_email"].lower():
            raise MarketplaceError(
                400, "self_review", "sellers cannot review their own listings"
            )
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM mk_entitlements"
            " WHERE listing_id=%s AND email=%s) verified",
            (listing["id"], email),
        )
        verified = bool(cur.fetchone()["verified"])
        cur.execute(
            "INSERT INTO mk_reviews"
            " (id,listing_id,email,rating,body,verified_purchase)"
            " VALUES(%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT(listing_id,email) DO UPDATE SET"
            " rating=EXCLUDED.rating,body=EXCLUDED.body,"
            " verified_purchase=EXCLUDED.verified_purchase,"
            " hidden=FALSE,updated_at=now()"
            " RETURNING id,rating,body,verified_purchase,created_at,updated_at",
            (
                str(uuid.uuid4()),
                listing["id"],
                email,
                rating,
                review_body,
                verified,
            ),
        )
        review = cur.fetchone()
        cur.execute(
            "UPDATE mk_listings SET rating_sum=stats.rating_sum,"
            " rating_count=stats.rating_count FROM"
            " (SELECT COALESCE(sum(rating),0)::integer rating_sum,"
            " count(*)::integer rating_count FROM mk_reviews"
            " WHERE listing_id=%s AND NOT hidden) stats"
            " WHERE mk_listings.id=%s",
            (listing["id"], listing["id"]),
        )
    return {"review": dict(review)}


@router.post("/listings/{listing_id}/report")
def report_listing(
    request: Request, listing_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    listing = _published_listing(listing_id)
    reason = str(body.get("reason") or "").strip().lower()
    if reason not in REPORT_REASONS:
        raise MarketplaceError(400, "invalid_report", "choose a report reason")
    with tx() as cur:
        cur.execute(
            "INSERT INTO mk_reports"
            " (id,listing_id,reporter_email,reason,detail)"
            " VALUES(%s,%s,%s,%s,%s)"
            " ON CONFLICT(listing_id,reporter_email) WHERE status='open'"
            " DO UPDATE SET reason=EXCLUDED.reason,detail=EXCLUDED.detail"
            " RETURNING id,status",
            (
                str(uuid.uuid4()),
                listing["id"],
                user.email.lower(),
                reason,
                _text(body.get("detail", ""), "detail", 8000),
            ),
        )
        report = cur.fetchone()
    return {
        "report": {"id": str(report["id"]), "status": report["status"]}
    }


@router.get("/download/{asset_id}")
def download_asset(request: Request, asset_id: str) -> FileResponse:
    marketplace_enabled_or_404()
    user = optional_user(request)
    viewer = user.email.lower() if user else None
    is_admin = bool(user and user.role == "admin")
    with tx() as cur:
        cur.execute(
            "SELECT a.*,l.status,l.seller_email,l.pricing_kind,l.open_source"
            " FROM mk_assets a JOIN mk_listings l ON l.id=a.listing_id"
            " WHERE a.id::text=%s",
            (asset_id,),
        )
        asset = cur.fetchone()
        if asset is None or asset["kind"] == "evidence":
            raise HTTPException(status_code=404, detail="not found")
        listing = dict(asset)
        listing["id"] = asset["listing_id"]
        if not _visible(listing, viewer, is_admin) or not asset_visible(
            asset, listing, viewer, is_admin
        ):
            raise HTTPException(status_code=404, detail="not found")
        if viewer:
            cur.execute(
                "INSERT INTO mk_downloads(listing_id,email,day)"
                " VALUES(%s,%s,CURRENT_DATE)"
                " ON CONFLICT(listing_id,email,day) DO UPDATE"
                " SET count=mk_downloads.count+1 RETURNING count",
                (asset["listing_id"], viewer),
            )
            if int(cur.fetchone()["count"]) == 1:
                bump_counter(str(asset["listing_id"]), "downloads", 1)
    target = asset_path(str(asset["listing_id"]), asset["rel_path"])
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type = safe_media_type(asset["filename"])
    return FileResponse(
        str(target),
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": content_disposition(asset["filename"], media_type),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _collection_json(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "blurb": row["blurb"],
        "is_public": bool(row["is_public"]),
        "item_count": int(row.get("item_count") or 0),
    }


def _collection_slug(
    cur: Any, email: str, name: str, exclude_id: str | None = None
) -> str:
    base = slugify(name, max_len=61)
    if not base:
        raise MarketplaceError(
            400, "invalid_name", "collection name needs letters or numbers"
        )
    candidate = base
    for number in range(2, 1000):
        cur.execute(
            "SELECT 1 FROM mk_collections WHERE email=%s"
            " AND lower(slug)=lower(%s)"
            " AND (%s::uuid IS NULL OR id<>%s::uuid)",
            (email, candidate, exclude_id, exclude_id),
        )
        if cur.fetchone() is None:
            return candidate
        suffix = f"-{number}"
        candidate = base[: 61 - len(suffix)].rstrip("-") + suffix
    raise MarketplaceError(
        409, "collection_name_taken", "choose a different collection name"
    )


def _load_collections(email: str) -> list[dict[str, Any]]:
    with tx() as cur:
        cur.execute(
            "SELECT c.*,count(i.listing_id) item_count FROM mk_collections c"
            " LEFT JOIN mk_collection_items i ON i.collection_id=c.id"
            " WHERE c.email=%s GROUP BY c.id ORDER BY c.created_at DESC",
            (email,),
        )
        collections = cur.fetchall()
        result = []
        for collection in collections:
            cur.execute(
                _listing_select()
                + " JOIN mk_collection_items ci ON ci.listing_id=l.id"
                " WHERE ci.collection_id=%s AND l.status='published'"
                " ORDER BY ci.sort_order,ci.added_at DESC",
                (collection["id"],),
            )
            rows = cur.fetchall()
            _personalize_rows(rows, email)
            value = _collection_json(collection)
            value["items"] = [listing_card(row, email) for row in rows]
            result.append(value)
    return result


@router.get("/collections")
def get_collections(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    return {"items": _load_collections(user.email.lower())}


@router.post("/collections")
def create_collection(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    email = user.email.lower()
    name = _text(body.get("name"), "name", 80, 1)
    try:
        with tx() as cur:
            slug = _collection_slug(cur, email, name)
            cur.execute(
                "INSERT INTO mk_collections"
                " (id,email,name,slug,blurb,is_public)"
                " VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    str(uuid.uuid4()),
                    email,
                    name,
                    slug,
                    _text(body.get("blurb", ""), "blurb", 1000),
                    _bool(body.get("is_public")),
                ),
            )
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name != "mk_collections_slug_unique":
            raise
        raise MarketplaceError(
            409, "collection_name_taken", "choose a different collection name"
        ) from exc
    value = _collection_json(row)
    value["items"] = []
    return {"collection": value}


@router.patch("/collections/{collection_id}")
def patch_collection(
    request: Request, collection_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    email = user.email.lower()
    try:
        with tx() as cur:
            cur.execute(
                "SELECT * FROM mk_collections"
                " WHERE id::text=%s AND email=%s FOR UPDATE",
                (collection_id, email),
            )
            current = cur.fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="not found")
            name = _text(body.get("name", current["name"]), "name", 80, 1)
            slug = (
                _collection_slug(cur, email, name, str(current["id"]))
                if name != current["name"]
                else current["slug"]
            )
            cur.execute(
                "UPDATE mk_collections SET name=%s,slug=%s,blurb=%s,is_public=%s"
                " WHERE id=%s RETURNING *",
                (
                    name,
                    slug,
                    _text(body.get("blurb", current["blurb"]), "blurb", 1000),
                    _bool(body.get("is_public"), bool(current["is_public"])),
                    current["id"],
                ),
            )
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name != "mk_collections_slug_unique":
            raise
        raise MarketplaceError(
            409, "collection_name_taken", "choose a different collection name"
        ) from exc
    return {"collection": _collection_json(row)}


@router.delete("/collections/{collection_id}")
def delete_collection(request: Request, collection_id: str) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        cur.execute(
            "DELETE FROM mk_collections WHERE id::text=%s AND email=%s RETURNING 1",
            (collection_id, user.email.lower()),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.put("/collections/{collection_id}/items/{listing_id}")
def add_collection_item(
    request: Request, collection_id: str, listing_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    listing = _published_listing(listing_id)
    with tx() as cur:
        cur.execute(
            "SELECT id FROM mk_collections WHERE id::text=%s AND email=%s",
            (collection_id, user.email.lower()),
        )
        collection = cur.fetchone()
        if collection is None:
            raise HTTPException(status_code=404, detail="not found")
        cur.execute(
            "INSERT INTO mk_collection_items(collection_id,listing_id)"
            " VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (collection["id"], listing["id"]),
        )
    return {"ok": True}


@router.delete("/collections/{collection_id}/items/{listing_id}")
def remove_collection_item(
    request: Request, collection_id: str, listing_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        cur.execute(
            "SELECT id FROM mk_collections WHERE id::text=%s AND email=%s",
            (collection_id, user.email.lower()),
        )
        collection = cur.fetchone()
        if collection is None:
            raise HTTPException(status_code=404, detail="not found")
        cur.execute(
            "DELETE FROM mk_collection_items"
            " WHERE collection_id=%s AND listing_id::text=%s",
            (collection["id"], listing_id),
        )
    return {"ok": True}


@router.get("/me")
def marketplace_me(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    email = user.email.lower()
    seller = get_seller(email)
    with tx() as cur:
        cur.execute(
            _listing_select()
            + " JOIN mk_favorites mf ON mf.listing_id=l.id"
            " WHERE mf.email=%s AND l.status='published'"
            " ORDER BY mf.created_at DESC LIMIT 96",
            (email,),
        )
        favorites = cur.fetchall()
        cur.execute(
            _listing_select()
            + " JOIN mk_entitlements me ON me.listing_id=l.id"
            " WHERE me.email=%s"
            " AND l.status IN ('published','unlisted','removed')"
            " ORDER BY me.granted_at DESC LIMIT 96",
            (email,),
        )
        entitlements = cur.fetchall()
    _personalize_rows(favorites, email)
    _personalize_rows(entitlements, email)
    return {
        "account": {"email": email, "display": user.display},
        "seller": _seller_json(seller) if seller else None,
        "favorites": [listing_card(row, email) for row in favorites],
        "entitlements": [listing_card(row, email) for row in entitlements],
        "collections": _load_collections(email),
    }


def _owned_listing(
    listing_id: str,
    email: str,
    *,
    lock: bool = False,
    cur: Any | None = None,
) -> dict[str, Any]:
    if cur is None:
        with tx() as owned_cur:
            return _owned_listing(
                listing_id, email, lock=lock, cur=owned_cur
            )
    suffix = " FOR UPDATE OF l" if lock else ""
    cur.execute(
        _listing_select()
        + " WHERE l.id::text=%s AND l.seller_email=%s"
        + suffix,
        (listing_id, email.lower()),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return dict(row)


def _ensure_evidence_mutable(cur: Any, listing_id: object) -> None:
    cur.execute(
        "SELECT 1 FROM mk_attestations"
        " WHERE listing_id=%s AND status<>'revoked' LIMIT 1",
        (listing_id,),
    )
    if cur.fetchone():
        raise MarketplaceError(
            409,
            "evidence_locked",
            "evidence cannot change while an attestation is live",
        )


def _studio_asset(asset: Any) -> dict[str, Any]:
    return {
        "id": str(asset["id"]),
        "version_id": (
            str(asset["version_id"]) if asset.get("version_id") else None
        ),
        "kind": asset["kind"],
        "role": asset["role"],
        "filename": asset["filename"],
        "content_type": asset["content_type"],
        "format": asset["format"],
        "bytes": int(asset["bytes"]),
        "sha256": asset["sha256"],
        "width": asset.get("width"),
        "height": asset.get("height"),
        "sort_order": asset["sort_order"],
        "gated": bool(asset["gated"]),
        "created_at": asset["created_at"],
    }


def _studio_listing(row: Any) -> dict[str, Any]:
    with tx() as cur:
        cur.execute(
            "SELECT * FROM mk_assets WHERE listing_id=%s"
            " ORDER BY sort_order,created_at,id",
            (row["id"],),
        )
        assets = cur.fetchall()
        cur.execute(
            "SELECT id,label,changelog,is_current,created_at FROM mk_versions"
            " WHERE listing_id=%s ORDER BY created_at DESC",
            (row["id"],),
        )
        versions = cur.fetchall()
        cur.execute(
            "SELECT * FROM mk_attestations WHERE listing_id=%s"
            " ORDER BY submitted_at DESC LIMIT 1",
            (row["id"],),
        )
        attestation = cur.fetchone()
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "status": row["status"],
        "title": row["title"],
        "summary": row["summary"],
        "description": row["description"],
        "category_id": row["category_id"],
        "license_id": row["license_id"],
        "pricing_kind": row["pricing_kind"],
        "price_cents": int(row["price_cents"]),
        "price_credits": int(row["price_credits"]),
        "tips_enabled": bool(row["tips_enabled"]),
        "origin": row["origin"],
        "origin_note": row["origin_note"],
        "attestation_status": row["attestation_status"],
        "open_source": bool(row["open_source"]),
        "featured": bool(row.get("featured")),
        "forked_from": (
            str(row["forked_from"]) if row.get("forked_from") else None
        ),
        "source_id": row.get("source_id"),
        "source_url": row.get("source_url") or "",
        "source_author": row.get("source_author") or "",
        "tags": list(row.get("tags") or []),
        "units": row.get("units") or "mm",
        "bbox_mm": row.get("bbox_mm"),
        "part_count": int(row.get("part_count") or 0),
        "triangle_count": int(row.get("triangle_count") or 0),
        "thumbnail_url": (
            f"/api/marketplace/assets/{row['thumbnail_asset_id']}"
            if row.get("thumbnail_asset_id")
            else None
        ),
        "stats": {
            "views": int(row.get("views") or 0),
            "downloads": int(row.get("downloads") or 0),
            "favorites": int(row.get("favorites") or 0),
            "sales": int(row.get("sales") or 0),
        },
        "review_note": row.get("review_note") or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "published_at": row.get("published_at"),
        "assets": [_studio_asset(asset) for asset in assets],
        "versions": [
            {**dict(version), "id": str(version["id"])} for version in versions
        ],
        "attestation": dict(attestation) if attestation else None,
    }


def _seller_for(request: Request) -> tuple[Any, dict[str, Any]]:
    user = require_user(request)
    seller = get_seller(user.email)
    require_seller(request, seller)
    return user, seller  # type: ignore[return-value]


def _validate_org(cur: Any, email: str, org_id: object) -> str | None:
    if not org_id:
        return None
    canonical = _uuid(org_id, "org_id")
    cur.execute(
        "SELECT role FROM org_members WHERE org_id=%s AND email=%s",
        (canonical, email),
    )
    membership = cur.fetchone()
    if membership is None or membership["role"] not in {"owner", "admin"}:
        raise MarketplaceError(
            403,
            "org_access_denied",
            "organization admin access is required",
        )
    return canonical


@studio_router.get("/seller")
def studio_get_seller(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    seller = get_seller(user.email)
    return {"seller": _seller_json(seller) if seller else None}


@studio_router.post("/seller")
def studio_create_seller(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace writes")
    raw_handle = str(body.get("handle") or "").strip().lower()
    handle = handleify(raw_handle)
    if handle != raw_handle or not handle or handle in _RESERVED_HANDLES:
        raise MarketplaceError(
            400,
            "invalid_handle",
            "use an unreserved 2-39 character seller handle",
        )
    email = user.email.lower()
    try:
        with tx() as cur:
            cur.execute("SELECT 1 FROM mk_sellers WHERE email=%s", (email,))
            if cur.fetchone():
                raise MarketplaceError(
                    409, "seller_exists", "seller account already exists"
                )
            cur.execute(
                "SELECT 1 FROM mk_sellers WHERE lower(handle)=lower(%s)", (handle,)
            )
            if cur.fetchone():
                raise MarketplaceError(
                    409, "handle_taken", "that seller handle is already in use"
                )
            org_id = _validate_org(cur, email, body.get("org_id"))
            cur.execute(
                "INSERT INTO mk_sellers"
                " (email,handle,display_name,bio,website,country,org_id)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    email,
                    handle,
                    _text(
                        body.get("display_name") or user.display,
                        "display_name",
                        120,
                        1,
                    ),
                    _text(body.get("bio", ""), "bio", 8000),
                    _text(body.get("website", ""), "website", 1000),
                    _text(body.get("country", ""), "country", 100),
                    org_id,
                ),
            )
            seller = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "mk_sellers_handle_unique":
            raise MarketplaceError(
                409, "handle_taken", "that seller handle is already in use"
            ) from exc
        if exc.diag.constraint_name == "mk_sellers_pkey":
            raise MarketplaceError(
                409, "seller_exists", "seller account already exists"
            ) from exc
        raise
    return {"seller": _seller_json(seller)}


@studio_router.patch("/seller")
def studio_patch_seller(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    email = user.email.lower()
    try:
        with tx() as cur:
            cur.execute(
                "SELECT * FROM mk_sellers WHERE email=%s FOR UPDATE", (email,)
            )
            current = cur.fetchone()
            handle = str(body.get("handle", current["handle"])).strip().lower()
            if handle != current["handle"]:
                cur.execute(
                    "SELECT 1 FROM mk_listings WHERE seller_email=%s LIMIT 1",
                    (email,),
                )
                if cur.fetchone():
                    raise MarketplaceError(
                        409,
                        "handle_immutable",
                        "the handle cannot change after a listing exists",
                    )
                if handleify(handle) != handle or handle in _RESERVED_HANDLES:
                    raise MarketplaceError(
                        400, "invalid_handle", "use a valid seller handle"
                    )
                cur.execute(
                    "SELECT 1 FROM mk_sellers"
                    " WHERE lower(handle)=lower(%s) AND email<>%s",
                    (handle, email),
                )
                if cur.fetchone():
                    raise MarketplaceError(
                        409, "handle_taken", "that seller handle is already in use"
                    )
            org_id = _validate_org(
                cur, email, body.get("org_id", current["org_id"])
            )
            cur.execute(
                "UPDATE mk_sellers SET handle=%s,display_name=%s,bio=%s,"
                " website=%s,country=%s,org_id=%s WHERE email=%s RETURNING *",
                (
                    handle,
                    _text(
                        body.get("display_name", current["display_name"]),
                        "display_name",
                        120,
                        1,
                    ),
                    _text(body.get("bio", current["bio"]), "bio", 8000),
                    _text(
                        body.get("website", current["website"]), "website", 1000
                    ),
                    _text(
                        body.get("country", current["country"]), "country", 100
                    ),
                    org_id,
                    email,
                ),
            )
            updated = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name != "mk_sellers_handle_unique":
            raise
        raise MarketplaceError(
            409, "handle_taken", "that seller handle is already in use"
        ) from exc
    return {"seller": _seller_json(updated)}


@studio_router.get("/overview")
def studio_overview(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, seller = _seller_for(request)
    with tx() as cur:
        cur.execute(
            "SELECT count(*) listings,"
            " count(*) FILTER(WHERE status='published') published,"
            " count(*) FILTER(WHERE status='draft') drafts,"
            " count(*) FILTER(WHERE status='in_review') in_review,"
            " COALESCE(sum(downloads),0) downloads,"
            " COALESCE(sum(sales),0) sales,"
            " COALESCE(sum(favorites),0) favorites"
            " FROM mk_listings WHERE seller_email=%s",
            (user.email.lower(),),
        )
        stats = dict(cur.fetchone())
        cur.execute(
            _listing_select()
            + " WHERE l.seller_email=%s ORDER BY l.updated_at DESC LIMIT 8",
            (user.email.lower(),),
        )
        recent = cur.fetchall()
    return {
        "seller": _seller_json(seller),
        "stats": {key: int(value) for key, value in stats.items()},
        "recent_listings": [_studio_listing(row) for row in recent],
    }


def _merged_listing_payload(row: Any, body: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "title", "summary", "description", "category_id", "license_id",
        "pricing_kind", "price_cents", "price_credits", "tips_enabled",
        "origin", "origin_note", "forked_from", "source_id", "source_url",
        "source_author", "tags", "units",
    )
    return {key: body[key] if key in body else row.get(key) for key in keys}


def _listing_slug_candidates(title: str, listing_id: str) -> Iterator[str]:
    base = slugify(title) or f"model-{listing_id[:8]}"
    yield base
    for number in range(2, 1000):
        suffix = f"-{number}"
        yield base[: 79 - len(suffix)].rstrip("-") + suffix
    yield f"model-{listing_id}"


@studio_router.get("/listings")
def studio_listings(
    request: Request, status: str = "", cursor: str = "", limit: int = 24
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    size = clamp_limit(limit)
    where = "l.seller_email=%s"
    args: list[Any] = [user.email.lower()]
    if status:
        if status not in LISTING_STATUSES:
            raise MarketplaceError(400, "invalid_status", "unknown listing status")
        where += " AND l.status=%s"
        args.append(status)
    if cursor:
        try:
            keys = decode_cursor(cursor, expect_sort="studio")
        except CursorError as exc:
            raise MarketplaceError(
                400, "invalid_cursor", "malformed cursor"
            ) from exc
        if len(keys) != 2:
            raise MarketplaceError(400, "invalid_cursor", "malformed cursor")
        where += " AND (l.updated_at,l.id)<(%s,%s)"
        args.extend(keys)
    args.append(size + 1)
    with tx() as cur:
        cur.execute(
            _listing_select()
            + f" WHERE {where} ORDER BY l.updated_at DESC,l.id DESC LIMIT %s",
            args,
        )
        rows = cur.fetchall()
    page = rows[:size]
    return {
        "items": [_studio_listing(row) for row in page],
        "next_cursor": (
            encode_cursor("studio", [page[-1]["updated_at"], page[-1]["id"]])
            if len(rows) > size
            else None
        ),
    }


@studio_router.post("/listings")
def studio_create_listing(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    data = normalize_listing_input(body, viewer_email=user.email.lower())
    listing_id = str(uuid.uuid4())
    for slug in _listing_slug_candidates(data["title"], listing_id):
        try:
            with tx() as cur:
                cur.execute(
                    "INSERT INTO mk_listings"
                    " (id,slug,seller_email,title,summary,description,category_id,"
                    " license_id,status,pricing_kind,price_cents,price_credits,"
                    " tips_enabled,origin,origin_note,attestation_status,open_source,"
                    " forked_from,source_id,source_url,source_author,tags,units)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,"
                    " 'none',%s,%s,%s,%s,%s,%s,%s)",
                    (
                        listing_id, slug, user.email.lower(), data["title"],
                        data["summary"], data["description"], data["category_id"],
                        data["license_id"], data["pricing_kind"], data["price_cents"],
                        data["price_credits"], data["tips_enabled"], data["origin"],
                        data["origin_note"], data["open_source"], data["forked_from"],
                        data["source_id"], data["source_url"], data["source_author"],
                        data["tags"], data["units"],
                    ),
                )
            break
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name != "mk_listings_slug_unique":
                raise
    else:
        raise MarketplaceError(
            409, "listing_slug_taken", "could not allocate a unique listing slug"
        )
    return {
        "listing": _studio_listing(
            _owned_listing(listing_id, user.email)
        )
    }


@studio_router.get("/listings/{listing_id}")
def studio_get_listing(
    request: Request, listing_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    return {
        "listing": _studio_listing(_owned_listing(listing_id, user.email))
    }


@studio_router.patch("/listings/{listing_id}")
def studio_patch_listing(
    request: Request, listing_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        current = _owned_listing(
            listing_id, user.email, lock=True, cur=cur
        )
        if current["status"] in {"in_review", "removed"}:
            raise MarketplaceError(
                409,
                "listing_locked",
                "this listing cannot be edited in its current state",
            )
        data = normalize_listing_input(
            _merged_listing_payload(current, body), viewer_email=user.email.lower()
        )
        if data["forked_from"] == str(current["id"]):
            raise MarketplaceError(
                400, "invalid_fork", "a listing cannot be forked from itself"
            )
        if current["status"] == "published":
            if (
                current["pricing_kind"] == "free"
                and data["pricing_kind"] != "free"
            ):
                raise MarketplaceError(
                    409,
                    "resubmit_required",
                    "unlist and submit again before converting a free listing to paid",
                )
            if current["origin"] != "human" and data["origin"] == "human":
                raise MarketplaceError(
                    409,
                    "resubmit_required",
                    "unlist, attest, and submit again before claiming human-made origin",
                )
        attestation_status = (
            current["attestation_status"] if data["origin"] == "human" else "none"
        )
        slugs: Iterator[str]
        if data["title"] != current["title"]:
            slugs = _listing_slug_candidates(data["title"], str(current["id"]))
        else:
            slugs = iter((current["slug"],))
        for slug in slugs:
            try:
                with cur.connection.transaction():
                    cur.execute(
                        "UPDATE mk_listings SET slug=%s,title=%s,summary=%s,"
                        " description=%s,category_id=%s,license_id=%s,pricing_kind=%s,"
                        " price_cents=%s,price_credits=%s,tips_enabled=%s,origin=%s,"
                        " origin_note=%s,attestation_status=%s,open_source=%s,"
                        " forked_from=%s,source_id=%s,source_url=%s,source_author=%s,"
                        " tags=%s,units=%s,updated_at=now() WHERE id=%s",
                        (
                            slug, data["title"], data["summary"], data["description"],
                            data["category_id"], data["license_id"],
                            data["pricing_kind"], data["price_cents"],
                            data["price_credits"], data["tips_enabled"],
                            data["origin"], data["origin_note"], attestation_status,
                            data["open_source"], data["forked_from"], data["source_id"],
                            data["source_url"], data["source_author"], data["tags"],
                            data["units"], current["id"],
                        ),
                    )
                break
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name != "mk_listings_slug_unique":
                    raise
            except psycopg.errors.CheckViolation as exc:
                if exc.diag.constraint_name != "mk_listings_no_self_fork":
                    raise
                raise MarketplaceError(
                    400, "invalid_fork", "a listing cannot be forked from itself"
                ) from exc
        else:
            raise MarketplaceError(
                409, "listing_slug_taken", "could not allocate a unique listing slug"
            )
    return {
        "listing": _studio_listing(_owned_listing(listing_id, user.email))
    }


@studio_router.delete("/listings/{listing_id}")
def studio_delete_listing(
    request: Request, listing_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        listing = _owned_listing(
            listing_id, user.email, lock=True, cur=cur
        )
        if listing["status"] != "draft":
            raise MarketplaceError(
                409, "draft_only", "only draft listings can be deleted"
            )
        cur.execute("SELECT id,filename FROM mk_assets WHERE listing_id=%s AND kind IN ('source','export')",
                    (listing["id"],))
        staged_assets = cur.fetchall()
        cur.execute("DELETE FROM mk_listings WHERE id=%s", (listing["id"],))
    root = settings.listing_asset_root(str(listing["id"]))
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove listing assets %s", root, exc_info=True)
    try:
        from .marketplace_render import cleanup_listing_asset_stage
    except ImportError:
        from marketplace_render import cleanup_listing_asset_stage
    for asset in staged_assets:
        cleanup_listing_asset_stage(user.email, str(listing["id"]), str(asset["id"]), asset["filename"])
    return {"ok": True}


def _allowed_extensions(kind: str) -> tuple[str, ...]:
    if kind in {"source", "export"}:
        return SOURCE_EXTENSIONS
    if kind == "image":
        return IMAGE_EXTENSIONS
    if kind == "document":
        return DOCUMENT_EXTENSIONS
    if kind == "evidence":
        return IMAGE_EXTENSIONS + DOCUMENT_EXTENSIONS
    return ()


def _normalized_filename(raw: object) -> str:
    filename = Path(str(raw or "").replace("\\", "/")).name
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise MarketplaceError(
            400, "invalid_filename", "file names cannot contain control characters"
        )
    filename = re.sub(r"\.{2,}", ".", filename)
    if not filename or filename.startswith("."):
        raise MarketplaceError(
            400, "unsupported_format", "choose a named file"
        )
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MarketplaceError(
            400, "invalid_filename", "file name is not valid UTF-8"
        ) from exc
    if len(encoded) > 255:
        raise MarketplaceError(
            400, "invalid_filename", "file name is too long"
        )
    return filename


def _cleanup_upload(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove failed upload %s", target, exc_info=True)
    try:
        target.parent.rmdir()
    except OSError:
        pass


@studio_router.post("/listings/{listing_id}/assets")
async def studio_upload_asset(request: Request, listing_id: str) -> dict[str, Any]:
    # Feature, seller and listing authorization precede multipart parsing/spooling.
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    uploads.enforce(user.email, "marketplace asset uploads")
    listing = await asyncio.to_thread(_owned_listing, listing_id, user.email)
    cap = settings.marketplace_max_asset_bytes + SMALL_REQUEST_BYTES
    async with bounded_form(request, cap, max_fields=5) as form:
        file = form.get("file")
        kind = form.get("kind")
        role = form.get("role", "")
        gated = form.get("gated", "true")
        version_id = form.get("version_id", "")
        if (not isinstance(file, UploadFile)
                or any(not isinstance(value, str) for value in (kind, role, gated, version_id))):
            raise HTTPException(422, "file and asset fields required")
        boolean = gated.lower()
        if boolean not in {"true", "1", "yes", "on", "t", "y", "false", "0", "no", "off", "f", "n"}:
            raise HTTPException(422, "gated must be a boolean")
        return await _store_studio_asset(
            user, listing_id, listing, file, kind, role,
            boolean in {"true", "1", "yes", "on", "t", "y"}, version_id,
        )


async def _store_studio_asset(
    user: Any, listing_id: str, listing: dict[str, Any], file: UploadFile,
    kind: str, role: str, gated: bool, version_id: str,
) -> dict[str, Any]:
    kind = kind.strip().lower()
    role = role.strip().lower()
    if kind not in ASSET_KINDS or kind == "render":
        raise MarketplaceError(
            400, "unsupported_format", "this asset kind cannot be uploaded"
        )
    if role not in ASSET_ROLES:
        raise MarketplaceError(400, "invalid_role", "unknown asset role")
    filename = _normalized_filename(file.filename)
    lower_name = filename.lower()
    extension = next(
        (value for value in _allowed_extensions(kind) if lower_name.endswith(value)),
        None,
    )
    if extension is None:
        raise MarketplaceError(
            400,
            "unsupported_format",
            "the file extension does not match its asset kind",
        )
    version = _uuid(version_id, "version_id") if version_id else None
    asset_id = str(uuid.uuid4())
    rel_path = f"uploads/{asset_id}/{filename}"
    actual_gated = (
        True if kind in ALWAYS_GATED_KINDS
        else False if kind in ALWAYS_UNGATED_KINDS
        else bool(gated)
    )
    content_type = safe_media_type(filename)
    # No listing/seller locks are held while the upload yields or touches disk.
    target = asset_path(str(listing["id"]), rel_path)
    committed = False
    staged = ""
    def commit_upload(size: int, sha256: str) -> dict[str, Any]:
        with tx() as cur:
            listing = _owned_listing(listing_id, user.email, lock=True, cur=cur)
            if kind == "evidence":
                _ensure_evidence_mutable(cur, listing["id"])
            if version:
                cur.execute(
                    "SELECT 1 FROM mk_versions WHERE id=%s AND listing_id=%s",
                    (version, listing["id"]),
                )
                if cur.fetchone() is None:
                    raise MarketplaceError(
                        400, "invalid_version", "version does not belong to this listing"
                    )
            cur.execute(
                "SELECT email FROM mk_sellers WHERE email=%s FOR UPDATE",
                (user.email.lower(),),
            )
            cur.execute(
                "SELECT COALESCE(sum(a.bytes),0) used FROM mk_assets a"
                " JOIN mk_listings l ON l.id=a.listing_id"
                " WHERE l.seller_email=%s",
                (user.email.lower(),),
            )
            if int(cur.fetchone()["used"]) + size > settings.marketplace_seller_quota_bytes:
                raise MarketplaceError(413, "quota_exceeded", "seller asset quota exceeded")
            cur.execute(
                "INSERT INTO mk_assets"
                " (id,listing_id,version_id,kind,role,filename,rel_path,"
                " content_type,format,bytes,sha256,gated)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    asset_id, listing["id"], version, kind, role, filename,
                    rel_path, content_type, extension.removeprefix("."), size,
                    sha256, actual_gated,
                ),
            )
            return dict(cur.fetchone())
    try:
        target.parent.mkdir(parents=True, exist_ok=False)
        with target.open("xb") as output:
            digest = hashlib.sha256()
            size = 0
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > settings.marketplace_max_asset_bytes:
                    raise MarketplaceError(
                        413, "asset_too_large", "asset exceeds the upload limit"
                    )
                digest.update(chunk)
                output.write(chunk)
        if kind in {"source", "export"}:
            try:
                from .marketplace_render import stage_listing_asset
            except ImportError:
                from marketplace_render import stage_listing_asset
            staging = asyncio.create_task(asyncio.to_thread(
                stage_listing_asset, user.email, str(listing["id"]), asset_id, rel_path, filename
            ))
            try:
                staged = await asyncio.shield(staging)
            except asyncio.CancelledError:
                staged = await staging
                raise
        # Reauthorize and serialize quota/metadata only after all file I/O.
        mutation = asyncio.create_task(
            asyncio.to_thread(commit_upload, size, digest.hexdigest())
        )
        try:
            asset = await asyncio.shield(mutation)
            committed = True
        except asyncio.CancelledError:
            await mutation
            committed = True
            raise
    except psycopg.errors.UniqueViolation as exc:
        if target is not None:
            _cleanup_upload(target)
        if exc.diag.constraint_name == "mk_assets_sha_unique":
            raise MarketplaceError(
                409,
                "duplicate_asset",
                "this listing already contains the same file",
            ) from exc
        if exc.diag.constraint_name == "mk_assets_one_thumbnail":
            raise MarketplaceError(
                409,
                "thumbnail_exists",
                "this listing already has a thumbnail",
            ) from exc
        raise
    except psycopg.errors.CheckViolation as exc:
        if target is not None:
            _cleanup_upload(target)
        if exc.diag.constraint_name == "mk_assets_rel_path_relative":
            raise MarketplaceError(
                400, "invalid_filename", "file name cannot be stored safely"
            ) from exc
        raise
    except BaseException:
        if target is not None and not committed:
            _cleanup_upload(target)
        raise
    finally:
        if staged and not committed:
            try:
                from .marketplace_render import cleanup_listing_asset_stage
            except ImportError:
                from marketplace_render import cleanup_listing_asset_stage
            await asyncio.to_thread(cleanup_listing_asset_stage, user.email,
                                    str(listing["id"]), asset_id, filename)
    return {"asset": _studio_asset(asset)}


@studio_router.delete("/listings/{listing_id}/assets/{asset_id}")
def studio_delete_asset(
    request: Request, listing_id: str, asset_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        listing = _owned_listing(
            listing_id, user.email, lock=True, cur=cur
        )
        cur.execute(
            "SELECT * FROM mk_assets WHERE id::text=%s AND listing_id=%s"
            " FOR UPDATE",
            (asset_id, listing["id"]),
        )
        asset = cur.fetchone()
        if asset is None:
            raise HTTPException(status_code=404, detail="not found")
        if asset["kind"] == "evidence":
            _ensure_evidence_mutable(cur, listing["id"])
        cur.execute("DELETE FROM mk_assets WHERE id=%s", (asset["id"],))
    target = asset_path(str(listing["id"]), asset["rel_path"])
    try:
        target.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove asset file %s", target, exc_info=True)
    try:
        target.parent.rmdir()
    except OSError:
        pass
    if asset["kind"] in {"source", "export"}:
        try:
            from .marketplace_render import cleanup_listing_asset_stage
        except ImportError:
            from marketplace_render import cleanup_listing_asset_stage
        cleanup_listing_asset_stage(user.email, str(listing["id"]), str(asset["id"]), asset["filename"])
    return {"ok": True}


@studio_router.patch("/listings/{listing_id}/assets/{asset_id}")
def studio_patch_asset(
    request: Request,
    listing_id: str,
    asset_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    try:
        with tx() as cur:
            listing = _owned_listing(
                listing_id, user.email, lock=True, cur=cur
            )
            cur.execute(
                "SELECT * FROM mk_assets WHERE id::text=%s AND listing_id=%s"
                " FOR UPDATE",
                (asset_id, listing["id"]),
            )
            asset = cur.fetchone()
            if asset is None:
                raise HTTPException(status_code=404, detail="not found")
            if asset["kind"] == "evidence":
                _ensure_evidence_mutable(cur, listing["id"])
            role = str(body.get("role", asset["role"])).strip().lower()
            if role not in ASSET_ROLES:
                raise MarketplaceError(400, "invalid_role", "unknown asset role")
            sort_order = _int(
                body.get("sort_order", asset["sort_order"]),
                "sort_order",
                maximum=1_000_000,
            )
            gated = _bool(body.get("gated"), bool(asset["gated"]))
            if asset["kind"] in ALWAYS_GATED_KINDS:
                gated = True
            elif asset["kind"] in ALWAYS_UNGATED_KINDS:
                gated = False
            cur.execute(
                "UPDATE mk_assets SET role=%s,sort_order=%s,gated=%s"
                " WHERE id=%s RETURNING *",
                (role, sort_order, gated, asset["id"]),
            )
            updated = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name != "mk_assets_one_thumbnail":
            raise
        raise MarketplaceError(
            409,
            "thumbnail_exists",
            "this listing already has a thumbnail",
        ) from exc
    return {"asset": _studio_asset(updated)}


@studio_router.post("/listings/{listing_id}/versions")
def studio_create_version(
    request: Request, listing_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    listing = _owned_listing(listing_id, user.email)
    label = _text(body.get("label"), "label", 40, 1)
    changelog = _text(body.get("changelog", ""), "changelog", 8000)
    with tx() as cur:
        cur.execute(
            "SELECT 1 FROM mk_versions"
            " WHERE listing_id=%s AND lower(label)=lower(%s)",
            (listing["id"], label),
        )
        if cur.fetchone():
            raise MarketplaceError(
                409,
                "version_exists",
                "this listing already has a version with that label",
            )
        cur.execute(
            "UPDATE mk_versions SET is_current=FALSE"
            " WHERE listing_id=%s AND is_current",
            (listing["id"],),
        )
        cur.execute(
            "INSERT INTO mk_versions"
            " (id,listing_id,label,changelog,is_current)"
            " VALUES(%s,%s,%s,%s,TRUE)"
            " RETURNING id,label,changelog,is_current,created_at",
            (
                str(uuid.uuid4()),
                listing["id"],
                label,
                changelog,
            ),
        )
        version = cur.fetchone()
    return {"version": {**dict(version), "id": str(version["id"])}}


@studio_router.post("/listings/{listing_id}/submit")
def studio_submit_listing(
    request: Request, listing_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        listing = _owned_listing(
            listing_id, user.email, lock=True, cur=cur
        )
        if listing["status"] not in {"draft", "rejected", "unlisted"}:
            raise MarketplaceError(
                409, "invalid_status", "this listing cannot be submitted"
            )
        cur.execute(
            "SELECT sellable FROM mk_licenses WHERE id=%s",
            (listing["license_id"],),
        )
        license_row = cur.fetchone()
        paid = listing["pricing_kind"] != "free"
        if paid and (license_row is None or not license_row["sellable"]):
            raise MarketplaceError(
                400,
                "license_not_sellable",
                "this license cannot be used for a paid listing",
            )
        verified_attestation = False
        if listing["origin"] == "human":
            cur.execute(
                "SELECT status FROM mk_attestations"
                " WHERE listing_id=%s AND status<>'revoked'"
                " ORDER BY submitted_at DESC LIMIT 1",
                (listing["id"],),
            )
            attestation = cur.fetchone()
            if attestation is None:
                raise MarketplaceError(
                    400,
                    "attestation_required",
                    "submit a human-made attestation first",
                )
            verified_attestation = attestation["status"] == "verified"
        first_paid_review = awaiting_first_paid_review(cur, listing)
        publish = not first_paid_review and (
            listing["origin"] != "human" or verified_attestation
        )
        new_status = "published" if publish else "in_review"
        cur.execute(
            "UPDATE mk_listings SET status=%s,"
            " published_at=CASE WHEN %s='published' THEN now() ELSE NULL END,"
            " attestation_status=CASE WHEN %s THEN 'verified'"
            " ELSE attestation_status END,"
            " updated_at=now(),review_note='' WHERE id=%s",
            (new_status, new_status, verified_attestation, listing["id"]),
        )
    if publish:
        try:
            from marketplace_render import queue_render
            queue_render(str(listing["id"]))
        except Exception:
            log.warning(
                "listing published but preview queue failed for %s",
                listing["id"],
                exc_info=True,
            )
    return {
        "listing": _studio_listing(_owned_listing(listing_id, user.email))
    }


@studio_router.post("/listings/{listing_id}/unlist")
def studio_unlist_listing(
    request: Request, listing_id: str
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        listing = _owned_listing(
            listing_id, user.email, lock=True, cur=cur
        )
        if listing["status"] not in {"published", "in_review"}:
            raise MarketplaceError(
                409, "invalid_status", "this listing is not live or under review"
            )
        cur.execute(
            "UPDATE mk_listings SET status='unlisted',published_at=NULL,"
            " updated_at=now() WHERE id=%s",
            (listing["id"],),
        )
    return {
        "listing": _studio_listing(_owned_listing(listing_id, user.email))
    }


@studio_router.post("/listings/{listing_id}/attestation")
def studio_submit_attestation(
    request: Request, listing_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller_for(request)
    api_writes.enforce(user.email, "marketplace writes")
    with tx() as cur:
        listing = _owned_listing(
            listing_id, user.email, lock=True, cur=cur
        )
        if listing["origin"] != "human":
            raise MarketplaceError(
                400,
                "attestation_not_applicable",
                "attestations are only for human-made listings",
            )
        process_note = _text(
            body.get("process_note"),
            "process_note",
            ATTESTATION_NOTE_MAX,
            ATTESTATION_NOTE_MIN,
        )
        tools_raw = body.get("tools", [])
        if not isinstance(tools_raw, list):
            raise MarketplaceError(400, "invalid_input", "tools must be a list")
        tools: list[str] = []
        for raw_tool in tools_raw[:64]:
            tool = _text(raw_tool, "tool", 120, 1)
            if tool not in tools:
                tools.append(tool)
        hours_raw = body.get("hours")
        hours = None
        if hours_raw not in (None, ""):
            try:
                hours = float(hours_raw)
            except (TypeError, ValueError) as exc:
                raise MarketplaceError(
                    400, "invalid_input", "hours must be a positive number"
                ) from exc
            if (
                not math.isfinite(hours)
                or hours <= 0
                or hours > 99999.99
            ):
                raise MarketplaceError(
                    400, "invalid_input", "hours must be a positive number"
                )
        ai_tools_note = _text(
            body.get("ai_tools_note", ""), "ai_tools_note", 8000
        )
        cur.execute(
            "SELECT id FROM mk_attestations"
            " WHERE listing_id=%s AND status<>'revoked' FOR UPDATE",
            (listing["id"],),
        )
        current = cur.fetchone()
        if current:
            cur.execute(
                "UPDATE mk_attestations SET process_note=%s,tools=%s,hours=%s,"
                " ai_tools_note=%s,status='pending',submitted_at=now(),"
                " reviewed_at=NULL,reviewed_by='',review_note=''"
                " WHERE id=%s RETURNING *",
                (
                    process_note, tools, hours, ai_tools_note, current["id"],
                ),
            )
        else:
            cur.execute(
                "INSERT INTO mk_attestations"
                " (id,listing_id,email,process_note,tools,hours,ai_tools_note)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    str(uuid.uuid4()), listing["id"], user.email.lower(),
                    process_note, tools, hours, ai_tools_note,
                ),
            )
        attestation = cur.fetchone()
        cur.execute(
            "UPDATE mk_assets SET attestation_id=%s"
            " WHERE listing_id=%s AND kind='evidence' AND attestation_id IS NULL",
            (attestation["id"], listing["id"]),
        )
        cur.execute(
            "UPDATE mk_listings SET attestation_status='pending',"
            " updated_at=now() WHERE id=%s",
            (listing["id"],),
        )
    return {"attestation": dict(attestation)}
