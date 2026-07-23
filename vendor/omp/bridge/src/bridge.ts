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
 * baseUrl?,system?,messages,maxTokens?}.
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
import { getBundledModel, getBundledModels, getBundledProviders } from "@oh-my-pi/pi-catalog";
import type { Api, Model } from "@oh-my-pi/pi-catalog";

type Json = Record<string, unknown>;
/** The branded provider-id parameter `getBundledModel`/`getBundledModels` accept. */
type ProviderId = Parameters<typeof getBundledModel>[0];

function log(...args: unknown[]): void {
	process.stderr.write(args.map(String).join(" ") + "\n");
}

// Narrow an unknown JSON value to an object map. `as Json` is safe after the typeof guard
// (Json is just Record<string, unknown>); used at ~every RPC boundary, hence a shared helper.
function asObject(v: unknown): Json {
	return v && typeof v === "object" ? (v as Json) : {};
}
// Read a string field from unknown RPC input; used across every method, kept in lockstep.
function str(v: unknown, fallback = ""): string {
	return typeof v === "string" ? v : fallback;
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

/** Resolve an OMP Model, honoring a caller-supplied base_url override for proxies/local endpoints. */
function resolveModel(provider: string, modelId: string, baseUrl?: string): Model<Api> {
	// `as ProviderId`: provider is a runtime string; the registry keys on a branded union.
	const providerId = provider as ProviderId;
	// getBundledModel is typed non-optional but returns undefined on a registry miss.
	let model = getBundledModel(providerId, modelId) as Model<Api> | undefined;
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

function modelInfo(m: Model<Api>): Json {
	return {
		id: m.id,
		name: m.name,
		api: m.api,
		provider: m.provider,
		input: m.input,
		supportsTools: m.supportsTools ?? true,
		reasoning: m.reasoning ?? false,
		contextWindow: m.contextWindow ?? null,
		cost: m.cost ?? null,
	};
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
