import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { SessionManager } from "@earendil-works/pi-coding-agent";

import { spaceSessionDir } from "../src/local/agent/agent-data-dir.js";
import { resolveConversationSessionPath } from "../src/local/agent/pi-client.js";
import { readTurnTrace } from "../src/local/agent/turn-trace.js";

test("readTurnTrace returns only the entries a turn appended, with secrets redacted", async (t) => {
  const agentDir = await mkdtemp(join(tmpdir(), "work-fold-turn-trace-agent-"));
  const spaceRoot = await mkdtemp(join(tmpdir(), "work-fold-turn-trace-space-"));
  t.after(() => Promise.all([rm(agentDir, { recursive: true, force: true }), rm(spaceRoot, { recursive: true, force: true })]));

  const previousAgentDir = process.env.WORKFOLD_AGENT_DIR;
  process.env.WORKFOLD_AGENT_DIR = agentDir;
  t.after(() => {
    if (previousAgentDir === undefined) delete process.env.WORKFOLD_AGENT_DIR;
    else process.env.WORKFOLD_AGENT_DIR = previousAgentDir;
  });

  const conversationId = "conversation-1";
  const sessionDir = spaceSessionDir(spaceRoot, agentDir);
  await mkdir(sessionDir, { recursive: true });
  const sessionPath = await resolveConversationSessionPath(sessionDir, conversationId);
  const sessionManager = SessionManager.open(sessionPath, sessionDir, spaceRoot);

  // A prior, unrelated turn: establishes the "before" boundary.
  sessionManager.appendMessage({ role: "user", content: "earlier turn", timestamp: Date.now() } as any);
  const sessionLeafBefore = sessionManager.getLeafId();

  sessionManager.appendMessage({ role: "user", content: "how many files are in my wallpapers folder", timestamp: Date.now() } as any);
  sessionManager.appendMessage({
    role: "assistant",
    content: [
      { type: "thinking", thinking: "I should count files with ls, using api_key=sk-should-be-redacted-0123456789." },
      { type: "toolCall", id: "tc1", name: "bash", arguments: { command: "ls -1 ./wallpapers | wc -l" } },
    ],
    api: "messages",
    provider: "anthropic",
    model: "claude-test",
    usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: "toolUse",
    timestamp: Date.now(),
  } as any);
  sessionManager.appendMessage({
    role: "toolResult",
    toolCallId: "tc1",
    toolName: "bash",
    content: [{ type: "text", text: "266" }],
    isError: false,
    timestamp: Date.now(),
  } as any);
  const sessionLeafAfter = sessionManager.getLeafId();

  // A later, unrelated turn: must not leak into this turn's trace.
  sessionManager.appendMessage({ role: "user", content: "later turn", timestamp: Date.now() } as any);

  const trace = await readTurnTrace({ spaceRoot, conversationId, sessionLeafBefore, sessionLeafAfter });

  assert.equal(trace.available, true);
  assert.equal(trace.entries.length, 3, "should capture exactly the 3 entries this turn appended");

  const [userEntry, assistantEntry, toolResultEntry] = trace.entries;
  assert.equal((userEntry!.detail as any).role, "user");
  assert.equal((userEntry!.detail as any).text, "how many files are in my wallpapers folder");

  const assistantDetail = assistantEntry!.detail as any;
  assert.equal(assistantDetail.role, "assistant");
  assert.equal(assistantDetail.toolCalls[0].name, "bash");
  assert.equal(assistantDetail.toolCalls[0].arguments.command, "ls -1 ./wallpapers | wc -l");
  assert.match(assistantDetail.thinking[0], /I should count files with ls/);
  assert.doesNotMatch(assistantDetail.thinking[0], /sk-should-be-redacted/, "secrets must be redacted from thinking text");
  assert.match(assistantDetail.thinking[0], /\[redacted\]/);

  const toolResultDetail = toolResultEntry!.detail as any;
  assert.equal(toolResultDetail.role, "toolResult");
  assert.equal(toolResultDetail.toolName, "bash");
  assert.equal(toolResultDetail.content, "266");
});

test("readTurnTrace reports unavailable when no leaf range was recorded", async (t) => {
  const spaceRoot = await mkdtemp(join(tmpdir(), "work-fold-turn-trace-unavailable-"));
  t.after(() => rm(spaceRoot, { recursive: true, force: true }));
  const trace = await readTurnTrace({ spaceRoot, conversationId: "conversation-1", sessionLeafBefore: null, sessionLeafAfter: null });
  assert.equal(trace.available, false);
  assert.deepEqual(trace.entries, []);
});
