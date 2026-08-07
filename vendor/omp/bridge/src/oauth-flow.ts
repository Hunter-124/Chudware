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
type FlowInteraction = "browser" | "device" | "manual";
type TerminalFlowStatus = Extract<FlowStatus, "connected" | "error" | "cancelled">;
type BridgeOAuthController = OAuthController & {
	onAuth: (info: { url: string; launchUrl?: string; instructions?: string }) => void;
	onPrompt: (prompt: { message: string; placeholder?: string }) => Promise<string>;
};

interface OAuthFlow {
	provider: string;
	status: FlowStatus;
	interaction?: FlowInteraction;
	url?: string;
	launchUrl?: string;
	instructions?: string;
	progress?: string;
	error?: string;
	identity?: OAuthLoginIdentity;
	controller: AbortController;
	/** Resolver for the in-flight `onManualCodeInput`/`onPrompt` callback, when the flow is currently awaiting one. */
	manualResolve?: (input: string) => void;
	manualReject?: (reason?: unknown) => void;
	retentionScheduled?: boolean;
}

const flows = new Map<string, OAuthFlow>();

/** Grace window a settled (connected/error/cancelled) flow stays pollable before eviction. */
const FLOW_RETENTION_MS = 10 * 60_000;
const UNKNOWN_FLOW_ERROR = "OAuth flow is unknown or expired";
const CANCELLED_FLOW_ERROR = "OAuth flow cancelled";
const LOGIN_FAILED_ERROR = "OAuth login failed";

function isTerminalStatus(status: FlowStatus): status is TerminalFlowStatus {
	return status === "connected" || status === "error" || status === "cancelled";
}

function newFlowHandle(): string {
	let handle: string;
	do {
		handle = crypto.randomUUID();
	} while (flows.has(handle));
	return handle;
}

function clearManualResolver(flow: OAuthFlow, reason?: Error): void {
	const reject = flow.manualReject;
	flow.manualResolve = undefined;
	flow.manualReject = undefined;
	flow.interaction = undefined;
	if (reason && reject) reject(reason);
}

function retainSettledFlow(handle: string, flow: OAuthFlow): void {
	if (flow.retentionScheduled) return;
	flow.retentionScheduled = true;
	setTimeout(() => {
		if (flows.get(handle) === flow) flows.delete(handle);
	}, FLOW_RETENTION_MS);
}

function settleFlow(
	handle: string,
	flow: OAuthFlow,
	status: TerminalFlowStatus,
	error?: string,
	identity?: OAuthLoginIdentity,
): void {
	if (isTerminalStatus(flow.status)) return;
	flow.status = status;
	flow.error = error;
	flow.identity = identity;
	clearManualResolver(flow, status === "cancelled" ? new Error(CANCELLED_FLOW_ERROR) : undefined);
	retainSettledFlow(handle, flow);
}

function requestManualInput(flow: OAuthFlow, instructions?: string): Promise<string> {
	if (isTerminalStatus(flow.status) || flow.controller.signal.aborted) {
		return Promise.reject(new Error(CANCELLED_FLOW_ERROR));
	}
	flow.status = "awaiting_auth";
	flow.interaction = "manual";
	if (instructions !== undefined) flow.instructions = instructions;
	return new Promise<string>((resolve, reject) => {
		flow.manualResolve = resolve;
		flow.manualReject = reject;
	});
}

