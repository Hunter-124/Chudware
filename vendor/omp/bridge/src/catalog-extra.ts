/**
 * CAD-side catalog supplement (PRESERVED across re-vendor, unlike vendor/omp/packages/catalog).
 *
 * The bundled OMP catalog (`@oh-my-pi/pi-catalog` models.json) is pinned to a vendor snapshot
 * that predates Kimi K3, and it still lists Moonshot's sunset `moonshot-v1-*` / `kimi-k2.5`
 * families. Rather than edit vendored data (which a re-vendor would clobber), we layer our own
 * additions + a deprecation denylist here, in our owned bridge package, and merge them in
 * `models.ts` for both `models_list` (the Providers-tab catalog) and `resolveModel` (completion).
 *
 * Each EXTRA entry is CLONED from a sibling bundled model so the full `Model<Api>` shape stays
 * valid without hand-authoring every field; we only retarget id/name/context/cost. `requestModelId`
 * is set explicitly where the display id differs from the wire id (kimi-code subscriptions only
 * accept `kimi-for-coding`).
 */
import { getBundledModel, getBundledModels } from "@oh-my-pi/pi-catalog";
import type { Api, Model } from "@oh-my-pi/pi-catalog";

type ProviderId = Parameters<typeof getBundledModel>[0];

/** Kimi K3 flat pricing on the official Moonshot API ($/Mtok): $3 in, $15 out, $0.30 cached. */
const K3_COST = { input: 3, output: 15, cacheRead: 0.3, cacheWrite: 0 };
const FREE_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
const K3_CONTEXT = 1_048_576;

/** Clone a bundled sibling and retarget it, or fall back to the provider's first model. */
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

/** Models missing from the pinned vendor catalog, keyed by provider id. Empty entries dropped. */
export function extraModels(provider: string): Model<Api>[] {
	const out: Model<Api>[] = [];
	const push = (m: Model<Api> | undefined) => {
		if (m) out.push(m);
	};
	if (provider === "moonshot") {
		push(clone("moonshot", "kimi-k2.7-code", {
			id: "kimi-k3", requestModelId: "kimi-k3", name: "Kimi K3",
			contextWindow: K3_CONTEXT, maxTokens: 262_144, cost: K3_COST,
		}));
	} else if (provider === "kimi-code") {
		// Subscription endpoint only accepts `kimi-for-coding` on the wire.
		push(clone("kimi-code", "kimi-for-coding", {
			id: "kimi-k3", requestModelId: "kimi-for-coding", name: "Kimi K3",
			contextWindow: K3_CONTEXT,
		}));
	} else if (provider === "zenmux") {
		push(clone("zenmux", "moonshotai/kimi-k2.7-code", {
			id: "moonshotai/kimi-k3", requestModelId: "moonshotai/kimi-k3", name: "Kimi K3",
			contextWindow: K3_CONTEXT, cost: K3_COST,
		}));
		push(clone("zenmux", "moonshotai/kimi-k2.7-code-free", {
			id: "moonshotai/kimi-k3-free", requestModelId: "moonshotai/kimi-k3-free",
			name: "Kimi K3 (Free)", contextWindow: K3_CONTEXT, cost: FREE_COST,
		}));
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
