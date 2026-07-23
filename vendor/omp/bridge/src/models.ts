/**
 * Catalog-backed model resolution + display rows, shared by `complete`/`listModels`
 * (bridge.ts) and the new `models_list`/`cost_summary` RPCs. Reuses
 * `@oh-my-pi/pi-catalog` verbatim — no local model/cost tables.
 */
import { getBundledModel, getBundledModels, getBundledProviders } from "@oh-my-pi/pi-catalog";
import type { Api, Model } from "@oh-my-pi/pi-catalog";
import { DEPRECATED_CATALOG, extraModels } from "./catalog-extra";
import { asObject, num, optStr, str, type Json } from "./json";

/** The branded provider-id parameter `getBundledModel`/`getBundledModels` accept. */
export type ProviderId = Parameters<typeof getBundledModel>[0];

/** Resolve an OMP Model, honoring a caller-supplied base_url override for proxies/local endpoints. */
export function resolveModel(provider: string, modelId: string, baseUrl?: string): Model<Api> {
	// `as ProviderId`: provider is a runtime string; the registry keys on a branded union.
	const providerId = provider as ProviderId;
	// getBundledModel is typed non-optional but returns undefined on a registry miss.
	let model = getBundledModel(providerId, modelId) as Model<Api> | undefined;
	if (!model) {
		// CAD-side additions (kimi-k3 etc.) missing from the pinned vendor catalog.
		model = extraModels(provider).find((m) => m.id === modelId);
	}
	if (!model) {
		// Unknown id under a known provider: clone the provider's first bundled model as a
		// template and retarget it, so proxied/newer ids still stream through the right API.
		const templates = getBundledModels(providerId);
		if (templates && templates.length > 0) {
			model = { ...templates[0], id: modelId, requestModelId: modelId, name: modelId };
		}
	}
	if (!model) {
		throw new Error(`unknown provider/model: ${provider}/${modelId} (no bundled model and no template)`);
	}
	return baseUrl ? { ...model, baseUrl } : model;
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
		contextWindow: m.contextWindow ?? null,
		maxTokens: m.maxTokens ?? null,
		cost: m.cost ?? null,
	};
}

/** `models_list {provider?}` — every bundled model for one provider, or every provider when omitted. */
export function handleModelsList(params: Json): Json {
	const provider = optStr(params.provider);
	const providers = provider ? [provider] : getBundledProviders();
	const models: Json[] = [];
	for (const p of providers) {
		for (const m of getBundledModels(p as ProviderId)) {
			if (DEPRECATED_CATALOG[p]?.[m.id] === true) continue;
			models.push(modelInfo(m));
		}
		for (const m of extraModels(p)) models.push(modelInfo(m));
	}
	return { models };
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
	usd: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
}

function newRollup(provider: string, model: string): CostRollup {
	return { provider, model, usd: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
}

function rollupRow(r: CostRollup): Json {
	return {
		provider: r.provider,
		model: r.model,
		usd: r.usd,
		tokens: { input: r.input, output: r.output, cacheRead: r.cacheRead, cacheWrite: r.cacheWrite },
	};
}

/**
 * `cost_summary {events?}` — pure calculator: $/Mtok pricing (catalog `Model.cost`) times
 * caller-supplied token counts, no persistence, no network. `events[].model` unresolvable
 * against the catalog (unknown provider/model, never bundled and no template) is skipped
 * rather than guessed at, since a wrong price would be worse than an absent one.
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
		const entry = perModel.get(key) ?? newRollup(event.provider, event.model);
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
	return {
		perModel: Array.from(perModel.values()).map(rollupRow),
		total: { usd: total.usd, tokens: { input: total.input, output: total.output, cacheRead: total.cacheRead, cacheWrite: total.cacheWrite } },
	};
}
