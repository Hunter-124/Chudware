/**
 * On-device STT model tiers — same registry keys / HF repos as OMP coding-agent
 * (`packages/coding-agent/src/stt/models.ts`). The bridge defaults to `fast`
 * (Whisper base ~60 MB) so first-use in CAD is light; override with
 * `CHUDWARE_STT_MODEL` or `stt_start.model`.
 */

export type SttEngine = "transformers";

export interface SttModelSpec {
	key: string;
	engine: SttEngine;
	repo: string;
	dtype: "q8" | "q4" | "fp16" | "fp32";
	englishOnly: boolean;
	label: string;
	description: string;
	sizeHint: string;
}

export const STT_MODELS = [
	{
		key: "fast",
		engine: "transformers",
		repo: "onnx-community/whisper-base",
		dtype: "q8",
		englishOnly: false,
		label: "Fast (Whisper base)",
		description: "Whisper base, multilingual. Smallest + fastest; lowest accuracy.",
		sizeHint: "~60 MB",
	},
	{
		key: "balanced",
		engine: "transformers",
		repo: "onnx-community/whisper-small",
		dtype: "q8",
		englishOnly: false,
		label: "Balanced (Whisper small)",
		description: "Whisper small, multilingual. More accurate than Fast.",
		sizeHint: "~190 MB",
	},
	{
		key: "turbo",
		engine: "transformers",
		repo: "onnx-community/whisper-large-v3-turbo",
		dtype: "q4",
		englishOnly: false,
		label: "Turbo (Whisper large-v3)",
		description: "Whisper large-v3-turbo. Widest language coverage; large download.",
		sizeHint: "~600 MB",
	},
] as const satisfies readonly SttModelSpec[];

/** CAD default — light first download. OMP coding-agent defaults to parakeet; we stay on Whisper here until sherpa is wired into this sidecar. */
export const DEFAULT_STT_MODEL_KEY = "fast";

export type SttModelKey = (typeof STT_MODELS)[number]["key"];
export type SttModel = (typeof STT_MODELS)[number];

export function getSttModelSpec(key: string): SttModel | undefined {
	return STT_MODELS.find(model => model.key === key);
}

export function resolveSttModelSpec(key: string | undefined): SttModel {
	const fromEnv = typeof process !== "undefined" ? process.env.CHUDWARE_STT_MODEL : undefined;
	const want = key || fromEnv || DEFAULT_STT_MODEL_KEY;
	return (want ? getSttModelSpec(want) : undefined) ?? getSttModelSpec(DEFAULT_STT_MODEL_KEY)!;
}
