/**
 * Shared JSON-RPC parameter/result helpers for the bridge. Every RPC boundary in
 * bridge.ts / auth-context.ts / oauth-flow.ts / models.ts reads untyped `params`
 * off the wire and needs the same narrow-and-default treatment; centralized here
 * so each call site stays a one-liner instead of re-deriving the same guards.
 */

export type Json = Record<string, unknown>;

/** Narrow an unknown JSON value to an object map. `as Json` is safe after the typeof guard (Json is just Record<string, unknown>). */
export function asObject(v: unknown): Json {
	return v && typeof v === "object" ? (v as Json) : {};
}

/** Read a required-ish string field from unknown RPC input, defaulting to `""` (or a caller-supplied fallback) when absent/wrong-typed. */
export function str(v: unknown, fallback = ""): string {
	return typeof v === "string" ? v : fallback;
}

/** Read an optional string field: `undefined` (not `""`) when absent/wrong-typed/empty, so callers can `??`/`?.` cleanly. */
export function optStr(v: unknown): string | undefined {
	return typeof v === "string" && v.length > 0 ? v : undefined;
}

/** Read a finite number from unknown RPC input (accepting numeric strings, since some transports round-trip ids as strings). */
export function num(v: unknown): number | undefined {
	if (typeof v === "number" && Number.isFinite(v)) return v;
	if (typeof v === "string" && v.trim() !== "") {
		const parsed = Number(v);
		if (Number.isFinite(parsed)) return parsed;
	}
	return undefined;
}
