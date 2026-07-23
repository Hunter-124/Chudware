/**
 * Conversation compaction, backed by OMP's snapcompact `compact()`
 * (vendor/omp/packages/snapcompact/src/snapcompact.ts:1849-1976) reused verbatim for the actual
 * archive/frame-planning algorithm — no local re-implementation of foveation, shape selection,
 * or PNG rendering.
 *
 * `compact()` trusts its caller to have already decided the keep/discard boundary
 * (`firstKeptEntryId`; throws without one, snapcompact.ts:1854-1856 — see
 * docs/omp-integration/research/omp-snapcompact-deep.md §1.1) instead of deriving it the way
 * CAD's own `services/agent/compaction.py:find_cut_index` does. CAD's frozen bridge contract
 * hands this a flat `messages` list + a `keepRecentTokens` budget, so splitting that list into
 * a kept tail and an archived head (`planCut` below) is bridge-owned glue, not a duplicate of
 * either OMP's or CAD-python's compaction logic — it is the seam between the two.
 *
 * Safety additions over bare snapcompact, per the dossier's §4 revamp opportunities:
 *  1. sha256 `checksum` over the returned archive (opportunity #2/#5: corruption detection
 *     across the bun-sidecar IPC boundary) plus a per-frame `sha256` on every returned frame.
 *     A caller-supplied `priorArchive` whose embedded checksum no longer matches its content is
 *     treated as corrupt and discarded rather than silently replayed into the next compaction
 *     (opportunity #5).
 *  2. A `recoverable` side channel: whenever OMP's own foveation drops characters
 *     (`archive.truncatedChars > 0` — planArchive, snapcompact.ts:1819-1826/1895; risk #6/#7,
 *     the dossier's single biggest unbounded-loss path) the exact raw input messages that were
 *     archived are handed back untouched, so nothing the caller sent is ever unrecoverable even
 *     though OMP's own foveated archive text is (opportunity #1).
 *  3. `mode:"text"`, or any `compact()` throw (no native `pi-natives` binary for this platform,
 *     or any other renderer failure), skips PNG rendering and returns a text-only summary built
 *     from OMP's own `serializeConversation` (snapcompact.ts:770-899), reused verbatim, so the
 *     bridge never crashes and always degrades to a working text archive (opportunity #4).
 *
 * Implements: compact. Wired into bridge.ts dispatch().
 */
import { createHash } from "node:crypto";
import type { Message } from "@oh-my-pi/pi-ai";
import { estimateTokens } from "@oh-my-pi/pi-mnemopi/core/token-counter";
import {
	compact as snapcompactCompact,
	createFileOps,
	serializeConversation,
	PRESERVE_KEY,
	type Archive,
	type CompactionPreparation,
	type Frame,
} from "@oh-my-pi/snapcompact";
import { asObject, num, optStr, str, type Json } from "./json";

function log(...args: unknown[]): void {
	process.stderr.write(args.map(String).join(" ") + "\n");
}

const DEFAULT_KEEP_RECENT_TOKENS = 4000;

// ============================================================================
// Wire message shape (structural subset of @oh-my-pi/pi-ai's real `Message`,
// see packages/ai/src/types.ts:660-776 — only the fields serializeConversation
// reads: role, content parts, toolCallId/toolName/isError/useless).
// ============================================================================

interface CompactTextPart {
	type: "text";
	text: string;
}
interface CompactThinkingPart {
	type: "thinking";
	thinking: string;
}
interface CompactToolCallPart {
	type: "toolCall";
	id: string;
	name: string;
	arguments: Json;
	intent?: string;
}
type CompactAssistantPart = CompactTextPart | CompactThinkingPart | CompactToolCallPart;

interface CompactUserMessage {
	id?: string;
	role: "user" | "developer";
	content: string | CompactTextPart[];
}
interface CompactAssistantMessage {
	id?: string;
	role: "assistant";
	content: CompactAssistantPart[];
}
interface CompactToolResultMessage {
	id?: string;
	role: "toolResult";
	toolCallId: string;
	toolName?: string;
	content: string | CompactTextPart[];
	isError?: boolean;
	useless?: boolean;
}
type CompactMessage = CompactUserMessage | CompactAssistantMessage | CompactToolResultMessage;