function snapshotFlow(handle: string, flow: OAuthFlow): Json {
	return {
		handle,
		provider: flow.provider,
		status: flow.status,
		url: flow.url ?? null,
		launchUrl: flow.launchUrl ?? null,
		instructions: flow.instructions ?? null,
		progress: flow.progress ?? null,
		interaction: flow.interaction ?? null,
		canSubmitManual: flow.manualResolve !== undefined,
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

function unknownFlowSnapshot(handle: string): Json {
	return {
		handle,
		provider: "",
		status: "error",
		url: null,
		launchUrl: null,
		instructions: null,
		progress: null,
		interaction: null,
		canSubmitManual: false,
		error: UNKNOWN_FLOW_ERROR,
		credential_saved: false,
		identity: null,
	};
}

async function runOAuthFlow(
	handle: string,
	flow: OAuthFlow,
	ctrl: BridgeOAuthController,
): Promise<void> {
	if (isTerminalStatus(flow.status)) return;
	try {
		const { authStorage } = await getAuthContext();
		if (isTerminalStatus(flow.status)) return;
		const identity = await authStorage.login(flow.provider, ctrl);
		settleFlow(handle, flow, "connected", undefined, identity);
	} catch (err: unknown) {
		if (flow.controller.signal.aborted) return;
		const message = err instanceof Error ? err.message : String(err);
		settleFlow(handle, flow, "error", message || LOGIN_FAILED_ERROR);
	}
}

/** `auth_begin_oauth {provider}` -> records a starting flow immediately, then runs `AuthStorage.login` outside bridge dispatch. */
export function handleAuthBeginOAuth(params: Json): Json {
	const provider = str(params.provider);
	if (!provider) throw new Error("auth_begin_oauth requires provider");
	if (!getProviderDefinition(provider)?.login) {
		throw new Error(`provider '${provider}' has no OAuth login flow`);
	}

	const handle = newFlowHandle();
	const controller = new AbortController();
	const flow: OAuthFlow = { provider, status: "starting", controller };
	flows.set(handle, flow);

	const ctrl: BridgeOAuthController = {
		onAuth: info => {
			if (isTerminalStatus(flow.status)) return;
			flow.url = info.url;
			flow.launchUrl = info.launchUrl;
			flow.instructions = info.instructions;
			flow.interaction = info.launchUrl ? "browser" : "device";
			flow.status = "awaiting_auth";
		},
		onProgress: message => {
			if (isTerminalStatus(flow.status)) return;
			flow.progress = message;
			// A progress callback after a submitted/browser callback means the
			// controller has advanced beyond the outstanding manual interaction.
			if (flow.manualResolve) clearManualResolver(flow);
		},
		// Raced against the callback server by OAuthCallbackFlow itself (callback-server.ts:377-397),
		// so loopback providers (Anthropic) get a manual-paste fallback too, not just device/paste-code ones.
		onManualCodeInput: () => requestManualInput(flow),
		onPrompt: prompt => requestManualInput(flow, prompt.message),
		signal: controller.signal,
	};

	// Starting login on the next event-loop turn guarantees the bridge can write
	// this response and continue consuming stdin even if login is slow.
	setTimeout(() => {
		void runOAuthFlow(handle, flow, ctrl);
	}, 0);
	return snapshotFlow(handle, flow);
}

/** `auth_oauth_poll {handle}` -> current flow snapshot, or a stable terminal snapshot after expiry. */
export function handleAuthOAuthPoll(params: Json): Json {
	const handle = str(params.handle);
	const flow = flows.get(handle);
	return flow ? snapshotFlow(handle, flow) : unknownFlowSnapshot(handle);
}

/** `auth_oauth_manual {handle, code}` -> feeds a pasted code/redirect URL into the in-flight `onManualCodeInput`/`onPrompt` wait, when one is outstanding. */
export function handleAuthOAuthManual(params: Json): Json {
	const handle = str(params.handle);
	const code = str(params.code);
	const flow = flows.get(handle);
	if (!flow) return { ok: false, detail: UNKNOWN_FLOW_ERROR };
	if (!flow.manualResolve) return { ok: false, detail: "flow is not awaiting manual input" };
	const resolve = flow.manualResolve;
	clearManualResolver(flow);
	resolve(code);
	return { ok: true };
}

/** `auth_oauth_cancel {handle}` -> idempotently aborts a live known flow without rewriting an already-settled result. */
export function handleAuthOAuthCancel(params: Json): Json {
	const handle = str(params.handle);
	const flow = flows.get(handle);
	if (!flow) {
		return { ok: false, cancelled: false, status: "error", detail: UNKNOWN_FLOW_ERROR };
	}
	if (!isTerminalStatus(flow.status)) {
		settleFlow(handle, flow, "cancelled");
		flow.controller.abort(new Error(CANCELLED_FLOW_ERROR));
	}
	return { ok: true, cancelled: flow.status === "cancelled", status: flow.status };
}
