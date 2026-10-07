# Report 1: Agent SDK, headless CLI, login and billing

Raw research for [Jarvis as a native app](../2026-10-07-native-app.md), 2026-10-07. Unchecked; the note is the checked version.

## **RESEARCH REPORT: Embedding Claude Code in a Native Desktop App**

### **1. Claude Agent SDK (TypeScript & Python)**

**How it runs Claude Code:**
- Both TypeScript (`@anthropic-ai/claude-agent-sdk` on npm) and Python (`claude-agent-sdk` on PyPI) **bundle a native Claude Code binary** with the SDK packages. [code.claude.com/docs/en/agent-sdk/quickstart.md](https://code.claude.com/docs/en/agent-sdk/quickstart) notes: "Both the TypeScript and Python SDKs bundle a native Claude Code binary, so most installs need no separate Claude Code install." The agent loop runs the same loop that powers interactive Claude Code.
- **Node.js 18+ required** (TypeScript); **Python 3.10+ required** (Python).
- The SDK is self-contained: you programmatically control the agent loop in your own process, no spawning or CLI subprocess by default.

**Streaming & Input Modes:**
- **Streaming input mode** (recommended): `ClaudeSDKClient` with an `AsyncGenerator<SDKUserMessage>` that yields messages dynamically. Enables:
  - Long-lived conversation (session stays alive across multiple turns)
  - Queued messages that process sequentially
  - **Real-time interruption via `AbortSignal`** (TypeScript) or cancel signal (Python)—documented in [code.claude.com/docs/en/agent-sdk/user-input.md](https://code.claude.com/docs/en/agent-sdk/user-input.md). Requires streaming input, not single-shot `query()`.
  - Image attachments mid-session
- **Single message input**: `query(prompt: string, options)` for one-off tasks. Returns all messages at once, does not support interruption mid-turn.

**Interrupting Mid-Reply (for Voice Barge-In):**
- **With streaming input:** the `canUseTool` callback's third argument carries an `AbortSignal` (TypeScript) or a signal field (Python, reserved for future use). Use the TypeScript signal with `.addEventListener('abort', ...)` to detect interruption.
- **Direct interrupt:** `ClaudeSDKClient` in Python offers `interrupt()` method; TypeScript offers `query(...).interrupt()` to cancel mid-turn. Sources: [code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md) describes streaming input's "real-time interruption," and [Claude Agent SDK issue tracker](https://claudeissues.com/issue/41665-feature-support-an-interrupt-message-on-stdin) mentions interrupt receipt patterns.
- **Limitation:** This is designed for permission prompts or user input flow, not arbitrary mid-response cancellation for barge-in during model text generation. Per-turn interruption for voice barge-in is not explicitly documented as a first-class feature.

**Model Switching Per Turn:**
- **No built-in per-turn model switching.** You set `model` once in `ClaudeAgentOptions` / `Options` at session start. [code.claude.com/docs/en/agent-sdk/agent-loop.md](https://code.claude.com/docs/en/agent-sdk/agent-loop.md#model) states: "If you don't set `model`, the SDK uses Claude Code's default... Set it explicitly to pin a specific model."
- **Feature request exists** ([claudeissues.com/issue/9173](https://claudeissues.com/issue/9173-feature-allow-specifying-a-specific-model-for-a-single-turn)) for per-turn model routing, but is not implemented. **Workaround:** use subagents with different `model` fields on their `AgentDefinition`, but this spawns a new isolated session per model switch, not a mid-turn model swap.

**Permissions & Permission Callbacks:**
- **`canUseTool` callback** ([code.claude.com/docs/en/agent-sdk/user-input.md](https://code.claude.com/docs/en/agent-sdk/user-input.md)): async function called when Claude wants to use a tool that isn't auto-approved. Return `{ behavior: "allow", updatedInput: ... }` or `{ behavior: "deny", message: "..." }`.
- **Permission modes** (`permissionMode`): `"default"`, `"dontAsk"`, `"acceptEdits"`, `"plan"`, `"bypassPermissions"`, `"auto"` ([code.claude.com/docs/en/agent-sdk/permissions.md](https://code.claude.com/docs/en/agent-sdk/permissions.md)).
- **PreToolUse hooks** ([code.claude.com/docs/en/agent-sdk/hooks.md](https://code.claude.com/docs/en/agent-sdk/hooks)): programmatic callbacks that run **before** the permission flow; can deny, allow, or modify any tool call, even auto-approved ones.

**Hooks:**
- **Programmatic hooks** in `query(options.hooks)`: callbacks for `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`, `PreCompact` (Python and TypeScript support these; TypeScript also supports `SessionStart`, `SessionEnd`, `TeammateIdle`, `TaskCompleted`). [code.claude.com/docs/en/agent-sdk/hooks.md](https://code.claude.com/docs/en/agent-sdk/hooks.md).
- **Filesystem hooks** from `~/.claude/settings.json` or `.claude/settings.json` auto-load when `settingSources` includes `"user"` or `"project"`.

**Custom Tools & MCP:**
- **In-process MCP servers** via `createSdkMcpServer()` (TypeScript) or `create_sdk_mcp_server()` (Python): define tools with the SDK and pass them in `mcpServers` option.
- **Custom tool definitions** with the `tool()` decorator (Python) or `tool()` function (TypeScript), typed with Zod schemas. [code.claude.com/docs/en/agent-sdk/custom-tools.md](https://code.claude.com/docs/en/agent-sdk/custom-tools.md).

**Sessions & Continuity:**
- Capture `session_id` from `ResultMessage` to resume later with `resume: session_id` or `continue: true`.
- `sessionStore` adapter (TypeScript) / `session_store` (Python) for remote session persistence across containers.

**Settings Loading (CLAUDE.md, Skills, Slash Commands):**
- **`settingSources`** option controls what the SDK loads: `["user"]` (from `~/.claude/`), `["project"]` (from `./.claude/` and parent dirs), `["local"]` (CLAUDE.local.md + settings.local.json). [code.claude.com/docs/en/agent-sdk/claude-code-features.md](https://code.claude.com/docs/en/agent-sdk/claude-code-features.md).
  - `CLAUDE.md` and `CLAUDE.local.md` load at session start.
  - `.claude/rules/*.md` load at session start.
  - Skills from `.claude/skills/` and `~/.claude/skills/` load on demand.
  - `.claude/settings.json` and `~/.claude/settings.json` (user + project): permissions, hooks (filesystem-based), MCP server configs, etc.
- **Slash commands** from `~/.claude/commands/` and `./.claude/commands/` (legacy; skills are preferred). Not a direct SDK option; loaded via `settingSources`.
- **Agents** from `.claude/agents/` and `~/.claude/agents/` (subagent definitions in YAML/JSON).

**Plugins & Mods (Function Hooks):**
- **`plugins` option** accepts `[{ type: "local", path: "./my-plugin" }]` to load plugins from disk. [code.claude.com/docs/en/agent-sdk/plugins.md](https://code.claude.com/docs/en/agent-sdk/plugins.md).
  - Plugins can include skills, agents, hooks, and `.mcp.json` (MCP server defs).
  - **Mods (function hooks)**: plugins with hooks modules (`.claude-plugin/hooks/hooks.json` naming a TypeScript/JavaScript module exporting `register(on, options)`). The SDK loads them. [code.claude.com/docs/en/plugins/mods/overview.md](https://code.claude.com/docs/en/plugins/mods/overview.md) describes mods; the plugin authoring reference confirms the contract.
  - **✓ YES: the Agent SDK supports loading mods.** Pass `plugins: [{ type: "local", path: "<mod folder>" }]` and the mod's hooks will register.

---

### **2. Login & Billing: API Key vs. Claude Pro Subscription**

**Agent SDK with Personal Use:**
- **Cannot use Claude Pro/Max subscription login.** The Agent SDK Quickstart [code.claude.com/docs/en/agent-sdk/quickstart.md](https://code.claude.com/docs/en/agent-sdk/quickstart.md) states: **"Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods described in this document instead."**
- **Must use `ANTHROPIC_API_KEY` environment variable** (pay-as-you-go). For third-party cloud platforms (Bedrock, Vertex AI, Foundry, Claude Platform on AWS), set the respective provider environment variables.
- **Personal app limitation:** even if you're running the app only on your own machine, the SDK is designed for "third-party developers," so Anthropic's terms restrict claude.ai login unless you've obtained explicit written approval.

**Headless CLI Alternative (`claude -p` with stream-json):**
- **Same billing restriction applies.** The CLI in `-p` mode also uses `ANTHROPIC_API_KEY` (or providers like Bedrock, etc.). There is no documented path to use your claude.ai login from `-p` headless mode; the login is for interactive CLI only.
- **Workaround mentioned but not endorsed:** Some users may hope that the CLI respects their claude.ai login cached locally. Not explicitly documented as supported by Anthropic.

---

### **3. Headless CLI as Alternative (`claude -p` with `--input-format` and `--output-format`)**

**Multi-Turn over stdin:**
- **`claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages`** supports long-lived streaming: [code.claude.com/docs/en/headless.md](https://code.claude.com/docs/en/headless.md#stream-responses).
- Send newline-delimited JSON on stdin; SDK emits streaming events one per line.
- **`--continue` or `--resume <session-id>`** to continue conversations.

**Interrupts/Control:**
- **`--permission-prompts none`** to deny all permission prompts in unattended runs.
- No explicit "interrupt mid-turn" flag for `-p`; you'd need to close stdin or send SIGINT (which ends the run without persisting the interrupted turn) or SIGTERM (graceful shutdown).

**Plugin Support:**
- **`--plugin-dir <path>` and `--plugin-url <url>`** load plugins (skills, agents, hooks, MCP). Yes, **mods load in `-p` mode** ([code.claude.com/docs/en/plugins/mods/overview.md](https://code.claude.com/docs/en/plugins/mods/overview.md) and plugin authoring reference confirm hooks run in headless sessions).

**Model Selection:**
- **`--model <id>`** to set the model once at start. No per-turn model switching in headless mode.

**Limitations:**
- `--bare` mode (no hooks, skills, CLAUDE.md auto-discovery) reduces context and startup time.
- More verbose output parsing required vs. Agent SDK (the SDK gives you typed messages).

---

### **4. Other Enablers & Constraints**

**Claude Desktop App Extensibility:**
- **No direct embedding API documented.** The Claude desktop app has a "Code" tab that runs Claude Code, but no published extensibility hooks for third-party native apps. [code.claude.com/docs/en/desktop.md](https://code.claude.com/docs/en/desktop.md) and [code.claude.com/docs/en/desktop-quickstart.md](https://code.claude.com/docs/en/desktop-quickstart.md) describe the desktop UI, not embedding.

**Remote Control:**
- Claude Code supports **Remote Control** (cloud session or desktop) for headless driving. Not documented as a native-app embedding mechanism, but could be used if your app spawns a remote session and drives it via HTTP/WebSocket. Not a documented Agent SDK feature.

**Rate Limits & Terms:**
- **API key usage:** subject to Anthropic's standard rate limits per API key.
- **Claude Pro subscription:** cannot be used with Agent SDK (as noted above).
- **Commercial terms:** [anthropic.com/legal/commercial-terms](https://www.anthropic.com/legal/commercial-terms) governs SDK use including "power products and services that you make available to your own customers and end users"; personal single-user use likely has less friction, but no special carve-out for it.

**Windows & macOS Support:**
- **Agent SDK:** Node.js and Python run on both; bundled Claude Code binary ships for Windows, macOS, and Linux (with some caveats for ARM64 Windows; check [code.claude.com/docs/en/agent-sdk/quickstart.md](https://code.claude.com/docs/en/agent-sdk/quickstart.md)).
- **Headless CLI:** `claude` CLI runs on both; native binaries distributed.

---

### **Summary for Jarvis Native App**

| Aspect | Agent SDK | Headless CLI (`-p`) | Managed Agents |
|--------|-----------|---------------------|-----------------|
| **Runs Claude Code** | ✓ Bundles binary | ✓ Invokes binary | ✗ Anthropic-hosted |
| **Multi-turn streaming** | ✓ Full control | ✓ `stream-json` | N/A |
| **Interrupts mid-turn** | ✓ (via signal, streaming input) | ~ (SIGINT/SIGTERM only) | N/A |
| **Per-turn model switch** | ✗ (feature request) | ✗ | N/A |
| **Permissions callbacks** | ✓ `canUseTool` | ✓ `--permission-prompt-tool` | N/A |
| **Hooks (function-based)** | ✓ Programmatic + FS | ✓ FS only (--hooks) | N/A |
| **Custom MCP tools** | ✓ In-process | ✗ (CLI tools only) | N/A |
| **Settings/CLAUDE.md/Skills** | ✓ `settingSources` | ✓ Auto-discovery | N/A |
| **Mods (hooks.json)** | ✓ Via plugins | ✓ Via --plugin-dir | N/A |
| **Login: claude.ai** | ✗ Requires approval | ✗ CLI-only | N/A |
| **Billing: API key** | ✓ Required | ✓ Required | N/A |
| **You host** | ✓ Your process | ✓ Your process | ✗ Anthropic hosts |

**Recommendation for Jarvis:**
- **Agent SDK (TypeScript or Python)** is the best fit for a native desktop voice assistant:
  - Full control over streaming and interruption for voice barge-in (via streaming input + `AbortSignal`).
  - In-process MCP servers for voice I/O integration.
  - Programmatic hooks for audio/turn lifecycle events.
  - Bundled Claude Code binary; you control startup and shutdown.
  - Requires API key (pay-as-you-go); cannot leverage claude.ai Pro subscription.

- **Headless CLI** if you want simpler integration: spawn `claude` as a subprocess, pipe JSON, parse responses. Less control over interruption and streaming.

- **Model routing per turn:** not available; use subagents to switch models (separate sessions) if needed, or avoid model switching.

---

**Sources:**
- [code.claude.com/docs/en/agent-sdk/overview.md](https://code.claude.com/docs/en/agent-sdk/overview.md)
- [code.claude.com/docs/en/agent-sdk/quickstart.md](https://code.claude.com/docs/en/agent-sdk/quickstart.md)
- [code.claude.com/docs/en/agent-sdk/streaming-output.md](https://code.claude.com/docs/en/agent-sdk/streaming-output.md)
- [code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md)
- [code.claude.com/docs/en/agent-sdk/user-input.md](https://code.claude.com/docs/en/agent-sdk/user-input.md)
- [code.claude.com/docs/en/agent-sdk/permissions.md](https://code.claude.com/docs/en/agent-sdk/permissions.md)
- [code.claude.com/docs/en/agent-sdk/hooks.md](https://code.claude.com/docs/en/agent-sdk/hooks.md)
- [code.claude.com/docs/en/agent-sdk/plugins.md](https://code.claude.com/docs/en/agent-sdk/plugins.md)
- [code.claude.com/docs/en/plugins/mods/overview.md](https://code.claude.com/docs/en/plugins/mods/overview.md)
- [code.claude.com/docs/en/agent-sdk/claude-code-features.md](https://code.claude.com/docs/en/agent-sdk/claude-code-features.md)
- [code.claude.com/docs/en/agent-sdk/agent-loop.md](https://code.claude.com/docs/en/agent-sdk/agent-loop.md)
- [code.claude.com/docs/en/headless.md](https://code.claude.com/docs/en/headless.md)
- [Plugin authoring reference (bundled types file)](file:///tmp/claude-0/bundled-skills/2.1.292/c10c8baefad44dcbceab6b3d84399fe8/plugin-authoring/reference.md)
- [claudeissues.com/issue/9173 (feature request: per-turn model routing)](https://claudeissues.com/issue/9173-feature-allow-specifying-a-specific-model-for-a-single-turn)
