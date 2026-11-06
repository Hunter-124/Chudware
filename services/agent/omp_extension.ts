// Trusted, session-bound CAD extension. No model-visible control commands.
import { connect } from "node:net";
import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, lstatSync, openSync } from "node:fs";
import { lstat, mkdir, open, realpath } from "node:fs/promises";
import { join } from "node:path";
import { getBlobsDir } from "@oh-my-pi/pi-utils";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { lookup } from "@oh-my-pi/pi-coding-agent/config/registry";
import { findScopedSettings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { getOAuthCredentialProvider } from "@oh-my-pi/pi-ai/oauth";
import { authProviders } from "@oh-my-pi/pi-catalog/compat/auth";
import type { TSchema } from "@oh-my-pi/pi-ai";

// Capture once at module load, before any subprocess launched by a tool can
// inherit these credentials. Child factories share the module, not a root ctx.
const endpoint = process.env.OMP_CAD_BRIDGE_ENDPOINT;
const token = process.env.OMP_CAD_BRIDGE_TOKEN;
const transport = process.env.OMP_CAD_BRIDGE_TRANSPORT;
const assetStagingDir = process.env.OMP_CAD_ASSET_STAGING_DIR;
const assetJournal = process.env.OMP_CAD_ASSET_JOURNAL;
delete process.env.OMP_CAD_BRIDGE_ENDPOINT;
delete process.env.OMP_CAD_BRIDGE_TOKEN;
delete process.env.OMP_CAD_BRIDGE_TRANSPORT;
delete process.env.OMP_CAD_ASSET_STAGING_DIR;
delete process.env.OMP_CAD_ASSET_JOURNAL;
const MAX_FRAME = 16 * 1024 * 1024;

type Descriptor = { name: string; description: string; input_schema: Record<string, unknown>; load_mode: string; active?: boolean; presentation?: "essential" | "discoverable" };
type Command = { id: string; operation: string; arguments: Record<string, unknown> };
type ToolResult = { content: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }>; details: unknown };
type ToolState = { done: boolean; updates: ToolResult[]; result: ToolResult; error?: string; error_detail?: string; generation: number };
const CAD_TOOL_LOADS_ENTRY = "chudware.cad-tool-loads.v1";
const MAX_IMAGE_BLOB_BYTES = 64 * 1024 * 1024;
const IMAGE_HASH = /^[0-9a-f]{64}$/;
const IMAGE_BLOB_PREFIX = "blob:sha256:";
const MAX_IMAGE_REFS = 32768;

// The official session manager and this check use the same OMP directory
// resolver. Never infer the blob root from the session directory or HOME.
async function inspectImageBlob(file: string, hash: string, collect = false):
    Promise<{ state: "missing" | "present" | "invalid"; data?: Buffer }> {
  let handle;
  try {
    const before = await lstat(file);
    if (!before.isFile() || before.isSymbolicLink()) return { state: "invalid" };
    handle = await open(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
    const info = await handle.stat();
    if (!info.isFile() || info.size > MAX_IMAGE_BLOB_BYTES || info.size < 0 ||
        info.dev !== before.dev || info.ino !== before.ino) return { state: "invalid" };
    const data = collect ? Buffer.alloc(info.size) : undefined;
    const chunk = Buffer.alloc(Math.min(64 * 1024, Math.max(1, info.size)));
    const digest = createHash("sha256");
    let offset = 0;
    while (offset < info.size) {
      const { bytesRead } = await handle.read(chunk, 0, Math.min(chunk.length, info.size - offset), offset);
      if (!bytesRead) return { state: "invalid" };
      digest.update(chunk.subarray(0, bytesRead));
      if (data) chunk.copy(data, offset, 0, bytesRead);
      offset += bytesRead;
    }
    const extra = await handle.read(chunk, 0, 1, offset);
    if (extra.bytesRead || digest.digest("hex") !== hash) return { state: "invalid" };
    return { state: "present", ...(data ? { data } : {}) };
  } catch (error: unknown) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT")
      return { state: "missing" };
    return { state: "invalid" };
  } finally {
    await handle?.close();
  }
}

