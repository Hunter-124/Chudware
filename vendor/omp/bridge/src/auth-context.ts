/**
 * Provider auth state, backed verbatim by OMP's `AuthStorage` +
 * `SqliteAuthCredentialStore` (vendor/omp/packages/ai/src/auth-storage.ts). One
 * singleton store per bridge process, at a CAD-owned SQLite path (never OMP's
 * `~/.omp/agent/agent.db`).
 *
 * Implements: auth_list_providers, auth_state, auth_validate_key, auth_set_key,
 * auth_remove_key, auth_logout. OAuth login/poll/manual/cancel live in
 * oauth-flow.ts (they need the same AuthStorage instance, hence `getAuthContext`
 * is exported rather than duplicated).
 */
import * as os from "node:os";
import * as path from "node:path";
import {
	AuthStorage,
	PROVIDER_REGISTRY,
	resolveUsedFraction,
	SqliteAuthCredentialStore,
	type CredentialOriginKind,
	type ProviderDefinition,
	type StoredAuthCredential,
} from "@oh-my-pi/pi-ai";
import {
	validateAnthropicCompatibleApiKey,
	validateApiKeyAgainstModelsEndpoint,
} from "@oh-my-pi/pi-ai/registry/api-key-validation";
import { getBundledModels } from "@oh-my-pi/pi-catalog";
import type { Api, Model } from "@oh-my-pi/pi-catalog";
import { num, optStr, str, type Json } from "./json";
import type { ProviderId } from "./models";

/** Providers with no auth concept at all — local/proxy servers CAD talks to unauthenticated. */
const KEYLESS_PROVIDERS: Readonly<Record<string, true>> = {
	ollama: true,
	"llama-cpp": true,
	"lm-studio": true,
};

/** Vendor key-console deep links, where publicly documented and stable. Unlisted providers get `null`. */
const CONSOLE_URLS: Readonly<Record<string, string>> = {
	anthropic: "https://console.anthropic.com/settings/keys",
	openai: "https://platform.openai.com/api-keys",
	"openai-codex": "https://platform.openai.com/api-keys",
	moonshot: "https://platform.moonshot.ai/console/api-keys",
	google: "https://aistudio.google.com/apikey",
	"google-vertex": "https://console.cloud.google.com/vertex-ai",
	xai: "https://console.x.ai",
	groq: "https://console.groq.com/keys",
	mistral: "https://console.mistral.ai/api-keys",
	deepseek: "https://platform.deepseek.com/api_keys",
	fireworks: "https://fireworks.ai/account/api-keys",
	cerebras: "https://cloud.cerebras.ai",
	openrouter: "https://openrouter.ai/keys",
	together: "https://api.together.xyz/settings/api-keys",
	perplexity: "https://www.perplexity.ai/settings/api",
	minimax: "https://platform.minimaxi.com/user-center/basic-information/interface-key",
	huggingface: "https://huggingface.co/settings/tokens",
};

/** `$XDG_DATA_HOME/Billet/omp-bridge-auth.db` (or `%APPDATA%`/`~/.local/share` equivalents) — mirrors host/src/core/app_state.cpp's `appdataDir()` naming, in its own file so the bridge never touches CAD's C++-owned config.json/AI-CAD legacy dir. `CAD_OMP_AUTH_DB` overrides outright. */
function resolveAuthDbPath(): string {
	const override = process.env.CAD_OMP_AUTH_DB;
	if (override) return override;
	const home = os.homedir();
	if (process.platform === "win32") {
		return path.join(process.env.APPDATA ?? home, "Billet", "omp-bridge-auth.db");
	}
	const base = process.env.XDG_DATA_HOME || path.join(home, ".local", "share");
	return path.join(base, "Billet", "omp-bridge-auth.db");
}

interface AuthContext {
	authStorage: AuthStorage;
	store: SqliteAuthCredentialStore;
}

let authContextPromise: Promise<AuthContext> | undefined;

