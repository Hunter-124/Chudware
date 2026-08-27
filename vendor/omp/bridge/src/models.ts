/**
 * Catalog-backed model resolution + display rows, shared by `complete`/`listModels`
 * (bridge.ts) and the new `models_list`/`cost_summary` RPCs. Reuses
 * `@oh-my-pi/pi-catalog` verbatim — no local model/cost tables.
 *
 * The bundled catalog is a pinned snapshot, so a model released after the vendor drop
 * (claude-opus-5 …) would never reach the picker. `models_list` therefore layers LIVE
 * discovery over the snapshot where the provider exposes a list endpoint; see
 * `liveModels` below. Live discovery is strictly additive: every failure path falls back
 * to exactly the bundled rows.
 */
import { getBundledModel, getBundledModels, getBundledProviders } from "@oh-my-pi/pi-catalog";
import type { Api, Model } from "@oh-my-pi/pi-catalog";
import { getAuthContext } from "./auth-context";
import { DEPRECATED_CATALOG, extraModels } from "./catalog-extra";
import { asObject, num, optStr, str, type Json } from "./json";

/** The branded provider-id parameter `getBundledModel`/`getBundledModels` accept. */
export type ProviderId = Parameters<typeof getBundledModel>[0];

/** Closest bundled sibling to `modelId`: the catalog entry sharing the longest id prefix, with the
 *  last match winning a tie (the catalog is id-sorted, so that is the newest of that family).
 *  `claude-opus-5` therefore inherits `claude-opus-4-8`'s 1M context / 128k output / reasoning
 *  rather than `claude-3-5-sonnet-20240620`'s 8k non-reasoning shape, which is what blind
 *  index-0 templating produced. */
function nearestBundled(providerId: ProviderId, modelId: string): Model<Api> | undefined {
	let best: Model<Api> | undefined;
	let bestShared = -1;
	for (const candidate of getBundledModels(providerId)) {
		let shared = 0;
		while (shared < candidate.id.length && shared < modelId.length && candidate.id[shared] === modelId[shared]) shared++;
		if (shared >= bestShared) {
			bestShared = shared;
			best = candidate;
		}
	}
	return best;
}

/** Resolve an OMP Model, honoring a caller-supplied base_url override for proxies/local endpoints. */
export function resolveModel(provider: string, modelId: string, baseUrl?: string): Model<Api> {
	// `as ProviderId`: provider is a runtime string; the registry keys on a branded union.
	const providerId = provider as ProviderId;
	// CAD-side extras (catalog-extra.ts) win over a bundled row of the same id — they carry
	// our overrides (e.g. kimi-code K3 real cost where the subscription catalog says $0).
	let model = extraModels(provider).find((m) => m.id === modelId);
	if (!model) {
		// getBundledModel is typed non-optional but returns undefined on a registry miss.
		model = getBundledModel(providerId, modelId) as Model<Api> | undefined;
	}
	if (!model) {
		// Unknown id under a known provider: clone its closest bundled sibling as a template and
		// retarget it, so proxied/newer ids still stream through the right API with sane limits.
		const template = nearestBundled(providerId, modelId);
		if (template) {
			model = { ...template, id: modelId, requestModelId: modelId, name: modelId };
		}
	}
	if (!model) {
		throw new Error(`unknown provider/model: ${provider}/${modelId} (no bundled model and no template)`);
	}
	return baseUrl ? { ...model, baseUrl } : model;
}

/**
 * Whether the catalog carries real price data for this model.
 *
 * `Model.cost` is a MANDATORY `{input,output,cacheRead,cacheWrite}` number object with no
 * "unknown price" sentinel, and many discovery adapters (aimlapi, antigravity, codex, cursor,
 * devin, gemini, gitlab-duo-workflow, openai-compatible, ollama, …) emit all-zeros for models
 * whose real price they never learned — 1658 of the 3902 bundled rows are all-zero. All-zero
 * therefore means "no price data", which is NOT the same as "free": a $0 derived from it is a
 * placeholder, never a measured amount, and callers MUST render it as unknown rather than as
 * an authoritative total.
 */
function isPriced(m: Model<Api>): boolean {
	const c = m.cost;
	return c.input > 0 || c.output > 0 || c.cacheRead > 0 || c.cacheWrite > 0;
}

