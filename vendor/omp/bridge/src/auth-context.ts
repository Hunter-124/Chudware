/**
 * Provider auth state, backed verbatim by OMP's `AuthStorage` +
 * `SqliteAuthCredentialStore` (vendor/omp/packages/ai/src/auth-storage.ts). One
 * singleton store per bridge process, at a CAD-owned SQLite path (never OMP's
 * `~/.omp/agent/agent.db`).
 *
 * That db is CAD's analogue of `agent.db`, so it gets the same corruption recovery OMP's
 * `AgentStorage` has: a malformed file is quarantined aside and recreated rather than failing
 * every auth RPC forever. See `quarantineCorruptAuthDb` / `getAuthContext`. Every OTHER open
 * failure (missing/unwritable data dir, full disk) is transient by nature, so it is NOT allowed
 * to become permanent either: the memoized context is dropped on failure and re-attempted on a
 * later RPC, cooldown-gated. One bad db must degrade auth, not the whole process.
 *
 * Credentials the provider revoked are reported, not hidden: `computeAuthState` reads the
 * soft-disabled tombstone rows that `listAuthCredentials` filters out in SQL, so a dead
 * credential surfaces as `state:"error"` with its cause instead of masquerading as
 * `signed_out` — and `auth_remove_key` can delete such a row (`purgeDisabledCredential`).
 *
 * Implements: auth_list_providers, auth_state, auth_validate_key, auth_set_key,
 * auth_remove_key, auth_logout. OAuth login/poll/manual/cancel live in
 * oauth-flow.ts (they need the same AuthStorage instance, hence `getAuthContext`
 * is exported rather than duplicated).
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import {
	AuthStorage,
	isSqliteCorruptError,
	PROVIDER_REGISTRY,
	resolveUsedFraction,
	SqliteAuthCredentialStore,
	type CredentialDisabledEvent,
	type CredentialOriginKind,
	type DisabledCredentialSummary,
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

/** `$XDG_DATA_HOME/Chudware/omp-bridge-auth.db` (or `%APPDATA%`/`~/.local/share` equivalents) — mirrors host/src/core/app_state.cpp's `appdataDir()` naming, in its own file so the bridge never touches CAD's C++-owned config.json. A pre-rebrand `Billet/omp-bridge-auth.db` is migrated (copied, incl. -wal/-shm) the first time the Chudware db is absent. `CAD_OMP_AUTH_DB` overrides outright. */
function resolveAuthDbPath(): string {
	const override = process.env.CAD_OMP_AUTH_DB;
	if (override) return override;
	const home = os.homedir();
	const base =
		process.platform === "win32"
			? (process.env.APPDATA ?? home)
			: process.env.XDG_DATA_HOME || path.join(home, ".local", "share");
	const dbPath = path.join(base, "Chudware", "omp-bridge-auth.db");
	if (!fs.existsSync(dbPath)) {
		const legacy = path.join(base, "Billet", "omp-bridge-auth.db");
		if (fs.existsSync(legacy)) {
			fs.mkdirSync(path.dirname(dbPath), { recursive: true });
			for (const suffix of ["", "-wal", "-shm"]) {
				if (fs.existsSync(legacy + suffix)) fs.copyFileSync(legacy + suffix, dbPath + suffix);
			}
		}
	}
	return dbPath;
}

interface AuthContext {
	authStorage: AuthStorage;
	store: SqliteAuthCredentialStore;
	/** Path `store` was opened from, kept so `purgeDisabledCredential` targets exactly that file. */
	dbPath: string;
}

/**
 * Minimum gap between auth-db open ATTEMPTS after a failure. A failed open no longer sticks
 * (see `getAuthContext`), so without this every auth RPC — and the Settings panel refreshes on
 * a timer — would re-stat/re-open a broken file as fast as the UI can ask. 2s is short enough
 * that a user who fixes permissions or frees disk sees the panel recover within one refresh
 * tick, and long enough that a burst of RPCs costs one open, not one open each.
 */
const AUTH_OPEN_RETRY_COOLDOWN_MS = 2_000;

