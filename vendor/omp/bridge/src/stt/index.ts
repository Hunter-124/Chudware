/**
 * Bridge RPC handlers for on-device speech-to-text (OMP ASR sidecar).
 *
 * Methods: stt_status | stt_start{model?,language?} | stt_stop | stt_cancel
 */
import type { Json } from "../json";
import { asObject, str } from "../json";
import { getSttSession } from "./session";

export async function handleSttStatus(_params: Json): Promise<Json> {
	return (await getSttSession().status()) as unknown as Json;
}

export async function handleSttStart(params: Json): Promise<Json> {
	const o = asObject(params);
	const model = str(o.model) || undefined;
	const language = str(o.language) || undefined;
	return (await getSttSession().start({ model, language })) as unknown as Json;
}

export async function handleSttStop(_params: Json): Promise<Json> {
	return (await getSttSession().stop()) as unknown as Json;
}

export async function handleSttCancel(_params: Json): Promise<Json> {
	return (await getSttSession().cancel()) as unknown as Json;
}
