/**
 * Live STT session for the CAD OMP bridge — mirrors OMP's STTController
 * (`coding-agent/src/stt/stt-controller.ts`): pi-natives AudioCapture @ 16 kHz
 * + energy endpointer + Whisper decode. Exposed over JSON-RPC so the host
 * composer can drive mic UX without embedding a second ASR stack.
 */
import { StreamEndpointer } from "./endpointer";
import { resolveSttModelSpec, type SttModel } from "./models";
import {
	ensureSttModel,
	isSttModelCached,
	probeTransformers,
	transcribeAudio,
} from "./transcribe";

export type SttState = "idle" | "listening" | "processing" | "error";

export interface SttStatus {
	available: boolean;
	reason?: string;
	state: SttState;
	model?: string;
	modelLabel?: string;
	modelSizeHint?: string;
	modelCached?: boolean;
	partial?: string;
	committed?: string;
	error?: string;
	level?: number;
}

type CaptureHandle = { stop(): void };
type CaptureFactory = (onAudio: (error: Error | null, samples: Float32Array) => void) => CaptureHandle;

let AudioCaptureCtor: (new (sampleRate: number, onAudio: (error: Error | null, samples: Float32Array) => void) => CaptureHandle) | null =
	null;
let nativesErr: string | null = null;

async function probeNatives(): Promise<{ ok: boolean; reason?: string }> {
	if (AudioCaptureCtor) return { ok: true };
	if (nativesErr) return { ok: false, reason: nativesErr };
	try {
		const natives = await import("@oh-my-pi/pi-natives");
		if (typeof natives.AudioCapture !== "function") {
			nativesErr = "pi-natives AudioCapture missing";
			return { ok: false, reason: nativesErr };
		}
		AudioCaptureCtor = natives.AudioCapture as typeof AudioCaptureCtor;
		return { ok: true };
	} catch (err) {
		nativesErr =
			err instanceof Error
				? err.message
				: "Failed to load @oh-my-pi/pi-natives (microphone capture unavailable)";
		return { ok: false, reason: nativesErr };
	}
}

function defaultCaptureFactory(onAudio: (error: Error | null, samples: Float32Array) => void): CaptureHandle {
	if (!AudioCaptureCtor) throw new Error(nativesErr ?? "microphone capture unavailable");
	return new AudioCaptureCtor(16_000, onAudio);
}

function rmsLevel(samples: Float32Array): number {
	let sum = 0;
	for (let i = 0; i < samples.length; i += 1) sum += samples[i]! * samples[i]!;
	return Math.min(1, Math.sqrt(sum / Math.max(1, samples.length)) * 8);
}

class SttSession {
	#state: SttState = "idle";
	#error: string | undefined;
	#model: SttModel | null = null;
	#partial = "";
	#committed = "";
	#level = 0;
	#streamAbort: AbortController | null = null;
	#recorder: CaptureHandle | null = null;
	#endpointer: StreamEndpointer | null = null;
	#decodeChain: Promise<void> = Promise.resolve();
	#language: string | undefined;
	readonly #createCapture: CaptureFactory;

	constructor(createCapture: CaptureFactory = defaultCaptureFactory) {
		this.#createCapture = createCapture;
	}

	async status(): Promise<SttStatus> {
		const natives = await probeNatives();
		const xf = await probeTransformers();
		const available = natives.ok && xf.ok;
		const reason = !natives.ok ? natives.reason : !xf.ok ? xf.reason : undefined;
		const model = this.#model ?? resolveSttModelSpec(undefined);
		const modelCached = available ? await isSttModelCached(model.key) : false;
		return {
			available,
			reason: available
				? undefined
				: `${reason ?? "Speech-to-text unavailable"}. Install bun deps under vendor/omp (pi-natives + @huggingface/transformers).`,
			state: this.#state,
			model: model.key,
			modelLabel: model.label,
			modelSizeHint: model.sizeHint,
			modelCached,
			partial: this.#partial || undefined,
			committed: this.#committed || undefined,
			error: this.#error,
			level: this.#level,
		};
	}