// Snapcompact prices a frame synchronously before deciding whether to use it.
// Inspect only its safe file metadata; never read or synthesize image bytes.
function imageBlobSize(hash: string): number | undefined {
  let fd: number | undefined;
  try {
    const file = join(getBlobsDir(), hash);
    const before = lstatSync(file);
    if (!before.isFile() || before.isSymbolicLink()) return undefined;
    fd = openSync(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
    const info = fstatSync(fd);
    if (!info.isFile() || info.dev !== before.dev || info.ino !== before.ino ||
        info.size < 0 || info.size > MAX_IMAGE_BLOB_BYTES) return undefined;
    return info.size;
  } catch { return undefined; }
  finally { if (fd !== undefined) closeSync(fd); }
}

async function imageContextRefs(pi: ExtensionAPI) {
  if (!assetJournal) unsupported();
  // Unlike SessionManager.open(), this public loader does not resolve blobs,
  // create a session, write breadcrumbs, or persist in-memory migrations.
  const loaded = await pi.pi.loadSessionFile(assetJournal, undefined, { throwIfMissing: true });
  if (loaded.invalidHeader || !loaded.entries.length) throw new Error("invalid session journal");
  pi.pi.migrateToCurrentVersion(loaded.entries);
  const entries = loaded.entries.filter(entry => entry.type !== "session");
  const framesSeen = new Set<string>();
  const framesUnavailable = new Set<string>();
  const framesSelected = new Set<string>();
  let malformedFrame = false;
  const context = pi.pi.buildSessionContext(entries, undefined, undefined, {
    resolveFrameData: (data: string) => {
      if (!data.startsWith(IMAGE_BLOB_PREFIX)) return { bytes: data.length, read: () => data };
      const hash = data.slice(IMAGE_BLOB_PREFIX.length);
      if (!IMAGE_HASH.test(hash)) { malformedFrame = true; return undefined; }
      framesSeen.add(hash);
      const size = imageBlobSize(hash);
      if (size === undefined) { framesUnavailable.add(hash); return undefined; }
      return { bytes: Math.ceil(size / 3) * 4, read: () => { framesSelected.add(hash); return data; } };
    },
  });
  const required = new Set<string>();
  let malformedRequired = false;
  const add = (value: unknown) => {
    if (typeof value !== "string" || !value.startsWith(IMAGE_BLOB_PREFIX)) return;
    const hash = value.slice(IMAGE_BLOB_PREFIX.length);
    if (!IMAGE_HASH.test(hash)) malformedRequired = true;
    else required.add(hash);
  };
  const images = (value: unknown, provider = false) => {
    if (!Array.isArray(value)) return;
    for (const block of value) {
      if (!block || typeof block !== "object" || Array.isArray(block)) continue;
      if (block.type === "image") add(block.data);
      else if (provider && ["input_image", "output_image", "image_url"].includes(block.type)) add(block.image_url);
    }
  };
  const providerItems = (value: unknown) => {
    if (!Array.isArray(value)) return;
    for (const item of value) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      if (item.type === "image_generation_call") add(item.result);
      else if (item.type === "message") images(item.content, true);
      else if (item.type === "computer_call_output") add(item.output?.image_url);
    }
  };
  for (const message of context.messages) {
    // Snapcompact frames follow OMP's omission policy, not the stricter
    // required-direct-image policy; framesSeen still makes omissions explicit.
    if (message.role !== "compactionSummary") {
      if ("content" in message) images(message.content);
      if ("images" in message) images(message.images);
    }
    if ("providerPayload" in message && message.providerPayload?.type === "openaiResponsesHistory")
      providerItems(message.providerPayload.items);
    if ("providerMetadata" in message && message.providerMetadata?.type === "computer")
      add(message.providerMetadata.screenshot?.image_url);
  }
  if (required.size + framesSeen.size + framesSelected.size + framesUnavailable.size > MAX_IMAGE_REFS)
    throw new Error("too many session image references");
  return { required: [...required], framesSeen: [...framesSeen], framesSelected: [...framesSelected],
    framesUnavailable: [...framesUnavailable], malformedRequired, malformedFrame };
}

function toolLoadScope(args: Record<string, unknown>): string {
  if (typeof args.scope !== "string" || !/^[0-9a-f]{64}$/.test(args.scope)) invalid();
  return args.scope;
}

function toolLoadNames(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 4096 ||
      value.some(name => typeof name !== "string" || !name || name.length > 256)) invalid();
  return [...new Set(value as string[])];
}
const COMPACTION_KEYS = ["enabled", "methodOrder", "supersedeReads", "dropUseless", "keepRecentTokens"] as const;

