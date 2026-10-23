"""Operator marketplace moderation, curation, and revenue routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from db import tx
from limits import api_writes
from marketplace_authz import MarketplaceError, marketplace_enabled_or_404, require_admin
from marketplace_schema import (
    ATTESTATION_REVIEW_STATUSES,
    LISTING_STATUSES,
    clamp_limit,
    content_disposition,
    decode_cursor,
    encode_cursor,
    safe_media_type,
    scrub_text,
    to_tsquery_text,
)

router = APIRouter(prefix="/api/admin/marketplace", tags=["admin-marketplace"])
log = logging.getLogger("chudware.gateway.admin_marketplace")

_LISTING_ACTIONS = frozenset(("approve", "reject", "remove", "restore", "feature", "unfeature"))
_ATTESTATION_DECISIONS = frozenset(("verified", "rejected", "revoked"))
_REPORT_DECISIONS = frozenset(("actioned", "dismissed"))
_PAYOUT_DECISIONS = frozenset(("approved", "denied"))


def _admin(request: Request) -> Any:
    admin = require_admin(request)
    marketplace_enabled_or_404()
    return admin


def _write_admin(request: Request, label: str) -> Any:
    admin = _admin(request)
    api_writes.enforce(admin.email, label)
    return admin


def _body_text(body: dict[str, Any], key: str, *, required: bool = False) -> str:
    # scrub_text, not str(): a NUL reaching a bound parameter makes psycopg raise
    # DataError before the statement is sent, turning an operator moderation note
    # into a 500. Same bug class as the storefront query parameters.
    value = scrub_text(body.get(key)).strip()
    if required and not value:
        raise MarketplaceError(400, "invalid_request", f"{key} is required")
    if len(value) > 8000:
        raise MarketplaceError(400, "invalid_request", f"{key} is too long")
    return value


def _cursor(cursor: str | None, sort: str) -> tuple[Any, Any] | None:
    if not cursor:
        return None
    try:
        keys = decode_cursor(cursor, expect_sort=sort)
    except ValueError as exc:
        raise MarketplaceError(400, "invalid_cursor", "the cursor is invalid") from exc
    if len(keys) != 2:
        raise MarketplaceError(400, "invalid_cursor", "the cursor is invalid")
    return keys[0], keys[1]


def _page(rows: list[dict[str, Any]], sort: str, time_key: str, id_key: str, limit: int) -> dict[str, Any]:
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(sort, (last[time_key], last[id_key]))
    return {"items": items, "next_cursor": next_cursor}


def _queue_stat(row: dict[str, Any]) -> dict[str, Any]:
    age = row.get("oldest_age_seconds")
    return {"depth": int(row.get("depth") or 0), "oldest_age_seconds": max(0, int(float(age))) if age is not None else None}


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    _admin(request)
    with tx() as cur:
        cur.execute("""SELECT count(*) FILTER (WHERE status = 'in_review') AS depth, extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'in_review')) AS oldest_age_seconds FROM mk_listings""")
        listing_queue = cur.fetchone()
        cur.execute("""SELECT count(*) FILTER (WHERE status = 'pending') AS depth, extract(epoch FROM now() - min(submitted_at) FILTER (WHERE status = 'pending')) AS oldest_age_seconds FROM mk_attestations""")
        attestation_queue = cur.fetchone()
        cur.execute("""SELECT count(DISTINCT listing_id) FILTER (WHERE status = 'open') AS depth, count(*) FILTER (WHERE status = 'open') AS report_count, extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'open')) AS oldest_age_seconds FROM mk_reports""")
        report_queue = cur.fetchone()
        cur.execute("""SELECT count(*) FILTER (WHERE status = 'pending') AS depth, extract(epoch FROM now() - min(requested_at) FILTER (WHERE status = 'pending')) AS oldest_age_seconds FROM mk_payout_requests""")
        payout_queue = cur.fetchone()
        cur.execute("""SELECT count(*) FILTER (WHERE status = 'pending') AS depth, extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'pending')) AS oldest_age_seconds FROM mk_imports""")
        import_queue = cur.fetchone()
        cur.execute("SELECT status, count(*) AS total FROM mk_listings GROUP BY status")
        listing_totals = {row["status"]: int(row["total"]) for row in cur.fetchall()}
        cur.execute("""SELECT count(*) AS total, count(*) FILTER (WHERE verified) AS verified, count(*) FILTER (WHERE publishing_blocked) AS blocked FROM mk_sellers""")
        seller_totals = cur.fetchone()
        cur.execute("""SELECT currency, count(*) AS unit_sales, sum(amount_cents - refunded_cents) AS gmv_cents, sum(amount_credits - refunded_credits) AS gmv_credits, sum(CASE WHEN amount_cents > 0 THEN floor(fee_cents::numeric * (amount_cents - refunded_cents) / amount_cents) ELSE 0 END) AS fee_cents, sum(CASE WHEN amount_credits > 0 THEN floor(fee_credits::numeric * (amount_credits - refunded_credits) / amount_credits) ELSE 0 END) AS fee_credits FROM mk_orders WHERE paid_at IS NOT NULL GROUP BY currency ORDER BY currency""")
        money = cur.fetchall()
    report_stat = _queue_stat(report_queue)
    report_stat["report_count"] = int(report_queue.get("report_count") or 0)
    return {
        "queues": {"listings": _queue_stat(listing_queue), "attestations": _queue_stat(attestation_queue), "reports": report_stat, "payouts": _queue_stat(payout_queue), "imports": _queue_stat(import_queue)},
        "listings_by_status": {status: listing_totals.get(status, 0) for status in LISTING_STATUSES},
        "sellers": {key: int(seller_totals.get(key) or 0) for key in ("total", "verified", "blocked")},
        "money": [{"currency": row["currency"], "unit_sales": int(row["unit_sales"] or 0), "gmv_cents": int(row["gmv_cents"] or 0), "gmv_credits": int(row["gmv_credits"] or 0), "fee_cents": int(row["fee_cents"] or 0), "fee_credits": int(row["fee_credits"] or 0)} for row in money],
    }


@router.get("/listings")
async def listings(request: Request, status: str | None = None, q: str = "", cursor: str | None = None, limit: int = 24) -> dict[str, Any]:
    _admin(request)
    page_limit = clamp_limit(limit)
    key = _cursor(cursor, "admin_listings")
    where: list[str] = []
    params: list[Any] = []
    if status:
        if status not in LISTING_STATUSES:
            raise MarketplaceError(400, "invalid_status", "unknown listing status")
        where.append("l.status = %s")
        params.append(status)
    query = to_tsquery_text(q)
    if query:
        where.append("(l.search_doc @@ websearch_to_tsquery('english', %s) OR l.title ILIKE %s)")
        params.extend((query, f"%{query}%"))
    if key:
        where.append("(l.created_at, l.id) < (%s, %s)")
        params.extend(key)
    predicate = " AND ".join(where) if where else "TRUE"
    params.append(page_limit + 1)
    with tx() as cur:
        cur.execute(f"""SELECT l.id, l.slug, l.title, l.summary, l.status, l.pricing_kind, l.price_cents, l.price_credits, l.origin, l.attestation_status, l.open_source, l.featured, l.featured_at, l.review_note, l.reviewed_by, l.created_at, l.updated_at, l.published_at, l.sales, l.downloads, s.email AS seller_email, s.handle AS seller_handle, s.display_name AS seller_name, s.verified AS seller_verified, c.name AS category_name, lic.id AS license_id, lic.name AS license_name, a.id AS thumbnail_asset_id FROM mk_listings l JOIN mk_sellers s ON s.email = l.seller_email JOIN mk_licenses lic ON lic.id = l.license_id LEFT JOIN mk_categories c ON c.id = l.category_id LEFT JOIN mk_assets a ON a.id = l.thumbnail_asset_id AND a.kind <> 'evidence' WHERE {predicate} ORDER BY l.created_at DESC, l.id DESC LIMIT %s""", tuple(params))
        rows = cur.fetchall()
    for row in rows:
        asset_id = row.pop("thumbnail_asset_id", None)
        row["thumbnail_url"] = f"/api/marketplace/assets/{asset_id}" if asset_id else None
    return _page(rows, "admin_listings", "created_at", "id", page_limit)


@router.post("/listings/{listing_id}/moderate")
async def moderate_listing(listing_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _write_admin(request, "admin marketplace listing moderation")
    action = str(body.get("action") or "").strip().lower()
    if action not in _LISTING_ACTIONS:
        raise MarketplaceError(400, "invalid_action", "unknown moderation action")
    note = _body_text(body, "note", required=action in ("reject", "remove"))
    should_render = False
    with tx() as cur:
        cur.execute(
            "SELECT id, origin, attestation_status"
            " FROM mk_listings WHERE id = %s FOR UPDATE",
            (listing_id,),
        )
        listing = cur.fetchone()
        if listing is None:
            raise MarketplaceError(404, "listing_not_found", "listing not found")
        if action in ("approve", "restore"):
            if (
                listing["origin"] == "human"
                and listing["attestation_status"] != "verified"
            ):
                raise MarketplaceError(
                    409,
                    "attestation_not_verified",
                    "decide and verify the human-made attestation before publishing",
                )
            cur.execute("""UPDATE mk_listings SET status = 'published', published_at = now(), review_note = %s, reviewed_by = %s, updated_at = now() WHERE id = %s RETURNING *""", (note, admin.email, listing_id))
            should_render = action == "approve"
        elif action in ("reject", "remove"):
            next_status = "rejected" if action == "reject" else "removed"
            cur.execute("""UPDATE mk_listings SET status = %s, published_at = NULL, featured = FALSE, featured_at = NULL, review_note = %s, reviewed_by = %s, updated_at = now() WHERE id = %s RETURNING *""", (next_status, note, admin.email, listing_id))
        else:
            featured = action == "feature"
            cur.execute("""UPDATE mk_listings SET featured = %s, featured_at = CASE WHEN %s THEN now() ELSE NULL END, review_note = %s, reviewed_by = %s, updated_at = now() WHERE id = %s RETURNING *""", (featured, featured, note, admin.email, listing_id))
        updated = dict(cur.fetchone())
    if should_render:
        try:
            from marketplace_render import queue_render
            queue_render(listing_id)
        except Exception:
            log.warning("preview render could not be queued for listing %s", listing_id, exc_info=True)
    updated["action"] = action
    return updated


@router.get("/attestations")
async def attestations(request: Request, status: str | None = None, cursor: str | None = None, limit: int = 24) -> dict[str, Any]:
    _admin(request)
    page_limit = clamp_limit(limit)
    key = _cursor(cursor, "admin_attestations")
    where: list[str] = []
    params: list[Any] = []
    if status:
        if status not in ATTESTATION_REVIEW_STATUSES:
            raise MarketplaceError(400, "invalid_status", "unknown attestation status")
        where.append("at.status = %s")
        params.append(status)
    if key:
        where.append("(at.submitted_at, at.id) > (%s, %s)")
        params.extend(key)
    predicate = " AND ".join(where) if where else "TRUE"
    params.append(page_limit + 1)
    with tx() as cur:
        cur.execute(f"""SELECT at.id, at.listing_id, at.email, at.process_note, at.tools, at.hours, at.ai_tools_note, at.status, at.submitted_at, at.reviewed_at, at.reviewed_by, at.review_note, jsonb_build_object('id', l.id, 'slug', l.slug, 'title', l.title, 'summary', l.summary, 'origin', l.origin, 'attestation_status', l.attestation_status, 'status', l.status) AS listing, jsonb_build_object('email', s.email, 'handle', s.handle, 'display_name', s.display_name, 'verified', s.verified, 'publishing_blocked', s.publishing_blocked) AS seller, coalesce((SELECT jsonb_agg(jsonb_build_object('id', a.id, 'filename', a.filename, 'content_type', a.content_type, 'width', a.width, 'height', a.height, 'role', a.role) ORDER BY a.sort_order, a.created_at) FROM mk_assets a WHERE a.attestation_id = at.id AND a.listing_id = l.id AND a.kind = 'evidence'), '[]'::jsonb) AS evidence_assets, coalesce((SELECT jsonb_agg(jsonb_build_object('id', r.id, 'filename', r.filename, 'content_type', r.content_type, 'width', r.width, 'height', r.height, 'role', r.role) ORDER BY r.sort_order, r.created_at) FROM mk_assets r WHERE r.listing_id = l.id AND r.kind IN ('render', 'image')), '[]'::jsonb) AS listing_previews FROM mk_attestations at JOIN mk_listings l ON l.id = at.listing_id JOIN mk_sellers s ON s.email = l.seller_email WHERE {predicate} ORDER BY at.submitted_at ASC, at.id ASC LIMIT %s""", tuple(params))
        rows = cur.fetchall()
    for row in rows:
        for asset in row["evidence_assets"]:
            asset["content_type"] = safe_media_type(asset["filename"])
            asset["url"] = (
                f"/api/admin/marketplace/attestations/{row['id']}/evidence/{asset['id']}"
            )
        for asset in row["listing_previews"]:
            asset["url"] = f"/api/marketplace/assets/{asset['id']}"
    return _page(rows, "admin_attestations", "submitted_at", "id", page_limit)

@router.get("/attestations/{attestation_id}/evidence/{asset_id}")
async def attestation_evidence(
    attestation_id: str, asset_id: str, request: Request
) -> FileResponse:
    admin = _admin(request)
    with tx() as cur:
        cur.execute(
            """
            SELECT l.*, a.id AS evidence_asset_id, a.rel_path AS evidence_rel_path,
                   a.kind AS evidence_kind, a.filename AS evidence_filename,
                   a.gated AS evidence_gated
            FROM mk_attestations at
            JOIN mk_listings l ON l.id = at.listing_id
            JOIN mk_assets a
              ON a.attestation_id = at.id AND a.listing_id = l.id
            WHERE at.id = %s AND a.id = %s AND a.kind = 'evidence'
            """,
            (attestation_id, asset_id),
        )
        row = cur.fetchone()
    if row is None:
        raise MarketplaceError(404, "evidence_not_found", "evidence not found")
    from marketplace import asset_path, asset_visible

    asset = {
        "id": row["evidence_asset_id"],
        "kind": row["evidence_kind"],
        "gated": row["evidence_gated"],
    }
    listing = dict(row)
    if not asset_visible(asset, listing, admin.email, True):
        raise MarketplaceError(404, "evidence_not_found", "evidence not found")
    try:
        path = asset_path(str(row["id"]), row["evidence_rel_path"])
    except MarketplaceError as exc:
        raise MarketplaceError(404, "evidence_not_found", "evidence not found") from exc
    if not path.is_file():
        raise MarketplaceError(404, "evidence_not_found", "evidence not found")
    media_type = safe_media_type(row["evidence_filename"])
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": content_disposition(
                row["evidence_filename"], "application/octet-stream"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/attestations/{attestation_id}/decide")
async def decide_attestation(attestation_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _write_admin(request, "admin marketplace attestation decisions")
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in _ATTESTATION_DECISIONS:
        raise MarketplaceError(400, "invalid_decision", "unknown attestation decision")
    note = _body_text(body, "note", required=True)
    with tx() as cur:
        # `l.id` and NOT `at.id`: this row is handed to
        # marketplace.awaiting_first_paid_review, which reads `listing_row["id"]`
        # to exclude the listing from its own "has a prior paid listing" check.
        # Selecting both would leave a dict_row where `id` is whichever column
        # came last -- correct here only by accident, and a trap for the next edit.
        cur.execute("""SELECT at.id AS attestation_id, at.listing_id, l.id, l.origin, l.status, l.pricing_kind, l.seller_email FROM mk_attestations at JOIN mk_listings l ON l.id = at.listing_id WHERE at.id = %s FOR UPDATE OF at, l""", (attestation_id,))
        claim = cur.fetchone()
        if claim is None:
            raise MarketplaceError(404, "attestation_not_found", "attestation not found")
        cur.execute("""UPDATE mk_attestations SET status = %s, reviewed_at = now(), reviewed_by = %s, review_note = %s WHERE id = %s RETURNING *""", (decision, admin.email, note, attestation_id))
        updated = dict(cur.fetchone())
        # mk_listings_attestation_needs_human forbids carrying a stale decision after origin changes.
        listing_attestation_status = (
            decision
            if claim["origin"] == "human" and decision != "rejected"
            else "none"
        )
        moved_to_review = (
            claim["origin"] == "human"
            and claim["status"] == "published"
            and decision in ("rejected", "revoked")
        )
        cur.execute(
            """
            UPDATE mk_listings
            SET attestation_status = %s,
                status = CASE WHEN %s THEN 'in_review' ELSE status END,
                published_at = CASE WHEN %s THEN NULL ELSE published_at END,
                review_note = %s,
                reviewed_by = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING attestation_status, status
            """,
            (
                listing_attestation_status,
                moved_to_review,
                moved_to_review,
                note,
                admin.email,
                claim["listing_id"],
            ),
        )
        listing = cur.fetchone()
        # Verifying provenance has to RELEASE the listing, not just relabel it.
        #
        # A human-made listing sits in 'in_review' precisely because its claim was
        # unverified. Updating attestation_status alone left it in the queue with
        # nothing left to review and no way out except a second, separate moderate
        # action nobody would know to take -- so a verified human-made model never
        # reached the shelf its verification exists for.
        #
        # The other reason a listing can be held is a seller's first paid listing
        # awaiting commercial review, and a provenance decision does NOT satisfy
        # that. marketplace.awaiting_first_paid_review is the single definition of
        # it, shared with studio_submit_listing so the two cannot drift; re-check
        # it here rather than restating the rule.
        #
        # status and published_at move in one statement because
        # mk_listings_published_at_matches_status requires the pair to agree.
        released = False
        if decision == "verified" and claim["status"] == "in_review":
            from marketplace import awaiting_first_paid_review

            if not awaiting_first_paid_review(cur, claim):
                cur.execute(
                    "UPDATE mk_listings SET status='published', published_at=now(),"
                    " updated_at=now() WHERE id=%s AND status='in_review'",
                    (claim["listing_id"],),
                )
                released = cur.rowcount == 1
        # Read the status back rather than reusing the earlier RETURNING.
        #
        # `listing` came from the attestation_status UPDATE, which ran BEFORE the
        # release above. Reporting it verbatim told an operator who had just
        # released a listing that it was still 'in_review' -- so the panel invited
        # a second, unnecessary moderate action on a listing that was already
        # published. Same hazard on the withdraw path.
        cur.execute(
            "SELECT status, attestation_status FROM mk_listings WHERE id=%s",
            (claim["listing_id"],),
        )
        final = cur.fetchone() or {}
    updated["listing_attestation_status"] = final.get(
        "attestation_status", listing["attestation_status"]
    )
    updated["listing_published"] = released
    updated["listing_status"] = final.get("status", listing["status"])
    updated["listing_moved_to_review"] = moved_to_review
    if released:
        try:
            from marketplace_render import queue_render

            queue_render(str(claim["listing_id"]))
        except Exception:
            log.warning(
                "attestation released listing %s but preview queue failed",
                claim["listing_id"],
                exc_info=True,
            )
    return updated


@router.get("/reports")
async def reports(request: Request, status: str | None = None, cursor: str | None = None, limit: int = 24) -> dict[str, Any]:
    _admin(request)
    page_limit = clamp_limit(limit)
    key = _cursor(cursor, "admin_reports")
    params: list[Any] = []
    report_filter = ""
    if status:
        report_filter = "WHERE r.status = %s"
        params.append(status)
    cursor_filter = ""
    if key:
        cursor_filter = "WHERE (g.oldest_at, g.listing_id) > (%s, %s)"
        params.extend(key)
    params.append(page_limit + 1)
    with tx() as cur:
        cur.execute(f"""WITH grouped AS (SELECT r.listing_id, min(r.created_at) AS oldest_at, count(*) AS report_count, array_agg(r.id ORDER BY r.created_at) AS report_ids, jsonb_agg(jsonb_build_object('id', r.id, 'reporter_email', r.reporter_email, 'reason', r.reason, 'detail', r.detail, 'status', r.status, 'resolution', r.resolution, 'created_at', r.created_at) ORDER BY r.created_at) AS reports FROM mk_reports r {report_filter} GROUP BY r.listing_id) SELECT g.listing_id AS id, g.listing_id, g.oldest_at, g.report_count, g.report_ids, g.reports, l.slug, l.title, l.status AS listing_status, s.handle AS seller_handle, s.display_name AS seller_name, s.publishing_blocked FROM grouped g JOIN mk_listings l ON l.id = g.listing_id JOIN mk_sellers s ON s.email = l.seller_email {cursor_filter} ORDER BY g.oldest_at ASC, g.listing_id ASC LIMIT %s""", tuple(params))
        rows = cur.fetchall()
    return _page(rows, "admin_reports", "oldest_at", "id", page_limit)


@router.post("/reports/{report_id}/resolve")
async def resolve_report(report_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _write_admin(request, "admin marketplace report resolutions")
    status = str(body.get("status") or "").strip().lower()
    if status not in _REPORT_DECISIONS:
        raise MarketplaceError(400, "invalid_status", "resolution status must be actioned or dismissed")
    resolution = _body_text(body, "resolution", required=True)
    with tx() as cur:
        cur.execute("SELECT listing_id FROM mk_reports WHERE id = %s FOR UPDATE", (report_id,))
        report = cur.fetchone()
        if report is None:
            raise MarketplaceError(404, "report_not_found", "report not found")
        cur.execute("""UPDATE mk_reports SET status = %s, resolution = %s, resolved_at = now(), resolved_by = %s WHERE listing_id = %s AND status = 'open' RETURNING id""", (status, resolution, admin.email, report["listing_id"]))
        resolved_ids = [str(row["id"]) for row in cur.fetchall()]
    return {"listing_id": str(report["listing_id"]), "status": status, "resolution": resolution, "resolved_count": len(resolved_ids), "report_ids": resolved_ids}


@router.get("/sellers")
async def sellers(request: Request, q: str = "", cursor: str | None = None, limit: int = 24) -> dict[str, Any]:
    _admin(request)
    page_limit = clamp_limit(limit)
    key = _cursor(cursor, "admin_sellers")
    where: list[str] = []
    params: list[Any] = []
    query = q.strip()[:200]
    if query:
        where.append("(s.email ILIKE %s OR s.handle ILIKE %s OR s.display_name ILIKE %s)")
        like = f"%{query}%"
        params.extend((like, like, like))
    if key:
        where.append("(s.created_at, s.email) < (%s, %s)")
        params.extend(key)
    predicate = " AND ".join(where) if where else "TRUE"
    params.append(page_limit + 1)
    with tx() as cur:
        cur.execute(f"""WITH listing_totals AS (SELECT seller_email, count(*) AS listing_count, count(*) FILTER (WHERE status = 'published') AS published_count FROM mk_listings GROUP BY seller_email), earnings AS (SELECT seller_email, coalesce(sum(net_credits) FILTER (WHERE status = 'paid'), 0) AS earned_credits, coalesce(sum(net_cents) FILTER (WHERE status = 'paid'), 0) AS earned_cents FROM mk_orders GROUP BY seller_email) SELECT s.email AS id, s.email, s.handle, s.display_name, s.country, s.verified, s.featured, s.publishing_blocked, s.blocked_reason, s.connect_account_id, s.connect_charges_enabled, s.connect_payouts_enabled, s.created_at, coalesce(l.listing_count, 0) AS listing_count, coalesce(l.published_count, 0) AS published_count, coalesce(e.earned_credits, 0) AS earned_credits, coalesce(e.earned_cents, 0) AS earned_cents FROM mk_sellers s LEFT JOIN listing_totals l ON l.seller_email = s.email LEFT JOIN earnings e ON e.seller_email = s.email WHERE {predicate} ORDER BY s.created_at DESC, s.email DESC LIMIT %s""", tuple(params))
        rows = cur.fetchall()
    return _page(rows, "admin_sellers", "created_at", "id", page_limit)


@router.post("/sellers/{email}/flags")
async def seller_flags(email: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    _write_admin(request, "admin marketplace seller flags")
    allowed = ("verified", "featured", "publishing_blocked", "blocked_reason")
    supplied = [key for key in allowed if key in body]
    if not supplied:
        raise MarketplaceError(400, "invalid_request", "provide at least one seller flag")
    with tx() as cur:
        cur.execute("SELECT * FROM mk_sellers WHERE email = %s FOR UPDATE", (email,))
        seller = cur.fetchone()
        if seller is None:
            raise MarketplaceError(404, "seller_not_found", "seller not found")
        values: dict[str, Any] = {}
        for key in supplied:
            if key == "blocked_reason":
                values[key] = str(body[key] or "").strip()[:8000]
            elif isinstance(body[key], bool):
                values[key] = body[key]
            else:
                raise MarketplaceError(400, "invalid_request", f"{key} must be boolean")
        blocked = values.get("publishing_blocked", seller["publishing_blocked"])
        reason = values.get("blocked_reason", seller["blocked_reason"])
        if blocked and not str(reason or "").strip():
            raise MarketplaceError(400, "blocked_reason_required", "blocking requires a reason")
        assignments = ", ".join(f"{key} = %s" for key in supplied)
        cur.execute(f"UPDATE mk_sellers SET {assignments} WHERE email = %s RETURNING *", tuple(values[key] for key in supplied) + (email,))
        updated = cur.fetchone()
    return dict(updated)


@router.get("/payouts")
async def payouts(request: Request, status: str | None = None, cursor: str | None = None, limit: int = 24) -> dict[str, Any]:
    _admin(request)
    page_limit = clamp_limit(limit)
    key = _cursor(cursor, "admin_payouts")
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("p.status = %s")
        params.append(status)
    if key:
        where.append("(p.requested_at, p.id) > (%s, %s)")
        params.extend(key)
    predicate = " AND ".join(where) if where else "TRUE"
    params.append(page_limit + 1)
    with tx() as cur:
        cur.execute(f"""SELECT p.id, p.seller_email, p.credits, p.cents, p.credits_per_dollar, p.status, p.stripe_transfer_id, p.note, p.requested_at, p.decided_at, p.decided_by, s.handle, s.display_name, s.connect_account_id, s.connect_payouts_enabled FROM mk_payout_requests p JOIN mk_sellers s ON s.email = p.seller_email WHERE {predicate} ORDER BY p.requested_at ASC, p.id ASC LIMIT %s""", tuple(params))
        rows = cur.fetchall()
    return _page(rows, "admin_payouts", "requested_at", "id", page_limit)


@router.post("/payouts/{payout_id}/decide")
async def decide_payout(payout_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _write_admin(request, "admin marketplace payout decisions")
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in _PAYOUT_DECISIONS:
        raise MarketplaceError(400, "invalid_decision", "decision must be approved or denied")
    note = _body_text(body, "note", required=decision == "denied")
    from marketplace_commerce import deny_payout, execute_payout
    if decision == "approved":
        return execute_payout(payout_id, admin.email)
    return deny_payout(payout_id, admin.email, note)


@router.get("/revenue")
async def revenue(request: Request, days: int = 90) -> dict[str, Any]:
    _admin(request)
    window = max(1, min(int(days), 365))
    with tx() as cur:
        cur.execute("""WITH dates AS (SELECT generate_series(current_date - (%s::integer - 1), current_date, interval '1 day')::date AS day), orders_daily AS (SELECT paid_at::date AS day, coalesce(sum(amount_cents - refunded_cents) FILTER (WHERE currency = 'usd'), 0) AS cash_gmv_cents, coalesce(sum(amount_credits - refunded_credits) FILTER (WHERE currency = 'credits'), 0) AS credit_gmv, coalesce(sum(CASE WHEN amount_cents > 0 THEN floor(fee_cents::numeric * (amount_cents - refunded_cents) / amount_cents) ELSE 0 END), 0) AS fee_cents, coalesce(sum(CASE WHEN amount_credits > 0 THEN floor(fee_credits::numeric * (amount_credits - refunded_credits) / amount_credits) ELSE 0 END), 0) AS fee_credits, count(*) AS unit_sales FROM mk_orders WHERE paid_at IS NOT NULL AND paid_at >= current_date - (%s::integer - 1) GROUP BY paid_at::date), listings_daily AS (SELECT created_at::date AS day, count(*) AS new_listings FROM mk_listings WHERE created_at >= current_date - (%s::integer - 1) GROUP BY created_at::date), sellers_daily AS (SELECT created_at::date AS day, count(*) AS new_sellers FROM mk_sellers WHERE created_at >= current_date - (%s::integer - 1) GROUP BY created_at::date) SELECT d.day, coalesce(o.cash_gmv_cents, 0) AS cash_gmv_cents, coalesce(o.credit_gmv, 0) AS credit_gmv, coalesce(o.fee_cents, 0) AS fee_cents, coalesce(o.fee_credits, 0) AS fee_credits, coalesce(o.unit_sales, 0) AS unit_sales, coalesce(l.new_listings, 0) AS new_listings, coalesce(s.new_sellers, 0) AS new_sellers FROM dates d LEFT JOIN orders_daily o ON o.day = d.day LEFT JOIN listings_daily l ON l.day = d.day LEFT JOIN sellers_daily s ON s.day = d.day ORDER BY d.day""", (window, window, window, window))
        series = cur.fetchall()
    totals = {key: sum(int(row[key] or 0) for row in series) for key in ("cash_gmv_cents", "credit_gmv", "fee_cents", "fee_credits", "unit_sales", "new_listings", "new_sellers")}
    return {"days": window, "series": series, "totals": totals}


@router.get("/imports")
async def imports(request: Request, status: str | None = None, cursor: str | None = None, limit: int = 24) -> dict[str, Any]:
    _admin(request)
    page_limit = clamp_limit(limit)
    key = _cursor(cursor, "admin_imports")
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("i.status = %s")
        params.append(status)
    if key:
        where.append("(i.created_at, i.id) > (%s, %s)")
        params.extend(key)
    predicate = " AND ".join(where) if where else "TRUE"
    params.append(page_limit + 1)
    with tx() as cur:
        cur.execute(f"""SELECT i.id, i.source_id, s.name AS source_name, s.kind AS source_kind, i.source_ref, i.source_url, i.requested_by, i.listing_id, i.status, i.error, i.created_at, i.completed_at, coalesce(i.payload->>'detected_license', i.payload->>'license_id', '') AS detected_license, coalesce(i.payload->>'outcome', i.error, '') AS outcome FROM mk_imports i JOIN mk_sources s ON s.id = i.source_id WHERE {predicate} ORDER BY i.created_at ASC, i.id ASC LIMIT %s""", tuple(params))
        rows = cur.fetchall()
    return _page(rows, "admin_imports", "created_at", "id", page_limit)