/** Lazily open the CAD-owned SQLite auth store and reload `AuthStorage` from it. Called once; every RPC handler awaits the same promise. */
export function getAuthContext(): Promise<AuthContext> {
	if (!authContextPromise) {
		authContextPromise = (async () => {
			const dbPath = resolveAuthDbPath();
			const store = await SqliteAuthCredentialStore.open(dbPath);
			const authStorage = new AuthStorage(store, { sourceLabel: `local ${dbPath}` });
			await authStorage.reload();
			return { authStorage, store };
		})();
	}
	return authContextPromise;
}

interface ProviderAuthState {
	provider: string;
	state: "signed_out" | "key_set" | "oauth_connected" | "error" | "disabled";
	origin: CredentialOriginKind | null;
	envVar: string | null;
	accounts: Json[];
	error: string | null;
}

const KEY_SET_ORIGINS: Readonly<Record<CredentialOriginKind, true>> = {
	runtime: true,
	config: true,
	env: true,
	fallback: true,
	api_key: true,
	oauth: true,
};

function computeAuthState(provider: string, authStorage: AuthStorage, store: SqliteAuthCredentialStore): ProviderAuthState {
	const origin = authStorage.getCredentialOrigin(provider);
	const rows: StoredAuthCredential[] = store.listAuthCredentials(provider);
	const active = rows.filter(row => row.disabledCause === null);
	const disabled = rows.filter(row => row.disabledCause !== null);
	const accounts = authStorage.listOAuthAccounts(provider).map(account => ({
		position: account.position,
		credentialId: account.credentialId,
		accountId: account.accountId ?? null,
		email: account.email ?? null,
		orgId: account.orgId ?? null,
		orgName: account.orgName ?? null,
	}));

	let state: ProviderAuthState["state"];
	let error: string | null = null;
	if (active.some(row => row.credential.type === "oauth") || origin?.kind === "oauth") {
		state = "oauth_connected";
	} else if (active.some(row => row.credential.type === "api_key") || (origin && KEY_SET_ORIGINS[origin.kind])) {
		state = "key_set";
	} else if (disabled.length > 0) {
		state = "error";
		error = disabled[disabled.length - 1]?.disabledCause ?? "credential disabled";
	} else {
		state = "signed_out";
	}
	return { provider, state, origin: origin?.kind ?? null, envVar: origin?.envVar ?? null, accounts, error };
}

function providerRow(def: ProviderDefinition, authStorage: AuthStorage, store: SqliteAuthCredentialStore): Json {
	const keyless = KEYLESS_PROVIDERS[def.id] === true;
	const authKinds: string[] = [];
	if (!keyless) authKinds.push("api_key");
	if (def.login) authKinds.push("oauth");
	const state = computeAuthState(def.id, authStorage, store);
	return {
		id: def.id,
		label: def.name,
		authKinds,
		keyless,
		state: state.state,
		origin: state.origin,
		envVar: state.envVar,
		accounts: state.accounts,
		error: state.error,
		consoleUrl: CONSOLE_URLS[def.id] ?? null,
		modelsAvailable: getBundledModels(def.id as ProviderId).length,
	};
}

/** `auth_list_providers {}` -> `{providers: ProviderRow[]}` (no usage — that's `auth_state`). */
export async function handleAuthListProviders(): Promise<Json> {
	const { authStorage, store } = await getAuthContext();
	return { providers: PROVIDER_REGISTRY.map(def => providerRow(def, authStorage, store)) };
}

async function usageWindowFor(authStorage: AuthStorage, provider: string): Promise<Json | null> {
	try {
		const reports = await authStorage.fetchUsageReports({ signal: AbortSignal.timeout(8_000) });
		const report = reports?.find(r => r.provider === provider);
		if (!report) return null;
		return {
			fetchedAt: report.fetchedAt,
			notes: report.notes ?? [],
			windows: report.limits.map(limit => ({
				id: limit.id,
				label: limit.label,
				windowLabel: limit.window?.label ?? null,
				usedFraction: resolveUsedFraction(limit) ?? null,
				status: limit.status ?? "unknown",
				resetsAt: limit.window?.resetsAt ?? null,
			})),
		};
	} catch {
		// Usage endpoints are best-effort ("+ usage window if available"); auth_state must
		// still answer with sign-in state even when the provider's usage API is down/slow.
		return null;
	}
}

