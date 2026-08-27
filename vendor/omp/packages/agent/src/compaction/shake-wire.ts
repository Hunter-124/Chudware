import type { ImageContent, TextContent, ToolResultMessage } from "@oh-my-pi/pi-ai";
import { Tokenizer } from "../tokenizer";
import type { AgentMessage } from "../types";
import type { CustomMessageEntry, SessionEntry, SessionMessageEntry } from "./entries";
import {
	AGGRESSIVE_SHAKE_CONFIG,
	applyShakeRegions,
	collectShakeRegions,
	DEFAULT_SHAKE_CONFIG,
	type ShakeConfig,
	type ShakeRegion,
} from "./shake";

export type ShakeWireJson = null | boolean | number | string | ShakeWireJson[] | { [key: string]: ShakeWireJson };

export type ShakeWireJsonObject = { [key: string]: ShakeWireJson };

/** A conservative, JSON-safe chat message accepted by the shake wire adapter. */
export interface ShakeWireMessage {
	role: string;
	content: string | ShakeWireJsonObject[];
	tool_calls?: ShakeWireJsonObject[];
	tool_call_id?: string;
	toolCallId?: string;
	tool_name?: string;
	toolName?: string;
	is_error?: boolean;
	isError?: boolean;
	pruned_at?: ShakeWireJson;
	prunedAt?: ShakeWireJson;
	useless?: boolean;
	[key: string]: ShakeWireJson | undefined;
}

export interface ShakeWireRequest {
	messages: ShakeWireMessage[];
	aggressive?: boolean;
	keepBoundaryIndex?: number;
}

export interface ShakeWireRegion {
	kind: "toolResult" | "block";
	message_index: number;
	block_index?: number;
	start?: number;
	end?: number;
	tokens: number;
	original_text: string;
	label: string;
	region_index: number;
	placeholder: string;
}

export interface ShakeWireResult {
	messages: ShakeWireMessage[];
	savedTokens: number;
	regions: ShakeWireRegion[];
	placeholders: number;
	notice: string;
}

interface EntryBinding {
	readonly messageIndex: number;
	readonly wireMessage: ShakeWireMessage;
	readonly entry: SessionMessageEntry | CustomMessageEntry;
	readonly stringContent: boolean;
	readonly assistantStringContent: boolean;
	readonly toolResult: boolean;
	readonly camelPrunedStyle: boolean;
}

interface CanonicalWireToolCall {
	readonly type: "toolCall";
	readonly id: string;
	readonly name: string;
	readonly arguments: Record<string, unknown>;
}

const NO_TOOL_CALLS: readonly CanonicalWireToolCall[] = [];

const SYNTHETIC_TIMESTAMP = "1970-01-01T00:00:00.000Z";
const PLACEHOLDER_TOKEN_ESTIMATE = 16;

