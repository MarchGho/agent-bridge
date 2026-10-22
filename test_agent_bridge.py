"""Run: python test_agent_bridge.py  (transcript parsing only; touches no app)."""
import json
import queue
import tempfile
import threading
import time
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

# Code-tab sessions copied between two accounts sit in both <account>/<org> folders with one sessionId. Each is
# listed once, as the copy in the folder the app wrote last (the signed-in account): its isArchived and title count.
desk = b.DESKTOP = Path(tempfile.mkdtemp())
b._claude_live = lambda: {}


def session(folder, sid, title, act, archived=False, mtime=0):
    f = desk / folder / f"local_{sid}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"sessionId": sid, "cliSessionId": "cli-" + sid, "title": title, "cwd": "C:\\work",
                             "createdAt": 1000, "lastActivityAt": act, "isArchived": archived}), "utf8")
    b.os.utime(f, (mtime, mtime))


session("old_acct/org", "a1", "review", 5_000, mtime=100)  # copy left behind, newer activity
session("new_acct/org", "a1", "review", 4_000, mtime=200)  # the signed-in account's copy
session("old_acct/org", "gone", "archived here only", 4_000, mtime=100)
session("new_acct/org", "gone", "archived here only", 4_000, archived=True, mtime=300)
listed = b._sessions("claude_code")
assert [s["id"] for s in listed] == ["a1"] and listed[0]["shown"], listed
assert b._chat("claude_code", "a1")["title"] == "review" and b._chat("claude_code", "cli-a1")["id"] == "a1"
# Different sessions with one title (forks keep their parent's): the sidebar's one wins and the note names the others.
session("new_acct/org", "fork_new", "design", 9_000, mtime=200)
session("new_acct/org", "fork_old", "design", 8_000, mtime=200)
session("old_acct/org", "fork_hidden", "design", 9_500, mtime=100)  # newest, but not in the sidebar
s = b._chat("claude_code", "Design")
assert s["id"] == "fork_new" and "3 claude_code chats" in s["note"] and "fork_hidden" in s["note"]


class FakeProc:
    """A stream-json `claude -p`: init, then one result per user line (each after `gate`, if set); EOF after stdin
    closes and every line is answered, as the CLI finishes its queued turns before it exits."""
    gate, procs, pid = None, [], 1

    def __init__(self, cmd, **kw):
        self.cmd, self.got, self.controls, self.closed, self.returncode = cmd, [], [], False, None
        self.todo, self.out = queue.Queue(), queue.Queue()
        self.stdin, self.stdout = self, iter(self.out.get, None)
        FakeProc.procs.append(self)
        self.out.put(json.dumps({"type": "system", "subtype": "init", "session_id": "c1"}) + "\n")
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        for text in iter(self.todo.get, None):
            if self.gate:
                self.gate.wait()
            self.out.put(json.dumps({"type": "result", "session_id": "c1", "result": "re " + text}) + "\n")
        self.out.put(None)

    def write(self, s):
        o = json.loads(s)
        (self.got.append(o["message"]["content"]) or self.todo.put(o["message"]["content"])) if o["type"] == "user" \
            else self.controls.append(o["request"])

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self.todo.put(None)

    def wait(self, timeout=None):
        self.returncode = 0

    kill = close


def until(ok, s=10):
    end = time.time() + s
    while not ok():
        assert time.time() < end, "timed out"
        time.sleep(0.05)


# claude_cli: a send to a chat whose process is running goes into its stdin within 2 s: no second process, and both
# jobs get the run's last result. Then the idle chat resumes in a new process; a dead run's inbox falls back to that.
b.JOBS = Path(tempfile.mkdtemp())
b.LIVE, b.GRACE_S, b.subprocess.Popen = b.JOBS / "live", 0.5, FakeProc
b._chat = lambda app, chat, project=None: {"id": "c1", "title": "t", "cwd": None, "project": ""}
b._spawn_watch = lambda job_id: threading.Thread(target=b._watch, args=(job_id,), daemon=True).start()
FakeProc.gate = threading.Event()
j1 = b._send("claude_cli", "first", chat="c1", notify_caller=False)
until(lambda: (b.LIVE / "c1.inbox").exists() and FakeProc.procs[0].got == ["first"])
assert FakeProc.procs[0].cmd[2:7] == ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
j2 = b._send("claude_cli", "second", chat="c1", notify_caller=False, send_now=True)
assert j2["into"] == j1["id"] and len(FakeProc.procs) == 1
until(lambda: FakeProc.procs[0].got == ["first", "second"] and FakeProc.procs[0].controls, s=2)
assert FakeProc.procs[0].controls[0]["send_now"] is True and not FakeProc.procs[0].closed
FakeProc.gate.set()
until(lambda: b._load(j2["id"])["status"] == "done")
assert b._load(j1["id"])["reply"] == b._load(j2["id"])["reply"] == "re second" and FakeProc.procs[0].closed
j3 = b._send("claude_cli", "third", chat="c1", notify_caller=False)
until(lambda: b._load(j3["id"])["status"] == "done")
assert len(FakeProc.procs) == 2 and FakeProc.procs[1].cmd[-2:] == ["--resume", "c1"] and "into" not in j3
(b.LIVE / "c1.inbox").write_text(json.dumps({"job": "dead"}) + "\n", "utf8")  # left by a killed watcher
j4 = b._send("claude_cli", "fourth", chat="c1", notify_caller=False)
until(lambda: b._load(j4["id"])["status"] == "done")
assert "into" not in j4 and len(FakeProc.procs) == 3 and b._load(j4["id"])["reply"] == "re fourth"
lock = b._hold("c1")  # another process holds the chat: a resume waits, never two at once
j5 = b._send("claude_cli", "fifth", chat="c1", notify_caller=False)
time.sleep(1)
assert len(FakeProc.procs) == 3
b._release(lock)
until(lambda: b._load(j5["id"])["status"] == "done")
assert len(FakeProc.procs) == 4
print("ok")