function readTextParts(v: unknown): CompactTextPart[] {
	if (typeof v === "string") return v.length > 0 ? [{ type: "text", text: v }] : [];
	if (!Array.isArray(v)) return [];
	const parts: CompactTextPart[] = [];
	for (const item of v) {
		const o = asObject(item);
		if (typeof o.text === "string") parts.push({ type: "text", text: o.text });
	}
	return parts;
}

function readAssistantParts(v: unknown): CompactAssistantPart[] {
	if (!Array.isArray(v)) return [];
	const parts: CompactAssistantPart[] = [];
	for (const item of v) {
		const o = asObject(item);
		if (o.type === "text" && typeof o.text === "string") {
			parts.push({ type: "text", text: o.text });
		} else if (o.type === "thinking" && typeof o.thinking === "string") {
			parts.push({ type: "thinking", thinking: o.thinking });
		} else if (o.type === "toolCall" && typeof o.id === "string" && typeof o.name === "string") {
			parts.push({ type: "toolCall", id: o.id, name: o.name, arguments: asObject(o.arguments), intent: optStr(o.intent) });
		}
	}
	return parts;
}

function readCompactMessage(v: unknown): CompactMessage | undefined {
	const o = asObject(v);
	const id = optStr(o.id);
	if (o.role === "toolResult") {
		if (typeof o.toolCallId !== "string") return undefined;
		return {
			id,
			role: "toolResult",
			toolCallId: o.toolCallId,
			toolName: optStr(o.toolName),
			content: readTextParts(o.content),
			isError: o.isError === true,
			useless: o.useless === true,
		};
	}
	if (o.role === "assistant") {
		return { id, role: "assistant", content: readAssistantParts(o.content) };
	}
	// Everything else (including a missing/unknown role) archives as a plain user turn —
	// serializeConversation only special-cases "assistant"/"toolResult" (snapcompact.ts:817-895).
	return { id, role: o.role === "developer" ? "developer" : "user", content: readTextParts(o.content) };
}

function readCompactMessages(v: unknown): CompactMessage[] {
	if (!Array.isArray(v)) return [];
	const messages: CompactMessage[] = [];
	for (const item of v) {
		const message = readCompactMessage(item);
		if (message) messages.push(message);
	}
	return messages;
}

/** Boundary cast into OMP's `Message`: serializeConversation only reads role/content/
 *  toolCallId/toolName/isError/useless (checked field-by-field above in readCompactMessage);
 *  the provider-billing-only fields real `Message` variants also carry (usage/api/model/
 *  timestamp/…) are never touched by this call path, so reconstructing them here would add no
 *  runtime safety — same pattern as bridge.ts's own `convertMessages`. */
function toOmpMessage(m: CompactMessage): Message {
	return m as unknown as Message;
}

function messageText(m: CompactMessage): string {
	if (m.role === "toolResult") return typeof m.content === "string" ? m.content : m.content.map(p => p.text).join("");
	if (m.role === "assistant") {
		return m.content
			.map(p => (p.type === "text" ? p.text : p.type === "thinking" ? p.thinking : JSON.stringify(p.arguments)))
			.join("");
	}
	return typeof m.content === "string" ? m.content : m.content.map(p => p.text).join("");
}

// ============================================================================
// Cut planning: split a flat transcript into a kept tail and an archived head.
// ============================================================================

interface CutPlan {
	keptMessages: CompactMessage[];
	archivedMessages: CompactMessage[];
	firstKeptEntryId: string;
}

/** Walk newest -> oldest accumulating estimated tokens until `keepRecentTokens` is exceeded,
 *  then snap the boundary forward past any leading `toolResult` so the kept window never opens
 *  on an orphaned tool result whose originating call has already been archived. */