/** Display row for one catalog model: capabilities + cost, no wire-only fields. */
export function modelInfo(m: Model<Api>): Json {
	return {
		id: m.id,
		name: m.name,
		api: m.api,
		provider: m.provider,
		input: m.input,
		vision: m.input.includes("image"),
		supportsTools: m.supportsTools ?? true,
		reasoning: m.reasoning ?? false,
		// OMP catalog is the source of truth for the effort ladder — the GUI offers
		// exactly these rungs (K3: low|high|max; current Claude: low|medium|high|xhigh|max).
		reasoningEfforts: m.thinking?.efforts ?? [],
		contextWindow: m.contextWindow ?? null,
		maxTokens: m.maxTokens ?? null,
		// `cost` is a mandatory catalog field (never undefined here: 0 of 3902 bundled rows
		// lack it), so it is emitted verbatim; `priced` is the only thing that distinguishes
		// a real $0 from an unpriced row full of zeros.
		cost: m.cost,
		priced: isPriced(m),
	};
}

/** Row shape returned by a provider's live model-list endpoint: wire id + human label. */
interface LiveModel {
	id: string;
	name: string;
}

const ANTHROPIC_VERSION = "2023-06-01";
/** Beta flag Anthropic requires when the caller authenticates with a subscription OAuth token. */
const ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20";
/** Whole-request budget for one live page. The picker blocks on this, so it must be short. */
const LIVE_TIMEOUT_MS = 10_000;
/** Pagination guard: the endpoint returns ~11 models with limit=1000, so >5 pages means the
 *  cursor is not advancing and we would otherwise spin forever. */
const LIVE_MAX_PAGES = 5;
/** 120s: long enough that opening the picker repeatedly costs one request, short enough that a
 *  model released mid-session appears without restarting CAD. */
const LIVE_TTL_MS = 120_000;
/** Failures get a much shorter cooldown so a transient outage self-heals quickly, while an
 *  offline machine still doesn't pay the 10s timeout on every single picker open. */
const LIVE_FAIL_TTL_MS = 30_000;

/** In-process only (the bridge is respawned by the host, so there is nothing to persist).
 *  `models: undefined` records a failure — a failed fetch is never cached as a success. */
const liveCache = new Map<string, { expires: number; models?: LiveModel[] }>();

/** Narrow one `{type,id,display_name,created_at}` entry off the wire; unusable rows are dropped. */
function readLiveModel(v: unknown): LiveModel | undefined {
	if (!v || typeof v !== "object") return undefined;
	if (!("id" in v) || typeof v.id !== "string" || v.id.length === 0) return undefined;
	const label = "display_name" in v && typeof v.display_name === "string" && v.display_name.length > 0 ? v.display_name : v.id;
	return { id: v.id, name: label };
}

/** Auth headers for Anthropic's list endpoint, resolved through the same CAD-owned AuthStorage the
 *  completion path uses — OAuth (subscription) first, since `getApiKey` would hand back a bearer
 *  token that `x-api-key` rejects. `undefined` when the provider is signed out. */
