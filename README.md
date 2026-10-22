# agent-bridge

**Let Claude steer Codex, and Codex steer Claude, in the real desktop chats you already have open.**

<!-- mcp-name: io.github.EngMarchG/agent-bridge -->

![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)
![Windows](https://img.shields.io/badge/platform-Windows-0078D6)

<!-- demo.gif: Claude sends a steer into a running Codex chat; the answer is typed back into Claude's chat. -->

agent-bridge is a single-file MCP server for Windows. It lets one AI coding agent list, read and message
another agent's chats. It covers **Codex** in the ChatGPT desktop app, the **Claude desktop Code tab** and headless
**Claude Code CLI** sessions. It drives the desktop apps through Windows UI Automation, filling the same composer and
pressing the same buttons you would. No subprocess copy and no hidden session: the message lands in the chat you
are watching, with that chat's full context.

- Claude as PI, Codex as worker. Claude reads a long-running Codex chat and steers it mid-turn.
- Replies come back on their own. When the caller is itself a desktop chat, the answer is typed into the caller's chat when it is ready.
- It works in both directions. Register it in Codex and Codex can hand work to a Claude session.

## Tools

| Tool | What it does |
|---|---|
| `list_projects(app)` | Projects (folders) for `codex`, `claude_code` or `claude_cli`, most recently active first. |
| `list_chats(app, project)` | Chats in a project, newest first, with each Codex chat's goal status and each Claude chat's busy/idle state. |
| `read_chat(app, chat, last)` | The last messages of a chat: what was asked and each turn's final answer. |
| `send_message(app, text, chat?, project?, send_now?)` | Types into an existing chat, or starts a new one in any folder. A busy Codex chat gets the message as a Steer; a running `claude_cli` chat gets it in its live process (`send_now` presses the CLI's send-now key). Returns a `job_id`. |
| `wait_reply(job_id)` | Waits for the answer to a send. |

## Install

Requires Windows, Python 3.11+, and the Codex (ChatGPT) and/or Claude desktop apps installed and signed in.

**Claude Code**

```bash
claude mcp add -s user agent-bridge -- uvx agent-bridge-mcp
```

**Codex** (`~/.codex/config.toml`). Replies can take a while, so raise the tool timeout:

```toml
[mcp_servers.agent_bridge]
command = "uvx"
args = ["agent-bridge-mcp"]
tool_timeout_sec = 1800
```

**Any other MCP client**

```json
{ "mcpServers": { "agent-bridge": { "command": "uvx", "args": ["agent-bridge-mcp"] } } }
```

Without uv: `pip install agent-bridge-mcp`, then use `agent-bridge-mcp` as the command.
From source: `uvx --from git+https://github.com/EngMarchG/agent-bridge agent-bridge-mcp`.

## Example

> Read my Codex chat "Implement milestones" in project `bridges`, tell me where it is stuck, and steer it toward the failing test.

The calling agent runs `list_chats`, `read_chat` and then `send_message`. The answer arrives in its chat when Codex finishes the turn.

## How it works

- **Listing and reading** opens each app's own local files read-only: Codex's SQLite state and rollouts, and Claude's session JSON and JSONL transcripts.
- **Sending** goes through UI Automation. The bridge first switches the ChatGPT app to Codex mode, then opens the chat from the sidebar. It handles a split Claude window. A busy Codex chat shows Queue and then Steer, and the bridge clicks Steer. Afterwards it puts back the chats you were looking at.
- **Replies.** Every send starts a detached watcher. The watcher waits for the turn to end in the transcript and stores the reply in `~/.agent-bridge/jobs/` (set `AGENT_BRIDGE_HOME` to move it). If the caller is a desktop chat, the watcher types the reply into it.
- **`claude_cli`** runs `claude -p --resume` headless with the `permission_mode` you pass, in stream-json mode (`--input-format stream-json --output-format stream-json`). The watcher owns the process and keeps its stdin open.
- **Mid-run messages (`claude_cli`).** A chat runs one process at a time. A send to a chat whose run is still going does not start a second process: it goes into the running one's stdin within about a second, and the run takes it in, usually in the same turn. The job shows `into` (the run's job id), and both jobs get the run's last result as their reply. With `send_now=true` the bridge also sends the CLI's send-now control, so the running turn makes way for the message at once instead of after the tool call under way. When a result has stood a few seconds with nothing new, the bridge closes stdin and the process exits; a later send resumes the chat as usual.

## Limits and safety

- **Windows only.** It depends on UI Automation, and on the apps' current labels and layout, so an app update can break it.
- **It types into your real chats** and briefly brings the app window forward. Don't type in the target window during a send.
- **Run one call at a time.** Parallel calls drive the same UI and collide. A file lock serialises calls across processes.
- **Wake the window if needed.** If a send fails with "no window found", the Chromium window has its accessibility tree off. Click into the app once.
- The bridge never accepts "Trust this workspace?" for you. It never overwrites a draft that's already in the composer.
- **Prompt injection.** Text in one agent's reply becomes input for another. Treat chains of agents with the same care as any other untrusted input.

## Development

```bash
python test_agent_bridge.py
```

The test checks transcript parsing only and touches no app.

## License

MIT