function planCut(messages: CompactMessage[], keepRecentTokens: number): CutPlan {
	if (messages.length === 0) return { keptMessages: [], archivedMessages: [], firstKeptEntryId: "" };
	let tokens = 0;
	let cut = 0;
	for (let i = messages.length - 1; i >= 0; i--) {
		tokens += estimateTokens(messageText(messages[i]));
		if (tokens > keepRecentTokens) {
			cut = i + 1;
			break;
		}
	}
	while (cut < messages.length && messages[cut].role === "toolResult") cut++;
	const archivedMessages = messages.slice(0, cut);
	const keptMessages = messages.slice(cut);
	const boundaryIndex = keptMessages.length > 0 ? cut : messages.length - 1;
	const boundaryMessage = keptMessages.length > 0 ? keptMessages[0] : messages[messages.length - 1];
	return { keptMessages, archivedMessages, firstKeptEntryId: boundaryMessage.id ?? `m${boundaryIndex}` };
}

// ============================================================================
// Checksums + archive (de)serialization
// ============================================================================

function sha256(data: string): string {
	return createHash("sha256").update(data).digest("hex");
}

/** Stable JSON of the fields OMP's own `Archive` actually carries (Archive, snapcompact.ts:531-548)
 *  — excludes the per-frame `sha256` this module injects, so the hash is comparable across a
 *  round trip through the RPC wire regardless of which side computed it last. */
function coreArchiveJson(archive: Archive): string {
	return JSON.stringify({
		frames: archive.frames.map(f => f.data),
		totalChars: archive.totalChars,
		truncatedChars: archive.truncatedChars,
		text: archive.text ?? null,
		textHead: archive.textHead ?? null,
		textTail: archive.textTail ?? null,
	});
}

function readFrame(v: unknown): Frame | undefined {
	const o = asObject(v);
	if (typeof o.data !== "string" || typeof o.mimeType !== "string") return undefined;
	if (typeof o.cols !== "number" || typeof o.rows !== "number" || typeof o.chars !== "number") return undefined;
	return { data: o.data, mimeType: o.mimeType, cols: o.cols, rows: o.rows, chars: o.chars };
}

function readArchive(v: unknown): { archive: Archive; checksum?: string } | undefined {
	if (v === undefined || v === null) return undefined;
	const o = asObject(v);
	if (!Array.isArray(o.frames)) return undefined;
	const frames: Frame[] = [];
	for (const f of o.frames) {
		const frame = readFrame(f);
		if (frame) frames.push(frame);
	}
	const archive: Archive = {
		frames,
		totalChars: typeof o.totalChars === "number" ? o.totalChars : 0,
		truncatedChars: typeof o.truncatedChars === "number" ? o.truncatedChars : 0,
		...(typeof o.text === "string" ? { text: o.text } : {}),
		...(typeof o.textHead === "string" ? { textHead: o.textHead } : {}),
		...(typeof o.textTail === "string" ? { textTail: o.textTail } : {}),
	};
	return { archive, checksum: optStr(o.checksum) };
}

/** Validate a caller-echoed `priorArchive` against its own embedded checksum (dossier
 *  opportunity #5 — detect a truncated/corrupt bun-sidecar IPC round trip instead of silently
 *  replaying it into the next compaction pass). A missing checksum is trusted as-is (first use
 *  of a fresh archive); a mismatching one is dropped with a stderr warning, never thrown. */
function verifyPriorArchive(params: Json): { archive?: Archive; corrupt: boolean } {
	const read = readArchive(params.priorArchive);
	if (!read) return { corrupt: false };
	if (read.checksum === undefined) return { archive: read.archive, corrupt: false };
	const actual = sha256(coreArchiveJson(read.archive));
	if (actual !== read.checksum) {
		log(`compact: priorArchive checksum mismatch (expected ${read.checksum}, got ${actual}) — discarding corrupt archive`);
		return { corrupt: true };
	}
	return { archive: read.archive, corrupt: false };
}

// ============================================================================
// Result builders
// ============================================================================

