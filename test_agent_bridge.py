"""Run: python test_agent_bridge.py  (transcript parsing only; touches no app)."""
import json
import tempfile
from pathlib import Path

import agent_bridge as b


def transcript(*lines):
    f = Path(tempfile.mkdtemp()) / "t.jsonl"
    f.write_text("".join(json.dumps(x) + "\n" for x in lines), "utf8")
    return f


def claude_msg(mid, kind, text="", stop="end_turn"):
    block = {"type": kind, kind: text} if kind == "thinking" else {"type": kind, "text": text}
    return {"type": "assistant", "message": {"id": mid, "stop_reason": stop, "content": [block]}}


# Claude: one answer split over a thinking line and a text line, both marked end_turn;
# the asked message arrived mid-turn as a queued_command attachment.
claude = transcript(
    {"type": "user", "message": {"content": "Run the sleep"}},
    {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "Busy check: say queued ok"}},
    claude_msg("m1", "thinking", "hmm"),
    claude_msg("m1", "text", "slept\nqueued ok"),
    {"type": "system", "subtype": "turn_duration"},
)
assert b._reply_since("claude_code", claude, 0, "Busy check") == "slept\nqueued ok"
assert b._reply_since("claude_code", claude, 0, "never sent") is None

# Final line still being written: no premature empty reply.
partial = transcript({"type": "user", "message": {"content": "hi there"}}, claude_msg("m2", "thinking", "..."))
assert b._reply_since("claude_code", partial, 0, "hi there") is None

# Codex: /goal shows up as thread_goal_updated, the turn ends with task_complete.
codex = transcript(
    {"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {"objective": "Count to 3"}}},
    {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "1 2 3"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>"}]}},
)
assert b._reply_since("codex", codex, 0, b._ws("/goal Count to 3")[:50]) == "1 2 3"
assert [k for k, _ in b._events("codex", codex)] == ["user", "end"]  # injected context is not a user message
print("ok")
