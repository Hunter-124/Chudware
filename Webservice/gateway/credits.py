"""Prepaid AI credits: pricing cache, atomic ledger mutations, and admin controls.

One credit is $0.001 of upstream model cost. BYOK actors never enter this module's
metering path. Pooled-key debits lock the account row, append the immutable
ledger entry, and update the materialised balance in the same transaction.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, suppress
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from fastapi import APIRouter, HTTPException, Request

try:
    from .db import tx
    from . import deployment_drain
    from .limits import api_writes
except ImportError:  # uvicorn app:app from package dir
    from db import tx  # type: ignore
    import deployment_drain
    from limits import api_writes  # type: ignore

log = logging.getLogger("chudware.gateway.credits")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CREDIT_CEILING_UNLIMITED = 0
_RATE_TTL_SECONDS = 24 * 60 * 60
# Native wire IDs are not OpenRouter slugs and never arrive in its price refresh.
# Source: upstream oh-my-pi catalog (Opus 5.5 $4/$20) and Anthropic's Sonnet 5.5
# model overview ($2/$10); units are USD per million input/output tokens.
_DIRECT_MODEL_RATES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-mythos-5": (Decimal("10"), Decimal("50")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
_RECONCILE_SECONDS = 24 * 60 * 60
_reconcile_task: asyncio.Task[None] | None = None


@asynccontextmanager
async def _credits_lifespan(_app: Any):
    global _reconcile_task
    if _reconcile_task is None or _reconcile_task.done():
        _reconcile_task = asyncio.create_task(
            _nightly_reconcile(), name="credit-balance-reconciliation"
        )
    try:
        yield
    finally:
        task, _reconcile_task = _reconcile_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


router = APIRouter(tags=["credits"], lifespan=_credits_lifespan)


def credit_ceiling_is_limited(value: Any) -> bool:
    """True only for a positive cap; the locked 0 sentinel means unlimited."""
    return int(value) != CREDIT_CEILING_UNLIMITED


class CreditRateUnavailable(RuntimeError):
    """No trustworthy model price is available, so pooled dispatch must stop."""


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _actor_value(actor: Any, key: str, default: Any = None) -> Any:
    if isinstance(actor, Mapping):
        return actor.get(key, default)
    return getattr(actor, key, default)


def is_pool_actor(actor: Any) -> bool:
    return str(_actor_value(actor, "key_source", "")).lower() == "pool"


def actor_for(email: str) -> dict[str, Any]:
    """Return the server-authoritative actor injected into a web agent RPC."""
    normalized = _email(email)
    with tx() as cur:
        cur.execute(
            """
            SELECT email, plan, credit_balance, ai_grant
              FROM accounts
             WHERE email = %s
            """,
            (normalized,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"account not found: {normalized}")
    return {
        "email": row["email"],
        "plan": row["plan"],
        "credits_remaining": int(row["credit_balance"] or 0),
        "key_source": "pool" if bool(row["ai_grant"]) else "byok",
    }


def _default_fetch(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Chudware/credits"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 - pinned HTTPS URL
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("OpenRouter models response is not an object")
    return value


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid price {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"invalid price {value!r}")
    return parsed


def refresh_model_rates(
    fetch: Callable[[str], Mapping[str, Any]] | None = None,
    *,
    now: float | None = None,
) -> int:
    """Refresh OpenRouter prices. ``fetch`` is injectable for deterministic tests.

    OpenRouter publishes USD/token strings; storage deliberately uses USD per
    million tokens so the unit is explicit at every billing calculation.
    """
    payload = (fetch or _default_fetch)(OPENROUTER_MODELS_URL)
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("OpenRouter models response has no data list")
    fetched_at = float(time.time() if now is None else now)
    parsed: list[tuple[str, Decimal, Decimal, float]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        model = str(item.get("id") or "").strip()
        pricing = item.get("pricing")
        if not model or not isinstance(pricing, Mapping):
            continue
        try:
            prompt = _decimal(pricing.get("prompt")) * Decimal(1_000_000)
            completion = _decimal(pricing.get("completion")) * Decimal(1_000_000)
        except ValueError:
            continue
        parsed.append((model, prompt, completion, fetched_at))
    if not parsed:
        raise ValueError("OpenRouter models response contained no usable prices")
    with tx() as cur:
        cur.executemany(
            """
            INSERT INTO model_rates
                (model, prompt_usd_per_mtok, completion_usd_per_mtok, fetched_at)
            VALUES (%s, %s, %s, to_timestamp(%s))
            ON CONFLICT (model) DO UPDATE SET
                prompt_usd_per_mtok = EXCLUDED.prompt_usd_per_mtok,
                completion_usd_per_mtok = EXCLUDED.completion_usd_per_mtok,
                fetched_at = EXCLUDED.fetched_at
            """,
            parsed,
        )
    return len(parsed)


def _rates_stale(now: float) -> bool:
    with tx() as cur:
        cur.execute("SELECT EXTRACT(EPOCH FROM max(fetched_at)) AS newest FROM model_rates")
        row = cur.fetchone()
    newest = None if row is None else row["newest"]
    return newest is None or now - float(newest) >= _RATE_TTL_SECONDS


def model_rate(
    model: str,
    *,
    refresh: bool = True,
    fetch: Callable[[str], Mapping[str, Any]] | None = None,
    now: float | None = None,
) -> tuple[Decimal, Decimal]:
    """Return prompt/completion USD-per-MTok, failing unknown models expensive."""
    direct = _DIRECT_MODEL_RATES.get(str(model or "").strip())
    if direct is not None:
        return direct
    current = float(time.time() if now is None else now)
    if refresh and _rates_stale(current):
        try:
            refresh_model_rates(fetch, now=current)
        except Exception:
            # A stale table is safer than making live inference free. An empty
            # table is rejected below rather than assigned a made-up cheap rate.
            log.warning("model-rate refresh failed; using cached prices", exc_info=True)
    with tx() as cur:
        cur.execute(
            """
            SELECT prompt_usd_per_mtok, completion_usd_per_mtok
              FROM model_rates WHERE model = %s
            """,
            (str(model or "").strip(),),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT max(prompt_usd_per_mtok) AS prompt_usd_per_mtok,
                       max(completion_usd_per_mtok) AS completion_usd_per_mtok
                  FROM model_rates
                """
            )
            row = cur.fetchone()
    if (
        row is None
        or row["prompt_usd_per_mtok"] is None
        or row["completion_usd_per_mtok"] is None
    ):
        raise CreditRateUnavailable("no model rates are cached")
    return Decimal(row["prompt_usd_per_mtok"]), Decimal(row["completion_usd_per_mtok"])


