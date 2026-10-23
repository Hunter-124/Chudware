"""Compact-but-complete MCP result serving.

Shared helpers for tool return payloads so large structured results stay
summary-first and token-bounded while preserving teaching fields
(ok/summary/counts/hint/next_actions) and explicit detail=full drill-down.

Token heuristic matches the agent stack (``CHARS_PER_TOKEN = 4`` —
services/agent/tiers.py / ledger.py). Default JSON is compact; set
``CHUDWARE_MCP_PRETTY=1`` for indented dumps during debugging.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional, Sequence

CHARS_PER_TOKEN = 4

# Keys that priority-drop must NEVER remove (identity / teaching / counts).
_NEVER_DROP: frozenset[str] = frozenset({
    "ok",
    "summary",
    "reading",
    "verdict",
    "hint",
    "next_actions",
    "error",
    "reason",
    "warnings",
    "doc_id",
    "part_id",
    "id",
    "kind",
    "name",
    "revision",
    "component_count",
    "net_count",
    "footprint_count",
    "track_count",
    "via_count",
    "count",
    "total",
    "page",
    "page_size",
    "has_more",
    "next_page",
    "errors",
    "warning_count",
    "unconnected_count",
    "device_count",
    "converged",
    "analysis",
    "backend",
    "meta",
    # Input-dialect echo: how the tool re-read the caller's arguments. Bounded
    # (scalars + the caller's own facets/vendors) and the ONLY way the model
    # learns the canonical spelling of what it asked for, so budget pressure
    # must never silently drop it — dropping it turns a rewritten query into a
    # silent rewrite.
    "applied",
    "interpreted",
    "interpreted_args",
    "power",  # board-level totals — small, always pertinent
    "erc",    # keep error/warning counts (groups may be dropped separately)
    "drc",
})

# Soft/verbose blobs deleted first under budget. Large identity lists
# (components/nets/results) are SHRUNK, never fully dropped until last resort.
_SOFT_DROP: tuple[str, ...] = (
    "sim_log",
    "nodes",
    "probes",
    "by_rule",
    "groups",
    "violations",
    "fix_hints",
    "notices",
    "facet_summary",
    "members",
    "pins",
    "pos",
    "footprint",
    "sourcing",
    "params",
    "provenance",
    "license",
    "geometry_ref",
    "related_ids",
    "op",
    "dc",
    "tran",
    "ac",
)

# Arrays that may be truncated to a sample under budget, but not removed early.
_SAMPLEABLE_LISTS: frozenset[str] = frozenset({
    "components", "footprints", "nets", "results", "unconnected_pins", "items",
})


def count_tokens(obj: Any) -> int:
    """Estimate tokens for a string or JSON-serializable object (ceil(chars/4))."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        n = len(obj)
    else:
        n = len(dumps(obj))
    if n <= 0:
        return 0
    return int(math.ceil(n / float(CHARS_PER_TOKEN)))


def dumps(obj: Any, indent: Optional[int] = None, **kwargs: Any) -> str:
    """Serialize ``obj`` to JSON.

    Default is compact (no indent, tight separators). When ``indent`` is None and
    env ``CHUDWARE_MCP_PRETTY=1``, pretty-print with indent=2. Explicit ``indent``
    always wins.
    """
    pretty = False
    if indent is None:
        env = (os.environ.get("CHUDWARE_MCP_PRETTY") or "").strip().lower()
        pretty = env in ("1", "true", "yes", "on")
        indent = 2 if pretty else None
    seps = (", ", ": ") if indent is not None else (",", ":")
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("separators", seps)
    return json.dumps(obj, indent=indent, **kwargs)


def ok_envelope(
    data: Optional[Mapping[str, Any]] = None,
    *,
    warnings: Optional[Sequence[Any]] = None,
    hint: str = "",
    next_actions: Optional[Sequence[str]] = None,
    **extra: Any,
) -> dict:
    """Teaching success envelope: ``{ok:true, ...data, warnings, hint, next_actions}``."""
    out: dict[str, Any] = {"ok": True}
    if data:
        for k, v in data.items():
            if k == "ok":
                continue
            out[k] = v
    for k, v in extra.items():
        if k == "ok":
            continue
        out[k] = v
    out.setdefault("warnings", list(warnings) if warnings is not None else [])
    out.setdefault("hint", hint if hint is not None else "")
    out.setdefault("next_actions", list(next_actions) if next_actions is not None else [])
    return out


def err_envelope(
    error: str,
    *,
    hint: str = "",
    next_actions: Optional[Sequence[str]] = None,
    **extra: Any,
) -> dict:
    """Teaching error envelope: ``{ok:false, error, warnings, hint, next_actions, ...}``."""
    out: dict[str, Any] = {
        "ok": False,
        "error": str(error),
        "warnings": list(extra.pop("warnings", []) or []),
        "hint": hint if hint else str(error),
        "next_actions": list(next_actions) if next_actions is not None else [],
    }
    for k, v in extra.items():
        if k in ("ok", "error"):
            continue
        out[k] = v
    return out


