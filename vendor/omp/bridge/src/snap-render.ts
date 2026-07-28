/**
 * Foveated transcript rendering, backed by OMP's snapcompact `compact()`
 * (vendor/omp/packages/snapcompact/src/snapcompact.ts:1880) reused verbatim — the render-only
 * counterpart to this bridge's `compact` RPC (compact.ts): the caller owns the cut AND the
 * serialization (it hands us an already-¶-section-formatted transcript chunk in `text`), while
 * this RPC owns the foveated PNG rendering plus the OG-source unfold/re-render round trip.
 *
 * The unfold mechanism is exactly snapcompact's own re-snap path (snapcompact.ts:1925-1930):
 * `previousPreserveData[PRESERVE_KEY].text` — the full prior kept source, echoed back from the
 * previous `snap_render` result's `text` field — ages in ahead of the newly serialized chunk,
 * then the whole thing is re-rendered into a fresh frame set. That makes "re-render the same
 * source for a different provider/shape" (e.g. after a provider switch) just a call with
 * `priorText` set and `text` empty.
 *
 * Wire validation, the sha256 helpers, the archive read-back shape, and the synthetic-message
 * structural subset all mirror compact.ts's conventions (serializeConversation only reads
 * role/content from a user message — snapcompact.ts:817-895), duplicated locally rather than
 * imported so compact.ts stays untouched for its existing consumers.
 *
 * Implements: snap_render{text,priorText?,modelId?,api?,maxFrames?,includeThinking?} ->
 * {frames,text,textHead,textTail,totalChars,truncatedChars,checksum,summary,shortSummary}.
 * Wired into bridge.ts dispatch().
 */
import { createHash } from "node:crypto";
import { estimateTokens } from "@oh-my-pi/pi-mnemopi/core/token-counter";
import {
	compact as snapcompactCompact,
	createFileOps,
	PRESERVE_KEY,
	type Archive,
	type CompactionPreparation,
	type Frame,
} from "@oh-my-pi/snapcompact";
import { asObject, num, str, type Json } from "./json";

const DEFAULT_API = "anthropic";

function sha256(data: string): string {
	return createHash("sha256").update(data).digest("hex");
}

/** Stable JSON of the fields OMP's own `Archive` carries (Archive, snapcompact.ts:531-548) —
 *  same core-archive approach as compact.ts: frames' data + totalChars + truncatedChars +
 *  text/textHead/textTail, so the checksum is comparable across an IPC round trip. */
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
	const frame: Frame = { data: o.data, mimeType: o.mimeType, cols: o.cols, rows: o.rows, chars: o.chars };
	if (o.variant !== undefined) frame.variant = o.variant as Frame["variant"];
	if (o.font !== undefined) frame.font = o.font as Frame["font"];
	if (o.detail !== undefined) frame.detail = o.detail as Frame["detail"];
	if (typeof o.lineRepeat === "number") frame.lineRepeat = o.lineRepeat;
	if (typeof o.columns === "number") frame.columns = o.columns;
	if (o.stopwordDim === true) frame.stopwordDim = true;
	return frame;
}

/** Read the archive OMP persisted on `result.preserveData[PRESERVE_KEY]` — same readArchive-style
 *  fields as compact.ts (frames, totalChars, truncatedChars, text, textHead, textTail). */
function readArchive(v: unknown): Archive | undefined {
	if (v === undefined || v === null) return undefined;
	const o = asObject(v);
	if (!Array.isArray(o.frames)) return undefined;
	const frames: Frame[] = [];
	for (const f of o.frames) {
		const frame = readFrame(f);
		if (frame) frames.push(frame);
	}
	return {
		frames,
		totalChars: typeof o.totalChars === "number" ? o.totalChars : 0,
		truncatedChars: typeof o.truncatedChars === "number" ? o.truncatedChars : 0,
		...(typeof o.text === "string" ? { text: o.text } : {}),
		...(typeof o.textHead === "string" ? { textHead: o.textHead } : {}),
		...(typeof o.textTail === "string" ? { textTail: o.textTail } : {}),
	};
}

/** `snap_render {text,priorText?,modelId?,api?,maxFrames?,includeThinking?}` ->
 *  `{frames,text,textHead,textTail,totalChars,truncatedChars,checksum,summary,shortSummary}`.
 *  `text` is the NEW pre-serialized (¶-section) transcript chunk — may be "" when the caller
 *  only wants `priorText` re-rendered; `priorText` is a previous result's `text` echoed back
 *  for the unfold-and-re-render round trip. */
export async function handleSnapRender(params: Json): Promise<Json> {
	const text = str(params.text);
	const priorText = str(params.priorText);
	const api = str(params.api, DEFAULT_API);
	const modelId = str(params.modelId);
	const maxFrames = num(params.maxFrames);
	const includeThinking = typeof params.includeThinking === "boolean" ? params.includeThinking : undefined;

	// One synthetic user turn carries the caller-serialized chunk — the structural subset
	// serializeConversation reads (role + content parts), same shape compact.ts builds.
	const messagesToSummarize =
		text.length > 0
			? [{ role: "user" as const, content: [{ type: "text" as const, text }] }]
			: [];

	const preparation: CompactionPreparation = {
		// Non-empty is mandatory: compact() throws without one (snapcompact.ts:1885-1887).
		firstKeptEntryId: "snap",
		messagesToSummarize: messagesToSummarize as CompactionPreparation["messagesToSummarize"],
		turnPrefixMessages: [],
		tokensBefore: estimateTokens(text),
		previousPreserveData:
			priorText.length > 0
				? { [PRESERVE_KEY]: { frames: [], totalChars: priorText.length, truncatedChars: 0, text: priorText } }
				: undefined,
		fileOps: createFileOps(),
	};

	const result = await snapcompactCompact(preparation, {
		model: { api, id: modelId },
		...(maxFrames !== undefined ? { maxFrames } : {}),
		...(includeThinking !== undefined ? { includeThinking } : {}),
	});

	const archive = readArchive(result.preserveData?.[PRESERVE_KEY]);
	const frames = (archive?.frames ?? []).map(f => ({ ...f, sha256: sha256(f.data) }));

	return {
		frames,
		// archive.text is the FULL OG kept source — echoed verbatim back as the next call's
		// priorText to keep the unfold/re-render chain going.
		text: archive?.text ?? "",
		textHead: archive?.textHead ?? "",
		textTail: archive?.textTail ?? "",
		totalChars: archive?.totalChars ?? 0,
		truncatedChars: archive?.truncatedChars ?? 0,
		checksum: sha256(archive ? coreArchiveJson(archive) : coreArchiveJson({ frames: [], totalChars: 0, truncatedChars: 0 })),
		summary: result.summary,
		shortSummary: result.shortSummary ?? "",
	};
}
