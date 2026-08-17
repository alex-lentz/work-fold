import {
  SessionManager,
  type SessionEntry,
} from "@earendil-works/pi-coding-agent";

import { resolveConversationSessionPath } from "./pi-client.js";
import { resolvePiRuntime } from "./pi-runtime-config.js";
import { redactJsonDeep, redactSecrets } from "./redact.js";

export interface TurnTraceEntry {
  id: string;
  parentId: string | null;
  timestamp: string;
  kind: SessionEntry["type"];
  /** Redacted, JSON-shaped detail specific to `kind` (message role, tool call, usage, etc.). */
  detail: unknown;
}

export interface TurnTrace {
  /** False when the turn's leaf range could not be resolved (e.g. an older turn recorded before this feature shipped). */
  available: boolean;
  entries: TurnTraceEntry[];
}

const maxTraceEntries = 2_000;

/**
 * Reads the Pi session entries a single turn appended, by walking parent
 * pointers from `sessionLeafAfter` back to (but excluding) `sessionLeafBefore`.
 * Read-only: opens the same session file the live Chat client already writes,
 * but never calls an append method on it.
 */
export async function readTurnTrace(options: {
  spaceRoot: string;
  conversationId: string;
  sessionLeafBefore?: string | null;
  sessionLeafAfter?: string | null;
}): Promise<TurnTrace> {
  const { spaceRoot, conversationId, sessionLeafBefore, sessionLeafAfter } = options;
  if (!sessionLeafAfter) return { available: false, entries: [] };

  const runtime = await resolvePiRuntime(spaceRoot, undefined, { requestProjectTrust: false });
  const sessionPath = await resolveConversationSessionPath(runtime.sessionDir, conversationId);
  const sessionManager = SessionManager.open(sessionPath, runtime.sessionDir, spaceRoot);

  const chain: SessionEntry[] = [];
  let currentId: string | null = sessionLeafAfter;
  const visited = new Set<string>();
  while (currentId && currentId !== sessionLeafBefore && chain.length < maxTraceEntries) {
    if (visited.has(currentId)) break;
    visited.add(currentId);
    const entry = sessionManager.getEntry(currentId);
    if (!entry) break;
    chain.push(entry);
    currentId = entry.parentId;
  }
  chain.reverse();

  return {
    available: true,
    entries: chain.map((entry) => ({
      id: entry.id,
      parentId: entry.parentId,
      timestamp: entry.timestamp,
      kind: entry.type,
      detail: redactJsonDeep(entryDetail(entry)),
    })),
  };
}

function entryDetail(entry: SessionEntry): unknown {
  if (entry.type !== "message") return entry;
  const message = entry.message as any;
  switch (message?.role) {
    case "user":
      return { role: "user", text: extractText(message.content) };
    case "assistant":
      return {
        role: "assistant",
        provider: message.provider,
        model: message.model,
        responseModel: message.responseModel,
        responseId: message.responseId,
        stopReason: message.stopReason,
        errorMessage: message.errorMessage,
        usage: message.usage,
        diagnostics: message.diagnostics,
        text: extractText(message.content),
        thinking: extractThinking(message.content),
        toolCalls: extractToolCalls(message.content),
      };
    case "toolResult":
      return {
        role: "toolResult",
        toolCallId: message.toolCallId,
        toolName: message.toolName,
        isError: message.isError,
        content: extractText(message.content),
        details: message.details,
      };
    default:
      return message;
  }
}

function extractText(content: unknown): string {
  if (typeof content === "string") return redactSecrets(content);
  if (!Array.isArray(content)) return "";
  return content
    .filter((item: any) => item?.type === "text" && typeof item.text === "string")
    .map((item: any) => redactSecrets(item.text))
    .join("\n");
}

function extractThinking(content: unknown): string[] {
  if (!Array.isArray(content)) return [];
  return content
    .filter((item: any) => item?.type === "thinking" && typeof item.thinking === "string")
    .map((item: any) => redactSecrets(item.thinking));
}

function extractToolCalls(content: unknown): Array<{ id: string; name: string; arguments: unknown }> {
  if (!Array.isArray(content)) return [];
  return content
    .filter((item: any) => item?.type === "toolCall")
    .map((item: any) => ({ id: item.id, name: item.name, arguments: item.arguments }));
}