@dataclass
class ServeOpts:
    """Options for :func:`serve`.

    ``detail``:
      - ``summary`` — compact projection (default for status/search)
      - ``list``    — compact rows, paginated
      - ``full``    — full records / members (escape hatch)
    """

    detail: str = "summary"
    budget_tokens: Optional[int] = None
    page: int = 0
    page_size: int = 25
    fields: Optional[list[str]] = None
    include: Optional[list[str]] = field(default=None)

    def normalized(self) -> "ServeOpts":
        detail = (self.detail or "summary").strip().lower()
        if detail not in ("summary", "list", "full"):
            detail = "summary"
        page = max(0, int(self.page or 0))
        page_size = max(1, min(int(self.page_size or 25), 500))
        fields = list(self.fields) if self.fields else None
        include = list(self.include) if self.include else None
        budget = self.budget_tokens
        if budget is not None:
            try:
                budget = int(budget)
            except (TypeError, ValueError):
                budget = None
            if budget is not None and budget <= 0:
                budget = None
        return ServeOpts(
            detail=detail,
            budget_tokens=budget,
            page=page,
            page_size=page_size,
            fields=fields,
            include=include,
        )


Projector = Callable[[dict, ServeOpts], dict]


def paginate_list(
    items: Sequence[Any],
    *,
    page: int = 0,
    page_size: int = 25,
) -> dict:
    """Slice ``items`` into a page envelope."""
    total = len(items)
    page = max(0, int(page or 0))
    page_size = max(1, int(page_size or 25))
    start = page * page_size
    end = start + page_size
    chunk = list(items[start:end])
    has_more = end < total
    return {
        "items": chunk,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        "next_page": (page + 1) if has_more else None,
    }


def project_fields(row: Mapping[str, Any], fields: Optional[Sequence[str]]) -> dict:
    """Keep only allowlisted keys from ``row`` (identity keys always kept if present)."""
    if not fields:
        return dict(row)
    keep = set(fields) | {"part_id", "ref", "id", "name", "ok"}
    return {k: row[k] for k in row.keys() if k in keep}


def _priority_drop(payload: MutableMapping[str, Any], budget_tokens: int) -> list[str]:
    """Drop/shrink lowest-priority content until under budget. Returns omitted keys.

    NEVER removes keys in ``_NEVER_DROP``. Large identity lists (components/nets/
    results/...) are truncated to a sample first and only fully dropped as a last
    resort after every soft blob is gone — callers keep the corresponding ``*_count``.
    """
    omitted: list[str] = []
    if count_tokens(payload) <= budget_tokens:
        return omitted

    def _under() -> bool:
        return count_tokens(payload) <= budget_tokens

    # 1) Soft blobs in priority order (full delete).
    for key in _SOFT_DROP:
        if key in _NEVER_DROP or key not in payload:
            continue
        # Nested under erc/drc: trim groups only, keep error/warning counts.
        if key in ("groups", "by_rule") and isinstance(payload.get("erc"), dict):
            if "groups" in payload["erc"]:
                payload["erc"] = {k: v for k, v in payload["erc"].items() if k != "groups"}
                omitted.append("erc.groups")
                if _under():
                    return omitted
            continue
        if key in ("groups", "by_rule") and isinstance(payload.get("drc"), dict):
            if "groups" in payload["drc"]:
                payload["drc"] = {k: v for k, v in payload["drc"].items() if k != "groups"}
                omitted.append("drc.groups")
                if _under():
                    return omitted
            continue
        del payload[key]
        omitted.append(key)
        if _under():
            return omitted

    # 2) Truncate sampleable lists progressively (keep at least a small sample).
    for target_len in (24, 12, 6, 3, 1):
        if _under():
            return omitted
        for key in list(_SAMPLEABLE_LISTS):
            val = payload.get(key)
            if not isinstance(val, list) or len(val) <= target_len:
                continue
            payload[key] = val[:target_len]
            omitted.append(f"{key}[truncated:{target_len}]")
            if _under():
                return omitted

    # 3) Drop remaining non-protected non-sampleable keys alphabetically.
    for key in sorted(list(payload.keys())):
        if key in _NEVER_DROP or key in _SAMPLEABLE_LISTS:
            continue
        del payload[key]
        omitted.append(key)
        if _under():
            return omitted

    # 4) Last resort: drop sampleable lists entirely (counts already preserved).
    for key in sorted(_SAMPLEABLE_LISTS):
        if key in payload and key not in _NEVER_DROP:
            del payload[key]
            omitted.append(key)
            if _under():
                return omitted
    return omitted


