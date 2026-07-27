/**
 * CAD-side catalog supplement (PRESERVED across re-vendor, unlike vendor/omp/packages/catalog).
 *
 * Layers our own rows over the bundled OMP catalog (`@oh-my-pi/pi-catalog` models.json) without
 * editing vendored data (a re-vendor would clobber it). Merged in `models.ts` for both
 * `models_list` (the Providers-tab catalog) and `resolveModel` (completion + cost_summary);
 * a same-id extra OVERRIDES the bundled row.
 *
 * Each entry is CLONED from the bundled model of the same id so the full `Model<Api>` shape
 * stays valid without hand-authoring every field.
 */
import { getBundledModel, getBundledModels } from "@oh-my-pi/pi-catalog";
import type { Api, Model } from "@oh-my-pi/pi-catalog";

type ProviderId = Parameters<typeof getBundledModel>[0];

/** Kimi K3 flat pricing on the official Moonshot API ($/Mtok): $3 in, $15 out, $0.30 cached. */
const K3_COST = { input: 3, output: 15, cacheRead: 0.3, cacheWrite: 0 };

/** Clone a bundled model and retarget it, or fall back to the provider's first model. */
function clone(
	provider: string,
	siblingId: string,
	over: Partial<Model<Api>>,
): Model<Api> | undefined {
	const pid = provider as ProviderId;
	const base =
		(getBundledModel(pid, siblingId) as Model<Api> | undefined) ??
		(getBundledModels(pid)[0] as Model<Api> | undefined);
	if (!base) return undefined;
	return { ...base, ...over };
}

/** Same-id overrides + models missing from the bundled catalog, keyed by provider id. */
export function extraModels(provider: string): Model<Api>[] {
	const out: Model<Api>[] = [];
	const push = (m: Model<Api> | undefined) => {
		if (m) out.push(m);
	};
	if (provider === "kimi-code") {
		// cost MUST be overridden explicitly. Bundled `kimi-code` rows carry
		// {input:0, output:0, cacheRead:0} because it is a subscription plan, so the GUI
		// renders the row as "free"/$0 (host/src/ui/copilot/settings.cpp treats
		// costIn==0 && costOut==0 as free) and the Metrics view prices every K3 turn at
		// $0. This is the same Kimi K3 the metered `moonshot` provider sells, so both
		// context variants (1M and 256k) get real per-Mtok economics; that keeps the K3
		// rows (moonshot / kimi-code / zenmux) agreeing instead of one silently reading $0.
		push(clone("kimi-code", "k3", { cost: K3_COST }));
		push(clone("kimi-code", "k3-256k", { cost: K3_COST }));
	}
	return out;
}

/**
 * Sunset / superseded catalog ids to hide from `models_list`, keyed by provider id.
 * Static membership table → nested `Record<string, true>`; `models.ts` checks
 * `DEPRECATED_CATALOG[provider]?.[id] === true`.
 */
export const DEPRECATED_CATALOG: Record<string, Record<string, true>> = {
	moonshot: {
		"moonshot-v1-8k": true, "moonshot-v1-32k": true, "moonshot-v1-128k": true,
		"moonshot-v1-8k-vision-preview": true, "moonshot-v1-32k-vision-preview": true,
		"moonshot-v1-128k-vision-preview": true, "moonshot-v1-auto": true,
		"kimi-k2.5": true, "kimi-k2-0711-preview": true,
		"kimi-k2-0905-preview": true, "kimi-k2-turbo-preview": true,
	},
	"kimi-code": { "kimi-k2.5": true, "kimi-k2-turbo-preview": true, "kimi-k2": true },
	zenmux: { "moonshotai/kimi-k2.5": true, "moonshotai/kimi-k2-0711": true },
};
