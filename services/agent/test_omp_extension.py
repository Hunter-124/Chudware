"""Opt-in, provider-free regressions against official compiled OMP runtimes.

CHUDWARE_OMP_TEST_BIN selects the digest-pinned artifact; the separate
CHUDWARE_OMP_INSTALLED_TEST_BIN probe accepts an explicitly selected installed
runtime. Neither opens an existing profile or sends a provider prompt.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_FIXTURE = r'''
import { toolWireSchema } from "@oh-my-pi/pi-ai/utils/schema";
import { validateToolArguments } from "@oh-my-pi/pi-ai";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { getBundledModels } from "@oh-my-pi/pi-catalog/models";

const report = process.env.OMP_SCHEMA_TEST_REPORT;
export default function (pi) {
  pi.on("session_start", async (_event, ctx) => {
    if (ctx.agent.kind !== "main") return;
    let child;
    try {
      const root = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (!root || root.sessionManager !== ctx.sessionManager || !root.preparedExtensions) {
        throw new Error("root session or prepared extension factories unavailable");
      }
      const prefixModel = getBundledModels("anthropic").find(model => model.id === "claude-opus-5-5");
      if (!prefixModel?.thinking?.prefixBinding) throw new Error("prefix-bound Claude model unavailable");
      const signedReply = { role: "assistant",
        content: [{ type: "thinking", thinking: "fixture", thinkingSignature: "opaque-fixture-signature" },
          { type: "text", text: "fixture" }],
        api: prefixModel.api, provider: prefixModel.provider, model: prefixModel.id,
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: "stop", timestamp: Date.now() };
      const childManager = pi.pi.SessionManager.inMemory(ctx.cwd);
      childManager.appendMessage(signedReply);
      const created = await pi.pi.createAgentSession({
        cwd: ctx.cwd,
        taskDepth: 1,
        agentId: "schema-child",
        parentAgentId: ctx.agent.id,
        sessionManager: childManager,
        model: prefixModel,
        modelRegistry: ctx.modelRegistry,
        settings: root.settings.overlay(),
        providerPromptCacheKey: "fixture-inherited-cache",
        providerPromptCacheKeySource: "fork",
        preloadedPreparedExtensions: root.preparedExtensions,
        disableExtensionDiscovery: true,
        bindProcessState: false,
      });
      child = created.session;
      const childCacheCreated = child.agent.promptCacheKey ?? null;
      const errors = [];
      await initializeExtensions(child, {
        reportSendError: (_action, error) => errors.push(String(error)),
        reportRuntimeError: error => errors.push(String(error.error)),
      });
      if (errors.length) throw new Error("child extension initialization failed");
      const childCacheInitialized = child.agent.promptCacheKey ?? null;
      // Exercise the installed extension's actual OMP tool-call hook in both
      // sessions, not a separately reimplemented timeout-selection function.
      const deadlines = [];
      const deadlineSchemaStable = [];
      for (const session of [root, child]) {
        const runner = session.extensionRunner;
        const schemaSnapshot = () => {
          const evalInfo = session.getAllToolInfos().find(tool => tool.name === "eval");
          if (!evalInfo) throw new Error("Eval tool missing from session registry");
          return JSON.stringify({ wire: toolWireSchema(evalInfo), prompt: session.systemPrompt,
            providerTools: session.agent.state.tools.map(tool => tool.name) });
        };
        const before = schemaSnapshot();
        for (const input of [
          { language: "py", code: "print(1)" },
          { language: "py", code: "print(1)", timeout: 1 },
          { language: "py", code: "print(1)", timeout: 0 },
        ]) {
          const revised = await runner.emitToolCall({
            type: "tool_call", toolCallId: "deadline-probe", toolName: "eval", input,
          });
          deadlines.push({ original: input, effective: revised?.input ?? input, blocked: !!revised?.block });
        }
        const other = { language: "py", code: "print(1)" };
        const unchanged = await runner.emitToolCall({
          type: "tool_call", toolCallId: "other-probe", toolName: "not_eval", input: other,
        });
        deadlines.push({ original: other, effective: unchanged?.input ?? other, blocked: !!unchanged?.block });
        deadlineSchemaStable.push(schemaSnapshot() === before);
      }
      const cancelTool = root.extensionRunner.getRegisteredTool("omp_cancel_probe")?.definition;
      if (!cancelTool) throw new Error("cancellation probe missing");
      const controller = new AbortController();
      const cancelExecution = cancelTool.execute("cancel-call", {}, controller.signal, undefined,
        root.extensionRunner.createContext());
      setTimeout(() => controller.abort(), 300);
      let cancelled = false;
      try { await cancelExecution; } catch { cancelled = true; }
      await Bun.sleep(100);
      const catalog = await Bun.file(process.env.OMP_SCHEMA_TEST_CATALOG).json();
      const result = [];
      for (const session of [root, child]) {
        const info = session.getAllToolInfos().find(tool => tool.name === "omp_schema_probe");
        const runner = session.extensionRunner;
        const registered = runner?.getRegisteredTool("omp_schema_probe");
        if (!info || !registered) throw new Error("CAD tool missing from session registry");
        const wire = toolWireSchema(info);
        const args = { document: "part", feature: { depth: 2 }, mode: "cut" };
        const validated = validateToolArguments(info, { type: "toolCall", id: "call", name: info.name, arguments: args });
        let invalidRejected = false;
        try {
          validateToolArguments(info, { type: "toolCall", id: "bad", name: info.name, arguments: { document: "part" } });
        } catch { invalidRejected = true; }
        const callContext = runner.createContext();
        const topLevel = new Set(session.agent.state.tools.map(tool => tool.name));
        const mounted = new Set(session.getMountedXdevToolNames());
        const enabled = new Set(session.getEnabledToolNames());
        const inactive = catalog.filter(tool => !tool.active);
        const inactiveVisible = inactive.filter(tool => topLevel.has(tool.name) || mounted.has(tool.name));
        const activeMissing = catalog.filter(tool => tool.active && !enabled.has(tool.name));
        const execution = await registered.definition.execute("schema-call", validated, undefined, undefined, callContext);
        let diagnosticError = "";
        try {
          await registered.definition.execute("refused-call", { ...validated, document: "invalid" },
            undefined, undefined, callContext);
        } catch (error) { diagnosticError = String(error); }
        result.push({ agent: callContext.agent, wire, validated, invalidRejected, execution, diagnosticError,
          promptChars: session.systemPrompt.reduce((n, block) => n + Array.from(block).length, 0),
          promptUtf8Bytes: session.systemPrompt.reduce((n, block) => n + Buffer.byteLength(block), 0),
          mountedToolCount: mounted.size, providerToolCount: topLevel.size,
          registeredToolCount: session.getAllToolInfos().length,
          inactiveCount: inactive.length, inactiveVisible: inactiveVisible.map(tool => tool.name),
          activeMissing: activeMissing.map(tool => tool.name) });
      }
      // Establish the startup presentation signature before testing changes.
      // Only this temporary fixture's sessions receive a signed reply; no
      // provider request is sent and no user session or journal is touched.
      for (const session of [root, child]) {
        await session.setActiveToolPresentation(session.getEnabledToolNames(), session.getMountedXdevToolNames());
      }
      root.agent.setModel(prefixModel);
      root.agent.replaceMessages([...root.agent.state.messages, signedReply]);
      const childCacheBaseline = child.agent.promptCacheKey ?? null;
      const activation = [];
      const unchanged = [];
      const replacements = [];
      const separateChanges = [];
      const disableFromBase = [];
      const authorization = [];
      const directDisable = [];
      const snapshot = async session => {
        const tools = session.agent.state.tools.map(tool => ({
          name: tool.name, customWireName: tool.customWireName, description: tool.description,
          schema: toolWireSchema(tool), strict: tool.strict, deferLoading: tool.deferLoading,
        }));
        // This is the same public hook OMP invokes after converting the
        // provider payload. Compare the bytes that leave that hook, not the
        // live Eval getter that intentionally sees changing bridge names.
        const wire = await session.extensionRunner.emitBeforeProviderRequest({
          system: session.systemPrompt, tools,
        }, prefixModel);
        return {
          prompt: JSON.stringify(session.baseSystemPrompt),
          effectivePrompt: JSON.stringify(wire.system),
          cache: session.agent.promptCacheKey ?? null,
          provider: session.agent.state.tools.map(tool => tool.name),
          providerWire: JSON.stringify(wire.tools),
          mounted: session.getMountedXdevToolNames(),
        };
      };
      const providerHook = [];
      for (const name of ["eval", "_eval"]) {
        const input = { system: ["system"], messages: [{ role: "assistant", content: [
          { type: "thinking", thinking: "opaque", signature: "fixture-signature" },
        ] }], tools: [
          { name, description: "before\nSession tool names: `one`, `two`.\nafter", input_schema: { type: "object" } },
          { name: "other", description: "unrelated" },
        ] };
        const before = JSON.stringify(input);
        const output = await root.extensionRunner.emitBeforeProviderRequest(input, prefixModel);
        providerHook.push({ name,
          originalUnchanged: JSON.stringify(input) === before,
          preserved: JSON.stringify({ ...output, tools: [input.tools[0], output.tools[1]] }) === before,
          description: output.tools[0].description });
      }
      const delta = (before, after) => {
        const oldLines = JSON.parse(before.prompt).flatMap(block => block.split("\n"));
        const newLines = JSON.parse(after.prompt).flatMap(block => block.split("\n"));
        const oldSet = new Set(oldLines), newSet = new Set(newLines);
        return {
          added: newLines.filter(line => !oldSet.has(line)).slice(0, 12),
          removed: oldLines.filter(line => !newSet.has(line)).slice(0, 12),
          providerBefore: before.provider, providerAfter: after.provider,
          mountedBefore: before.mounted, mountedAfter: after.mounted,
        };
      };
      const providerDelta = (before, after) => {
        const first = JSON.parse(before.providerWire);
        const second = JSON.parse(after.providerWire);
        const beforeByName = new Map(first.map(tool => [tool.name, tool]));
        const afterByName = new Map(second.map(tool => [tool.name, tool]));
        const names = new Set([...beforeByName.keys(), ...afterByName.keys()]);
        return {
          beforeOrder: first.map(tool => tool.name),
          afterOrder: second.map(tool => tool.name),
          changed: [...names].filter(name =>
            JSON.stringify(beforeByName.get(name)) !== JSON.stringify(afterByName.get(name)))
            .map(name => ({ name, before: beforeByName.get(name), after: afterByName.get(name) })),
        };
      };
      const catalogHas = (session, name) => (session.baseSystemPrompt[0] ?? "").split("\n")
        .some(line => line.startsWith(`- xd://${name} — `) ||
          line === `Execute by writing JSON to xd://${name}.`);
      const inventoryHas = (session, name) => (session.baseSystemPrompt[0] ?? "").split("\n")
        .some(line => line === `- ${name}: \`${name}\`` || line.startsWith(`type ${name} = (`));
      const dispatchDevice = async (session, name, args = {}) => {
        if (!session.getMountedXdevToolNames().includes(name) &&
            !session.agent.state.tools.some(tool => tool.name === name)) return false;
        const write = session.getToolByName("write");
        if (!write) throw new Error("xd transport missing");
        try {
          const result = await write.execute("device-probe", {
            path: `xd://${name}`, content: JSON.stringify(args),
          });
          return !result.isError && result.details?.xdev?.mode === "execute";
        } catch { return false; }
      };
      const deviceDocs = async (session, name) => {
        const read = session.getToolByName("read");
        if (!read) throw new Error("xd help transport missing");
        const result = await read.execute("device-help", { path: `xd://${name}` });
        return result.content.filter(block => block.type === "text").map(block => block.text).join("\n");
      };
      const invoke = async (session, args) => {
        const runner = session.extensionRunner;
        const tool = runner?.getRegisteredTool("omp_activate_probe")?.definition;
        if (!tool) throw new Error("activation probe missing");
        await tool.execute("activation-call", args, undefined, undefined, runner.createContext());
      };
      const enable = async session => {
        const runner = session.extensionRunner;
        const tool = runner?.getRegisteredTool("enable_tools")?.definition;
        if (!tool) throw new Error("enable_tools missing");
        return tool.execute("enable-call", { tools: ["omp_inactive_probe"] },
          undefined, undefined, runner.createContext());
      };
      const lazyDevices = [];
      for (const session of [root, child]) {
        const evalTool = session.getToolByName("eval");
        for (const op of ["read", "write"]) {
          await invoke(session, { enabled: false });
          const result = await evalTool.execute(`lazy-${op}`, {
            language: "js", timeout: 30, code: op === "read"
              ? `return await tool.read({path:"xd://omp_inactive_probe"});`
              : `return await tool.write({path:"xd://omp_inactive_probe",content:JSON.stringify({doc_id:"lazy"})});`,
          });
          lazyDevices.push({ op, result: JSON.stringify(result),
            mounted: session.getMountedXdevToolNames().includes("omp_inactive_probe") });
        }
        const refused = await session.getToolByName("read").execute("lazy-refused", { path: "xd://omp_dormant_probe" });
        const unknown = await session.getToolByName("read").execute("lazy-unknown", { path: "xd://undo" });
        lazyDevices.push({ refused: JSON.stringify(refused), unknown: JSON.stringify(unknown) });
      }
      const concurrentEnable = [];
      for (const session of [root, child]) {
        await invoke(session, { enabled: false });
        const runner = session.extensionRunner;
        const id = `race-${runner.createContext().agent.id}`;
        await runner.emitToolCall({ type: "tool_call", toolCallId: id,
          toolName: "enable_tools", input: { tools: ["omp_inactive_probe"] } });
        const read = await runner.emitToolCall({ type: "tool_call", toolCallId: `${id}-read`,
          toolName: "read", input: { path: "xd://omp_inactive_probe" } });
        const write = await runner.emitToolCall({ type: "tool_call", toolCallId: `${id}-write`,
          toolName: "write", input: { path: "xd://omp_inactive_probe", content: "{}" } });
        const earlyDocs = await runner.emitToolResult({ type: "tool_result", toolCallId: `${id}-read`,
          toolName: "read", input: { path: "xd://omp_inactive_probe" },
          content: [{ type: "text", text: "No such tool: xd://omp_inactive_probe. Mounted devices: ." }],
          details: undefined, isError: true });
        const unrelated = await runner.emitToolCall({ type: "tool_call", toolCallId: `${id}-other`,
          toolName: "read", input: { path: "xd://omp_dormant_probe" } });
        const catalogDocs = !earlyDocs?.isError && earlyDocs?.content?.some(
          block => block.type === "text" && block.text.includes("JSON parameter schema"));
        const enabled = await enable(session);
        await runner.emitToolResult({ type: "tool_result", toolCallId: id, toolName: "enable_tools",
          input: { tools: ["omp_inactive_probe"] }, content: enabled.content, details: enabled.details, isError: false });
        // The later enabled read goes through OMP's normal mounted-device path.
        const docs = await deviceDocs(session, "omp_inactive_probe");
        concurrentEnable.push({ catalogDocs, readBlocked: !!read?.block, writeBlocked: !!write?.block,
          writeReason: write?.reason, unrelatedBlocked: !!unrelated?.block, docsVisible: docs.includes("type Args"),
          executable: await dispatchDevice(session, "omp_inactive_probe") });
      }
      for (const session of [root, child]) {
        // Start the same transition sequence even when the other factory's
        // preceding sequence left the shared registry enabled.
        await invoke(session, { enabled: false });
        for (const desired of [true, false, true]) {
          const before = await snapshot(session);
          const executableBefore = await dispatchDevice(session, "omp_inactive_probe");
          const enabled = desired ? await enable(session) : (await invoke(session, { enabled: false }), undefined);
          const mounted = session.getMountedXdevToolNames().includes("omp_inactive_probe");
          const topLevel = session.agent.state.tools.some(tool => tool.name === "omp_inactive_probe");
          const executable = await dispatchDevice(session, "omp_inactive_probe", { doc_id: "fixture" });
          const docs = desired ? await deviceDocs(session, "omp_inactive_probe") : "";
          const after = await snapshot(session);
          activation.push({ agent: session.extensionRunner.createContext().agent.id, desired, mounted, topLevel,
            executable, executableBefore, catalogVisible: catalogHas(session, "omp_inactive_probe"),
            promptStable: before.prompt === after.prompt,
            effectivePromptStable: before.effectivePrompt === after.effectivePrompt,
            providerStable: before.providerWire === after.providerWire,
            providerDelta: before.providerWire === after.providerWire ? undefined : providerDelta(before, after),
            schemaVisible: !desired || (docs.includes("type Args") && docs.includes("doc_id") &&
              docs.includes("Execute by writing JSON to xd://omp_inactive_probe.")),
            added: enabled?.details?.added, toolAccess: enabled?.details?.tool_access,
            cacheStable: before.cache === after.cache, cacheBefore: before.cache,
            promptDelta: before.prompt === after.prompt ? undefined : delta(before, after) });
        }
      }
      // Rebind the extension after a tool was enabled. Its current host
      // descriptor says "essential", but its original presentation was a
      // discoverable device and must not become a new provider schema.
      const replacementManager = pi.pi.SessionManager.inMemory(ctx.cwd);
      const replacementCreated = await pi.pi.createAgentSession({
        cwd: ctx.cwd, taskDepth: 1, agentId: "schema-replacement", parentAgentId: ctx.agent.id,
        sessionManager: replacementManager, model: prefixModel, modelRegistry: ctx.modelRegistry,
        settings: root.settings.overlay(), preloadedPreparedExtensions: root.preparedExtensions,
        disableExtensionDiscovery: true, bindProcessState: false,
      });
      const replacement = replacementCreated.session;
      let replacementState;
      try {
        const replacementErrors = [];
        await initializeExtensions(replacement, {
          reportSendError: (_action, error) => replacementErrors.push(String(error)),
          reportRuntimeError: error => replacementErrors.push(String(error.error)),
        });
        if (replacementErrors.length) throw new Error("replacement extension initialization failed");
        const enabled = await enable(replacement);
        replacementState = {
          enabled: enabled.details,
          mounted: replacement.getMountedXdevToolNames().includes("omp_inactive_probe"),
          topLevel: replacement.agent.state.tools.some(tool => tool.name === "omp_inactive_probe"),
          executable: await dispatchDevice(replacement, "omp_inactive_probe", { doc_id: "fixture" }),
        };
      } finally { await replacement.dispose(); }
      // OMP's persisted-agent reviver supplies its saved enabled names as an
      // explicit toolNames list without the original mounted partition.
      const pinnedCreated = await pi.pi.createAgentSession({
        cwd: ctx.cwd, taskDepth: 1, agentId: "schema-pinned-replacement", parentAgentId: ctx.agent.id,
        sessionManager: pi.pi.SessionManager.inMemory(ctx.cwd), model: prefixModel,
        modelRegistry: ctx.modelRegistry, settings: root.settings.overlay(),
        toolNames: root.getEnabledToolNames(), preloadedPreparedExtensions: root.preparedExtensions,
        disableExtensionDiscovery: true, bindProcessState: false,
      });
      const pinnedReplacement = pinnedCreated.session;
      let pinnedState;
      try {
        const replacementErrors = [];
        await initializeExtensions(pinnedReplacement, {
          reportSendError: (_action, error) => replacementErrors.push(String(error)),
          reportRuntimeError: error => replacementErrors.push(String(error.error)),
        });
        if (replacementErrors.length) throw new Error("pinned replacement extension initialization failed");
        await pinnedReplacement.setActiveToolPresentation(
          pinnedReplacement.getEnabledToolNames(),
          [...pinnedReplacement.getMountedXdevToolNames(), "omp_inactive_probe"]);
        const mountedAfterExplicitRestore = pinnedReplacement.getMountedXdevToolNames().includes("omp_inactive_probe");
        let error = "";
        try { await enable(pinnedReplacement); } catch (caught) { error = String(caught); }
        pinnedState = { error, mountedAfterExplicitRestore,
          mounted: pinnedReplacement.getMountedXdevToolNames().includes("omp_inactive_probe"),
          topLevel: pinnedReplacement.agent.state.tools.some(tool => tool.name === "omp_inactive_probe"),
          executable: await dispatchDevice(pinnedReplacement, "omp_inactive_probe", { doc_id: "fixture" }) };
      } finally { await pinnedReplacement.dispose(); }
      // A runtime transition can discard the CAD-mounted partition while
      // leaving the delivered model catalog and bridge generation intact.
      // Exercise Eval, then repeat enable_tools (a no-op publication) and
      // check the calling factory's return barrier.
      const lostMounts = [];
      const evalDevice = async session => {
        const tool = session.getToolByName("eval");
        if (!tool) throw new Error("Eval tool missing");
        try {
          const result = await tool.execute("device-eval", {
            language: "js", code: `return await tool.write({
              path: "xd://omp_inactive_probe", content: JSON.stringify({doc_id: "fixture"})
            });`, timeout: 30,
          });
          return JSON.stringify(result);
        } catch (error) { return String(error); }
      };
      for (const session of [root, child]) {
        await enable(session);
        const before = await snapshot(session);
        const originalMounted = session.getMountedXdevToolNames();
        const cadNames = new Set(catalog.map(tool => tool.name));
        const removed = new Set(originalMounted.filter(name => cadNames.has(name)));
        await session.setActiveToolPresentation(
          session.getEnabledToolNames().filter(name => !removed.has(name)),
          originalMounted.filter(name => !removed.has(name)));
        const remaining = session.getMountedXdevToolNames();
        const failure = await evalDevice(session);
        const enabled = await enable(session);
        const after = await snapshot(session);
        lostMounts.push({
          agent: session.extensionRunner.createContext().agent.id,
          originalMounted, remaining, failure, enabled: enabled.details,
          mounted: after.mounted, executable: await dispatchDevice(session, "omp_inactive_probe", { doc_id: "fixture" }),
          evalAfter: await evalDevice(session), promptStable: before.prompt === after.prompt,
          effectivePromptStable: before.effectivePrompt === after.effectivePrompt,
          providerStable: before.providerWire === after.providerWire, cacheStable: before.cache === after.cache,
        });
      }
      // Each factory has its own refresh barrier; settle both before comparing
      // the following no-op with a genuinely unchanged registry.
      await invoke(root, { operation: "noop" });
      await invoke(child, { operation: "noop" });
      for (const [index, session] of [root, child].entries()) {
        for (const operation of ["noop", "inactive_metadata"]) {
          const before = await snapshot(session);
          await invoke(session, { operation, version: index + 1 });
          const after = await snapshot(session);
          unchanged.push({ operation, agent: session.extensionRunner.createContext().agent.id,
            promptStable: before.prompt === after.prompt, cacheStable: before.cache === after.cache,
            cacheBefore: before.cache });
        }
      }
      for (const [index, session] of [root, child].entries()) {
        await invoke(session, { name: "omp_schema_probe", enabled: true });
        const version = index + 1;
        await invoke(session, { operation: "replace", version });
        const info = session.getAllToolInfos().find(tool => tool.name === "omp_schema_probe");
        if (!info) throw new Error("replacement probe missing");
        let oldArgsRejected = false;
        try {
          validateToolArguments(info, { type: "toolCall", id: "old", name: info.name,
            arguments: { document: "part" } });
        } catch { oldArgsRejected = true; }
        replacements.push({ version, description: info.description, wire: toolWireSchema(info),
          oldArgsRejected, executable: await dispatchDevice(session, info.name, { version }),
          catalogVisible: catalogHas(session, info.name),
          promptHasDescription: session.baseSystemPrompt.some(block => block.includes(`CAD schema probe revision ${version}`)) });
        await invoke(session, { operation: "schema_only", version: version + 10 });
        const schemaOnly = session.getAllToolInfos().find(tool => tool.name === "omp_schema_probe");
        await invoke(session, { operation: "metadata_only", version });
        const metadataOnly = session.getAllToolInfos().find(tool => tool.name === "omp_schema_probe");
        separateChanges.push({
          version, schemaDescription: schemaOnly.description, schemaWire: toolWireSchema(schemaOnly),
          metadataDescription: metadataOnly.description, metadataWire: toolWireSchema(metadataOnly),
          executable: await dispatchDevice(session, info.name, { version: version + 10 }),
          promptHasMetadata: session.baseSystemPrompt.some(block => block.includes(`CAD metadata only ${version}`)),
        });
        await invoke(session, { name: "omp_schema_probe", enabled: false });
        disableFromBase.push({
          agent: session.extensionRunner.createContext().agent.id,
          mounted: session.getMountedXdevToolNames().includes(info.name),
          topLevel: session.agent.state.tools.some(tool => tool.name === info.name),
          catalogVisible: catalogHas(session, info.name),
          executable: await dispatchDevice(session, info.name, { version }),
        });
      }
      for (const session of [root, child]) {
        await invoke(session, { operation: "restore" });
        const before = await dispatchDevice(session, "omp_revoke_probe");
        await invoke(session, { operation: "remove" });
        const removed = {
          mounted: session.getMountedXdevToolNames().includes("omp_revoke_probe"),
          topLevel: session.agent.state.tools.some(tool => tool.name === "omp_revoke_probe"),
          catalogVisible: catalogHas(session, "omp_revoke_probe"),
          executable: await dispatchDevice(session, "omp_revoke_probe"),
        };
        await invoke(session, { operation: "restore" });
        authorization.push({ before, removed, restored: await dispatchDevice(session, "omp_revoke_probe") });
      }
      for (const session of [root, child]) {
        await invoke(session, { name: "omp_essential_probe", enabled: true });
        // Exercise revocation of a genuinely advertised direct tool even if
        // the preceding activation was delivered only as a frozen-base notice.
        await session.setActiveToolPresentation(session.getEnabledToolNames(), session.getMountedXdevToolNames(), true);
        const before = {
          topLevel: session.agent.state.tools.some(tool => tool.name === "omp_essential_probe"),
          inventoryVisible: inventoryHas(session, "omp_essential_probe"),
          executable: await dispatchDevice(session, "omp_essential_probe"),
        };
        await invoke(session, { name: "omp_essential_probe", enabled: false });
        directDisable.push({ before, after: {
          topLevel: session.agent.state.tools.some(tool => tool.name === "omp_essential_probe"),
          mounted: session.getMountedXdevToolNames().includes("omp_essential_probe"),
          inventoryVisible: inventoryHas(session, "omp_essential_probe"),
          executable: await dispatchDevice(session, "omp_essential_probe"),
        } });
      }
      const historyUnchanged = [root, child].every(session =>
        JSON.stringify(session.agent.state.messages.at(-1)) === JSON.stringify(signedReply));
      await Bun.write(report, JSON.stringify({ result, activation, concurrentEnable, lazyDevices, unchanged, replacements, separateChanges, disableFromBase,
        authorization, directDisable, deadlines, deadlineSchemaStable, cancelled, historyUnchanged, providerHook, lostMounts,
        replacementState, pinnedState, childCacheCreated, childCacheInitialized, childCacheBaseline }));
    } catch (error) {
      await Bun.write(report, JSON.stringify({ error: String(error) }));
    } finally {
      if (child) await child.dispose();
    }
  });
}
'''

_RUNNER = r'''
import json
import os
from pathlib import Path
import time
from omp_rpc import RpcClient
from services.agent.omp_bridge import ExtensionBridge
from services.agent.omp_host import CAD_SESSION_TOOLS, _CAD_SESSION_SETTINGS, _CAD_MEMORY_SETTINGS, _binary_supports_flag
from services.agent.omp_tools import build_host_tools
from services.agent.tool_categories import MAIN_CHAT_CORE_TOOLS

root = Path(os.environ["OMP_SCHEMA_TEST_ROOT"])
records = []
def dispatch(name, args, ctx):
    records.append({"name": name, "args": args, "agent": ctx.agent_id,
                    "parent": ctx.parent_agent_id, "session": ctx.session_id})
    record = records[-1]
    if name in ("enable_tools", "list_tool_categories"):
        binding = next(item for item in bindings if item.name == name)
        return binding.to_host_tool(lambda **kw: kw["execute"])(args, ctx)
    if name == "omp_schema_probe" and args.get("document") == "invalid":
        raise ValueError("document is not a part; use new_part or an explicit part doc_id")
    if name == "omp_cancel_probe":
        deadline = time.monotonic() + 5
        while not ctx.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        record["cancelled"] = ctx.cancelled
    if name == "omp_activate_probe":
        operation = args.get("operation", "activate")
        if operation == "activate":
            for definition in definitions:
                if definition["name"] == args.get("name", "omp_inactive_probe"):
                    definition["active"] = args["enabled"]
                    definition["load_mode"] = "essential" if args["enabled"] else "discoverable"
        elif operation == "inactive_metadata":
            for definition in definitions:
                if definition["name"] == "omp_dormant_probe":
                    definition["description"] = f"Inactive metadata {args['version']}"
                if definition["name"] == "omp_schema_probe":
                    old_schema = definition["input_schema"]
                    # Dictionary key order alone is not a new schema. Pair this
                    # with an unrelated real generation change.
                    definition["input_schema"] = {**old_schema, "properties":
                        dict(reversed(list(old_schema["properties"].items())))}
        elif operation == "replace":
            for definition in definitions:
                if definition["name"] == "omp_schema_probe":
                    version = args["version"]
                    definition["description"] = f"CAD schema probe revision {version}"
                    definition["input_schema"] = {"type": "object", "properties": {
                        "version": {"type": "integer", "const": version}},
                        "required": ["version"], "additionalProperties": False}
        elif operation == "schema_only":
            for definition in definitions:
                if definition["name"] == "omp_schema_probe":
                    definition["input_schema"] = {**definition["input_schema"], "properties": {
                        "version": {"type": "integer", "const": args["version"]}}}
        elif operation == "metadata_only":
            for definition in definitions:
                if definition["name"] == "omp_schema_probe":
                    definition["description"] = f"CAD metadata only {args['version']}"
        elif operation == "remove":
            definitions[:] = [definition for definition in definitions if definition["name"] != "omp_revoke_probe"]
        elif operation == "restore":
            if not any(definition["name"] == "omp_revoke_probe" for definition in definitions):
                definitions.append(revocable)
        elif operation != "noop":
            raise ValueError("unknown probe operation")
        bridge.publish_tools(definitions)
    return {"content": [{"type": "text", "text": "ok"}], "details": {}}

schema = {"type": "object", "properties": {
    "document": {"type": "string", "description": "Document identity"},
    "feature": {"type": "object", "properties": {"depth": {"type": "number", "minimum": 0}}, "required": ["depth"]},
    "mode": {"type": "string", "enum": ["add", "cut"]}},
    "required": ["document", "feature"], "additionalProperties": False}
tool_context = {"allowed_tools": list(MAIN_CHAT_CORE_TOOLS),
                "categories": [], "project_dir": str(root)}
bindings = build_host_tools(tool_context)
bridge = ExtensionBridge(root / "bridge", dispatch).start()
definitions = [binding.as_dict() for binding in bindings]
definitions.append({"name": "omp_schema_probe", "description": "CAD schema probe",
                    "input_schema": schema, "load_mode": "discoverable"})
revocable = {"name": "omp_revoke_probe", "description": "Authorization probe",
             "input_schema": {"type": "object", "properties": {}}, "load_mode": "discoverable", "active": True}
definitions.append(revocable)
definitions.extend([
    {"name": "omp_inactive_probe", "description": "Initially inactive probe",
     "input_schema": {"type": "object", "properties": {"doc_id": {"type": "string"}}},
     "load_mode": "discoverable", "active": False},
    {"name": "omp_dormant_probe", "description": "Inactive metadata 0",
     "input_schema": {"type": "object", "properties": {}}, "load_mode": "discoverable", "active": False},
    {"name": "omp_essential_probe", "description": "Direct authorization probe",
     "input_schema": {"type": "object", "properties": {}}, "load_mode": "essential", "active": True},
    {"name": "omp_activate_probe", "description": "Activate probe",
     "input_schema": {"type": "object", "properties": {
         "enabled": {"type": "boolean"}, "name": {"type": "string"},
         "operation": {"type": "string"}, "version": {"type": "integer"}}},
     "load_mode": "essential", "active": True},
    {"name": "omp_cancel_probe", "description": "Cancellation probe",
     "input_schema": {"type": "object", "properties": {}}, "load_mode": "essential", "active": True},
])
def enable_categories(tokens):
    if "omp_dormant_probe" in tokens:
        return {"ok": True, "enabled": tokens, "added": [], "unknown": []}
    added = []
    for definition in definitions:
        if definition["name"] in tokens and definition["name"] == "omp_inactive_probe":
            if not definition["active"]:
                added.append(definition["name"])
            definition["active"] = True
            # The real category callback publishes selected default-mode tools
            # as essential, even though their inactive descriptor was discoverable.
            definition["load_mode"] = "essential"
    bridge.publish_tools(definitions)
    return {"ok": True, "enabled": tokens, "added": added, "unknown": []}

tool_context["enable_categories"] = enable_categories
catalog = root / "registry.json"
catalog.write_text(json.dumps([{"name": item["name"], "active": item.get("active", True)} for item in definitions]), encoding="utf-8")
os.environ["OMP_SCHEMA_TEST_CATALOG"] = str(catalog)
bridge.publish_tools(definitions)
overlay = root / "omp-test.yml"
overlay.write_text(_CAD_SESSION_SETTINGS + _CAD_MEMORY_SETTINGS["off"] +
                   f"tools.xdevDocs: {os.environ['OMP_SCHEMA_TEST_DOCS']}\n", encoding="utf-8")
binary = os.environ["OMP_SCHEMA_TEST_BIN"]
extra = ["--no-lsp"]
if _binary_supports_flag(binary, "--no-mcp"):
    extra.append("--no-mcp")
if _binary_supports_flag(binary, "--config"):
    extra.extend(["--config", str(overlay)])
extra.extend(["--no-extensions", "--trusted-extension", os.environ["OMP_SCHEMA_CAD_EXTENSION"],
              "--trusted-extension", str(root / "fixture.ts")])
info = bridge.info()
client = RpcClient(executable=binary, cwd=root, session_dir=root / "sessions",
    no_skills=True, tools=CAD_SESSION_TOOLS,
    env={"OMP_CAD_BRIDGE_ENDPOINT": info["endpoint"], "OMP_CAD_BRIDGE_TOKEN": info["token"],
         "OMP_CAD_BRIDGE_TRANSPORT": info["transport"]},
    extra_args=extra, startup_timeout=90)
try:
    client.start()
    path = root / "report.json"
    deadline = time.monotonic() + 30
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("runtime fixture did not report")
        time.sleep(0.05)
    report = json.loads(path.read_text(encoding="utf-8"))
    if "error" in report:
        raise RuntimeError(report["error"])
    print(json.dumps({"report": report, "records": records}))
finally:
    client.stop()
    bridge.stop()
'''


def _run_extension_fixture(tmp_path, binary, docs_mode):
    repo = Path(__file__).resolve().parents[2]
    fixture = tmp_path / "fixture.ts"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "Unrelated project guidance can mention xd://omp_inactive_probe without listing it as active.\n",
        encoding="utf-8",
    )
    (tmp_path / "sessions").mkdir()
    environment = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL",
    ) if key in os.environ}
    environment.update({
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "local"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
        "OMP_SCHEMA_TEST_ROOT": str(tmp_path), "OMP_SCHEMA_TEST_BIN": str(binary),
        "OMP_SCHEMA_TEST_REPORT": str(tmp_path / "report.json"),
        "OMP_SCHEMA_TEST_DOCS": docs_mode,
        "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path),
        "OMP_SCHEMA_CAD_EXTENSION": str(repo / "services" / "agent" / "omp_extension.ts"),
    })
    completed = subprocess.run([sys.executable, "-c", _RUNNER], cwd=tmp_path,
                               env=environment, capture_output=True, text=True,
                               check=False, timeout=150)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


def _assert_activation_preserves_prefix(output):
    activation = output["report"]["activation"]
    lazy = output["report"]["lazyDevices"]
    assert len(lazy) == 6
    for offset in (0, 3):
        assert lazy[offset]["op"] == "read" and lazy[offset]["mounted"]
        assert "doc_id" in lazy[offset]["result"]
        assert lazy[offset + 1]["op"] == "write" and lazy[offset + 1]["mounted"]
        assert "ok" in lazy[offset + 1]["result"]
        assert "unavailable under the current tool policy" in lazy[offset + 2]["refused"]
        assert "list_tool_categories" in lazy[offset + 2]["unknown"]
        assert "Closest:" in lazy[offset + 2]["unknown"]
        assert "Mounted devices" not in lazy[offset + 2]["unknown"]
    assert len(output["report"]["concurrentEnable"]) == 2
    for item in output["report"]["concurrentEnable"]:
        assert item["catalogDocs"] and item["docsVisible"] and item["executable"]
        assert not item["readBlocked"] and item["writeBlocked"] and not item["unrelatedBlocked"]
        assert "earlier turn" in item["writeReason"]
    assert [item["name"] for item in output["report"]["providerHook"]] == ["eval", "_eval"]
    for item in output["report"]["providerHook"]:
        assert item["originalUnchanged"] and item["preserved"]
        assert "Session tool names:" not in item["description"]
        assert "Enabled session tools are available via `tool.<name>`" in item["description"]
    assert len(activation) == 6
    for item in activation:
        assert item["mounted"] == item["desired"]
        assert item["executable"] == item["desired"]
        assert not item["topLevel"]
        if not item["desired"]:
            assert not item["catalogVisible"]
        assert item["effectivePromptStable"] and item["providerStable"], item
        assert item["schemaVisible"]
        if item["desired"]:
            assert not item["executableBefore"]
            assert item["added"] == ["omp_inactive_probe"]
            assert "read xd://<tool_name>" in item["toolAccess"]
            assert "write its JSON arguments" in item["toolAccess"]
        assert item["promptStable"] and item["cacheStable"]
        if item["agent"] == "schema-child":
            assert item["cacheBefore"] == "fixture-inherited-cache"


@pytest.mark.parametrize("docs_mode", ["catalog", "inline"])
def test_official_omp_extension_schema_and_child_binding(tmp_path, docs_mode):
    configured = os.environ.get("CHUDWARE_OMP_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_TEST_BIN to the digest-pinned official OMP artifact")
    pytest.importorskip("omp_rpc")
    from scripts.pin_omp import host_target, load_lock

    binary = Path(configured).expanduser().resolve(strict=True)
    target = os.environ.get("CHUDWARE_OMP_TEST_TARGET") or host_target()
    expected = load_lock()["artifacts"][target]["sha256"]
    with binary.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == expected

    output = _run_extension_fixture(tmp_path, binary, docs_mode)
    rows = output["report"]["result"]
    assert [row["agent"]["kind"] for row in rows] == ["main", "sub"]
    assert rows[1]["agent"]["id"] == "schema-child"
    assert rows[1]["agent"]["parentId"] == rows[0]["agent"]["id"]
    print(json.dumps({"omp_prompt_metrics": [
        {"kind": row["agent"]["kind"], "promptChars": row["promptChars"],
         "promptUtf8Bytes": row["promptUtf8Bytes"], "mountedToolCount": row["mountedToolCount"],
         "providerToolCount": row["providerToolCount"], "registeredToolCount": row["registeredToolCount"],
         "inactiveCount": row["inactiveCount"]} for row in rows]}))
    for row in rows:
        assert row["promptChars"] > 0 and row["promptUtf8Bytes"] > 0
        assert row["mountedToolCount"] > 0
        assert row["registeredToolCount"] > row["mountedToolCount"]
        assert row["inactiveCount"] > 100
        assert row["inactiveVisible"] == []
        assert row["activeMissing"] == []
        assert row["providerToolCount"] < 50
        assert row["mountedToolCount"] < 50
        wire = row["wire"]
        assert wire["required"] == ["document", "feature"]
        assert wire["properties"]["document"]["description"] == "Document identity"
        assert wire["properties"]["feature"]["properties"]["depth"]["minimum"] == 0
        assert wire["properties"]["mode"]["enum"] == ["add", "cut"]
        assert row["validated"] == {"document": "part", "feature": {"depth": 2}, "mode": "cut"}
        assert row["invalidRejected"] is True
        assert row["execution"]["content"][0]["text"] == "ok"
        assert "omp_schema_probe" in row["diagnosticError"]
        assert "use new_part or an explicit part doc_id" in row["diagnosticError"]
    deadlines = output["report"]["deadlines"]
    assert len(deadlines) == 8
    for offset in (0, 4):
        assert deadlines[offset]["effective"] == {**deadlines[offset]["original"], "timeout": 120}
        for item in deadlines[offset + 1:offset + 4]:
            assert item["effective"] == item["original"]
        assert all(not item["blocked"] for item in deadlines[offset:offset + 4])
    assert output["report"]["deadlineSchemaStable"] == [True, True]
    assert output["report"]["cancelled"] is True
    cancelled_records = [record for record in output["records"] if record["name"] == "omp_cancel_probe"]
    assert len(cancelled_records) == 1
    assert cancelled_records[0]["cancelled"] is True
    assert output["report"]["historyUnchanged"] is True
    _assert_activation_preserves_prefix(output)
    for item in output["report"]["unchanged"]:
        assert item["promptStable"] and item["cacheStable"]
        if item["agent"] == "schema-child":
            assert item["cacheBefore"] == "fixture-inherited-cache"
    assert len(output["report"]["unchanged"]) == 4
    for item in output["report"]["replacements"]:
        assert item["description"] == f"CAD schema probe revision {item['version']}"
        assert item["wire"]["required"] == ["version"]
        assert item["wire"]["properties"]["version"]["const"] == item["version"]
        assert item["oldArgsRejected"] and item["executable"]
        assert item["catalogVisible"] and item["promptHasDescription"]
    assert len(output["report"]["replacements"]) == 2
    assert len(output["report"]["separateChanges"]) == 2
    for item in output["report"]["separateChanges"]:
        assert item["schemaDescription"] == f"CAD schema probe revision {item['version']}"
        assert item["schemaWire"]["properties"]["version"]["const"] == item["version"] + 10
        assert item["metadataDescription"] == f"CAD metadata only {item['version']}"
        assert item["metadataWire"] == item["schemaWire"]
        assert item["executable"] and item["promptHasMetadata"]
    for item in output["report"]["disableFromBase"]:
        assert not item["mounted"] and not item["topLevel"]
        assert not item["catalogVisible"] and not item["executable"]
    assert len(output["report"]["disableFromBase"]) == 2
    assert len(output["report"]["authorization"]) == 2
    for item in output["report"]["authorization"]:
        assert item["before"] and item["restored"]
        assert not any(item["removed"].values())
    assert len(output["report"]["directDisable"]) == 2
    for item in output["report"]["directDisable"]:
        assert all(item["before"].values())
        assert not any(item["after"].values())
    records = [record for record in output["records"] if record["name"] == "omp_schema_probe"
               and record["args"] == {"document": "part", "feature": {"depth": 2}, "mode": "cut"}]
    assert len(records) == 2
    assert [record["agent"] for record in records] == [rows[0]["agent"]["id"], "schema-child"]
    assert records[1]["parent"] == rows[0]["agent"]["id"]


@pytest.mark.parametrize("docs_mode", ["catalog", "inline"])
def test_installed_omp_enable_tools_preserves_provider_prefix(tmp_path, docs_mode):
    configured = os.environ.get("CHUDWARE_OMP_INSTALLED_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_INSTALLED_TEST_BIN to the official installed OMP")
    pytest.importorskip("omp_rpc")
    binary = Path(configured).expanduser().resolve(strict=True)
    output = _run_extension_fixture(tmp_path, binary, docs_mode)
    _assert_activation_preserves_prefix(output)


@pytest.mark.parametrize("docs_mode", ["catalog", "inline"])
def test_installed_omp_remounts_devices_after_transition(tmp_path, docs_mode):
    configured = os.environ.get("CHUDWARE_OMP_INSTALLED_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_INSTALLED_TEST_BIN to the official installed OMP")
    pytest.importorskip("omp_rpc")
    binary = Path(configured).expanduser().resolve(strict=True)
    output = _run_extension_fixture(tmp_path, binary, docs_mode)
    replacement = output["report"]["replacementState"]
    assert replacement["enabled"]["ok"] is True
    assert replacement["mounted"] and not replacement["topLevel"]
    assert replacement["executable"]
    # Explicitly pinned startup names are an OMP revival limitation; they
    # remain callable through xd:// rather than aborting the entire child.
    pinned = output["report"]["pinnedState"]
    assert pinned["error"] == "" and not pinned["mounted"] and pinned["topLevel"] and pinned["executable"]
    assert not pinned["mountedAfterExplicitRestore"]
    for item in output["report"]["lostMounts"]:
        assert "omp_inactive_probe" in item["originalMounted"]
        assert not any(name.startswith("omp_") for name in item["remaining"])
        if not item["remaining"] and "No such tool:" in item["failure"]:
            assert "No such tool: xd://omp_inactive_probe. Mounted devices: ." in item["failure"]
        assert item["enabled"]["ok"] is True
        original_cad = {name for name in item["originalMounted"] if name.startswith("omp_")}
        assert original_cad <= set(item["mounted"])
        assert item["executable"]
        assert "No such tool:" not in item["evalAfter"]
        assert "device-eval" in item["evalAfter"] or "ok" in item["evalAfter"]
        assert item["promptStable"] and item["effectivePromptStable"]
        assert item["providerStable"] and item["cacheStable"]


_RESUME_PROBE = r'''
import { rename, unlink } from "node:fs/promises";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai";
import { getBundledModels } from "@oh-my-pi/pi-catalog/models";

export default function (pi) {
  pi.on("session_start", (_event, ctx) => {
    if (ctx.agent.kind !== "main") return;
    const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
    if (!session || session.sessionManager !== ctx.sessionManager) throw new Error("session unavailable");
    let boundaryNames;
    let boundaryDropped;
    // This fixture loads before the CAD extension. Lose the mounts inside an
    // earlier real model-call hook, after any timer's preceding observation.
    session.agent.addBeforeModelCallHook(async () => {
      if (!boundaryNames) return;
      const removed = new Set(boundaryNames);
      boundaryNames = undefined;
      await session.setActiveToolPresentation(
        session.getEnabledToolNames().filter(name => !removed.has(name)),
        session.getMountedXdevToolNames().filter(name => !removed.has(name)));
      boundaryDropped = session.getMountedXdevToolNames();
    });
    let busy = false;
    ctx.setInterval(() => {
      if (busy) return;
      busy = true;
      void (async () => {
        let command;
        try {
          command = JSON.parse(await Bun.file(process.env.OMP_MOUNT_COMMAND).text());
          await unlink(process.env.OMP_MOUNT_COMMAND);
        } catch { return; }
        try {
          const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
          if (!session || session.sessionManager !== ctx.sessionManager) throw new Error("session unavailable");
          let result;
          if (command.action === "seed") {
            await session.sessionManager.ensureOnDisk();
            await session.sessionManager.flush();
            result = { id: session.sessionManager.getSessionId(), file: session.sessionManager.getSessionFile() };
          } else if (command.action === "state") {
            result = { enabled: session.getEnabledToolNames(), mounted: session.getMountedXdevToolNames(),
              provider: session.agent.state.tools.map(tool => tool.name) };
          } else if (command.action === "drop") {
            const removed = new Set(command.names);
            await session.setActiveToolPresentation(
              session.getEnabledToolNames().filter(name => !removed.has(name)),
              session.getMountedXdevToolNames().filter(name => !removed.has(name)));
            result = { mounted: session.getMountedXdevToolNames() };
          } else if (command.action === "enable") {
            const tool = session.extensionRunner?.getRegisteredTool("enable_tools")?.definition;
            if (!tool) throw new Error("enable_tools unavailable");
            result = await tool.execute("resume-enable", { tools: command.names }, undefined, undefined,
              session.extensionRunner.createContext());
          } else if (command.action === "eval") {
            const tool = session.getToolByName("eval");
            if (!tool) throw new Error("Eval unavailable");
            result = await tool.execute("resume-eval", { language: "js", timeout: 30,
              code: `return await tool.write({path: ${JSON.stringify(`xd://${command.name}`)},
                content: JSON.stringify({doc_id: "resume-fixture"})});` });
          } else if (command.action === "model") {
            const model = getBundledModels("anthropic").find(item => item.thinking?.prefixBinding);
            if (!model) throw new Error("fixture model unavailable");
            session.agent.setModel(model);
            const originalStream = session.agent.streamFn;
            const originalKey = session.agent.getApiKey;
            let captured;
            session.agent.getApiKey = async () => "fixture-only-never-sent";
            session.agent.streamFn = (selected, context) => {
              captured = { mounted: session.getMountedXdevToolNames(),
                enabled: session.getEnabledToolNames(), provider: context.tools.map(tool => tool.name) };
              const output = { role: "assistant", api: selected.api, provider: selected.provider, model: selected.id,
                content: [{ type: "text", text: "fixture complete" }],
                usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
                stopReason: "stop", timestamp: Date.now() };
              const stream = new AssistantMessageEventStream();
              stream.push({ type: "start", partial: output });
              stream.push({ type: "done", reason: "stop", message: output });
              return stream;
            };
            boundaryNames = command.names;
            try { await session.agent.prompt("fixture model boundary"); }
            finally {
              session.agent.streamFn = originalStream;
              session.agent.getApiKey = originalKey;
            }
            if (!captured) throw new Error("model call was not reached");
            result = { dropped: boundaryDropped, captured };
          } else {
            throw new Error("unknown probe action");
          }
          const report = process.env.OMP_MOUNT_REPORT;
          await Bun.write(report + ".tmp", JSON.stringify({ ok: true, result }));
          await rename(report + ".tmp", report);
        } catch (error) {
          const report = process.env.OMP_MOUNT_REPORT;
          await Bun.write(report + ".tmp", JSON.stringify({ ok: false, error: String(error) }));
          await rename(report + ".tmp", report);
        }
      })().finally(() => { busy = false; });
    }, 25);
  });
}
'''


_RESUME_RUNNER = r'''
import json
import os
from pathlib import Path
import time

from omp_rpc import RpcClient
from services.agent import config
from services.agent.omp_host import OmpSessionHost
from services.agent.omp_roles import _register_category_refresh
from services.agent.omp_tools import build_host_tools
from services.agent.test_omp_tools import _Policy, _entry

root = Path(os.environ["OMP_MOUNT_ROOT"])
command_file = root / "mount-command.json"
report_file = root / "mount-report.json"
config.get_tool_options = lambda *_: {
    "enabled": True, "timeout_s": None, "max_calls_per_turn": None,
    "param_defaults": {}, "prompt_note": "",
}
config.tool_timeout_s_default = lambda: 0

def fixture_tool(name):
    def call(doc_id: str = ""):
        return {"ok": True, "tool": name, "doc_id": doc_id}
    return call

schema = {"type": "object", "properties": {"doc_id": {"type": "string"}}}
entries = {name: _entry(fixture_tool(name), name, schema)
           for name in ("cad_guide", "add_features", "new_assembly")}

def factory(**kwargs):
    kwargs["extra_args"] = ("--trusted-extension", str(root / "mount-probe.ts"), *kwargs.get("extra_args", ()))
    kwargs["env"] = {**kwargs.get("env", {}), "OMP_MOUNT_COMMAND": str(command_file),
                     "OMP_MOUNT_REPORT": str(report_file)}
    return RpcClient(**kwargs)

def probe(action, **kwargs):
    report_file.unlink(missing_ok=True)
    temporary = root / "mount-command.tmp"
    temporary.write_text(json.dumps({"action": action, **kwargs}))
    os.replace(temporary, command_file)
    deadline = time.monotonic() + 30
    while not report_file.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("mount probe timed out")
        time.sleep(0.025)
    response = json.loads(report_file.read_text())
    if not response["ok"]:
        raise RuntimeError(response["error"])
    return response["result"]

categories = []
context = {
    "session_id": "mount-conversation", "run_id": "mount-run", "workspace_dir": str(root),
    "project_dir": str(root), "mode": "agent", "allowed_tools": None,
    "categories": categories, "enabled_categories": categories, "persist_tool_loads": True,
    "tool_entries": entries, "resolver": lambda name: entries[name].fn if name in entries else None,
    "policy_registry": _Policy(), "omp_builtin_names": (),
}
bindings = build_host_tools(context)
params = {"workspace_dir": str(root), "omp_session_dir": str(root / "sessions"),
          "host_tools": bindings, "register_callbacks": _register_category_refresh(context, bindings)}
host = OmpSessionHost(client_factory=factory)
try:
    resume = os.environ.get("OMP_MOUNT_JOURNAL") or None
    session = host._get_or_create_session("mount-conversation", params, resume)
    host._configure_turn(session, params)
    names = ["cad_guide", "add_features"]
    if resume is None:
        identity = probe("seed")
        enabled = probe("enable", names=names)
        assert enabled["details"]["ok"] and set(enabled["details"]["added"]) == set(names)
        state = probe("state")
        assert set(names) <= set(state["mounted"])
        print(json.dumps({"identity": identity, "state": state}))
    else:
        assert session.client.get_state().session_id == os.environ["OMP_MOUNT_SESSION"]
        restored = probe("state")
        assert set(names) <= set(restored["mounted"]), restored
        assert not (set(names) & set(restored["provider"]))
        for name in names:
            result = json.dumps(probe("eval", name=name))
            assert "No such tool:" not in result and "resume-fixture" in result, result
        dropped = probe("drop", names=restored["mounted"])
        assert dropped["mounted"] == [], dropped
        repeated = probe("enable", names=names)
        assert repeated["details"]["ok"] and repeated["details"]["added"] == []
        repaired = probe("state")
        assert set(names) <= set(repaired["mounted"]), repaired
        enabled = probe("enable", names=["new_assembly"])
        assert enabled["details"]["ok"] and enabled["details"]["added"] == ["new_assembly"]
        refreshed = probe("state")
        assert set(names + ["new_assembly"]) <= set(refreshed["mounted"]), refreshed
        assert not (set(names + ["new_assembly"]) & set(refreshed["provider"]))
        for name in names + ["new_assembly"]:
            result = json.dumps(probe("eval", name=name))
            assert "No such tool:" not in result and "resume-fixture" in result, result
        boundary = probe("model", names=refreshed["mounted"])
        assert boundary["dropped"] == [], boundary
        assert set(names + ["new_assembly"]) <= set(boundary["captured"]["mounted"]), boundary
        assert not (set(names + ["new_assembly"]) & set(boundary["captured"]["provider"]))
        result = json.dumps(probe("eval", name="add_features"))
        assert "No such tool:" not in result and "resume-fixture" in result, result
        print(json.dumps({"restored": restored, "repaired": repaired, "refreshed": refreshed}))
finally:
    host.shutdown()
'''


def test_installed_omp_restores_mounted_devices_in_new_worker(tmp_path):
    configured = os.environ.get("CHUDWARE_OMP_INSTALLED_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_INSTALLED_TEST_BIN to the official installed OMP")
    pytest.importorskip("omp_rpc")
    binary = Path(configured).expanduser().resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    (tmp_path / "mount-probe.ts").write_text(_RESUME_PROBE, encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    environment = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL",
    ) if key in os.environ}
    environment.update({
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "local"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
        "CHUDWARE_OMP_BIN": str(binary), "OMP_MOUNT_ROOT": str(tmp_path),
        "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path),
    })

    def run_worker(**extra):
        completed = subprocess.run([sys.executable, "-c", _RESUME_RUNNER], cwd=tmp_path,
                                   env={**environment, **extra}, capture_output=True, text=True,
                                   check=False, timeout=150)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        return json.loads(completed.stdout)

    first = run_worker()
    journal = Path(first["identity"]["file"])
    prefix = journal.read_bytes()
    resumed = run_worker(OMP_MOUNT_JOURNAL=str(journal), OMP_MOUNT_SESSION=first["identity"]["id"])
    assert set(first["state"]["mounted"]) <= set(resumed["restored"]["mounted"])
    assert journal.read_bytes().startswith(prefix)


_POLICY_RUNNER = r'''
import json
from pathlib import Path
import os
from services.agent.config import online_compaction_settings
from services.agent.omp_host import OmpSessionHost

root = Path(os.environ["CHUDWARE_PROJECT_DIR"])
host = OmpSessionHost()
session = None
try:
    session = host._get_or_create_session(
        "policy", {"workspace_dir": str(root), "omp_session_dir": str(root / "sessions"),
                   "memory_backend": "off", "host_tools": []}, None)
    bridge = session.client._omp_bridge
    bridge.wait_tools(timeout=60)
    orders = {"initial": bridge.call("settings_get", {}, timeout=60)["compaction"]["methodOrder"]}
    for strategy in ("snapcompact", "auto"):
        _, settings = online_compaction_settings({"agent": {"compaction_strategy": strategy}})
        applied = bridge.call("settings_set", {"compaction": settings}, timeout=60)
        orders[strategy] = applied["compaction"]["methodOrder"]
    print(json.dumps(orders))
finally:
    if session is not None:
        host._release_live_session(session)
    host.shutdown()
'''


def test_official_omp_automatic_compaction_never_runs_snapcompact(tmp_path):
    configured = os.environ.get("CHUDWARE_OMP_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_TEST_BIN to the digest-pinned official OMP artifact")
    pytest.importorskip("omp_rpc")
    repo = Path(__file__).resolve().parents[2]
    (tmp_path / "sessions").mkdir()
    environment = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL",
    ) if key in os.environ}
    environment.update({
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "local"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
        "CHUDWARE_OMP_BIN": str(Path(configured).expanduser().resolve(strict=True)),
        "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path),
    })
    completed = subprocess.run([sys.executable, "-c", _POLICY_RUNNER], cwd=tmp_path,
                               env=environment, capture_output=True, text=True,
                               check=False, timeout=150)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    orders = json.loads(completed.stdout.strip().splitlines()[-1])
    # The runtime's own default still lists snapcompact; Automatic drops only that
    # method, and an explicit selection still runs exactly what was chosen.
    assert "snapcompact" in orders["initial"]
    assert orders["auto"] == [method for method in orders["initial"] if method != "snapcompact"]
    assert orders["snapcompact"] == ["snapcompact"]



def test_bridge_keeps_runtime_presentation_across_category_updates(tmp_path):
    from services.agent.omp_bridge import ExtensionBridge

    def descriptor(name, active, load_mode):
        return {"name": name, "description": name, "input_schema": {"type": "object"},
                "active": active, "load_mode": load_mode}

    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    bootstrap = descriptor("bootstrap", True, "essential")
    deferred = descriptor("deferred", False, "discoverable")
    bridge.publish_tools([bootstrap, deferred])
    first = bridge._handle("bootstrap", {})
    assert {item["name"]: item["presentation"] for item in first["tools"]} == {
        "bootstrap": "essential", "deferred": "discoverable",
    }

    activated = descriptor("deferred", True, "essential")
    late = descriptor("late", True, "essential")
    bridge.publish_tools([bootstrap, activated, late])
    expanded = bridge._handle("bootstrap", {})
    assert {item["name"]: item["presentation"] for item in expanded["tools"]} == {
        "bootstrap": "essential", "deferred": "discoverable", "late": "discoverable",
    }
    bridge.publish_tools([bootstrap, activated, late])
    assert bridge._handle("bootstrap", {})["generation"] == expanded["generation"]

    bridge.publish_tools([bootstrap])
    bridge.publish_tools([bootstrap, activated])
    restored = bridge._handle("bootstrap", {})
    assert next(item for item in restored["tools"] if item["name"] == "deferred")["presentation"] == "discoverable"