function sleep(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

async function request<T>(action: string, params: object = {}): Promise<T> {
  if (!endpoint || !token || !["unix", "named_pipe"].includes(transport || "")) throw new Error("bridge unavailable");
  const payload = JSON.stringify({ token, action, params }) + "\n";
  if (Buffer.byteLength(payload) > MAX_FRAME) throw new Error("bridge frame too large");
  const { promise, resolve, reject } = Promise.withResolvers<T>();
  const socket = connect(endpoint);
  const chunks: Buffer[] = [];
  let length = 0;
  let settled = false;
  const finish = (error?: Error, value?: T) => {
    if (settled) return;
    settled = true;
    socket.destroy();
    if (error) reject(error);
    else resolve(value as T);
  };
  socket.setTimeout(5000, () => finish(new Error("bridge timeout")));
  socket.on("error", () => finish(new Error("bridge unavailable")));
  socket.on("connect", () => socket.write(payload));
  socket.on("data", (chunk: Buffer) => {
    length += chunk.length;
    if (length > MAX_FRAME) return finish(new Error("bridge frame too large"));
    chunks.push(chunk);
    if (chunk.indexOf(10) < 0) return;
    const all = Buffer.concat(chunks, length);
    const end = all.indexOf(10);
    try {
      // The peer is the authenticated per-runtime ControlServer; each caller
      // checks the fields it needs before using a response.
      const envelope = JSON.parse(all.subarray(0, end).toString("utf8")) as { ok: boolean; result?: T };
      if (!envelope.ok || !envelope.result) return finish(new Error("bridge rejected request"));
      finish(undefined, envelope.result);
    } catch { finish(new Error("invalid bridge response")); }
  });
  socket.on("end", () => finish(new Error("bridge closed")));
  return promise;
}

function invalid(): never { throw Object.assign(new Error("invalid request"), { code: "invalid_request" }); }
function unsupported(): never { throw Object.assign(new Error("unsupported"), { code: "unsupported" }); }
function keysOnly(args: Record<string, unknown>, allowed: string[]) {
  if (!args || typeof args !== "object" || Array.isArray(args) || Object.keys(args).some(key => !allowed.includes(key))) invalid();
}
function provider(args: Record<string, unknown>): string {
  if (typeof args.provider !== "string" || !args.provider || args.provider.length > 256) invalid();
  return args.provider;
}

function authState(ctx: ExtensionContext, p: string) {
  const credentials = ctx.modelRegistry.authStorage.credentials;
  const credentialProvider = getOAuthCredentialProvider(p);
  const keys = ctx.modelRegistry.authStorage.keys;
  const source = keys.source(credentialProvider, { env: "aliases" });
  const keylessMarker = keys.keyless(credentialProvider);
  return { provider: p, credentialProvider, configured: source !== undefined,
    effectiveSource: source?.kind ?? null, effectiveSourceConcrete: source?.concrete ?? false,
    oauth: credentials.hasOAuth(credentialProvider), keylessMarker,
    hasApiKey: !keylessMarker && credentials.list(credentialProvider).some(row => row.credential.type === "api_key") };
}

async function control(pi: ExtensionAPI, ctx: ExtensionContext, settings: ExtensionAPI["pi"]["settings"], operation: string, args: Record<string, unknown>, requestId: string): Promise<unknown> {
  switch (operation) {
    case "auth_state":
    case "set_api_key":
    case "remove_api_key": {
      const credentials = ctx.modelRegistry.authStorage.credentials;
      keysOnly(args, operation === "set_api_key" ? ["provider", "api_key"] : ["provider"]);
      const p = provider(args);
      const credentialProvider = getOAuthCredentialProvider(p);
      if (operation === "set_api_key") {
        if (typeof args.api_key !== "string" || !args.api_key || args.api_key.length > 16384) invalid();
        await credentials.upsert(credentialProvider, { type: "api_key", key: args.api_key });
      } else if (operation === "remove_api_key") {
        for (const row of credentials.list(credentialProvider)) {
          if (row.credential.type === "api_key") await credentials.removeById(credentialProvider, row.id);
        }
      }
      return authState(ctx, p);
    }
    case "auth_states": {
      keysOnly(args, ["providers"]);
      if (!Array.isArray(args.providers) || args.providers.length > 32 ||
          args.providers.some(p => typeof p !== "string" || !p || p.length > 128)) invalid();
      return { providers: (args.providers as string[]).map(p => {
        try { return authState(ctx, p); }
        catch { return { provider: p }; }
      }) };
    }
    case "setup_providers": {
      keysOnly(args, []);
      return { providers: authProviders()
        .filter(entry => !entry.allowsMissingApiKey && !entry.nativeAuthApis?.length &&
          (!entry.login || entry.login.kind === "api-key" || entry.result === "api-key"))
        .map(entry => ({ id: entry.id, name: entry.name, authKinds: ["api_key"] })) };
    }
    case "memory_status":
      keysOnly(args, []);
      if (!ctx.memory) unsupported();
      return await ctx.memory.status();
    case "memory_search": {
      keysOnly(args, ["query", "limit"]);
      if (!ctx.memory) unsupported();
      if (typeof args.query !== "string" || !args.query || (args.limit !== undefined && (typeof args.limit !== "number" || !Number.isInteger(args.limit) || args.limit < 1 || args.limit > 100))) invalid();
      return await ctx.memory.search(args.query, args.limit === undefined ? undefined : { limit: args.limit as number });
    }
    case "memory_save": {
      keysOnly(args, ["content", "context", "source", "importance"]);
      if (!ctx.memory) unsupported();
      if (typeof args.content !== "string" || !args.content || (args.context !== undefined && typeof args.context !== "string") || (args.source !== undefined && typeof args.source !== "string") || (args.importance !== undefined && (typeof args.importance !== "number" || !Number.isFinite(args.importance)))) invalid();
      return await ctx.memory.save({ content: args.content, context: args.context as string | undefined,
        source: args.source as string | undefined, importance: args.importance as number | undefined });
    }
    case "model_metadata": {
      keysOnly(args, []);
      const model = ctx.models.current();
      return model ? { provider: model.provider, id: model.id, name: model.name, contextWindow: model.contextWindow, maxTokens: model.maxTokens, input: model.input, reasoning: model.reasoning } : null;
    }
    case "cad_image_context_refs": {
      keysOnly(args, []);
      const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (ctx.agent.kind !== "main" || !session || session.sessionManager !== ctx.sessionManager) unsupported();
      return await imageContextRefs(pi);
    }
    case "cad_image_blobs_status":
    case "cad_image_blob_import": {
      const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (ctx.agent.kind !== "main" || !session || session.sessionManager !== ctx.sessionManager) unsupported();
      if (operation === "cad_image_blobs_status") {
        keysOnly(args, ["hashes"]);
        if (!Array.isArray(args.hashes) || args.hashes.length > 128 ||
            args.hashes.some(hash => typeof hash !== "string" || !IMAGE_HASH.test(hash))) invalid();
        const hashes = [...new Set(args.hashes as string[])];
        const states = [];
        for (const hash of hashes) {
          const result = await inspectImageBlob(join(getBlobsDir(), hash), hash);
          states.push({ hash, state: result.state });
        }
        return { root: getBlobsDir(), states };
      }
      keysOnly(args, ["hash"]);
      if (typeof args.hash !== "string" || !IMAGE_HASH.test(args.hash) || !assetStagingDir) invalid();
      const hash = args.hash;
      const root = await lstat(assetStagingDir);
      if (!root.isDirectory() || root.isSymbolicLink()) invalid();
      const existing = await inspectImageBlob(join(getBlobsDir(), hash), hash);
      if (existing.state !== "missing") return { hash, state: existing.state };
      const staged = await inspectImageBlob(join(assetStagingDir, hash), hash, true);
      if (staged.state !== "present" || !staged.data) return { hash, state: "invalid" };
      await mkdir(getBlobsDir(), { recursive: true, mode: 0o700 });
      // Do not replace an asset that appeared while the staged file was read.
      const raced = await inspectImageBlob(join(getBlobsDir(), hash), hash);
      if (raced.state !== "missing") return { hash, state: raced.state };
      const result = await ctx.sessionManager.putBlob(staged.data);
      if (result.hash !== hash || result.ref !== `blob:sha256:${hash}`) throw new Error("blob reference mismatch");
      const installed = await inspectImageBlob(join(getBlobsDir(), hash), hash);
      if (installed.state !== "present") return { hash, state: "invalid" };
      return { hash, state: "imported", ref: result.ref };
    }
    case "cad_tool_loads_get":
    case "cad_tool_loads_put":
    case "cad_tool_loads_legacy": {
      keysOnly(args, operation === "cad_tool_loads_put" ? ["scope", "authority", "tools"] :
        operation === "cad_tool_loads_legacy" ? ["scope", "authority", "sessionId", "sessionFile", "workspace"] : ["scope", "authority"]);
      const scope = toolLoadScope(args);
      if (typeof args.authority !== "string" || !/^[0-9a-f]{64}$/.test(args.authority)) invalid();
      const authority = args.authority;
      const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (ctx.agent.kind !== "main" || !session || session.sessionManager !== ctx.sessionManager) unsupported();
      const manager = session.sessionManager;
      const sessionId = manager.getSessionId();
      const branch = manager.getBranchView();
      const prior = branch.findLast(item =>
        item.type === "custom" && item.customType === CAD_TOOL_LOADS_ENTRY);
      const data = prior && prior.type === "custom" && prior.data && typeof prior.data === "object"
        ? prior.data as Record<string, unknown> : undefined;
      const header = manager.getHeader();
      // A real fork mints a new runtime identity but copies its ancestor's
      // custom entries. Only the same workspace and credential owner may
      // adopt that selection; reopening the same journal under another
      // conversation must not transfer ownership.
      const inherited = !!data && data.authority === authority &&
        typeof data.sessionId === "string" && data.sessionId !== sessionId &&
        header?.id === sessionId && typeof header.parentSession === "string" &&
        !!header.parentSession && header.parentSession !== manager.getSessionFile();
      const exact = !!data && data.scope === scope && data.sessionId === sessionId &&
        (data.authority === undefined || data.authority === authority);
      if (operation === "cad_tool_loads_put") {
        if (prior && !exact && !inherited) unsupported();
        const tools = toolLoadNames(args.tools);
        if (!manager.getSessionFile()) throw new Error("CAD tool loads require a persisted session");
        await manager.ensureOnDisk();
        pi.appendEntry(CAD_TOOL_LOADS_ENTRY, { scope, authority, sessionId, tools });
        // The worker may exit while the model is still in this turn.
        await manager.flush();
        return { tools };
      }
      if (operation === "cad_tool_loads_get") {
        const found = !!prior && (exact || inherited);
        return { tools: found ? toolLoadNames(data!.tools) : [], found, claimed: !!prior,
          rebind: found && (inherited || data!.authority === undefined) };
      }
      if (prior) return { tools: [] };
      if (typeof args.sessionId !== "string" || !args.sessionId || args.sessionId.length > 256 ||
          typeof args.sessionFile !== "string" || !args.sessionFile || args.sessionFile.length > 4096 ||
          typeof args.workspace !== "string" || !args.workspace || args.workspace.length > 4096) invalid();
      const sessionFile = manager.getSessionFile();
      if (!header || header.id !== sessionId || sessionId !== args.sessionId ||
          !sessionFile || typeof header.cwd !== "string") return { tools: [] };
      try {
        const [recordedCwd, currentCwd, workspace, recordedFile, expectedFile] = await Promise.all([
          realpath(header.cwd), realpath(manager.getCwd()), realpath(args.workspace),
          realpath(sessionFile), realpath(args.sessionFile),
        ]);
        const key = (value: string) => process.platform === "win32" ? value.toLowerCase() : value;
        if (key(recordedCwd) !== key(workspace) || key(currentCwd) !== key(workspace) ||
            key(recordedFile) !== key(expectedFile)) return { tools: [] };
      } catch { return { tools: [] }; }
      const tools = new Set<string>();
      for (const entry of branch) {
        if (entry.type === "reset_boundary") { tools.clear(); continue; }
        if (entry.type !== "message") continue;
        const message = entry.message as unknown as Record<string, unknown>;
        if (!message || message.role !== "toolResult" || message.toolName !== "enable_tools" ||
            message.isError !== false || !message.details || typeof message.details !== "object") continue;
        const details = message.details as Record<string, unknown>;
        if (details.ok !== true) continue;
        try {
          for (const name of toolLoadNames(details.added)) tools.add(name);
        } catch { /* malformed metadata is not evidence of an activation */ }
      }
      return { tools: [...tools] };
    }
    case "tool_state": {
      keysOnly(args, []);
      const enabled = new Set(pi.getActiveTools());
      return { tools: pi.getAllTools().map(tool => ({
        name: tool.name,
        description: tool.description.slice(0, 256),
        builtin: tool.sourceInfo.source === "builtin",
        available: enabled.has(tool.name),
      })) };
    }
    case "handoff": {
      keysOnly(args, ["customInstructions"]);
      if (args.customInstructions !== undefined && (typeof args.customInstructions !== "string" || args.customInstructions.length > 16384)) invalid();
      const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      // Agent registry ids are process-global; bind to this exact session, not
      // merely its id or the workspace currently selected in the UI.
      if (!session || session.sessionManager !== ctx.sessionManager) unsupported();
      if (!ctx.isIdle()) throw Object.assign(new Error("session busy"), { code: "busy" });
      const controller = new AbortController();
      let timedOut = false;
      let checking = false;
      const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 85000);
      const heartbeat = setInterval(() => {
        if (checking || controller.signal.aborted) return;
        checking = true;
        void request<{ pending: boolean }>("is_pending", { id: requestId })
          .then(state => { if (!state.pending) controller.abort(); })
          .catch(() => controller.abort())
          .finally(() => { checking = false; });
      }, 500);
      try {
        const live = await request<{ pending: boolean }>("is_pending", { id: requestId });
        if (!live.pending) {
          controller.abort();
          throw Object.assign(new Error("handoff cancelled"), { code: "cancelled" });
        }
        const result = await session.handoff(args.customInstructions as string | undefined, { signal: controller.signal });
        if (controller.signal.aborted) throw Object.assign(new Error("handoff interrupted"), { code: timedOut ? "timeout" : "cancelled" });
        if (!result || typeof result.document !== "string") throw new Error("handoff did not complete");
        return { document: result.document, ...(result.savedPath === undefined ? {} : { savedPath: result.savedPath }) };
      } catch (error) {
        if (controller.signal.aborted) throw Object.assign(new Error("handoff interrupted"), { code: timedOut ? "timeout" : "cancelled" });
        throw error;
      } finally {
        clearTimeout(deadline);
        clearInterval(heartbeat);
      }
    }
    case "settings_get":
    case "settings_set": {
      keysOnly(args, operation === "settings_set" ? ["compaction"] : []);
      const handles = COMPACTION_KEYS.map(key => {
        const handle = lookup(`compaction.${key}`);
        if (!handle) unsupported();
        return [key, handle] as const;
      });
      if (operation === "settings_set") {
        const value = args.compaction;
        if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
        const policy = value as Record<string, unknown>;
        keysOnly(policy, [...COMPACTION_KEYS]);
        if (policy.methodOrder !== undefined &&
            (!Array.isArray(policy.methodOrder) || policy.methodOrder.length !== 1 ||
             !["snapcompact", "shake", "soft", "remote", "handoff"].includes(policy.methodOrder[0]))) invalid();
        for (const [key, handle] of handles) {
          if (Object.hasOwn(policy, key)) {
            try { handle.assertWritable(policy[key]); } catch { invalid(); }
          }
        }
        const layers = settings.overlayLayers().overrides as Record<string, unknown>;
        const previous = layers.compaction && typeof layers.compaction === "object"
          ? layers.compaction as Record<string, unknown> : {};
        try {
          for (const [key, handle] of handles) {
            if (Object.hasOwn(policy, key)) handle.override(settings, policy[key] as never);
            else handle.clearOverride(settings);
          }
          // Automatic (no methodOrder) runs the runtime's effective order minus the
          // experimental snapcompact, which runs only when selected explicitly.
          if (!Object.hasOwn(policy, "methodOrder")) {
            const [, orderHandle] = handles.find(([key]) => key === "methodOrder")!;
            const order = orderHandle.get(settings) as unknown;
            if (Array.isArray(order) && order.includes("snapcompact")) {
              const stable = order.filter(method => method !== "snapcompact");
              orderHandle.override(settings, (stable.length ? stable : ["soft"]) as never);
              if ((orderHandle.get(settings) as unknown[]).includes("snapcompact")) unsupported();
            }
          }
          // An environment binding may outrank the in-memory override.
          for (const [key, handle] of handles) {
            if (Object.hasOwn(policy, key) && JSON.stringify(handle.get(settings)) !== JSON.stringify(policy[key])) unsupported();
          }
        } catch (error) {
          for (const [key, handle] of handles) {
            if (Object.hasOwn(previous, key)) handle.override(settings, previous[key] as never);
            else handle.clearOverride(settings);
          }
          throw error;
        }
      }
      return { compaction: Object.fromEntries(handles.map(([key, handle]) => [key, handle.get(settings)])) };
    }
    case "compact": {
      keysOnly(args, ["mode", "instructions"]);
      if (args.mode !== undefined && args.mode !== "soft" && args.mode !== "snapcompact") unsupported();
      if (args.instructions !== undefined && typeof args.instructions !== "string") invalid();
      if (args.mode === "snapcompact" && args.instructions) unsupported();
      const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (!session || session.sessionManager !== ctx.sessionManager) unsupported();
      // Use the owning OMP session's official compact operation. The extension
      // context overload accepts either a mode OR focus text, not both.
      const previousId = session.sessionManager.getBranchView()
        .findLast(entry => entry.type === "compaction")?.id;
      const usage = () => {
        try {
          const value = session.getContextBreakdown();
          return value ? { tokens: value.usedTokens, estimated: !value.anchored } : undefined;
        } catch { return undefined; }
      };
      const contextBefore = usage();
      const result = await session.compact(
        args.instructions as string | undefined,
        args.mode === undefined ? undefined : { mode: args.mode as "soft" | "snapcompact" },
      );
      const entry = session.sessionManager.getBranchView().findLast(item => item.type === "compaction");
      if (!entry || entry.id === previousId) throw new Error("compaction returned without a new journal entry");
      return { completed: true, result, method: entry.method, contextBefore, contextAfter: usage() };
    }
    default: unsupported();
  }
}