let authContextPromise: Promise<AuthContext> | undefined;
/** Last open failure, replayed verbatim to callers arriving inside the cooldown window. */
let lastAuthOpenError: Error | undefined;
let lastAuthOpenFailureMs = 0;

/**
 * Move a corrupt auth db aside so the next open recreates a usable one, and report which
 * files were moved. The `-wal`/`-shm` companions go with it: a stale WAL left beside a fresh
 * db is replayed into it and re-corrupts it immediately, which is why OMP's own `agent.db`
 * recovery moves all three. (SQLite often deletes its own companions while failing the open,
 * in which case there is simply nothing left to move.) The timestamped suffix means
 * successive failures never collide and nothing is ever deleted — the bad file stays on disk
 * for postmortem. Returns [] when the MAIN db file could not be moved; the caller must then
 * propagate its ORIGINAL open error rather than a confusing rename error.
 */
function quarantineCorruptAuthDb(dbPath: string): string[] {
	const stamp = `corrupt-${Date.now()}`;
	const moved: string[] = [];
	let movedMain = false;
	for (const file of [dbPath, `${dbPath}-wal`, `${dbPath}-shm`]) {
		try {
			if (!fs.existsSync(file)) continue;
			const dest = `${file}.${stamp}`;
			fs.renameSync(file, dest);
			moved.push(dest);
			if (file === dbPath) movedMain = true;
		} catch (err) {
			process.stderr.write(
				`[omp-bridge] WARN could not quarantine corrupt auth db file ${file}: ${err instanceof Error ? err.message : String(err)}\n`,
			);
			break;
		}
	}
	return movedMain ? moved : [];
}

/**
 * `SqliteAuthCredentialStore.open` already closes its own handle when it throws, but a
 * failure in `reload()` (corruption in a data page rather than the schema) leaves the db —
 * and its live `-wal`/`-shm` — held open. Close it before letting the error out: a caller
 * that quarantines the file must not be renaming a db this process still has open (that is
 * outright forbidden on Windows).
 */
async function openAuthContext(dbPath: string): Promise<AuthContext> {
	const store = await SqliteAuthCredentialStore.open(dbPath);
	try {
		const authStorage = new AuthStorage(store, {
			sourceLabel: `local ${dbPath}`,
			// Without a subscriber these events queue up inside AuthStorage and are never read,
			// so a credential auto-disabled mid-session (OAuth refresh rejected upstream) left no
			// trace anywhere. `auth_state` reports it to the UI on the next poll; this line is the
			// log record for the moment it happened. stderr only — stdout is the RPC channel.
			onCredentialDisabled: (event: CredentialDisabledEvent): void => {
				process.stderr.write(
					`[omp-bridge] WARN auth credential for ${event.provider} disabled: ${event.disabledCause}\n`,
				);
			},
		});
		await authStorage.reload();
		return { authStorage, store, dbPath };
	} catch (err) {
		store.close();
		throw err;
	}
}

/**
 * Lazily open the CAD-owned SQLite auth store and reload `AuthStorage` from it. Every RPC
 * handler awaits the same promise; a SUCCESSFUL open is memoized for the process lifetime.
 *
 * `SQLITE_CORRUPT`/`SQLITE_NOTADB` cannot be retried in place — a malformed db would
 * otherwise fail every auth RPC for the whole life of the install. So the corrupt file is
 * quarantined aside EXACTLY ONCE and the open retried against a fresh db (bounded retry, no
 * loop); a second failure, or any non-corruption error, propagates unchanged. Stored
 * credentials are the casualty, hence the stderr warning telling the user to sign in again.
 *
 * A FAILED open is deliberately not memoized. A rejected promise caches its rejection, so
 * memoizing one turned any transient open error — `SQLITE_CANTOPEN` from a data dir that does
 * not exist yet or is not writable, `SQLITE_IOERR` from a full disk, any native error outside
 * the {corrupt, busy} pair already handled — into every provider being permanently
 * unreachable until the app restarts, because the C++ caller treats a method-level `ok:false`
 * as final and never respawns the bridge (core/agent/omp_bridge_client.cpp `callOnce`).
 * Dropping the memo lets a later RPC retry once the condition is cleared; the in-flight
 * promise is still shared, so concurrent callers cost one open attempt between them, and
 * `AUTH_OPEN_RETRY_COOLDOWN_MS` keeps a caller that retries in a tight loop off the disk.
 */
