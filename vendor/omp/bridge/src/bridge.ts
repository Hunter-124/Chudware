/**
 * CAD ↔ OMP bridge (bun sidecar).
 *
 * Reuses oh-my-pi's real provider layer verbatim: CAD's C++ core / Python service layer speak
 * a newline-delimited JSON-RPC protocol to this process over stdio, and we translate each
 * request into an OMP `stream(...)` call, collapsing the streamed AssistantMessage down to the
 * `(text, thinking, usage)` shape CAD's synchronous `complete()` seam expects.
 *
 * Lifecycle: spawn ONE long-lived bridge per app session (like CAD's worker_rpc workers);
 * requests are multiplexed by `id`. The consumer respawns on EOF/exit. Frames are
 * newline-delimited JSON; stdout carries ONLY response lines, all logging goes to stderr.
 *
 * Protocol (one JSON object per line):
 *   -> {"id": <number>, "method": <string>, "params": <object>}
 *   <- {"id": <number>, "ok": true,  "result": <object>}
 *   <- {"id": <number>, "ok": false, "error": {"message": <string>, "status"?: <number>}}
 *
 * Methods: ping | listProviders | listModels{provider} | complete{provider,model,apiKey?,
 * baseUrl?,system?,messages,maxTokens?} | models_list{provider?} | cost_summary{events?} |
 * auth_list_providers | auth_state{provider?} | auth_validate_key{provider,apiKey} |
 * auth_set_key{provider,apiKey} | auth_remove_key{provider,account?} |
 * auth_logout{provider,account?} | auth_begin_oauth{provider} | auth_oauth_poll{handle} |
 * auth_oauth_manual{handle,code} | auth_oauth_cancel{handle}. Auth/model/cost methods are
 * implemented in the sibling json.ts/models.ts/auth-context.ts/oauth-flow.ts modules and wired
 * in through dispatch() below; see those files for the OMP surface each one reuses.
 *
 * NOTE: `stream().result()` resolves (does not throw) on provider HTTP errors — the error is
 * embedded in the returned AssistantMessage (stopReason:"error", errorStatus, errorMessage).
 * We surface that as a successful RPC result with those fields set so CAD can classify auth vs
 * transient failures without regex-scraping.
 */
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

import { stream } from "@oh-my-pi/pi-ai";
import type { AssistantMessage, Context, Message, StreamOptions } from "@oh-my-pi/pi-ai";
import { getBundledModels, getBundledProviders } from "@oh-my-pi/pi-catalog";
import {
	handleAuthListProviders,
	handleAuthLogout,
	handleAuthRemoveKey,
	handleAuthSetKey,
	handleAuthState,
	handleAuthValidateKey,
} from "./auth-context";
import { asObject, str, type Json } from "./json";
import { handleCostSummary, handleModelsList, modelInfo, resolveModel, type ProviderId } from "./models";
import { handleAuthBeginOAuth, handleAuthOAuthCancel, handleAuthOAuthManual, handleAuthOAuthPoll } from "./oauth-flow";

function log(...args: unknown[]): void {
	process.stderr.write(args.map(String).join(" ") + "\n");
}

interface CadPart {
	type: "text" | "image";
	text?: string;
	path?: string;
	data?: string; // base64 (alternative to path)
	mime?: string;
}
interface CadMessage {
	role: "user" | "assistant";
	content: string | CadPart[];
}

function readPart(v: unknown): CadPart {
	const o = asObject(v);
	return {
		type: str(o.type) === "image" ? "image" : "text",
		text: typeof o.text === "string" ? o.text : undefined,
		path: typeof o.path === "string" ? o.path : undefined,
		data: typeof o.data === "string" ? o.data : undefined,
		mime: typeof o.mime === "string" ? o.mime : undefined,
	};
}
function readMessages(v: unknown): CadMessage[] {
	if (!Array.isArray(v)) return [];
	return v.map(m => {
		const o = asObject(m);
		const role = str(o.role) === "assistant" ? "assistant" : "user";
		const content = typeof o.content === "string"
			? o.content
			: (Array.isArray(o.content) ? o.content.map(readPart) : []);
		return { role, content };
	});
}

/** Convert a CAD content part to an OMP content part (text or base64 image). */
function convertPart(part: CadPart): Json {
	if (part.type === "image") {
		let data = part.data;
		if (!data && part.path) data = readFileSync(part.path).toString("base64");
		if (!data) throw new Error("image part has neither data nor readable path");
		return { type: "image", data, mimeType: part.mime || "image/png" };
	}
	return { type: "text", text: part.text ?? "" };
}