function textFallbackResult(
	keptMessages: CompactMessage[],
	archivedMessages: CompactMessage[],
	reason: string,
	corrupt: boolean,
): Json {
	const serialized = serializeConversation(archivedMessages.map(toOmpMessage));
	const summary = serialized.length > 0 ? `[text-fallback: ${reason}]\n${serialized}` : "No prior history.";
	return {
		keptMessages,
		summary,
		shortSummary: `Archived ${archivedMessages.length} message(s) as text (${reason}).`,
		checksum: sha256(summary),
		...(corrupt ? { warning: "priorArchive checksum mismatch; discarded" } : {}),
	};
}

async function runSnapcompact(
	keptMessages: CompactMessage[],
	archivedMessages: CompactMessage[],
	firstKeptEntryId: string,
	priorArchive: Archive | undefined,
	corrupt: boolean,
): Promise<Json> {
	const preparation: CompactionPreparation = {
		firstKeptEntryId,
		messagesToSummarize: archivedMessages.map(toOmpMessage),
		turnPrefixMessages: [],
		tokensBefore: estimateTokens(archivedMessages.map(messageText).join("\n")),
		previousPreserveData: priorArchive ? { [PRESERVE_KEY]: priorArchive } : undefined,
		fileOps: createFileOps(),
	};
	const result = await snapcompactCompact(preparation);
	const preserveData = result.preserveData ?? {};
	const read = readArchive(preserveData[PRESERVE_KEY]);
	const archive = read?.archive;

	const checksum = archive ? sha256(coreArchiveJson(archive)) : sha256(result.summary);
	const frames = archive?.frames.map(f => ({ ...f, sha256: sha256(f.data) }));
	const archiveOut = archive ? { ...archive, frames: frames ?? [], checksum } : undefined;

	// Opportunity #1: OMP's own foveation permanently drops characters past its frame budget
	// once truncatedChars > 0 (planArchive, snapcompact.ts:1819-1826) with no recoverable
	// payload of its own. Since the bridge still holds the untouched raw input messages that
	// were archived, hand them back verbatim as a recoverable side channel instead.
	const recoverable =
		archive && archive.truncatedChars > 0
			? {
					reason: `snapcompact foveation dropped ${archive.truncatedChars} archived character(s) past its frame budget`,
					messages: archivedMessages,
				}
			: undefined;

	return {
		keptMessages,
		summary: result.summary,
		shortSummary: result.shortSummary ?? result.summary,
		checksum,
		...(archiveOut ? { archive: archiveOut } : {}),
		...(frames ? { frames } : {}),
		...(recoverable ? { recoverable } : {}),
		...(corrupt ? { warning: "priorArchive checksum mismatch; discarded" } : {}),
	};
}

/** `compact {messages,keepRecentTokens?,priorArchive?,mode?}` ->
 *  `{keptMessages,summary,shortSummary,archive?,checksum,frames?}`. `mode:"text"` forces the
 *  text-only path below without ever invoking the native PNG renderer. */
export async function handleCompact(params: Json): Promise<Json> {
	const messages = readCompactMessages(params.messages);
	const keepRecentTokens = num(params.keepRecentTokens) ?? DEFAULT_KEEP_RECENT_TOKENS;
	const mode = str(params.mode, "auto");
	const { archive: priorArchive, corrupt } = verifyPriorArchive(params);
	const { keptMessages, archivedMessages, firstKeptEntryId } = planCut(messages, keepRecentTokens);

	if (archivedMessages.length === 0) {
		return {
			keptMessages,
			summary: "",
			shortSummary: "Nothing to archive.",
			checksum: sha256(""),
			...(corrupt ? { warning: "priorArchive checksum mismatch; discarded" } : {}),
		};
	}

	if (mode === "text") {
		return textFallbackResult(keptMessages, archivedMessages, "mode=text requested", corrupt);
	}

	try {
		return await runSnapcompact(keptMessages, archivedMessages, firstKeptEntryId, priorArchive, corrupt);
	} catch (err) {
		log(`compact: snapcompact failed (${String(err)}) — falling back to text-only summary`);
		return textFallbackResult(keptMessages, archivedMessages, `snapcompact error: ${String(err)}`, corrupt);
	}
}