export default async function cadExtension(pi: ExtensionAPI) {
  // Tool-call hooks run for the entire batch before execution. Never await an
  // enable here: doing so would prevent that same enable from starting.
  const enabling = new Map<string, { names: Set<string>; failed: boolean }>();
  const schemaReads = new Map<string, { name: string; signature: string; enable: { failed: boolean } }>();
  pi.on("tool_call", (event, ctx) => {
    if (event.toolName === "enable_tools" && Array.isArray(event.input.tools)) {
      enabling.set(event.toolCallId, {
        names: new Set(event.input.tools.filter((name): name is string => typeof name === "string")),
        failed: false,
      });
      return;
    }
    if (event.toolName !== "read" && event.toolName !== "write") return;
    const path = event.input.path;
    const name = typeof path === "string" ? /^xd:\/\/([^/?#:]+)/.exec(path)?.[1] : undefined;
    const descriptor = name && registered.get(name);
    if (!name || !descriptor) return;
    const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
    if (!session || session.sessionManager !== ctx.sessionManager ||
        session.getMountedXdevToolNames().includes(name) ||
        session.agent.state.tools.some(tool => tool.name === name)) return;
    const enable = [...enabling.values()].find(item => item.names.has(name));
    if (!enable) return;
    if (event.toolName === "write") {
      return { block: true, reason: `Wait for enable_tools to succeed in an earlier turn, then write to xd://${name}.` };
    }
    schemaReads.set(event.toolCallId, { name, signature: descriptor.signature, enable });
  });
  pi.on("tool_result", async (event, ctx) => {
    if (event.toolName === "enable_tools") {
      const pending = enabling.get(event.toolCallId);
      if (pending) {
        const details = event.details as { ok?: boolean } | undefined;
        pending.failed = event.isError || details?.ok === false;
        enabling.delete(event.toolCallId);
      }
      return;
    }
    if (event.toolName !== "read" && event.toolName !== "write") return;
    const pending = schemaReads.get(event.toolCallId);
    schemaReads.delete(event.toolCallId);
    const path = event.input.path;
    const name = typeof path === "string" ? /^xd:\/\/([^/?#:]+)$/.exec(path)?.[1] : undefined;
    if (!name || !event.content.some(block => block.type === "text" &&
        (block.text.startsWith(`No such tool: xd://${name}.`) ||
         block.text.startsWith(`No such tool device: xd://${name}.`) ||
         block.text === "xd:// is not mounted in this session."))) return;
    if (pending && !pending.enable.failed) {
      const descriptor = JSON.parse(pending.signature) as Descriptor;
      return { isError: false, content: [{ type: "text", text:
        `xd://${name}\n\n${descriptor.description}\n\nJSON parameter schema:\n` +
        `${JSON.stringify(descriptor.input_schema, null, 2)}\n\n` +
        `Wait for enable_tools to succeed before writing JSON to xd://${name}.` }] };
    }
    const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
    if (!session || session.sessionManager !== ctx.sessionManager) return;
    const fail = (message: string) => ({ isError: true, content: [{ type: "text" as const, text: message }] });
    try {
      let known = registered.has(name);
      if (!known) {
        const catalog = session.getToolByName("list_tool_categories");
        if (!catalog) throw new Error("CAD tool catalog unavailable");
        const result = await catalog.execute(`${event.toolCallId}-catalog`, { query: name, limit: 100 });
        const details = result.details as { ok?: boolean; tools?: Array<{ name?: string }> } | undefined;
        if (!details?.ok || !Array.isArray(details.tools)) throw new Error("CAD tool catalog unavailable");
        known = details.tools.some(tool => tool.name === name);
      }
      if (!known) {
        const distance = (a: string, b: string) => {
          let row = Array.from({ length: b.length + 1 }, (_, i) => i);
          for (let i = 0; i < a.length; i++) {
            const next = [i + 1];
            for (let j = 0; j < b.length; j++)
              next.push(Math.min(next[j] + 1, row[j + 1] + 1, row[j] + (a[i] === b[j] ? 0 : 1)));
            row = next;
          }
          return row[b.length];
        };
        const suggestions = [...registered.keys()].map(candidate => ({ candidate, score: distance(name, candidate) }))
          .sort((a, b) => a.score - b.score || a.candidate.localeCompare(b.candidate))
          .slice(0, 3).map(item => item.candidate);
        return fail(`Unknown CAD tool xd://${name}.${suggestions.length ? ` Closest: ${suggestions.join(", ")}.` : ""} Use list_tool_categories to discover tools.`);
      }
      const enable = session.getToolByName("enable_tools");
      if (!enable) throw new Error("CAD tool activation unavailable");
      const enabled = await enable.execute(`${event.toolCallId}-enable`, { tools: [name] });
      const details = enabled.details as { ok?: boolean; error?: string; reason?: string } | undefined;
      if (enabled.isError || details?.ok !== true)
        return fail(details?.error || details?.reason || enabled.content.filter(block => block.type === "text").map(block => block.text).join("\n") || "CAD tool activation failed");
      if (!session.getMountedXdevToolNames().includes(name) &&
          !session.agent.state.tools.some(tool => tool.name === name)) {
        return fail(`Tool ${name} is unavailable under the current tool policy.`);
      }
      const transport = session.getToolByName(event.toolName);
      if (!transport) throw new Error("CAD device transport unavailable");
      const result = await transport.execute(`${event.toolCallId}-retry`, event.input);
      return { content: result.content, details: result.details, isError: !!result.isError };
    } catch (error) {
      return fail(error instanceof Error ? error.message : String(error));
    }
  });
  pi.on("turn_end", () => {
    enabling.clear();
    schemaReads.clear();
  });
  // OMP's omitted Eval deadline is 30s, shorter than CAD's 45s synchronous
  // solve-to-job handoff. Use the supported input-revision hook so the effective
  // deadline is visible in the call and still subject to OMP's timeout ceiling.
  // An explicit deadline (including zero) and Stop always retain their meaning.
  pi.on("tool_call", event => {
    if (event.toolName === "eval" && event.input.timeout === undefined) {
      return { input: { ...event.input, timeout: 120 } };
    }
  });
  // OMP's Eval description renders the live bridge names, including mounted
  // devices. A mount would therefore rewrite the provider tool array even
  // when Eval's schema and the system prompt are unchanged. Keep the provider
  // description independent of that changing roster; enabled tools announce
  // their names and xd:// schema access in the enable_tools result instead.
  pi.on("before_provider_request", event => {
    const payload = event.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return;
    const body = payload as Record<string, unknown>;
    if (!Array.isArray(body.tools)) return;
    let changed = false;
    const tools = body.tools.map(tool => {
      if (!tool || typeof tool !== "object" || Array.isArray(tool)) return tool;
      const item = tool as Record<string, unknown>;
      if ((item.name !== "eval" && item.name !== "_eval") || typeof item.description !== "string") return tool;
      const description = item.description.replace(/^Session tool names: [^\n]*$/m,
        "Enabled session tools are available via `tool.<name>`; read their xd:// documentation for schemas.");
      if (description === item.description) return tool;
      changed = true;
      return { ...item, description };
    });
    if (changed) return { ...body, tools };
  });
  const snapshot = await request<{ tools: Descriptor[]; generation: number }>("bootstrap");
  if (!Array.isArray(snapshot.tools)) throw new Error("invalid CAD registry");
  let generation = snapshot.generation;
  let disposeAdmission: (() => void) | undefined;
  let refreshTools: ((fresh?: boolean) => Promise<void>) | undefined;
  let disposeRefresh: (() => void) | undefined;
  const registered = new Map<string, { signature: string; loadMode: "essential" | "discoverable" }>();
  // The bridge retains the runtime's original presentation across newly
  // created root and child factories. An enabled descriptor can otherwise say
  // "essential" even though it was first registered as a discoverable device.
  function register(tool: Descriptor, defaultInactive = tool.active === false) {
    if (!tool || typeof tool.name !== "string" || typeof tool.description !== "string" || !tool.input_schema || typeof tool.input_schema !== "object" ||
        (tool.presentation !== undefined && tool.presentation !== "essential" && tool.presentation !== "discoverable")) throw new Error("invalid CAD descriptor");
    const loadMode = registered.get(tool.name)?.loadMode ?? tool.presentation ??
      (defaultInactive || tool.load_mode !== "essential" ? "discoverable" : "essential");
    const signature = JSON.stringify(
      { name: tool.name, description: tool.description, input_schema: tool.input_schema, load_mode: loadMode },
      (_key, value) => value && typeof value === "object" && !Array.isArray(value)
        ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)))
        : value,
    );
    if (registered.get(tool.name)?.signature === signature) return;
    pi.registerTool({
      name: tool.name, label: tool.name, description: tool.description,
      // OMP accepts and validates raw JSON Schema at its serialization boundary.
      parameters: tool.input_schema as unknown as TSchema,
      loadMode,
      defaultInactive,
      async execute(toolCallId, args, signal, onUpdate, ctx) {
        const started = await request<{ id?: string }>("tool_start", { name: tool.name, arguments: args,
          context: { tool_call_id: toolCallId, agent_id: ctx.agent.id,
            parent_agent_id: ctx.agent.parentId ?? null, session_id: ctx.sessionManager.getSessionId() } });
        if (!started.id) throw new Error("CAD tool unavailable");
        const cancelCall = () => { void request("tool_cancel", { id: started.id }).catch(() => {}); };
        signal?.addEventListener("abort", cancelCall, { once: true });
        try {
        while (true) {
          if (signal?.aborted) {
            cancelCall();
            throw new Error("CAD tool cancelled");
          }
          const state = await request<ToolState>("tool_poll", { id: started.id });
          if (state.error && !state.done) throw new Error("CAD tool unavailable");
          for (const update of state.updates || []) onUpdate?.(update);
          if (state.done) {
            if (signal?.aborted) throw new Error("CAD tool cancelled");
            if (state.error) throw new Error(state.error_detail
              ? `${tool.name}: ${state.error_detail}` : "CAD tool failed");
            // A no-op enable can follow a session transition that discarded
            // mounts without changing the bridge generation. Check the actual
            // presentation as well as the generation before reporting success.
            const deadline = Date.now() + 20000;
            do {
              if (signal?.aborted) throw new Error("CAD tool cancelled");
              if (tool.name === "enable_tools" || generation < state.generation) {
                if (!refreshTools) throw new Error("CAD tool registry is not initialized");
                await refreshTools(true);
              }
              if (generation >= state.generation) break;
              if (Date.now() >= deadline) throw new Error("CAD tool registry refresh timed out");
              await sleep(50);
            } while (true);
            return state.result;
          }
          await sleep(100);
        }
        } finally {
          signal?.removeEventListener("abort", cancelCall);
        }
      },
    });
    registered.set(tool.name, { signature, loadMode });
  }
  // A non-forced OMP update preserves its base prompt and announces mount
  // deltas. If that base already advertises a tool that is now unavailable,
  // however, retaining it would leave a stale catalog. Examine only the
  // runtime-generated inventory/device sections, not arbitrary project text.
  function stalePromptCatalog(prompt: readonly string[], topLevel: ReadonlySet<string>, mounted: ReadonlySet<string>): boolean {
    let section: "inventory" | "devices" | undefined;
    let inlineDevice: string | undefined;
    let catalogList = false;
    for (const line of (prompt[0] ?? "").split("\n")) {
      if (section === "devices" && inlineDevice) {
        if (line === `Execute by writing JSON to xd://${inlineDevice}.`) {
          if (!mounted.has(inlineDevice)) return true;
          inlineDevice = undefined;
        }
        continue;
      }
      if (line === "# Tool Inventory") { section = "inventory"; catalogList = false; continue; }
      if (line === "# xd:// Tool Devices") { section = "devices"; catalogList = false; continue; }
      if (line.startsWith("§ ") || line.startsWith("# ")) {
        section = undefined;
        catalogList = false;
        continue;
      }
      if (section === "inventory") {
        const compact = /^- (?:[^`]*: )?`([^`]+)`$/.exec(line);
        const declaration = /^type (\S+) = \(/.exec(line);
        const name = compact?.[1] ?? declaration?.[1];
        if (name && !topLevel.has(name)) return true;
      } else if (section === "devices") {
        if (line === "## Additional devices (docs on demand)") { catalogList = true; continue; }
        if (catalogList) {
          const entry = /^- xd:\/\/(\S+) — /.exec(line);
          if (entry && !mounted.has(entry[1])) return true;
          if (!line) catalogList = false;
          continue;
        }
        const heading = /^## (\S+)(?: — .+)?$/.exec(line);
        if (heading) inlineDevice = heading[1];
      }
    }
    return false;
  }
  for (const tool of snapshot.tools) register(tool);
  // Each child factory refreshes its own registry; only the root accepts SDK
  // controls. Acquire its concrete settings during the session-scoped hook.
  pi.on("session_shutdown", () => {
    disposeAdmission?.(); disposeAdmission = undefined;
    disposeRefresh?.(); disposeRefresh = undefined;
    refreshTools = undefined;
  });
  pi.on("session_start", (_event, ctx) => {
    const settings = findScopedSettings();
    const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
    if (!session || session.sessionManager !== ctx.sessionManager) {
      ctx.abort();
      throw new Error("Cannot bind CAD tools to their owning session");
    }
    // OMP can revive a child with an explicit toolNames list but without the
    // saved mounted partition. Its SDK pins those startup names top-level for
    // the lifetime of that session; they remain callable through xd://.
    const childPinned = new Set(ctx.agent.kind === "sub"
      ? session.agent.state.tools.filter(tool => registered.get(tool.name)?.loadMode === "discoverable")
        .map(tool => tool.name)
      : []);
    const wantsMount = (name: string) => registered.get(name)?.loadMode === "discoverable" && !childPinned.has(name);
    if (ctx.agent.kind === "sub") {
      // The child admission checks below use this same owning session.
      let preparing = false;
      let revision = 0;
      let preparedRevision = -1;
      let preparedModel = session.agent.state.model;
      const unsubscribe = session.subscribe(event => {
        if (event.type !== "model_changed") return;
        revision++;
        if (preparing) session.agent.abort(new Error("Model changed during admission"));
      });
      const authorize = async () => {
        const model = session.agent.state.model;
        if (!model?.provider || !model.id) throw new Error("No child model is selected");
        const initialRevision = revision;
        const credentialProvider = getOAuthCredentialProvider(model.provider);
        const keys = ctx.modelRegistry.authStorage.keys;
        let policy: { allowed: boolean };
        try {
          policy = await request<{ allowed: boolean }>("model_policy", { provider: model.provider });
        } catch {
          throw new Error("Cannot verify child model authorization");
        }
        if (typeof policy.allowed !== "boolean") throw new Error("Cannot verify child model authorization");
        if (revision !== initialRevision || session.agent.state.model !== model) throw new Error("Model changed during admission");
        if (!keys.source(credentialProvider, { env: "aliases" }) && !policy.allowed) {
          throw new Error(`Provider ${model.provider} has no configured authentication or keyless opt-in`);
        }
        return { model, revision: initialRevision };
      };
      const removeBefore = session.agent.addBeforeModelCallHook(async () => {
        preparing = true;
        try {
          const admitted = await authorize();
          preparedModel = admitted.model;
          preparedRevision = admitted.revision;
        } catch (error) {
          preparing = false;
          throw error;
        }
      });
      const removeGate = session.agent.addBeforeModelCall(async () => {
        try {
          const admitted = await authorize();
          if (admitted.model !== preparedModel || admitted.revision !== preparedRevision) {
            throw new Error("Model changed during admission");
          }
        } finally { preparing = false; }
      });
      disposeAdmission?.();
      disposeAdmission = () => { unsubscribe(); removeBefore(); removeGate(); };
    }
    let refreshing: Promise<void> | undefined;
    refreshTools = async (fresh = false) => {
      if (refreshing) {
        await refreshing;
        // A timer may have inspected the old presentation before this tool
        // returned or the model boundary was reached. Recheck after it settles.
        if (fresh) await refreshTools!();
        return;
      }
      const pending = (async () => {
        const next = await request<{ tools: Descriptor[]; generation: number }>("bootstrap");
        if (!Array.isArray(next.tools) || !Number.isInteger(next.generation)) throw new Error("invalid CAD registry");
        const priorNames = new Set(registered.keys());
        const nextNames = new Set(next.tools.map(tool => tool.name));
        const currentEnabled = new Set(session.getEnabledToolNames());
        const currentMounted = new Set(session.getMountedXdevToolNames());
        const selected = next.tools.filter(tool => tool.active !== false);
        const selectedNames = new Set(selected.map(tool => tool.name));
        const drifted = next.tools.some(tool =>
          currentEnabled.has(tool.name) !== selectedNames.has(tool.name) ||
          currentMounted.has(tool.name) !== (selectedNames.has(tool.name) && wantsMount(tool.name))) ||
          [...priorNames].some(name => !nextNames.has(name) &&
            (currentEnabled.has(name) || currentMounted.has(name)));
        if (generation !== next.generation || drifted) {
          const retained = [...currentEnabled].filter(name => !priorNames.has(name));
          const retainedMounted = [...currentMounted].filter(name => !priorNames.has(name));
          // New late registrations must not activate themselves before the
          // exact SDK partition is installed. Existing enabled definitions
          // still use OMP's own metadata-replacement refresh.
          for (const tool of next.tools) register(tool, !priorNames.has(tool.name) || tool.active === false);
          const enabled = [...new Set([...retained, ...selected.map(tool => tool.name)])];
          const mounted = [...new Set([...retainedMounted,
            ...selected.filter(tool => wantsMount(tool.name)).map(tool => tool.name)])];
          // Presentation mutations share OMP's registration queue, so this
          // await is also the registration and enable_tools return barrier.
          await session.setActiveToolPresentation(enabled, mounted);
          const actualEnabled = new Set(session.getEnabledToolNames());
          const actualMounted = new Set(session.getMountedXdevToolNames());
          if (next.tools.some(tool =>
            actualEnabled.has(tool.name) !== selectedNames.has(tool.name) ||
            actualMounted.has(tool.name) !== (selectedNames.has(tool.name) && wantsMount(tool.name))) ||
            [...priorNames].some(name => !nextNames.has(name) &&
              (actualEnabled.has(name) || actualMounted.has(name)))) {
            throw new Error("CAD tool presentation could not be restored");
          }
          const providerNames = new Set(session.agent.state.tools.flatMap(tool =>
            tool.customWireName ? [tool.name, tool.customWireName] : [tool.name]));
          const accessible = new Set([...actualMounted, ...[...childPinned].filter(name => actualEnabled.has(name))]);
          if (stalePromptCatalog(session.baseSystemPrompt, providerNames, accessible)) {
            await session.setActiveToolPresentation(enabled, mounted, true);
          }
          generation = next.generation;
        }
        if (ctx.agent.kind === "main") await request("tool_ack", { generation });
      })();
      refreshing = pending;
      try { await pending; }
      finally { if (refreshing === pending) refreshing = undefined; }
    };
    // OMP may restore or replace a session's tool partition without publishing
    // a new bridge generation. Check it at every model boundary, not just on a
    // timer, so a prompt never observes a stale catalog or an empty device set.
    disposeRefresh?.();
    disposeRefresh = session.agent.addBeforeModelCallHook(async () => { await refreshTools!(true); });
    ctx.setInterval(() => {
      void refreshTools?.().catch(() => { /* retry at the next tick or model boundary */ });
    }, 200);
    if (ctx.agent.kind !== "main" || !settings) return;
    let polling = false;
    let active = 0;
    ctx.setInterval(() => {
      if (polling || active >= 16) return;
      polling = true;
      void (async () => {
        try {
          const response = await request<{ command: Command | null }>("poll");
          const command = response.command;
          if (!command) return;
          active++;
          void (async () => {
            try {
              const result = await control(pi, ctx, settings, command.operation, command.arguments, command.id);
              await request("result", { id: command.id, result });
            } catch (error: unknown) {
              const code = error && typeof error === "object" && "code" in error ? error.code : undefined;
              try {
                await request("result", { id: command.id, error: code === "unsupported" || code === "invalid_request" || code === "busy" || code === "timeout" || code === "cancelled" ? code : "failed" });
              } catch { /* bridge already closed */ }
            } finally { active--; }
          })();
        } catch { /* private bridge may be shutting down */ }
        finally { polling = false; }
      })();
    }, 100);
  });
}
