/**
 * OAuth sign-in flows, driven entirely by `AuthStorage.login()`
 * (vendor/omp/packages/ai/src/auth-storage.ts:2589) — PKCE + loopback callback
 * server for Anthropic (registry/oauth/anthropic.ts, callback-server.ts),
 * device-code polling for Kimi (registry/oauth/kimi.ts), and everything else
 * PROVIDER_REGISTRY defines a `login` for. This module owns no OAuth protocol
 * logic itself: it just adapts OMP's `OAuthController` push callbacks
 * (onAuth/onProgress/onManualCodeInput/onPrompt) to a pollable handle, since
 * the bridge's stdio JSON-RPC has no server-push transport.
 */
import { getProviderDefinition } from "@oh-my-pi/pi-ai";
import type { OAuthController, OAuthLoginIdentity } from "@oh-my-pi/pi-ai";
import { getAuthContext } from "./auth-context";
import { str, type Json } from "./json";

type FlowStatus = "starting" | "awaiting_auth" | "connected" | "error" | "cancelled";

interface OAuthFlow {
	provider: string;
	status: FlowStatus;
	url?: string;
	launchUrl?: string;
	instructions?: string;
	progress?: string;
	error?: string;
	identity?: OAuthLoginIdentity;
	controller: AbortController;
	/** Resolver for the in-flight `onManualCodeInput`/`onPrompt` callback, when the flow is currently awaiting one. */
	manualResolve?: (input: string) => void;
}

const flows = new Map<string, OAuthFlow>();
let nextHandleId = 1;

/** Grace window a settled (connected/error/cancelled) flow stays pollable before eviction. */
const FLOW_RETENTION_MS = 10 * 60_000;
/** How long `auth_begin_oauth` waits for the flow to reach `onAuth` (a URL to show) before giving up. */
const BEGIN_TIMEOUT_MS = 20_000;

function snapshotFlow(handle: string, flow: OAuthFlow): Json {
	return {
		handle,
		provider: flow.provider,
		status: flow.status,
		url: flow.url ?? null,
		launchUrl: flow.launchUrl ?? null,
		instructions: flow.instructions ?? null,
		progress: flow.progress ?? null,
		error: flow.error ?? null,
		credential_saved: flow.status === "connected",
		identity: flow.identity
			? {
					type: flow.identity.type,
					email: flow.identity.email ?? null,
					accountId: flow.identity.accountId ?? null,
					orgId: flow.identity.orgId ?? null,
					orgName: flow.identity.orgName ?? null,
				}
			: null,
	};
}

/** `auth_begin_oauth {provider}` -> starts `AuthStorage.login`, waits for the first `onAuth` (or a terminal state), returns the launch URL + poll handle. */
export async function handleAuthBeginOAuth(params: Json): Promise<Json> {
	const provider = str(params.provider);
	if (!provider) throw new Error("auth_begin_oauth requires provider");
	if (!getProviderDefinition(provider)?.login) {
		throw new Error(`provider '${provider}' has no OAuth login flow`);
	}

	const { authStorage } = await getAuthContext();
	const handle = `oauth-${nextHandleId++}`;
	const controller = new AbortController();
	const flow: OAuthFlow = { provider, status: "starting", controller };
	flows.set(handle, flow);

	const ready = Promise.withResolvers<void>();

	const ctrl: OAuthController & {
		onAuth: (info: { url: string; launchUrl?: string; instructions?: string }) => void;
		onPrompt: (prompt: { message: string; placeholder?: string }) => Promise<string>;
	} = {
		onAuth: info => {
			flow.url = info.url;
			flow.launchUrl = info.launchUrl;
			flow.instructions = info.instructions;
			flow.status = "awaiting_auth";
			ready.resolve();
		},
		onProgress: message => {
			flow.progress = message;
		},
		// Raced against the callback server by OAuthCallbackFlow itself (callback-server.ts:377-397),
		// so loopback providers (Anthropic) get a manual-paste fallback too, not just device/paste-code ones.
		onManualCodeInput: () => new Promise<string>(resolve => (flow.manualResolve = resolve)),
		onPrompt: prompt =>
			new Promise<string>(resolve => {
				flow.instructions = prompt.message;
				flow.manualResolve = resolve;
			}),
		signal: controller.signal,
	};

	authStorage
		.login(provider, ctrl)
		.then(identity => {
			flow.status = "connected";
			flow.identity = identity;
			flow.manualResolve = undefined;
			ready.resolve();
			setTimeout(() => flows.delete(handle), FLOW_RETENTION_MS);
		})
		.catch((err: unknown) => {
			flow.status = controller.signal.aborted ? "cancelled" : "error";
			flow.error = err instanceof Error ? err.message : String(err);
			flow.manualResolve = undefined;
			ready.resolve();
			setTimeout(() => flows.delete(handle), FLOW_RETENTION_MS);
		});

	await Promise.race([ready.promise, new Promise<void>(resolve => setTimeout(resolve, BEGIN_TIMEOUT_MS))]);
	return snapshotFlow(handle, flow);
}

/** `auth_oauth_poll {handle}` -> current flow snapshot. */
export function handleAuthOAuthPoll(params: Json): Json {
	const handle = str(params.handle);
	const flow = flows.get(handle);
	if (!flow) throw new Error(`unknown oauth handle: ${handle}`);
	return snapshotFlow(handle, flow);
}

/** `auth_oauth_manual {handle, code}` -> feeds a pasted code/redirect URL into the in-flight `onManualCodeInput`/`onPrompt` wait, when one is outstanding. */
export function handleAuthOAuthManual(params: Json): Json {
	const handle = str(params.handle);
	const code = str(params.code);
	const flow = flows.get(handle);
	if (!flow) throw new Error(`unknown oauth handle: ${handle}`);
	if (!flow.manualResolve) return { ok: false, detail: "flow is not awaiting a manual code right now" };
	const resolve = flow.manualResolve;
	flow.manualResolve = undefined;
	resolve(code);
	return { ok: true };
}

/** `auth_oauth_cancel {handle}` -> aborts the flow's `AbortSignal`, which OMP's login threads through the callback wait and the device-code poll loop alike. */
export function handleAuthOAuthCancel(params: Json): Json {
	const handle = str(params.handle);
	const flow = flows.get(handle);
	if (!flow) throw new Error(`unknown oauth handle: ${handle}`);
	flow.status = "cancelled";
	flow.controller.abort(new Error("cancelled by user"));
	setTimeout(() => flows.delete(handle), FLOW_RETENTION_MS);
	return { ok: true };
}