export function getAuthContext(): Promise<AuthContext> {
	if (authContextPromise) return authContextPromise;
	if (lastAuthOpenError && Date.now() - lastAuthOpenFailureMs < AUTH_OPEN_RETRY_COOLDOWN_MS) {
		return Promise.reject(lastAuthOpenError);
	}
	const attempt = (async () => {
		const dbPath = resolveAuthDbPath();
		try {
			return await openAuthContext(dbPath);
		} catch (err) {
			const moved = isSqliteCorruptError(err) ? quarantineCorruptAuthDb(dbPath) : [];
			if (moved.length === 0) throw err;
			process.stderr.write(
				`[omp-bridge] WARN auth db ${dbPath} is corrupt (${err instanceof Error ? err.message : String(err)}); ` +
					`quarantined ${moved.join(", ")} and recreated the db empty — ` +
					"stored provider credentials are lost, please sign in again\n",
			);
			return await openAuthContext(dbPath);
		}
	})();
	authContextPromise = attempt;
	// Attaching handlers here also means `attempt` is never an unhandled rejection, whatever the
	// caller does with it. The rejection itself still propagates to every awaiting caller.
	attempt.then(
		() => {
			lastAuthOpenError = undefined;
		},
		(err: unknown) => {
			lastAuthOpenError = err instanceof Error ? err : new Error(String(err));
			lastAuthOpenFailureMs = Date.now();
			if (authContextPromise === attempt) authContextPromise = undefined;
		},
	);
	return attempt;
}

