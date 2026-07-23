/**
 * Prompt-cache retention policy for cache-warming decisions.
 *
 * Warming a provider's prompt cache (re-sending the cached prefix with a 1-token completion)
 * costs ~1/10 of a full-context input re-send, so it pays off when a task or idle gap will
 * outlive the provider's cache window. The host uses these per-provider windows to decide
 * WHEN to warm (interval < ttl) vs. when to instead cheap-model-recompact the context.
 *
 * Values are vendors' documented default cache lifetimes; refine as providers publish changes.
 */
import type { Json } from "./json";

export const DEFAULT_CACHE_TTL_MS = 5 * 60_000;

export const CACHE_TTL_MS: Record<string, number> = {
	anthropic: 5 * 60_000, // Anthropic prompt caching, 5m default (1h beta available)
	"anthropic-messages": 5 * 60_000,
	openai: 5 * 60_000, // OpenAI automatic prompt caching, ~5-10m idle eviction
	"openai-responses": 10 * 60_000,
	"openai-codex": 10 * 60_000,
	google: 60 * 60_000, // Gemini explicit context cache, default ~1h (configurable TTL)
	"google-gemini-cli": 60 * 60_000,
	moonshot: 5 * 60_000,
	"kimi-code": 5 * 60_000,
	deepseek: 60 * 60_000, // DeepSeek context caching (disk-backed), long-lived
	xai: 5 * 60_000,
	openrouter: 5 * 60_000, // depends on the upstream it routes to; conservative default
};

/** cache_policy{provider?} -> one provider's window, or the whole table. */
export function handleCachePolicy(params: Json): Json {
	const provider = typeof params.provider === "string" ? params.provider : "";
	if (provider) {
		return {
			provider,
			ttlMs: CACHE_TTL_MS[provider] ?? DEFAULT_CACHE_TTL_MS,
			defaultTtlMs: DEFAULT_CACHE_TTL_MS,
		};
	}
	return { policies: CACHE_TTL_MS, defaultTtlMs: DEFAULT_CACHE_TTL_MS };
}
