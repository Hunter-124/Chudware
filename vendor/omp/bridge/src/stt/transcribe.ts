/**
 * Lazy transformers.js ASR pipeline — same engine OMP uses for Whisper tiers
 * (`coding-agent/src/stt/asr-worker.ts`), adapted for the CAD bun sidecar
 * (in-process; no coding-agent worker subprocess).
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { getTinyModelsCacheDir } from "@oh-my-pi/pi-utils";
import { resolveSttModelSpec, type SttModel } from "./models";

const ASR_TASK = "automatic-speech-recognition";
const CHUNK_LENGTH_S = 30;
const STRIDE_LENGTH_S = 5;
const ASR_SAMPLE_RATE = 16_000;

type AsrPipeline = (
	audio: { raw: Float32Array; sampling_rate: number },
	options?: Record<string, unknown>,
) => Promise<{ text?: string } | string>;

interface TransformersMod {
	pipeline: (
		task: string,
		model: string,
		options?: Record<string, unknown>,
	) => Promise<AsrPipeline>;
	env: {
		cacheDir?: string;
		allowLocalModels?: boolean;
		backends?: { onnx?: { wasm?: { numThreads?: number } } };
	};
}

let transformersMod: TransformersMod | null = null;
let transformersErr: string | null = null;
const pipelines = new Map<string, Promise<AsrPipeline>>();

export async function probeTransformers(): Promise<{ ok: boolean; reason?: string }> {
	if (transformersMod) return { ok: true };
	if (transformersErr) return { ok: false, reason: transformersErr };
	try {
		const mod = (await import("@huggingface/transformers")) as unknown as TransformersMod;
		mod.env.allowLocalModels = true;
		mod.env.cacheDir = getTinyModelsCacheDir();
		transformersMod = mod;
		return { ok: true };
	} catch (err) {
		transformersErr =
			err instanceof Error
				? err.message
				: "Failed to load @huggingface/transformers (ASR runtime missing)";
		return { ok: false, reason: transformersErr };
	}
}

export async function isSttModelCached(key: string): Promise<boolean> {
	const spec = resolveSttModelSpec(key);
	const repoDir = path.join(getTinyModelsCacheDir(), spec.repo);
	try {
		const root = await fs.readdir(repoDir);
		if (!root.includes("config.json")) return false;
		const onnxFiles = await fs.readdir(path.join(repoDir, "onnx")).catch(() => [] as string[]);
		const hasEncoder = onnxFiles.some(f => f.startsWith("encoder") && f.endsWith(".onnx"));
		const hasDecoder = onnxFiles.some(f => f.startsWith("decoder") && f.endsWith(".onnx"));
		return hasEncoder && hasDecoder;
	} catch {
		return false;
	}
}

async function getPipeline(spec: SttModel): Promise<AsrPipeline> {
	const probe = await probeTransformers();
	if (!probe.ok || !transformersMod) {
		throw new Error(probe.reason ?? "ASR runtime unavailable");
	}
	let pending = pipelines.get(spec.key);
	if (!pending) {
		const mod = transformersMod;
		pending = mod.pipeline(ASR_TASK, spec.repo, {
			dtype: spec.dtype,
			device: "cpu",
		});
		pipelines.set(spec.key, pending);
	}
	return pending;
}

/** Warm / download the model so stop-time decode is hot. */
export async function ensureSttModel(key?: string): Promise<SttModel> {
	const spec = resolveSttModelSpec(key);
	await getPipeline(spec);
	return spec;
}

export async function transcribeAudio(
	audio: Float32Array,
	options?: { modelKey?: string; language?: string; signal?: AbortSignal },
): Promise<string> {
	if (options?.signal?.aborted) throw new Error("transcription cancelled");
	if (!audio.length) return "";
	const spec = resolveSttModelSpec(options?.modelKey);
	const pipe = await getPipeline(spec);
	if (options?.signal?.aborted) throw new Error("transcription cancelled");
	const callOpts: Record<string, unknown> = {
		chunk_length_s: CHUNK_LENGTH_S,
		stride_length_s: STRIDE_LENGTH_S,
		return_timestamps: false,
	};
	if (options?.language && !spec.englishOnly) {
		callOpts.language = options.language;
		callOpts.task = "transcribe";
	}
	const result = await pipe(
		{ raw: audio, sampling_rate: ASR_SAMPLE_RATE },
		callOpts,
	);
	if (options?.signal?.aborted) throw new Error("transcription cancelled");
	const text = typeof result === "string" ? result : (result?.text ?? "");
	return text.replace(/\s+/g, " ").trim();
}