interface ProviderAuthState {
	provider: string;
	state: "signed_out" | "key_set" | "oauth_connected" | "error" | "disabled";
	origin: CredentialOriginKind | null;
	envVar: string | null;
	accounts: Json[];
	/** Dead credential tombstones: `{id, cause, type, email, disabledAtMs}`, newest last. */
	disabled: Json[];
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

/**
 * Disable causes the vendored `AuthStorage` writes for USER-initiated teardown
 * (auth-storage.ts:2503/2505/2520/2523 for remove/removeCredential/logout, :1594 for dedup).
 * These tombstones are not failures — the user asked for the removal and already knows, and
 * dedup is hygiene — so they are dropped rather than reported: "you deleted this key" must not
 * render as a provider error. Every other cause (refresh rejected, upstream `invalidated OAuth
 * token`, 401 teardown) is a real failure the user was previously never told about.
 */
const USER_INITIATED_DISABLE_CAUSES: Readonly<Record<string, true>> = {
	"deleted by user": true,
	"deduplicated duplicate credential": true,
};

/** Reportable disabled rows grouped by provider id; providers with none are simply absent. */
type DisabledIndex = ReadonlyMap<string, DisabledCredentialSummary[]>;

/**
 * Snapshot every reportable disabled credential once per RPC, grouped by provider.
 *
 * `listDisabledCredentials` is the ONLY accessor that can see these rows: `listAuthCredentials`
 * is scoped `WHERE disabled_cause IS NULL` in SQL, so filtering its output for disabled rows can
 * only ever yield nothing. One unfiltered query grouped here (rather than one query per provider
 * inside `computeAuthState`) keeps the all-providers response a single consistent read of the
 * table instead of 70 interleaved ones.
 */
async function loadDisabledIndex(store: SqliteAuthCredentialStore): Promise<DisabledIndex> {
	const index = new Map<string, DisabledCredentialSummary[]>();
	try {
		for (const row of await store.listDisabledCredentials()) {
			// Cause strings are arbitrary upstream text, so `=== true` — a cause that happens to
			// name an Object.prototype member must not match. Same guard as `KEYLESS_PROVIDERS`.
			if (USER_INITIATED_DISABLE_CAUSES[row.cause] === true) continue;
			const existing = index.get(row.provider);
			if (existing) existing.push(row);
			else index.set(row.provider, [row]);
		}
	} catch (err) {
		// Tombstones are diagnostic. Failing to read them must not take sign-in state down with
		// them: an unreadable tombstone table still leaves every active credential usable.
		process.stderr.write(
			`[omp-bridge] WARN could not list disabled auth credentials: ${err instanceof Error ? err.message : String(err)}\n`,
		);
	}
	return index;
}

function computeAuthState(
	provider: string,
	authStorage: AuthStorage,
	store: SqliteAuthCredentialStore,
	disabledIndex: DisabledIndex,
): ProviderAuthState {
	const origin = authStorage.getCredentialOrigin(provider);
	// Every row `listAuthCredentials` returns is ACTIVE by SQL construction (auth-storage.ts
	// :6690/:6693 pin `disabled_cause IS NULL`); the dead ones arrive via `disabledIndex`.
	const active: StoredAuthCredential[] = store.listAuthCredentials(provider);
	const disabled = disabledIndex.get(provider) ?? [];
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
	// PRECEDENCE: a working credential always wins. A provider holding an active api key or
	// OAuth grant is `key_set`/`oauth_connected` even with dead rows still on file — one revoked
	// key out of a pool is not an outage. `error` is reported only when there is NO active
	// credential AND at least one disabled row, i.e. the provider WAS configured and its
	// credential died; that is the case previously indistinguishable from never-configured
	// `signed_out`, which is why users re-signed-in blindly instead of being told the cause.
	if (active.some(row => row.credential.type === "oauth") || origin?.kind === "oauth") {
		state = "oauth_connected";
	} else if (active.some(row => row.credential.type === "api_key") || (origin && KEY_SET_ORIGINS[origin.kind])) {
		state = "key_set";
	} else if (disabled.length > 0) {
		state = "error";
		// Most recent disable wins. Rows arrive id-ASC and `>=` keeps the later id on a tie, so
		// rows with no recorded timestamp still resolve to the newest one by insertion order.
		const newest = disabled.reduce<DisabledCredentialSummary | undefined>(
			(best, row) => (!best || (row.disabledAtMs ?? 0) >= (best.disabledAtMs ?? 0) ? row : best),
			undefined,
		);
		error = newest?.cause ?? "credential disabled";
	} else {
		state = "signed_out";
	}
	return {
		provider,
		state,
		origin: origin?.kind ?? null,
		envVar: origin?.envVar ?? null,
		accounts,
		disabled: disabled.map(row => ({
			// String, matching the frozen wire contract; `auth_remove_key`'s `account` parser
			// accepts numeric strings (json.ts `num`), so the UI passes this back verbatim.
			id: String(row.id),
			cause: row.cause,
			type: row.type,
			email: row.email ?? null,
			// 0 = timestamp unknown (legacy rows written before `updated_at` was populated).
			disabledAtMs: row.disabledAtMs ?? 0,
		})),
		error,
	};
}

function providerRow(
	def: ProviderDefinition,
	authStorage: AuthStorage,
	store: SqliteAuthCredentialStore,
	disabledIndex: DisabledIndex,
): Json {
	const keyless = KEYLESS_PROVIDERS[def.id] === true;
	const authKinds: string[] = [];
	if (!keyless) authKinds.push("api_key");
	if (def.login) authKinds.push("oauth");
	const state = computeAuthState(def.id, authStorage, store, disabledIndex);
	return {
		id: def.id,
		label: def.name,
		authKinds,
		keyless,
		state: state.state,
		origin: state.origin,
		envVar: state.envVar,
		accounts: state.accounts,
		disabled: state.disabled,
		error: state.error,
		consoleUrl: CONSOLE_URLS[def.id] ?? null,
		modelsAvailable: getBundledModels(def.id as ProviderId).length,
	};
}

/** `auth_list_providers {}` -> `{providers: ProviderRow[]}` (no usage — that's `auth_state`). */
export async function handleAuthListProviders(): Promise<Json> {
	const { authStorage, store } = await getAuthContext();
	const disabledIndex = await loadDisabledIndex(store);
	return { providers: PROVIDER_REGISTRY.map(def => providerRow(def, authStorage, store, disabledIndex)) };
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
	const disabledIndex = await loadDisabledIndex(store);
	const provider = optStr(params.provider);
	if (provider) {
		const state = computeAuthState(provider, authStorage, store, disabledIndex);
		const usage = await usageWindowFor(authStorage, provider);
		return { ...state, usage };
	}
	return { providers: PROVIDER_REGISTRY.map(def => computeAuthState(def.id, authStorage, store, disabledIndex)) };
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

/**
 * Hard-delete one disabled credential tombstone, `true` when a row was actually deleted.
 *
 * Nothing reachable from the vendored API can do this. `AuthStorage.removeCredential` looks the
 * id up in its in-memory snapshot, and `reload()` hydrates that snapshot from the active-only
 * `listAuthCredentials()`, so a disabled id is never found and it returns false without ever
 * reaching the store (auth-storage.ts:2514-2517). The store's own writers cannot finish the job
 * either: `deleteAuthCredential` merely re-stamps `disabled_cause` (:7442 — the row would stay
 * in `disabled[]` forever) and `deleteAuthCredentialsForProvider` is scoped
 * `WHERE disabled_cause IS NULL` (:6734-6736). The one `DELETE FROM auth_credentials`
 * statement in the package is private (`#hardDeleteStmt`, :6737) with no public caller, and
 * vendor/omp/packages is content-hash locked by scripts/vendor_omp.py, so it cannot grow one.
 *
 * Hence a short-lived second connection to the same file, alive for exactly one statement. Safe
 * rather than clever: the store runs `journal_mode=WAL` with `busy_timeout=5000`
 * (auth-storage.ts:6867-6870), so this single-row DELETE serializes against the long-lived
 * connection instead of failing, and a tombstone has no in-memory representation in
 * `AuthStorage`, so deleting it leaves no cache stale. `readwrite` without `create` means a
 * missing file errors instead of being conjured. The `provider` and `disabled_cause IS NOT NULL`
 * guards mean this can never delete a LIVE credential, or another provider's row, whatever id
 * the UI passes; `RETURNING id` reports whether it matched without trusting a driver-specific
 * `changes` field.
 */
function purgeDisabledCredential(dbPath: string, provider: string, credentialId: number): boolean {
	const db = new Database(dbPath, { readwrite: true });
	try {
		db.run("PRAGMA busy_timeout = 5000");
		const deleted = db
			.prepare(
				"DELETE FROM auth_credentials WHERE id = ? AND provider = ? AND disabled_cause IS NOT NULL RETURNING id",
			)
			.all(credentialId, provider);
		return deleted.length > 0;
	} finally {
		db.close();
	}
}

/**
 * Remove one credential row by id: the live path first, the tombstone path as a fallback.
 *
 * `auth_state` hands the UI both active `accounts[]` ids and dead `disabled[]` ids out of the
 * same id space, and the UI has no business knowing which store layer owns which — so one call
 * covers both instead of a second RPC the C++ side would have to choose between.
 */
async function removeCredentialById(ctx: AuthContext, provider: string, credentialId: number): Promise<boolean> {
	if (await ctx.authStorage.removeCredential(provider, credentialId)) return true;
	return purgeDisabledCredential(ctx.dbPath, provider, credentialId);
}

/** `auth_remove_key {provider, account?}` -> removes one stored credential row (`account` = its numeric id, active or disabled) or every credential for the provider. */
export async function handleAuthRemoveKey(params: Json): Promise<Json> {
	const provider = str(params.provider);
	if (!provider) throw new Error("auth_remove_key requires provider");
	const ctx = await getAuthContext();
	const account = num(params.account);
	if (account !== undefined) {
		return { ok: await removeCredentialById(ctx, provider, account) };
	}
	await ctx.authStorage.remove(provider);
	return { ok: true };
}

/** `auth_logout {provider, account?}` -> same removal semantics as auth_remove_key, named for the OAuth/UI flow that calls it. */
export async function handleAuthLogout(params: Json): Promise<Json> {
	const provider = str(params.provider);
	if (!provider) throw new Error("auth_logout requires provider");
	const ctx = await getAuthContext();
	const account = num(params.account);
	if (account !== undefined) {
		return { ok: await removeCredentialById(ctx, provider, account) };
	}
	await ctx.authStorage.logout(provider);
	return { ok: true };
}