function isJsonObject(value: ShakeWireJson | undefined): value is ShakeWireJsonObject {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstString(message: ShakeWireMessage, keys: readonly string[]): string | undefined {
	for (const key of keys) {
		const value = message[key];
		if (typeof value === "string" && value.length > 0) return value;
	}
	return undefined;
}

function parseToolArguments(value: ShakeWireJson | undefined): Record<string, unknown> {
	if (isJsonObject(value)) return value;
	if (typeof value !== "string") return {};
	try {
		const parsed: unknown = JSON.parse(value);
		return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
			? (parsed as Record<string, unknown>)
			: {};
	} catch {
		return {};
	}
}

function canonicalToolCalls(message: ShakeWireMessage): CanonicalWireToolCall[] {
	if (!Array.isArray(message.tool_calls)) return [];
	const calls: CanonicalWireToolCall[] = [];
	for (const value of message.tool_calls) {
		const fn = isJsonObject(value.function) ? value.function : undefined;
		const id = typeof value.id === "string" ? value.id : undefined;
		const name = typeof fn?.name === "string" ? fn.name : typeof value.name === "string" ? value.name : undefined;
		if (!id || !name) continue;
		calls.push({
			type: "toolCall",
			id,
			name,
			arguments: parseToolArguments(fn?.arguments ?? value.arguments),
		});
	}
	return calls;
}

function canonicalToolResultContent(content: ShakeWireMessage["content"]): ToolResultMessage["content"] {
	if (typeof content === "string") return content.length === 0 ? [] : [{ type: "text", text: content }];
	const canonical: ToolResultMessage["content"] = [];
	for (const block of content) {
		if (block.type === "text" && typeof block.text === "string") {
			const text: TextContent = { type: "text", text: block.text };
			if (typeof block.textSignature === "string") text.textSignature = block.textSignature;
			canonical.push(text);
			continue;
		}
		if (block.type !== "image" || typeof block.data !== "string" || typeof block.mimeType !== "string") continue;
		const image: ImageContent = { type: "image", data: block.data, mimeType: block.mimeType };
		if (block.detail === "auto" || block.detail === "low" || block.detail === "high" || block.detail === "original") {
			image.detail = block.detail;
		}
		canonical.push(image);
	}
	return canonical;
}

function canonicalPrunedAt(message: ShakeWireMessage): number | undefined {
	const snake = message.pruned_at;
	if (snake !== undefined && snake !== null) return 1;
	const camel = message.prunedAt;
	if (camel !== undefined && camel !== null) return 1;
	return undefined;
}

function makeEntry(
	message: ShakeWireMessage,
	messageIndex: number,
	toolNameById: ReadonlyMap<string, string>,
	assistantToolCalls: readonly CanonicalWireToolCall[],
): EntryBinding {
	const id = `shake-wire-${messageIndex}`;
	const base = {
		id,
		parentId: messageIndex === 0 ? null : `shake-wire-${messageIndex - 1}`,
		timestamp: SYNTHETIC_TIMESTAMP,
	};
	const stringContent = typeof message.content === "string";

	if (message.role === "tool" || message.role === "toolResult") {
		const content = canonicalToolResultContent(message.content);
		const toolCallId = firstString(message, ["tool_call_id", "toolCallId"]) ?? `shake-wire-tool-${messageIndex}`;
		const toolName =
			firstString(message, ["tool_name", "toolName", "name", "tool"]) ?? toolNameById.get(toolCallId) ?? "tool";
		const canonical: ToolResultMessage = {
			role: "toolResult",
			toolCallId,
			toolName,
			content,
			isError: message.is_error === true || message.isError === true,
			timestamp: 0,
		};
		if (message.details !== undefined) canonical.details = message.details;
		const prunedAt = canonicalPrunedAt(message);
		if (prunedAt !== undefined) canonical.prunedAt = prunedAt;
		if (message.useless === true) canonical.useless = true;
		const entry: SessionMessageEntry = { type: "message", ...base, message: canonical };
		return {
			messageIndex,
			wireMessage: message,
			entry,
			stringContent,
			assistantStringContent: false,
			toolResult: true,
			camelPrunedStyle: Object.hasOwn(message, "prunedAt") && !Object.hasOwn(message, "pruned_at"),
		};
	}

	if (message.role === "assistant") {
		const content = stringContent
			? [{ type: "text", text: message.content as string }, ...assistantToolCalls]
			: [...message.content, ...assistantToolCalls];
		const canonical = { role: "assistant", content, timestamp: 0 } as AgentMessage;
		const entry: SessionMessageEntry = { type: "message", ...base, message: canonical };
		return {
			messageIndex,
			wireMessage: message,
			entry,
			stringContent,
			assistantStringContent: stringContent,
			toolResult: false,
			camelPrunedStyle: false,
		};
	}

	if (message.role === "user" || message.role === "developer") {
		const canonical = { role: message.role, content: message.content, timestamp: 0 } as AgentMessage;
		const entry: SessionMessageEntry = { type: "message", ...base, message: canonical };
		return {
			messageIndex,
			wireMessage: message,
			entry,
			stringContent,
			assistantStringContent: false,
			toolResult: false,
			camelPrunedStyle: false,
		};
	}

	const entry: CustomMessageEntry = {
		type: "custom_message",
		...base,
		customType: message.role,
		content: message.content as CustomMessageEntry["content"],
		display: true,
	};
	return {
		messageIndex,
		wireMessage: message,
		entry,
		stringContent,
		assistantStringContent: false,
		toolResult: false,
		camelPrunedStyle: false,
	};
}

function configFor(request: ShakeWireRequest): ShakeConfig {
	const config: ShakeConfig = request.aggressive
		? { ...AGGRESSIVE_SHAKE_CONFIG, protectTokens: 0, minSavings: 0 }
		: { ...DEFAULT_SHAKE_CONFIG };
	const boundary = request.keepBoundaryIndex;
	if (boundary !== undefined && Number.isFinite(boundary) && boundary >= 0) {
		config.keepBoundaryId = `shake-wire-${Math.trunc(boundary)}`;
	}
	return config;
}

function toWireRegion(region: ShakeRegion, binding: EntryBinding, regionIndex: number): ShakeWireRegion {
	const result: ShakeWireRegion = {
		kind: region.kind,
		message_index: binding.messageIndex,
		tokens: region.tokens,
		original_text: region.originalText,
		label: region.label,
		region_index: regionIndex,
		placeholder: `[shaken ~${region.tokens} tokens]`,
	};
	if (region.kind === "block") {
		result.block_index = binding.assistantStringContent ? -1 : region.blockIndex;
		result.start = region.start;
		result.end = region.end;
	}
	return result;
}

function syncStringContent(binding: EntryBinding): void {
	if (!binding.stringContent || binding.toolResult) return;
	if (binding.entry.type === "custom_message") {
		binding.wireMessage.content = binding.entry.content as string;
		return;
	}
	const message = binding.entry.message;
	if (binding.assistantStringContent && message.role === "assistant") {
		const first = message.content[0];
		if (first?.type === "text") binding.wireMessage.content = first.text;
		return;
	}
	if ((message.role === "user" || message.role === "developer") && typeof message.content === "string") {
		binding.wireMessage.content = message.content;
	}
}

/** Shake JSON/OpenAI-style messages through OMP's canonical compaction algorithm. */
export function shakeWireMessages(request: ShakeWireRequest): ShakeWireResult {
	const messages = structuredClone(request.messages);
	const config = configFor(request);
	const toolNameById = new Map<string, string>();
	const toolCallsByMessage = new Map<ShakeWireMessage, CanonicalWireToolCall[]>();
	for (const message of messages) {
		if (message.role !== "assistant") continue;
		const calls = canonicalToolCalls(message);
		toolCallsByMessage.set(message, calls);
		for (const call of calls) toolNameById.set(call.id, call.name);
	}
	const bindings = messages.map((message, index) =>
		makeEntry(message, index, toolNameById, toolCallsByMessage.get(message) ?? NO_TOOL_CALLS),
	);
	const entries: SessionEntry[] = bindings.map(binding => binding.entry);

	const boundary = request.keepBoundaryIndex;
	if (boundary !== undefined && Number.isFinite(boundary) && boundary >= messages.length) {
		entries.push({
			type: "custom",
			id: config.keepBoundaryId as string,
			parentId: entries.length === 0 ? null : entries[entries.length - 1].id,
			timestamp: SYNTHETIC_TIMESTAMP,
			customType: "shake-wire-boundary",
			data: {},
		});
	}

	const regions = collectShakeRegions(entries, new Tokenizer(null), config);
	if (messages.length === 0) {
		return { messages, savedTokens: 0, regions: [], placeholders: 0, notice: "nothing to shake" };
	}
	if (regions.length === 0) {
		return {
			messages,
			savedTokens: 0,
			regions: [],
			placeholders: 0,
			notice: request.aggressive
				? "nothing to shake"
				: `minSavings not met or nothing eligible (threshold ${config.minSavings})`,
		};
	}

	const byEntry = new Map<SessionEntry, EntryBinding>(bindings.map(binding => [binding.entry, binding]));
	const wireRegions = regions.map((region, index) => {
		const binding = byEntry.get(region.entry);
		if (!binding) throw new Error("shake region does not map to an input message");
		return toWireRegion(region, binding, index + 1);
	});

	applyShakeRegions(regions.map((region, index) => ({ region, replacement: wireRegions[index].placeholder })));

	for (const binding of bindings) syncStringContent(binding);
	for (let index = 0; index < regions.length; index++) {
		const region = regions[index];
		if (region.kind !== "toolResult") continue;
		const binding = byEntry.get(region.entry);
		if (!binding) continue;
		binding.wireMessage.content = wireRegions[index].placeholder;
		binding.wireMessage[binding.camelPrunedStyle ? "prunedAt" : "pruned_at"] = true;
	}

	let savedTokens = 0;
	for (const region of regions) savedTokens += Math.max(0, region.tokens - PLACEHOLDER_TOKEN_ESTIMATE);
	return {
		messages,
		savedTokens,
		regions: wireRegions,
		placeholders: wireRegions.length,
		notice: `shook ${wireRegions.length} region(s), ~${savedTokens} tokens`,
	};
}