/** `auth_state {provider?}` -> one provider's state + usage window, or every provider's state when omitted. */
export async function handleAuthState(params: Json): Promise<Json> {
	const { authStorage, store } = await getAuthContext();
	const provider = optStr(params.provider);
	if (provider) {
		const state = computeAuthState(provider, authStorage, store);
		const usage = await usageWindowFor(authStorage, provider);
		return { ...state, usage };
	}
	return { providers: PROVIDER_REGISTRY.map(def => computeAuthState(def.id, authStorage, store)) };
}

/** `auth_validate_key {provider, apiKey}` -> `{ok, detail?}` via OMP's cheap-request validators (no key is ever persisted here). */
export async function handleAuthValidateKey(params: Json): Promise<Json> {
	const provider = str(params.provider);
	const apiKey = str(params.apiKey);
	if (!provider || !apiKey) throw new Error("auth_validate_key requires provider and apiKey");
	const models = getBundledModels(provider as ProviderId);
	const model: Model<Api> | undefined = models[0];
	if (!model) return { ok: false, detail: `unknown provider: ${provider}` };
	try {
		if (model.api === "anthropic-messages") {
			await validateAnthropicCompatibleApiKey({
				provider,
				apiKey,
				baseUrl: model.baseUrl,
				model: model.requestModelId ?? model.id,
			});
		} else if (
			model.api === "openai-completions" ||
			model.api === "openai-responses" ||
			model.api === "openrouter" ||
			model.api === "azure-openai-responses" ||
			model.api === "openai-codex-responses"
		) {
			await validateApiKeyAgainstModelsEndpoint({
				provider,
				apiKey,
				modelsUrl: `${model.baseUrl.replace(/\/+$/, "")}/models`,
			});
		} else {
			return { ok: false, detail: `live validation not supported for api '${model.api}'; save without validating` };
		}
	} catch (err) {
		return { ok: false, detail: err instanceof Error ? err.message : String(err) };
	}
	return { ok: true, detail: `key accepted — ${models.length} model(s) available` };
}

/** `auth_set_key {provider, apiKey}` -> persists via `AuthStorage.set` (SQLite row, `ApiKeyCredential{type:"api_key",source:"login"}`). */
export async function handleAuthSetKey(params: Json): Promise<Json> {
	const provider = str(params.provider);
	const apiKey = str(params.apiKey);
	if (!provider || !apiKey) throw new Error("auth_set_key requires provider and apiKey");
	const { authStorage } = await getAuthContext();
	await authStorage.set(provider, { type: "api_key", key: apiKey, source: "login" });
	return { ok: true };
}

/** `auth_remove_key {provider, account?}` -> removes one stored credential row (`account` = its numeric id) or every credential for the provider. */
export async function handleAuthRemoveKey(params: Json): Promise<Json> {
	const provider = str(params.provider);
	if (!provider) throw new Error("auth_remove_key requires provider");
	const { authStorage } = await getAuthContext();
	const account = num(params.account);
	if (account !== undefined) {
		return { ok: await authStorage.removeCredential(provider, account) };
	}
	await authStorage.remove(provider);
	return { ok: true };
}

/** `auth_logout {provider, account?}` -> same removal semantics as auth_remove_key, named for the OAuth/UI flow that calls it. */
export async function handleAuthLogout(params: Json): Promise<Json> {
	const provider = str(params.provider);
	if (!provider) throw new Error("auth_logout requires provider");
	const { authStorage } = await getAuthContext();
	const account = num(params.account);
	if (account !== undefined) {
		return { ok: await authStorage.removeCredential(provider, account) };
	}
	await authStorage.logout(provider);
	return { ok: true };
}