def serve(
    payload: Mapping[str, Any],
    opts: Optional[ServeOpts] = None,
    projector: Optional[Projector] = None,
) -> str:
    """Project → paginate → budget-drop → compact JSON with ``meta``.

    ``projector(payload, opts)`` should return a new dict shaped for the chosen
    detail level. When omitted, ``payload`` is used as-is (still paginates
    known large arrays and applies the budget).
    """
    o = (opts or ServeOpts()).normalized()
    body: dict[str, Any]
    if projector is not None:
        body = projector(dict(payload), o)
    else:
        body = dict(payload)

    # Optional field allowlist on top-level keys (rare; projectors usually handle).
    if o.fields:
        keep = set(o.fields) | _NEVER_DROP
        body = {k: v for k, v in body.items() if k in keep}

    # Paginate well-known large arrays when detail=list OR an explicit page>0.
    # detail=full without paging keeps the complete sets (escape hatch).
    if o.detail == "list" or o.page > 0:
        for arr_key in ("components", "footprints", "nets", "results", "items"):
            val = body.get(arr_key)
            if isinstance(val, list) and (o.detail == "list" or o.page > 0):
                page_env = paginate_list(val, page=o.page, page_size=o.page_size)
                body[arr_key] = page_env["items"]
                body.setdefault(f"{arr_key}_page", {
                    "total": page_env["total"],
                    "page": page_env["page"],
                    "page_size": page_env["page_size"],
                    "has_more": page_env["has_more"],
                    "next_page": page_env["next_page"],
                })

    omitted: list[str] = []
    truncated = False
    if o.budget_tokens is not None:
        omitted = _priority_drop(body, o.budget_tokens)
        truncated = bool(omitted) or count_tokens(body) > o.budget_tokens

    tokens = count_tokens(body)
    meta = {
        "detail": o.detail,
        "truncated": truncated,
        "omitted_keys": omitted,
        "tokens": tokens,
    }
    body["meta"] = meta
    # Recompute tokens including meta (meta is small).
    meta["tokens"] = count_tokens(body)
    return dumps(body)


# ---------------------------------------------------------------------------
# Domain projectors (used by ecad_tools / parts_tools)
# ---------------------------------------------------------------------------

_DEFAULT_PROBE_CAP = 12
_PREFERRED_NET_NAMES = frozenset({
    "out", "output", "vout", "vmid", "mid", "in", "input", "vin",
    "vcc", "vdd", "vee", "vss", "avdd", "dvdd", "vcore", "vio",
    "vsense", "fb", "sense", "load", "noload",
})