function convertMessages(messages: CadMessage[]): Message[] {
	const built = messages.map(m => {
		const content = typeof m.content === "string"
			? [{ type: "text", text: m.content }]
			: m.content.map(convertPart);
		return { role: m.role, content };
	});
	// Boundary cast: CAD sends a structural transcript (role + text/image parts). OMP's
	// stream() reads user/assistant turns structurally; reconstructing every nominal field of
	// AssistantMessage (usage, timestamps, api, provider) at this seam would add no runtime
	// safety. Shape is validated field-by-field above in readMessages/convertPart.
	return built as unknown as Message[];
}

/** Collapse a resolved AssistantMessage down to CAD's (text, thinking, usage) shape. */
function collapse(msg: AssistantMessage): Json {
	let text = "";
	let thinking = "";
	for (const part of msg.content ?? []) {
		if (part.type === "text") text += part.text ?? "";
		else if (part.type === "thinking") thinking += part.thinking ?? "";
	}
	const out: Json = {
		text,
		thinking,
		usage: msg.usage ?? null,
		stopReason: msg.stopReason ?? "stop",
	};
	if (msg.stopReason === "error") {
		out.errorStatus = msg.errorStatus ?? null;
		out.errorMessage = msg.errorMessage ?? "provider error";
	}
	return out;
}

async function handleComplete(params: Json): Promise<Json> {
	const provider = str(params.provider);
	const modelId = str(params.model);
	if (!provider || !modelId) throw new Error("complete requires provider and model");
	const model = resolveModel(provider, modelId, params.baseUrl ? str(params.baseUrl) : undefined);

	const context: Context = { messages: convertMessages(readMessages(params.messages)) };
	const system = str(params.system);
	if (system) context.systemPrompt = [system];

	const options: StreamOptions = {};
	if (params.apiKey) options.apiKey = str(params.apiKey);
	if (typeof params.maxTokens === "number") options.maxTokens = params.maxTokens;

	const result = await stream(model, context, options).result();
	return collapse(result);
}

async function dispatch(method: string, params: Json): Promise<Json> {
	switch (method) {
		case "ping":
			return { pong: true, bun: globalThis.Bun?.version ?? "unknown" };
		case "listProviders":
			return { providers: getBundledProviders() };
		case "listModels": {
			const provider = str(params.provider);
			if (!provider) throw new Error("listModels requires provider");
			// `as ProviderId`: runtime string into the branded registry key.
			return { models: getBundledModels(provider as ProviderId).map(modelInfo) };
		}
		case "complete":
			return handleComplete(params);
		case "models_list":
			return handleModelsList(params);
		case "cost_summary":
			return handleCostSummary(params);
		case "auth_list_providers":
			return handleAuthListProviders();
		case "auth_state":
			return handleAuthState(params);
		case "auth_validate_key":
			return handleAuthValidateKey(params);
		case "auth_set_key":
			return handleAuthSetKey(params);
		case "auth_remove_key":
			return handleAuthRemoveKey(params);
		case "auth_logout":
			return handleAuthLogout(params);
		case "auth_begin_oauth":
			return handleAuthBeginOAuth(params);
		case "auth_oauth_poll":
			return handleAuthOAuthPoll(params);
		case "auth_oauth_manual":
			return handleAuthOAuthManual(params);
		case "auth_oauth_cancel":
			return handleAuthOAuthCancel(params);
		default:
			throw new Error(`unknown method: ${method}`);
	}
}

async function main(): Promise<void> {
	log(`[omp-bridge] ready (bun ${globalThis.Bun?.version ?? "?"})`);
	const rl = createInterface({ input: process.stdin });
	for await (const line of rl) {
		const trimmed = line.trim();
		if (!trimmed) continue;
		let id: unknown = null;
		try {
			const req = asObject(JSON.parse(trimmed));
			id = req.id ?? null;
			const result = await dispatch(str(req.method), asObject(req.params));
			process.stdout.write(JSON.stringify({ id, ok: true, result }) + "\n");
		} catch (err: unknown) {
			// `in` + typeof narrowing (no inline cast): read message/status only when present.
			let message = String(err);
			const error: Json = {};
			if (err && typeof err === "object") {
				if ("message" in err && typeof err.message === "string") message = err.message;
				if ("status" in err && typeof err.status === "number") error.status = err.status;
			}
			error.message = message;
			process.stdout.write(JSON.stringify({ id, ok: false, error }) + "\n");
		}
	}
}

void main();