async function anthropicAuthHeaders(): Promise<Record<string, string> | undefined> {
	const { authStorage } = await getAuthContext();
	// getOAuthAccess refreshes an expired token through the normal pipeline before returning it.
	const oauth = await authStorage.getOAuthAccess("anthropic");
	if (oauth?.accessToken) {
		return {
			authorization: `Bearer ${oauth.accessToken}`,
			"anthropic-version": ANTHROPIC_VERSION,
			"anthropic-beta": ANTHROPIC_OAUTH_BETA,
		};
	}
	const key = await authStorage.getApiKey("anthropic");
	if (key) return { "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION };
	return undefined;
}

/** `GET /v1/models`, newest first, following `has_more`/`last_id` pagination. Throws on any
 *  transport/shape problem so the single catch in `liveModels` can fall back. */
async function fetchAnthropicModels(headers: Record<string, string>): Promise<LiveModel[]> {
	const out: LiveModel[] = [];
	const seen = new Set<string>();
	let afterId: string | undefined;
	for (let page = 0; page < LIVE_MAX_PAGES; page++) {
		const url = new URL("https://api.anthropic.com/v1/models");
		url.searchParams.set("limit", "1000");
		if (afterId) url.searchParams.set("after_id", afterId);
		const res = await fetch(url, { headers, signal: AbortSignal.timeout(LIVE_TIMEOUT_MS) });
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const body: unknown = await res.json();
		if (!body || typeof body !== "object") throw new Error("non-object body");
		const data = "data" in body ? body.data : undefined;
		if (!Array.isArray(data)) throw new Error("missing data[]");
		for (const entry of data) {
			const m = readLiveModel(entry);
			if (m && !seen.has(m.id)) {
				seen.add(m.id);
				out.push(m);
			}
		}
		const hasMore = "has_more" in body && body.has_more === true;
		const lastId = "last_id" in body && typeof body.last_id === "string" && body.last_id.length > 0 ? body.last_id : undefined;
		if (!hasMore || !lastId) break;
		afterId = lastId;
	}
	// An empty list is indistinguishable from a broken response and would only ever shrink the
	// picker, so treat it as a failure and keep the bundled rows.
	if (out.length === 0) throw new Error("empty data[]");
	return out;
}

/** Live model ids for `provider`, or `undefined` when unsupported/uncredentialed/unreachable.
 *  Never throws and never logs to stdout (stdout is the JSON-RPC channel). */
async function liveModels(provider: string): Promise<LiveModel[] | undefined> {
	if (provider !== "anthropic") return undefined;
	const now = Date.now();
	const hit = liveCache.get(provider);
	if (hit && hit.expires > now) return hit.models;
	try {
		const headers = await anthropicAuthHeaders();
		if (!headers) {
			// Signed out is the expected steady state for providers the user doesn't use: cache the
			// miss so we don't re-open AuthStorage on every picker paint, but say nothing.
			liveCache.set(provider, { expires: now + LIVE_FAIL_TTL_MS });
			return undefined;
		}
		const models = await fetchAnthropicModels(headers);
		liveCache.set(provider, { expires: now + LIVE_TTL_MS, models });
		return models;
	} catch (err: unknown) {
		liveCache.set(provider, { expires: now + LIVE_FAIL_TTL_MS });
		process.stderr.write(`models_list: live discovery for ${provider} failed, using bundled catalog: ${String(err)}\n`);
		return undefined;
	}
}

/** Bundled + CAD-extra rows for one provider, keyed by id (insertion order = today's row order). */
function bundledRows(provider: string): Map<string, Json> {
	const rows = new Map<string, Json>();
	for (const m of getBundledModels(provider as ProviderId)) {
		if (DEPRECATED_CATALOG[provider]?.[m.id] === true) continue;
		rows.set(m.id, modelInfo(m));
	}
	for (const m of extraModels(provider)) rows.set(m.id, modelInfo(m));
	return rows;
}

/** Display row for a live id with no catalog entry. `resolveModel` clones the provider's first
 *  bundled model as a template, which is exactly the shape completion will use for this id, so the
 *  capabilities stay truthful. Cost is nulled: the template's prices belong to a different model
 *  and a wrong price is worse than an absent one — which also makes the row unpriced regardless
 *  of what the template charged. */
function liveOnlyRow(provider: string, m: LiveModel): Json {
	const row = modelInfo({ ...resolveModel(provider, m.id), name: m.name });
	row.cost = null;
	row.priced = false;
	return row;
}

/** `models_list {provider?}` — one provider's models, or every provider's when omitted.
 *  Live-discovered ids (newest first) lead, keeping bundled metadata where both know the id;
 *  bundled-only ids follow, so the result is never smaller than the pure-catalog answer. */
export async function handleModelsList(params: Json): Promise<Json> {
	const provider = optStr(params.provider);
	const providers = provider ? [provider] : getBundledProviders();
	const models: Json[] = [];
	let anyLive = false;
	for (const p of providers) {
		const bundled = bundledRows(p);
		const live = await liveModels(p);
		if (live) {
			anyLive = true;
			for (const m of live) {
				const known = bundled.get(m.id);
				if (known) {
					known.source = "live";
					models.push(known);
					bundled.delete(m.id);
				} else {
					try {
						const row = liveOnlyRow(p, m);
						row.source = "live";
						models.push(row);
					} catch {
						// No bundled template for this provider at all — nothing truthful to report.
					}
				}
			}
		}
		for (const row of bundled.values()) {
			row.source = "bundled";
			models.push(row);
		}
	}
	return { models, source: anyLive ? "live" : "bundled" };
}

/** One `cost_summary` input row: tokens observed for one provider/model turn. */
interface CostEvent {
	provider: string;
	model: string;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
}

function readCostEvent(v: unknown): CostEvent | undefined {
	const o = asObject(v);
	const provider = str(o.provider);
	const model = str(o.model);
	if (!provider || !model) return undefined;
	const usage = asObject(o.usage);
	return {
		provider,
		model,
		input: num(usage.input) ?? num(o.input) ?? 0,
		output: num(usage.output) ?? num(o.output) ?? 0,
		cacheRead: num(usage.cacheRead) ?? num(o.cacheRead) ?? 0,
		cacheWrite: num(usage.cacheWrite) ?? num(o.cacheWrite) ?? 0,
	};
}

interface CostRollup {
	provider: string;
	model: string;
	/** Whether the catalog priced this model at all (see `isPriced`). `false` ⇒ `usd` is a
	 *  placeholder 0 with no pricing behind it, not a measured $0. */
	priced: boolean;
	usd: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
}

/** `provider`/`model` are `"*"` for the session aggregate, which is hand-serialized in
 *  `handleCostSummary` and never passes through `rollupRow`, so its `priced` is never emitted —
 *  aggregate provenance is reported as `pricedModels`/`unpricedModels` instead. */
function newRollup(provider: string, model: string, priced = false): CostRollup {
	return { provider, model, priced, usd: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
}

function rollupRow(r: CostRollup): Json {
	return {
		provider: r.provider,
		model: r.model,
		usd: r.usd,
		priced: r.priced,
		tokens: { input: r.input, output: r.output, cacheRead: r.cacheRead, cacheWrite: r.cacheWrite },
	};
}

/**
 * `cost_summary {events?}` — pure calculator: $/Mtok pricing (catalog `Model.cost`) times
 * caller-supplied token counts, no persistence, no network. `events[].model` unresolvable
 * against the catalog (unknown provider/model, never bundled and no template) is skipped
 * rather than guessed at, since a wrong price would be worse than an absent one.
 *
 * A model the catalog never priced contributes exactly 0 to `usd`, which is indistinguishable
 * from a genuinely free turn, so the result also carries provenance: `priced` per row plus
 * `totalModels`/`pricedModels`/`unpricedModels` over the models that actually moved tokens.
 * A caller that sees `unpricedModels` non-empty MUST NOT present `total.usd` as complete.
 */
export function handleCostSummary(params: Json): Json {
	const rawEvents = Array.isArray(params.events) ? params.events : [];
	const perModel = new Map<string, CostRollup>();
	const total = newRollup("*", "*");
	for (const raw of rawEvents) {
		const event = readCostEvent(raw);
		if (!event) continue;
		let model: Model<Api>;
		try {
			model = resolveModel(event.provider, event.model);
		} catch {
			continue;
		}
		const usd =
			(event.input * model.cost.input +
				event.output * model.cost.output +
				event.cacheRead * model.cost.cacheRead +
				event.cacheWrite * model.cost.cacheWrite) /
			1_000_000;
		const key = `${event.provider}/${event.model}`;
		// Same key ⇒ same resolved catalog entry, so `priced` is decided once at row creation.
		const entry = perModel.get(key) ?? newRollup(event.provider, event.model, isPriced(model));
		entry.usd += usd;
		entry.input += event.input;
		entry.output += event.output;
		entry.cacheRead += event.cacheRead;
		entry.cacheWrite += event.cacheWrite;
		perModel.set(key, entry);
		total.usd += usd;
		total.input += event.input;
		total.output += event.output;
		total.cacheRead += event.cacheRead;
		total.cacheWrite += event.cacheWrite;
	}
	const rows = Array.from(perModel.values());
	// A row only counts toward priced/unpriced provenance once it actually moved tokens: naming a
	// model the user never spent anything on would be noise.
	const counted = rows.filter((r) => r.input > 0 || r.output > 0 || r.cacheRead > 0 || r.cacheWrite > 0);
	const unpricedModels = counted.filter((r) => !r.priced).map((r) => `${r.provider}/${r.model}`);
	return {
		perModel: rows.map(rollupRow),
		total: { usd: total.usd, tokens: { input: total.input, output: total.output, cacheRead: total.cacheRead, cacheWrite: total.cacheWrite } },
		// All three range over token-contributing rows only, so
		// `pricedModels + unpricedModels.length === totalModels` always holds.
		totalModels: counted.length,
		pricedModels: counted.length - unpricedModels.length,
		unpricedModels,
	};
}
