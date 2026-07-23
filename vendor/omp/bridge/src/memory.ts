/**
 * Long-term project memory, backed verbatim by OMP's `Mnemopi` facade
 * (vendor/omp/packages/mnemopi/src/core/memory.ts:382-564). One `Mnemopi` instance per CAD
 * project root, opened at `<root>/.plm/mnemopi.sqlite` — never OMP's own
 * `~/.hermes/mnemopi/data/mnemopi.db` default (packages/mnemopi/src/config.ts:19-20) — so a
 * project's memory travels with its PLM tree instead of the host user's home directory.
 *
 * Kept deliberately degraded per the frozen contract: `embeddings: false, llm: false`
 * (MnemopiOptions, core/memory.ts:36-45) disables the fastembed pipeline and local-LLM
 * consolidation, so recall runs on FTS + recency + importance only (no native embedding
 * binary, no model download, no API key) — the same "keyword/degraded" mode OMP itself falls
 * back to when neither is configured.
 *
 * Implements: memory_remember, memory_recall, memory_stats. Wired into bridge.ts dispatch().
 */
import * as path from "node:path";
import {
	Mnemopi,
	type RecallFacadeOptions,
	type RecallResult,
	type RememberFacadeOptions,
} from "@oh-my-pi/pi-mnemopi";
import { estimateTokens } from "@oh-my-pi/pi-mnemopi/core/token-counter";
import { num, optStr, str, type Json } from "./json";

/** Live `Mnemopi` instances keyed by project root — one long-lived SQLite connection per
 *  root for the life of the bridge process, mirroring auth-context.ts's singleton store. */
const instances = new Map<string, Mnemopi>();

function getMnemopi(root: string): Mnemopi {
	const existing = instances.get(root);
	if (existing) return existing;
	const dbPath = path.join(root, ".plm", "mnemopi.sqlite");
	const created = new Mnemopi({ dbPath, embeddings: false, llm: false });
	instances.set(root, created);
	return created;
}

function requireRoot(params: Json): string {
	const root = str(params.root);
	if (!root) throw new Error("root is required");
	return root;
}

/** `memory_remember {root,content,kind?,source?,importance?}` -> `{id}`. `kind` maps to
 *  mnemopi's `memoryType` (RememberFacadeOptions, core/memory.ts:98-99). */
export function handleMemoryRemember(params: Json): Json {
	const root = requireRoot(params);
	const content = str(params.content);
	if (!content) throw new Error("memory_remember requires content");
	const options: RememberFacadeOptions = {
		source: optStr(params.source),
		importance: num(params.importance),
		memoryType: optStr(params.kind),
	};
	const id = getMnemopi(root).remember(content, options);
	return { id };
}

/** `memory_recall {root,query,limit?,tokenBudget?,kind?}` -> `{hits}`. mnemopi's
 *  `RecallOptions` has no `kind`/token-budget knob (beam/types.ts:157-184) — both are applied
 *  here against the score-ranked rows `Mnemopi.recall` (core/memory.ts:454-456) returns. */
export async function handleMemoryRecall(params: Json): Promise<Json> {
	const root = requireRoot(params);
	const query = str(params.query);
	if (!query) throw new Error("memory_recall requires query");
	const limit = num(params.limit) ?? 10;
	const tokenBudget = num(params.tokenBudget);
	const kind = optStr(params.kind);

	// Over-fetch so a post-hoc kind filter still has enough candidates left to fill `limit`.
	const topK = kind !== undefined ? Math.max(limit * 4, 40) : limit;
	const recallOptions: RecallFacadeOptions = { contentPreviewChars: 0 };
	const results: RecallResult[] = await getMnemopi(root).recall(query, topK, recallOptions);

	const hits: Json[] = [];
	let usedTokens = 0;
	for (const r of results) {
		if (kind !== undefined && r.memory_type !== kind) continue;
		const tokens = estimateTokens(r.content);
		if (tokenBudget !== undefined && hits.length > 0 && usedTokens + tokens > tokenBudget) break;
		hits.push({
			id: r.id,
			content: r.content,
			kind: r.memory_type ?? null,
			source: r.source,
			importance: r.importance ?? null,
			score: r.score,
			tokens,
		});
		usedTokens += tokens;
		if (hits.length >= limit) break;
	}
	return { hits };
}

interface KindCountRow {
	kind: string;
	count: number;
}

/** Breakdown of working-memory rows by `memory_type`, queried against mnemopi's own open
 *  connection (`Mnemopi.conn`, core/memory.ts:390-391) rather than re-deriving row counts. */
function kindCounts(mnemopi: Mnemopi): Json {
	const rows = mnemopi.conn
		.query("SELECT COALESCE(memory_type, 'unspecified') AS kind, COUNT(*) AS count FROM working_memory GROUP BY kind")
		.all() as KindCountRow[];
	const counts: Json = {};
	for (const row of rows) counts[row.kind] = row.count;
	return counts;
}

/** `memory_stats {root}` -> mnemopi's `MemoryFacadeStats` (core/memory.ts:476-504), plus
 *  `path`/`exists`/`kinds` conveniences for callers keying off a file-existence check. */
export function handleMemoryStats(params: Json): Json {
	const root = requireRoot(params);
	const mnemopi = getMnemopi(root);
	const stats = mnemopi.getStats();
	return { ...stats, path: stats.database, exists: true, kinds: kindCounts(mnemopi) };
}