def select_default_probes(
    nodes: Sequence[Any],
    devices: Sequence[Mapping[str, Any]],
    *,
    cap: int = _DEFAULT_PROBE_CAP,
) -> list[str]:
    """Pick a bounded default probe list: named/OUT nets first, then a cap.

    Voltage sources always contribute ``I(V*)`` (usually few). Node voltages are
    ordered: preferred names → other non-generated names → N$/numeric last.
    """
    cap = max(1, int(cap or _DEFAULT_PROBE_CAP))
    named: list[str] = []
    plain: list[str] = []
    generated: list[str] = []
    for n in nodes:
        s = str(n)
        if s in ("0", "gnd", "GND"):
            continue
        low = s.lower()
        if low in _PREFERRED_NET_NAMES or low.startswith("out") or low.startswith("v("):
            named.append(s)
        elif s.startswith("N$") or s.startswith("n$") or s.isdigit():
            generated.append(s)
        else:
            plain.append(s)
    ordered = named + plain + generated
    vprobes = [f"V({n})" for n in ordered[:cap]]
    iprobes = [f"I({d['name']})" for d in devices if d.get("kind") == "V" and d.get("name")]
    # Dedup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in vprobes + iprobes:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def project_ecad_status(payload: dict, opts: ServeOpts) -> dict:
    """Project a full schematic/PCB status payload to the requested detail level.

    Summary (default):
      - components as {ref, value, libid} (+ non-null power_w; never pins)
      - nets as {name, size} (never members)
      - unconnected_pins: count + ≤6 sample
      - erc/drc: errors/warnings counts; groups only at list/full
    Full: retains the same component refs + net names as the input payload.
    """
    o = opts.normalized()
    include = {str(x).strip().lower() for x in (o.include or []) if str(x).strip()}
    want_pins = "pins" in include or o.detail == "full"
    want_members = "members" in include or o.detail == "full"

    out = dict(payload)

    # ---- components ----
    comps = out.get("components")
    if isinstance(comps, list):
        projected = []
        for c in comps:
            if not isinstance(c, Mapping):
                projected.append(c)
                continue
            if o.detail == "full":
                projected.append(dict(c))
                continue
            row = {
                "ref": c.get("ref"),
                "value": c.get("value"),
                "libid": c.get("libid"),
            }
            # power_w is two scalars — always keep when the producer attached it
            # (Honesty rule: null typ/max must surface, never invented zeros).
            if "power_w" in c:
                row["power_w"] = c["power_w"]
            if o.detail == "list" and c.get("footprint") is not None:
                row["footprint"] = c.get("footprint")
            if want_pins and "pins" in c:
                row["pins"] = c["pins"]
            projected.append(row)
        out["components"] = projected
        out.setdefault("component_count", len(comps))

    # ---- footprints (PCB) ----
    fps = out.get("footprints")
    if isinstance(fps, list):
        if o.detail == "summary":
            out["footprints"] = [
                {"ref": f.get("ref"), "fpid": f.get("fpid"),
                 **({"power_w": f["power_w"]} if isinstance(f, Mapping)
                    and isinstance(f.get("power_w"), Mapping)
                    and (f["power_w"].get("typ") is not None
                         or f["power_w"].get("max") is not None) else {})}
                if isinstance(f, Mapping) else f
                for f in fps
            ]
        elif o.detail == "list":
            out["footprints"] = [
                {"ref": f.get("ref"), "fpid": f.get("fpid"),
                 "side": f.get("side"), "power_w": f.get("power_w")}
                if isinstance(f, Mapping) else f
                for f in fps
            ]
        # full: leave as-is

    # ---- nets ----
    nets = out.get("nets")
    if isinstance(nets, list):
        projected_n = []
        for n in nets:
            if isinstance(n, str):
                # PCB status historically returns bare names.
                projected_n.append(n if o.detail == "full" else {"name": n})
                continue
            if not isinstance(n, Mapping):
                projected_n.append(n)
                continue
            if o.detail == "full" or want_members:
                projected_n.append(dict(n))
            else:
                projected_n.append({"name": n.get("name"), "size": n.get("size")})
        out["nets"] = projected_n
        if "net_count" not in out:
            out["net_count"] = len(nets)

    # ---- unconnected sample ----
    unconn = out.get("unconnected_pins")
    if isinstance(unconn, list):
        out["unconnected_count"] = len(unconn)
        if o.detail == "summary" or (o.detail == "list" and len(unconn) > 6):
            out["unconnected_pins"] = unconn[:6]
        # full: keep all

    # ---- erc / drc groups ----
    for key in ("erc", "drc"):
        block = out.get(key)
        if isinstance(block, dict) and o.detail == "summary":
            slim = {
                "errors": block.get("errors", 0),
                "warnings": block.get("warnings", 0),
            }
            groups = block.get("groups")
            if isinstance(groups, list):
                slim["group_count"] = len(groups)
            out[key] = slim

    return out


_PART_ROW_KEYS = (
    "part_id", "title", "domain", "vendor", "price", "designation", "score",
)


def project_part_row(rec: Mapping[str, Any]) -> dict:
    """Project a full PartRecord to the compact search row."""
    vendor = rec.get("vendor")
    price = rec.get("price")
    if vendor is None or price is None:
        # Lazy import-free extraction from sourcing[] when present.
        if vendor is None:
            vendor = _row_vendor(rec)
        if price is None:
            price = _row_price(rec)
    score = rec.get("score")
    if score is None:
        score = rec.get("_score")
    return {
        "part_id": rec.get("part_id"),
        "title": rec.get("title"),
        "domain": rec.get("domain"),
        "vendor": vendor if vendor else "",
        "price": price if price not in (None, 0, 0.0) else (price if price == 0 else None),
        "designation": rec.get("designation") or "",
        "score": score if score is not None else None,
    }


def _row_vendor(rec: Mapping[str, Any]) -> str:
    src = rec.get("source")
    if isinstance(src, str) and src and src != "live":
        pass
    pid = str(rec.get("part_id") or "")
    if pid.startswith("catalog:"):
        return pid[len("catalog:"):].split("::", 1)[0]
    for row in rec.get("sourcing") or []:
        if isinstance(row, Mapping) and row.get("vendor"):
            return str(row["vendor"])
    return str(rec.get("brand") or rec.get("manufacturer") or "")


def _row_price(rec: Mapping[str, Any]) -> Optional[float]:
    for row in rec.get("sourcing") or []:
        if not isinstance(row, Mapping):
            continue
        p = row.get("price")
        if p is None:
            continue
        try:
            f = float(p)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def project_part_search(payload: dict, opts: ServeOpts) -> dict:
    """Project part_search results to compact rows unless detail=full."""
    o = opts.normalized()
    out = dict(payload)
    results = out.get("results")
    if not isinstance(results, list):
        return out
    if o.detail == "full":
        return out
    out["results"] = [project_part_row(r) if isinstance(r, Mapping) else r for r in results]
    return out