def usage_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    refresh: bool = True,
) -> int:
    """Convert actual tokens to integer credits, rounding upstream cost upward."""
    prompt_rate, completion_rate = model_rate(model, refresh=refresh)
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    # $/MTok * tokens / 1e6 * 1000 credits/$ == tokens * rate / 1000.
    value = (
        Decimal(prompt) * prompt_rate + Decimal(completion) * completion_rate
    ) / Decimal(1000)
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _monthly_spend(cur: Any, email: str) -> int:
    cur.execute(
        """
        SELECT GREATEST(
                   -COALESCE(sum(credits_delta), 0),
                   0
               ) AS spent
          FROM credit_ledger
         WHERE email = %s AND kind IN ('spend', 'refund', 'adjust')
           AND ts >= date_trunc('month', now())
        """,
        (email,),
    )
    return max(0, int(cur.fetchone()["spent"] or 0))


def preflight(
    actor: Any,
    *,
    model: str,
    prompt_tokens_estimate: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Refuse unaffordable pooled inference before a provider can be called."""
    requested_output = max(0, int(max_output_tokens or 0))
    if not is_pool_actor(actor):
        return {"ok": True, "max_output_tokens": requested_output, "estimated_credits": 0}
    email = _email(_actor_value(actor, "email"))
    if not email:
        return {"ok": False, "reason": "insufficient_credits"}
    prompt_rate, completion_rate = model_rate(model)
    prompt_estimate = max(0, int(prompt_tokens_estimate or 0))
    estimate = int((
        Decimal(prompt_estimate) * prompt_rate
        + Decimal(requested_output) * completion_rate
    ).__truediv__(Decimal(1000)).to_integral_value(rounding=ROUND_CEILING))
    with tx() as cur:
        cur.execute(
            """
            SELECT credit_balance, credit_ceiling_monthly, ai_grant
              FROM accounts WHERE email = %s FOR UPDATE
            """,
            (email,),
        )
        row = cur.fetchone()
        if row is None or not bool(row["ai_grant"]):
            return {"ok": False, "reason": "insufficient_credits"}
        balance = max(0, int(row["credit_balance"] or 0))
        available = balance
        ceiling = row["credit_ceiling_monthly"]
        if credit_ceiling_is_limited(ceiling):
            available = min(available, max(0, int(ceiling) - _monthly_spend(cur, email)))
    if estimate > available:
        return {
            "ok": False,
            "reason": "insufficient_credits",
            "estimated_credits": estimate,
            "credits_remaining": available,
        }
    # Cap output against the same authoritative balance even though the estimate
    # passed. This makes a later caller-supplied cap increase harmless.
    prompt_cost = (Decimal(prompt_estimate) * prompt_rate) / Decimal(1000)
    output_budget = max(Decimal(0), Decimal(available) - prompt_cost)
    if completion_rate > 0:
        affordable_output = int(
            (output_budget * Decimal(1000) / completion_rate).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        requested_output = min(requested_output, max(0, affordable_output))
    return {
        "ok": True,
        "max_output_tokens": requested_output,
        "estimated_credits": estimate,
        "credits_remaining": available,
    }


def reserve_turn(
    actor: Any,
    *,
    model: str,
    prompt_tokens_estimate: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Atomically reserve estimated pooled cost before provider dispatch."""
    if not is_pool_actor(actor):
        return {"ok": True, "reservation_id": None, "estimated_credits": 0}
    email = _email(_actor_value(actor, "email"))
    prompt_rate, completion_rate = model_rate(model)
    prompt = max(0, int(prompt_tokens_estimate or 0))
    output = max(0, int(max_output_tokens or 0))
    estimate = int((
        Decimal(prompt) * prompt_rate + Decimal(output) * completion_rate
    ).__truediv__(Decimal(1000)).to_integral_value(rounding=ROUND_CEILING))
    reservation_id = uuid.uuid4().hex
    with tx() as cur:
        cur.execute(
            """
            SELECT credit_balance, credit_ceiling_monthly, ai_grant
              FROM accounts WHERE email = %s FOR UPDATE
            """,
            (email,),
        )
        account = cur.fetchone()
        if account is None or not bool(account["ai_grant"]):
            return {"ok": False, "reason": "insufficient_credits"}
        available = max(0, int(account["credit_balance"] or 0))
        ceiling = account["credit_ceiling_monthly"]
        if credit_ceiling_is_limited(ceiling):
            available = min(
                available, max(0, int(ceiling) - _monthly_spend(cur, email))
            )
        if estimate > available:
            return {
                "ok": False,
                "reason": "insufficient_credits",
                "estimated_credits": estimate,
                "credits_remaining": available,
            }
        note = json.dumps({
            "reservation": {
                "id": reservation_id,
                "state": "reserved",
                "estimated_credits": estimate,
            }
        }, separators=(",", ":"), sort_keys=True)
        cur.execute(
            """
            INSERT INTO credit_ledger
                (id, email, kind, model, prompt_tokens, completion_tokens,
                 credits_delta, note)
            VALUES (%s, %s, 'spend', %s, %s, %s, %s, %s)
            """,
            (reservation_id, email, model, prompt, output, -estimate, note),
        )
        cur.execute(
            "UPDATE accounts SET credit_balance = credit_balance - %s WHERE email = %s",
            (estimate, email),
        )
    return {
        "ok": True,
        "reservation_id": reservation_id,
        "estimated_credits": estimate,
        "credits_remaining": available - estimate,
        "prompt_usd_per_mtok": prompt_rate,
        "completion_usd_per_mtok": completion_rate,
    }


def reconcile_reservation(
    reservation_id: str,
    *,
    actual_credits: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> dict[str, int | bool]:
    """Settle one reservation against terminal reported usage exactly once."""
    actual = max(0, int(actual_credits or 0))
    with tx() as cur:
        cur.execute(
            """
            SELECT id, email, model, credits_delta, note
              FROM credit_ledger
             WHERE id = %s AND kind = 'spend'
             FOR UPDATE
            """,
            (reservation_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"reservation not found: {reservation_id}")
        try:
            metadata = json.loads(row["note"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        reservation = metadata.get("reservation")
        if not isinstance(reservation, dict) or reservation.get("state") != "reserved":
            return {"ok": False, "charged": 0}
        estimated = max(0, -int(row["credits_delta"] or 0))
        email = str(row["email"])
        cur.execute(
            "SELECT credit_balance FROM accounts WHERE email = %s FOR UPDATE",
            (email,),
        )
        balance = max(0, int(cur.fetchone()["credit_balance"] or 0))
        if actual < estimated:
            refund = estimated - actual
            cur.execute(
                """
                INSERT INTO credit_ledger
                    (id, email, kind, model, prompt_tokens, completion_tokens,
                     credits_delta, note)
                VALUES (%s, %s, 'refund', %s, %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4().hex, email, row["model"],
                    max(0, int(prompt_tokens or 0)),
                    max(0, int(completion_tokens or 0)),
                    refund,
                    json.dumps({"reservation_id": reservation_id}, separators=(",", ":")),
                ),
            )
            cur.execute(
                "UPDATE accounts SET credit_balance = credit_balance + %s WHERE email = %s",
                (refund, email),
            )
            charged = actual
        elif actual > estimated:
            extra = actual - estimated
            billed = min(extra, balance)
            cur.execute(
                """
                INSERT INTO credit_ledger
                    (id, email, kind, model, prompt_tokens, completion_tokens,
                     credits_delta, note)
                VALUES (%s, %s, 'spend', %s, %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4().hex, email, row["model"],
                    max(0, int(prompt_tokens or 0)),
                    max(0, int(completion_tokens or 0)),
                    -extra,
                    json.dumps({"reservation_id": reservation_id}, separators=(",", ":")),
                ),
            )
            if extra > billed:
                cur.execute(
                    """
                    INSERT INTO credit_ledger
                        (id, email, kind, model, credits_delta, note)
                    VALUES (%s, %s, 'adjust', %s, %s, %s)
                    """,
                    (
                        uuid.uuid4().hex, email, row["model"], extra - billed,
                        f"absorbed reservation overshoot {reservation_id}; not billed",
                    ),
                )
            cur.execute(
                "UPDATE accounts SET credit_balance = credit_balance - %s WHERE email = %s",
                (billed, email),
            )
            charged = estimated + billed
        else:
            charged = actual
        reservation["state"] = "reconciled"
        reservation["actual_credits"] = actual
        reservation["prompt_tokens"] = max(0, int(prompt_tokens or 0))
        reservation["completion_tokens"] = max(0, int(completion_tokens or 0))
        cur.execute(
            "UPDATE credit_ledger SET note = %s WHERE id = %s",
            (
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                reservation_id,
            ),
        )
    return {"ok": True, "charged": charged}


def mark_stale_reservations(*, older_than_hours: int = 6) -> int:
    """Mark abandoned reservations without refunding their already-spent estimate."""
    marked = 0
    with tx() as cur:
        cur.execute(
            """
            SELECT id, note FROM credit_ledger
             WHERE kind = 'spend'
               AND ts < now() - (%s * interval '1 hour')
               AND note LIKE '%%\"state\":\"reserved\"%%'
             FOR UPDATE
            """,
            (max(1, int(older_than_hours)),),
        )
        for row in cur.fetchall():
            try:
                metadata = json.loads(row["note"])
                reservation = metadata["reservation"]
            except (KeyError, TypeError, ValueError):
                continue
            if reservation.get("state") != "reserved":
                continue
            reservation["state"] = "unreconciled"
            reservation["marked_at"] = int(time.time())
            cur.execute(
                "UPDATE credit_ledger SET note = %s WHERE id = %s",
                (
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    row["id"],
                ),
            )
            marked += 1
    return marked


def _note(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return ""
    safe = {str(k): str(v)[:200] for k, v in metadata.items()}
    return json.dumps(safe, sort_keys=True, separators=(",", ":"))[:1000]


def grant_purchased(
    email: str,
    amount: int,
    stripe_event_id: str,
    metadata: Mapping[str, str] | None = None,
) -> bool:
    """Apply a confirmed paid purchase once per delivery.

    Billing additionally claims the Checkout/payment settlement in the same
    transaction, so distinct provider event types cannot grant it again.
    """
    normalized = _email(email)
    credits = int(amount)
    event_id = str(stripe_event_id or "").strip()
    if not normalized or credits <= 0 or not event_id:
        raise ValueError("purchase requires email, positive credits, and stripe event id")
    with tx() as cur:
        # Establish the account before inserting its FK-bound ledger entry, and
        # retain the lock through both mutations so deletion cannot race grant.
        cur.execute("SELECT email FROM accounts WHERE email = %s FOR UPDATE", (normalized,))
        if cur.fetchone() is None:
            raise LookupError(f"account not found: {normalized}")
        cur.execute(
            """
            INSERT INTO credit_ledger
                (id, email, kind, credits_delta, stripe_event_id, note)
            VALUES (%s, %s, 'purchase', %s, %s, %s)
            ON CONFLICT (stripe_event_id) WHERE stripe_event_id IS NOT NULL DO NOTHING
            RETURNING id
            """,
            (uuid.uuid4().hex, normalized, credits, event_id, _note(metadata)),
        )
        if cur.fetchone() is None:
            return False
        cur.execute(
            "UPDATE accounts SET credit_balance = credit_balance + %s WHERE email = %s",
            (credits, normalized),
        )
        if cur.rowcount != 1:
            raise LookupError(f"account not found: {normalized}")
    return True


def grant(email: str, amount: int, *, note: str = "") -> int:
    normalized = _email(email)
    credits = int(amount)
    if credits <= 0:
        raise ValueError("grant must be positive")
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO credit_ledger (id, email, kind, credits_delta, note)
            VALUES (%s, %s, 'grant', %s, %s)
            """,
            (uuid.uuid4().hex, normalized, credits, str(note or "")[:1000]),
        )
        cur.execute(
            """
            UPDATE accounts SET credit_balance = credit_balance + %s
             WHERE email = %s RETURNING credit_balance
            """,
            (credits, normalized),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"account not found: {normalized}")
    return int(row["credit_balance"])


def record_spend(
    actor: Any,
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    """Record actual pooled usage immediately; BYOK returns without a write.

    If actual usage exceeds what remains, the full upstream cost is kept in the
    spend row and a compensating adjust row absorbs the overshoot. The account's
    materialised balance therefore reaches zero, never a negative value.
    """
    if not is_pool_actor(actor):
        return 0
    email = _email(_actor_value(actor, "email"))
    if not email:
        raise ValueError("pooled actor has no email")
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cost = usage_cost(model, prompt, completion)
    if cost <= 0:
        return 0
    with tx() as cur:
        cur.execute(
            """
            SELECT credit_balance, credit_ceiling_monthly
              FROM accounts WHERE email = %s FOR UPDATE
            """,
            (email,),
        )
        account = cur.fetchone()
        if account is None:
            raise LookupError(f"account not found: {email}")
        balance = max(0, int(account["credit_balance"] or 0))
        billable = balance
        ceiling = account["credit_ceiling_monthly"]
        if credit_ceiling_is_limited(ceiling):
            billable = min(billable, max(0, int(ceiling) - _monthly_spend(cur, email)))
        billed = min(cost, billable)
        cur.execute(
            """
            INSERT INTO credit_ledger
                (id, email, kind, model, prompt_tokens, completion_tokens,
                 credits_delta, note)
            VALUES (%s, %s, 'spend', %s, %s, %s, %s, '')
            """,
            (uuid.uuid4().hex, email, str(model or ""), prompt, completion, -cost),
        )
        overshoot = cost - billed
        if overshoot:
            cur.execute(
                """
                INSERT INTO credit_ledger
                    (id, email, kind, model, credits_delta, note)
                VALUES (%s, %s, 'adjust', %s, %s, %s)
                """,
                (
                    uuid.uuid4().hex,
                    email,
                    str(model or ""),
                    overshoot,
                    "absorbed final usage overshoot; not billed to customer",
                ),
            )
        cur.execute(
            "UPDATE accounts SET credit_balance = credit_balance - %s WHERE email = %s",
            (billed, email),
        )
    if overshoot:
        log.warning(
            "absorbed pooled-credit overshoot email=%s model=%s credits=%d",
            email,
            model,
            overshoot,
        )
    return billed


def spend_history(email: str, days: int = 30) -> list[dict[str, Any]]:
    """Daily net AI spend for the portal, expressed only in Chudware credits."""
    window = max(1, min(int(days), 366))
    with tx() as cur:
        cur.execute(
            """
            SELECT to_char(date_trunc('day', ts), 'YYYY-MM-DD') AS day,
                   GREATEST(-COALESCE(sum(credits_delta), 0), 0) AS credits
              FROM credit_ledger
             WHERE email = %s
               AND kind IN ('spend', 'refund', 'adjust')
               AND ts >= now() - (%s * interval '1 day')
             GROUP BY date_trunc('day', ts)
             ORDER BY date_trunc('day', ts)
            """,
            (_email(email), window),
        )
        return [
            {"day": str(row["day"]), "credits": int(row["credits"] or 0)}
            for row in cur.fetchall()
        ]


def reconcile_balances() -> list[dict[str, Any]]:
    """Return and alert on any account whose materialised balance drifted."""
    with tx() as cur:
        cur.execute(
            """
            SELECT a.email, a.credit_balance,
                   COALESCE(sum(l.credits_delta), 0) AS ledger_balance
              FROM accounts a
              LEFT JOIN credit_ledger l ON l.email = a.email
             GROUP BY a.email, a.credit_balance
            HAVING a.credit_balance <> COALESCE(sum(l.credits_delta), 0)
             ORDER BY a.email
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        log.error(
            "CREDIT BALANCE DRIFT email=%s materialised=%s ledger=%s",
            row["email"],
            row["credit_balance"],
            row["ledger_balance"],
        )
    return rows


async def _nightly_reconcile() -> None:
    while True:
        try:
            try:
                admission = deployment_drain.controller().acquire(inherit=False)
            except HTTPException:
                await asyncio.sleep(10)
                continue
            try:
                with admission.bind():
                    await deployment_drain.run_blocking(_reconcile_once)
            finally:
                admission.close()
        except Exception:
            log.exception("nightly credit reconciliation failed")
        await asyncio.sleep(_RECONCILE_SECONDS)


def _reconcile_once():
    mark_stale_reservations()
    reconcile_balances()




def _require_admin(request: Request) -> Any:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    if getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


@router.post("/api/admin/credits/{email}/grant")
async def admin_grant(email: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _require_admin(request)
    api_writes.enforce(admin.email, "admin credit grant")
    try:
        amount = int(body.get("credits") or 0)
        balance = grant(email, amount, note=f"manual grant by {admin.email}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "email": _email(email), "credit_balance": balance}


@router.post("/api/admin/credits/{email}/controls")
async def admin_controls(
    email: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    admin = _require_admin(request)
    api_writes.enforce(admin.email, "admin credit controls")
    raw_ceiling = body.get("credit_ceiling_monthly")
    try:
        ceiling = (
            CREDIT_CEILING_UNLIMITED
            if raw_ceiling in (None, "")
            else int(raw_ceiling)
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="credit ceiling must be an integer") from exc
    if ceiling < 0:
        raise HTTPException(status_code=400, detail="credit ceiling cannot be negative")
    ai_grant = body.get("ai_grant")
    if not isinstance(ai_grant, bool):
        raise HTTPException(status_code=400, detail="ai_grant must be boolean")
    normalized = _email(email)
    with tx() as cur:
        cur.execute(
            """
            UPDATE accounts
               SET credit_ceiling_monthly = %s, ai_grant = %s
             WHERE email = %s
         RETURNING credit_balance, credit_ceiling_monthly, ai_grant
            """,
            (ceiling, ai_grant, normalized),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"ok": True, "email": normalized, **dict(row)}


def is_trial_domain(email: str) -> bool:
    """An active, verified account on a configured, unexpired trial domain."""
    normalized = _email(email)
    if normalized.count("@") != 1:
        return False
    domain = normalized.rsplit("@", 1)[1]
    # Domain boundaries are dots, not arbitrary suffixes: x.example.com may
    # match example.com, but notexample.com never can.
    labels = domain.split(".")
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    if not candidates:
        return False
    with tx() as cur:
        cur.execute(
            """SELECT 1 FROM trial_domains td
               WHERE td.domain = ANY(%s)
                 AND (td.expires_at IS NULL OR td.expires_at > now())
                 AND EXISTS (
                   SELECT 1 FROM accounts a WHERE a.email = %s AND a.status = 'active'
                     AND (EXISTS (SELECT 1 FROM credentials c
                                  WHERE c.email = a.email AND c.email_verified = TRUE)
                       OR EXISTS (SELECT 1 FROM oauth_identities i
                                  WHERE i.email = a.email))
                 ) LIMIT 1""",
            (candidates, normalized),
        )
        return cur.fetchone() is not None


def _normalize_domains(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("trial_domains must be a list")
    domains: list[str] = []
    for item in value:
        domain = str(item or "").strip().lower().lstrip("@")
        if not domain or "." not in domain or any(c.isspace() for c in domain):
            raise ValueError(f"invalid trial domain: {item!r}")
        if domain not in domains:
            domains.append(domain)
    return domains


@router.get("/api/admin/credits/trial-domains")
async def admin_get_trial_domains(request: Request) -> dict[str, Any]:
    _require_admin(request)
    with tx() as cur:
        cur.execute("SELECT domain FROM trial_domains ORDER BY domain")
        rows = cur.fetchall()
    return {"trial_domains": [str(row["domain"]) for row in rows]}


@router.put("/api/admin/credits/trial-domains")
async def admin_put_trial_domains(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    admin = _require_admin(request)
    api_writes.enforce(admin.email, "admin trial domains")
    try:
        domains = _normalize_domains(body.get("trial_domains"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with tx() as cur:
        if domains:
            cur.execute(
                "DELETE FROM trial_domains WHERE NOT (domain = ANY(%s))",
                (domains,),
            )
            cur.executemany(
                """
                INSERT INTO trial_domains (domain, trial_days, ai_credits, note)
                VALUES (%s, 14, 0, %s)
                ON CONFLICT (domain) DO NOTHING
                """,
                [(domain, f"admin editor: {admin.email}") for domain in domains],
            )
        else:
            cur.execute("DELETE FROM trial_domains")
    return {"ok": True, "trial_domains": domains}