	async start(params: { model?: string; language?: string } = {}): Promise<SttStatus> {
		if (this.#state === "listening" || this.#state === "processing") {
			return this.status();
		}
		this.#error = undefined;
		const natives = await probeNatives();
		if (!natives.ok) {
			this.#state = "error";
			this.#error = natives.reason ?? "microphone unavailable";
			return this.status();
		}
		const xf = await probeTransformers();
		if (!xf.ok) {
			this.#state = "error";
			this.#error = xf.reason ?? "ASR runtime unavailable";
			return this.status();
		}

		try {
			this.#model = await ensureSttModel(params.model);
		} catch (err) {
			this.#state = "error";
			this.#error = err instanceof Error ? err.message : "Failed to load speech model";
			return this.status();
		}

		this.#language = params.language || undefined;
		this.#partial = "";
		this.#committed = "";
		this.#level = 0;
		this.#streamAbort = new AbortController();
		this.#endpointer = new StreamEndpointer();

		try {
			this.#recorder = this.#createCapture((error, samples) => {
				if (this.#state !== "listening") return;
				if (error) {
					this.#fail(error.message);
					return;
				}
				this.#level = rmsLevel(samples);
				this.#onAudio(samples);
			});
		} catch (err) {
			this.#cleanupCapture();
			this.#state = "error";
			this.#error = err instanceof Error ? err.message : "Failed to open microphone";
			return this.status();
		}

		this.#state = "listening";
		return this.status();
	}

	async stop(): Promise<SttStatus & { text: string }> {
		if (this.#state !== "listening" && this.#state !== "processing") {
			const s = await this.status();
			return { ...s, text: this.#committed };
		}
		this.#state = "processing";
		this.#cleanupCapture();

		const ep = this.#endpointer;
		this.#endpointer = null;
		if (ep) {
			for (const ev of ep.flush()) {
				if (ev.kind === "segment") await this.#decodeSegment(ev.audio, /*commit*/ true);
			}
		}
		await this.#decodeChain;

		const text = this.#committed.trim();
		this.#partial = "";
		this.#level = 0;
		this.#streamAbort = null;
		this.#state = "idle";
		const s = await this.status();
		return { ...s, text };
	}

	async cancel(): Promise<SttStatus> {
		this.#streamAbort?.abort();
		this.#cleanupCapture();
		this.#endpointer = null;
		this.#decodeChain = Promise.resolve();
		this.#partial = "";
		this.#committed = "";
		this.#level = 0;
		this.#streamAbort = null;
		this.#error = undefined;
		this.#state = "idle";
		return this.status();
	}

	#onAudio(samples: Float32Array): void {
		const ep = this.#endpointer;
		if (!ep) return;
		for (const ev of ep.push(samples)) {
			if (ev.kind === "partial") {
				void this.#decodeSegment(ev.audio, /*commit*/ false);
			} else {
				void this.#decodeSegment(ev.audio, /*commit*/ true);
			}
		}
	}

	#decodeSegment(audio: Float32Array, commit: boolean): Promise<void> {
		const modelKey = this.#model?.key;
		const language = this.#language;
		const signal = this.#streamAbort?.signal;
		this.#decodeChain = this.#decodeChain
			.then(async () => {
				if (signal?.aborted) return;
				const text = await transcribeAudio(audio, { modelKey, language, signal });
				if (signal?.aborted || !text) return;
				if (commit) {
					this.#committed = this.#join(this.#committed, text);
					this.#partial = "";
				} else {
					this.#partial = text;
				}
			})
			.catch(err => {
				if (signal?.aborted) return;
				const msg = err instanceof Error ? err.message : String(err);
				if (msg.includes("cancelled")) return;
				this.#fail(msg);
			});
		return this.#decodeChain;
	}

	#join(prior: string, next: string): string {
		const a = prior.replace(/\s+/g, " ").trim();
		const b = next.replace(/\s+/g, " ").trim();
		if (!a) return b;
		if (!b) return a;
		return `${a} ${b}`;
	}

	#fail(message: string): void {
		this.#cleanupCapture();
		this.#endpointer = null;
		this.#streamAbort?.abort();
		this.#streamAbort = null;
		this.#state = "error";
		this.#error = message;
		this.#level = 0;
	}

	#cleanupCapture(): void {
		try {
			this.#recorder?.stop();
		} catch {
			/* best effort */
		}
		this.#recorder = null;
	}
}

const g = globalThis as typeof globalThis & { __chudwareStt?: SttSession };

export function getSttSession(): SttSession {
	if (!g.__chudwareStt) g.__chudwareStt = new SttSession();
	return g.__chudwareStt;
}
