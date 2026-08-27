import {
	shakeWireMessages,
	type ShakeWireJson,
	type ShakeWireJsonObject,
	type ShakeWireMessage,
} from "@oh-my-pi/pi-agent-core/compaction";
import { asObject, num, type Json } from "./json";

/**
 * Narrow one JSON.parse-produced value into the platform's explicit JSON union.
 *
 * The bridge reads `unknown` at its RPC boundary. `shakeWireMessages` deliberately
 * accepts JSON only, so rejecting non-JSON values here keeps that platform contract
 * honest and avoids an `as any`/double-cast escape hatch.
 */
function readJson(value: unknown, path: string): ShakeWireJson {
	if (value === null || typeof value === "string" || typeof value === "boolean") return value;
	if (typeof value === "number") {
		if (Number.isFinite(value)) return value;
		throw new Error(`${path} must not contain a non-finite number`);
	}
	if (Array.isArray(value)) return value.map((item, index) => readJson(item, `${path}[${index}]`));
	if (typeof value === "object") {
		const result: ShakeWireJsonObject = {};
		for (const [key, item] of Object.entries(value)) result[key] = readJson(item, `${path}.${key}`);
		return result;
	}
	throw new Error(`${path} must contain JSON values only`);
}

function readMessage(value: unknown, index: number): ShakeWireMessage {
	const raw = asObject(value);
	const json = readJson(raw, `shake.messages[${index}]`);
	if (json === null || Array.isArray(json) || typeof json !== "object") {
		throw new Error(`shake.messages[${index}] must be an object`);
	}
	const role = json.role;
	if (typeof role !== "string" || role.trim().length === 0) {
		throw new Error(`shake.messages[${index}].role must be a non-empty string`);
	}
	const content = json.content;
	if (typeof content !== "string" && !Array.isArray(content)) {
		throw new Error(`shake.messages[${index}].content must be a string or array`);
	}
	for (let blockIndex = 0; Array.isArray(content) && blockIndex < content.length; blockIndex++) {
		const block = content[blockIndex];
		if (block === null || Array.isArray(block) || typeof block !== "object") {
			throw new Error(`shake.messages[${index}].content[${blockIndex}] must be an object`);
		}
	}
	return { ...json, role, content };
}

/** `shake{messages,aggressive?,keepBoundaryIndex?}` -> CAD's stable snake-case result. */
export function handleShake(params: Json): Json {
	if (!Array.isArray(params.messages)) throw new Error("shake requires messages[]");
	const messages = params.messages.map(readMessage);
	const keepBoundaryIndex = num(params.keepBoundaryIndex ?? params.keep_boundary_index);
	const result = shakeWireMessages({
		messages,
		aggressive: params.aggressive === true,
		...(keepBoundaryIndex === undefined ? {} : { keepBoundaryIndex }),
	});
	return {
		messages: result.messages,
		saved_tokens: result.savedTokens,
		regions: result.regions,
		placeholders: result.placeholders,
		notice: result.notice,
	};
}
