"""agent_bridge: an MCP server that lets one agent drive another.

Targets (the `app` argument):
  codex        Codex in the ChatGPT desktop app (ChatGPT.exe): projects, threads and their goal status.
  claude_code  Claude desktop app, Code tab sessions, including a split window (one chat per pane).
  claude_cli   Claude Code CLI sessions, run headless with `claude -p --resume`.

Listing reads each app's own files, opened read-only. Sending in the desktop apps goes through
Windows UI Automation: it fills the same composer and presses the same buttons a person would.
Local Codex projects only show in the app's Codex mode, so Codex actions flip the ChatGPT / Codex switch
first. A busy Codex chat offers Queue instead of Send; the queued message then shows Steer, which the
bridge clicks so the message joins the running turn. A busy Claude session takes it mid-turn on Send.
Every send starts a detached watcher (`agent_bridge.py watch <job>`) that waits for the answer,
stores it in ~/.agent-bridge/jobs/<job>.json (or $AGENT_BRIDGE_HOME/jobs) and, when the caller is
itself a desktop chat, types the answer into the caller's chat.

Register: see README.md. Check the transcript parsing: python test_agent_bridge.py
"""
import ctypes
import functools
import inspect
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path

import anyio
import psutil
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

WINDOWS = sys.platform == "win32"
if WINDOWS:
    import msvcrt
    from _ctypes import COMError

    with redirect_stdout(sys.stderr):  # comtypes prints while generating its wrappers; stdout belongs to MCP
        import uiautomation as auto
        from uiautomation.uiautomation import _AutomationClient
    BUTTON, EDIT, DOCUMENT, PANE, WINDOW = (auto.ControlType.ButtonControl, auto.ControlType.EditControl,
                                            auto.ControlType.DocumentControl, auto.ControlType.PaneControl,
                                            auto.ControlType.WindowControl)
else:  # elsewhere the server still starts and lists its tools (MCP directories check it in Linux Docker)
    COMError = OSError
    BUTTON = EDIT = DOCUMENT = PANE = WINDOW = None

HOME = Path.home()
CODEX = HOME / ".codex"
CLAUDE = HOME / ".claude"
# Current desktop apps keep their data in %LOCALAPPDATA%\Claude-Data\<id>. A Store (MSIX) install keeps
# %APPDATA%\Claude in its package folder; only processes inside the package see the redirected path, so a bridge
# started by Codex must use the real one.
_LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
DESKTOP = next(iter([*_LOCAL.glob("Claude-Data/*/claude-code-sessions"),
                     *_LOCAL.glob("Packages/Claude_*/LocalCache/Roaming/Claude/claude-code-sessions")]),
               Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code-sessions")
JOBS = Path(os.environ.get("AGENT_BRIDGE_HOME") or HOME / ".agent-bridge") / "jobs"
LIVE = JOBS / "live"  # claude_cli chats: <chat>.lock held by the watcher whose process runs it, <chat>.inbox
GRACE_S = 3  # a run's stdin closes once its last result stood this long with no new message
APPS = ("codex", "claude_code", "claude_cli")
WINDOW_EXE = {"codex": "chatgpt.exe", "claude_code": "claude.exe"}
RENAME = ", rename session"  # a Claude chat pane's title button: "<title>, rename session"
REPLY_CAP = 8000
WATCH_HOURS = 8

mcp = MCPServer("agent-bridge", instructions=(
    "Drive other agents on this PC. app = 'codex' (Codex in the ChatGPT app), 'claude_code' (Claude "
    "desktop Code tab) or 'claude_cli' (headless Claude CLI). Flow: list_projects -> list_chats -> "
    "send_message. send_message returns a job_id. If you are a desktop chat, the reply is typed into "
    "your chat when it is ready; otherwise call wait_reply(job_id)."))


# ---------- storage (read-only) ----------

def _ws(s):
    return " ".join((s or "").split())


def _path(p):
    return (p or "").removeprefix("\\\\?\\").rstrip("\\/")


def _key(p):
    return _path(p).lower()


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else None


def _q(db, sql, args=()):
    with closing(sqlite3.connect(f"file:{(CODEX / db).as_posix()}?mode=ro", uri=True)) as c:
        return c.execute(sql, args).fetchall()


def _codex_projects():
    seen, out = set(), []
    for pid, name, root in _q("state_5.sqlite", "select p.id, p.name, r.path from projects p join project_roots r "
                              "on r.project_id = p.id order by p.position, r.position"):
        if _key(root) not in seen:
            seen.add(_key(root))
            out.append({"id": pid, "name": name, "path": _path(root)})
    return out


def _codex_threads():
    goals = dict(_q("goals_1.sqlite", "select thread_id, status from thread_goals"))
    rows = _q("state_5.sqlite", "select id, coalesce(nullif(name, ''), nullif(title, ''), substr(first_user_message, 1, 60)), "
              "cwd, project_id, created_at, updated_at, rollout_path from threads where archived = 0 "
              "and coalesce(thread_source, 'user') = 'user' order by updated_at desc")
    return [{"id": i, "title": t or "", "cwd": _path(c), "project_id": p, "created": cr, "updated": u,
             "transcript": _path(r), "goal": goals.get(i)} for i, t, c, p, cr, u, r in rows]


def _claude_live():
    """cli session id -> 'busy' / 'idle' for Claude Code processes that are running now."""
    out = {}
    for f in (CLAUDE / "sessions").glob("*.json"):
        try:
            o = json.loads(f.read_text("utf8"))
        except (OSError, ValueError):
            continue
        if o.get("pid") and psutil.pid_exists(o["pid"]):
            out[o.get("sessionId")] = o.get("status") or "running"
    return out


def _desktop_sessions():
    """One entry per session. DESKTOP holds a folder per <account>/<org>, and a session copied between accounts keeps
    its sessionId in both, so it would be listed twice and even its id would match 2 chats. The copy that counts is
    the one in the folder the app wrote last (the signed-in account, whose sidebar is on screen), else the newest;
    that copy's isArchived decides."""
    live, copies, best = _claude_live(), [], {}
    for f in DESKTOP.glob("*/*/local_*.json"):
        try:
            copies.append((f.parent, f.stat().st_mtime, json.loads(f.read_text("utf8"))))
        except (OSError, ValueError):
            continue
    shown = max(copies, key=lambda c: c[1], default=(None,))[0]
    for folder, _, o in copies:
        rank = (folder == shown, o.get("lastActivityAt") or 0)
        if o["sessionId"] not in best or rank > best[o["sessionId"]][0]:
            best[o["sessionId"]] = rank, o
    out = []
    for (in_sidebar, _), o in best.values():
        if o.get("isArchived"):
            continue
        cli = o.get("cliSessionId")
        out.append({"id": o["sessionId"], "cli_id": cli, "title": o.get("title") or "", "cwd": _path(o.get("cwd")),
                    "created": (o.get("createdAt") or 0) / 1000, "updated": (o.get("lastActivityAt") or 0) / 1000,
                    "status": live.get(cli, "closed"), "shown": in_sidebar})
    return sorted(out, key=lambda s: -s["updated"])


def _cli_sessions():
    live, out = _claude_live(), []
    for f in (CLAUDE / "projects").glob("*/*.jsonl"):
        cwd = entry = None
        with open(f, encoding="utf8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 40 or (cwd and entry):
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                cwd, entry = cwd or o.get("cwd"), entry or o.get("entrypoint")
        if entry in ("cli", "sdk-cli") and cwd:  # sdk-cli = headless `claude -p`, including this bridge's runs
            out.append({"id": f.stem, "title": None, "cwd": _path(cwd), "updated": f.stat().st_mtime,
                        "status": live.get(f.stem, "closed"), "transcript": str(f)})
    return sorted(out, key=lambda s: -s["updated"])


def _cli_title(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 262144))
        tail = f.read().decode("utf8", "replace")
    for line in reversed(tail.splitlines()):
        if '"custom-title"' in line or '"ai-title"' in line:
            try:
                o = json.loads(line)
                return o.get("customTitle") or o.get("aiTitle") or o.get("title")
            except ValueError:
                pass
    for kind, text in _events("claude_cli", path):
        if kind == "user":
            return _ws(text)[:60]
    return ""


def _sessions(app):
    """Every app's chats, newest first, each id once: a chat listed twice makes even its own id match 2 chats.
    The first, newest copy stays."""
    if app not in APPS:
        raise ValueError(f"app must be one of {APPS}")
    first = {}
    for s in {"codex": _codex_threads, "claude_code": _desktop_sessions, "claude_cli": _cli_sessions}[app]():
        first.setdefault(s["id"], s)
    return list(first.values())


def _in_project(app, s, p):
    return _key(s["cwd"]) == _key(p["path"]) or (app == "codex" and s.get("project_id") == p.get("id"))


def _projects(app):
    sessions = _sessions(app)
    if app == "codex":
        projects = _codex_projects()
    else:
        projects = list({_key(s["cwd"]): {"name": Path(s["cwd"]).name, "path": s["cwd"]} for s in reversed(sessions)}.values())
    for p in projects:
        mine = [s for s in sessions if _in_project(app, s, p)]
        p.update(chats=len(mine), last_active=max((s["updated"] for s in mine), default=0))
    return sorted(projects, key=lambda p: -p["last_active"])


def _project(app, project):
    projects = _projects(app)
    for p in projects:
        if project.lower() == p["name"].lower() or _key(project) == _key(p["path"]):
            return p
    if Path(project).is_dir():  # any folder works for a new chat; Codex gets a project made for it
        return {"name": Path(project).resolve().name, "path": str(Path(project).resolve())}
    raise ValueError(f"no {app} project {project!r} and no such folder; known: {sorted({p['name'] for p in projects})}")


def _chat(app, chat, project=None):
    sessions = _sessions(app)
    if project:
        p = _project(app, project)
        sessions = [s for s in sessions if _in_project(app, s, p)]
    hit = [s for s in sessions if chat in (s["id"], s.get("cli_id"))]
    if not hit:
        if app == "claude_cli":
            for s in sessions:
                s["title"] = _cli_title(s["transcript"])
        hit = [s for s in sessions if s["title"].lower() == chat.strip().lower()]
    if not hit:
        raise ValueError(f"no {app} chat matches {chat!r}; pass a chat id or exact title from list_chats")
    # Forks and child sessions keep their parent's title: take the one in the app's sidebar, else the newest
    # (sessions come newest first), and say which.
    hit.sort(key=lambda s: not s.get("shown", True))
    s = hit[0]
    if len(hit) > 1:
        s["note"] = (f"{len(hit)} {app} chats are titled {chat!r}; took {s['id']} ({'in the sidebar, ' if s.get('shown') else ''}"
                     f"updated {_iso(s['updated'])}). The others: {[x['id'] for x in hit[1:]]}; pass an id to pick one.")
    if app == "codex":
        s["project"] = next((p["name"] for p in _codex_projects() if _in_project(app, s, p)), "")
    else:
        s["project"] = Path(s["cwd"]).name
    return s


def _transcript(app, s):
    if app == "claude_code":
        return next((CLAUDE / "projects").glob(f"*/{s['cli_id']}.jsonl"), None) if s.get("cli_id") else None
    return Path(s["transcript"])


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or []
                     if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text"))


def _events(app, path, start=0):
    """Yield ('user', text) for messages a person (or this bridge) sent and ('end', final answer) per turn."""
    msg_id, parts, ending = None, [], False
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            try:
                o = json.loads(raw)
            except ValueError:  # partial line at the seek point or at the end
                continue
            if app == "codex":
                p = o.get("payload") or {}
                if o.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                    t = _text(p.get("content"))
                    if t.strip() and not t.lstrip().startswith(("<", "# AGENTS.md")):
                        yield "user", t
                elif o.get("type") == "event_msg" and p.get("type") == "thread_goal_updated":  # a /goal message
                    yield "user", "/goal " + ((p.get("goal") or {}).get("objective") or "")
                elif o.get("type") == "event_msg" and p.get("type") == "task_complete":
                    yield "end", p.get("last_agent_message") or ""
                elif o.get("type") == "event_msg" and p.get("type") == "turn_aborted":
                    yield "end", "(turn stopped before it finished)"
                continue
            # Claude writes one line per content block (thinking, text, ...) and every line of the final
            # message carries the end stop_reason, so the turn ends once a line from another entry follows.
            m, att = o.get("message") or {}, o.get("attachment") or {}
            if ending and (o.get("type") != "assistant" or m.get("id") != msg_id):
                ending = False
                yield "end", "\n".join(parts)
            if att.get("type") == "queued_command":  # a message sent while the session was busy, absorbed mid-turn
                yield "user", att.get("prompt") or ""
            elif o.get("type") == "user" and not o.get("isMeta"):
                t = _text(m.get("content"))
                if t.strip() and not t.lstrip().startswith("<"):
                    yield "user", t
            elif o.get("type") == "assistant":
                if m.get("id") != msg_id:
                    msg_id, parts = m.get("id"), []
                if t := _text(m.get("content")):
                    parts.append(t)
                ending = m.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens")
    if ending and parts:  # last thing in the file; only trust it once its text has been written
        yield "end", "\n".join(parts)


def _reply_since(app, path, start, hint):
    seen = False
    for kind, text in _events(app, path, start):
        if kind == "user" and hint in _ws(text):
            seen = True
        elif kind == "end" and seen:
            return text
    return None


def _goal(thread_id):
    rows = _q("goals_1.sqlite", "select status, objective, tokens_used, token_budget, time_used_seconds, updated_at_ms "
              "from thread_goals where thread_id = ?", (thread_id,))
    if not rows:
        return None
    status, objective, used, budget, secs, upd = rows[0]
    return {"status": status, "objective": objective, "tokens_used": used, "token_budget": budget,
            "hours_used": round(secs / 3600, 2), "updated": _iso(upd / 1000)}


# ---------- desktop UI (UI Automation) ----------

@contextmanager
def _gui():
    """One GUI action at a time across every agent_bridge process, with COM ready on this thread."""
    if not WINDOWS:
        raise RuntimeError("agent-bridge drives the Windows desktop apps; this action needs Windows")
    JOBS.mkdir(parents=True, exist_ok=True)
    with open(JOBS / "gui.lock", "a+") as f:
        end = time.time() + 180
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() > end:
                    raise RuntimeError("another agent_bridge GUI action has held the lock for 3 minutes")
                time.sleep(0.5)
        try:
            with auto.UIAutomationInitializerInThread():
                yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _cond(name=None, ctype=None):
    ua = _AutomationClient.instance().IUIAutomation
    cs = [ua.CreatePropertyCondition(auto.PropertyId.NameProperty, name)] if name is not None else []
    if ctype:
        cs.append(ua.CreatePropertyCondition(auto.PropertyId.ControlTypeProperty, ctype))
    c = cs[0] if cs else ua.CreateTrueCondition()
    for extra in cs[1:]:
        c = ua.CreateAndCondition(c, extra)
    return c


def _all(root, name=None, ctype=None):
    try:
        arr = root.Element.FindAll(4, _cond(name, ctype))  # 4 = TreeScope_Descendants
    except COMError:  # root went stale while the page re-rendered
        return []
    out = []
    for i in range(arr.Length if arr else 0):
        try:
            out.append(auto.Control.CreateControlFromElement(arr.GetElement(i)))
        except COMError:
            pass
    return out


def _find(root, name=None, ctype=None, pred=None, timeout=5.0):
    end = time.time() + timeout
    while True:
        for c in _all(root, name, ctype):
            if pred is None or pred(c):
                return c
        if time.time() >= end:
            return None
        time.sleep(0.5)


def _window(app):
    exe = WINDOW_EXE[app]
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == exe}
    for w in auto.GetRootControl().GetChildren():
        if w.ProcessId in pids and w.ClassName == "Chrome_WidgetWin_1":
            if auto.IsIconic(w.NativeWindowHandle):
                auto.ShowWindow(w.NativeWindowHandle, auto.SW.Restore)
            if _find(w, ctype=DOCUMENT, timeout=4) or _wake(w):  # the first query switches Chromium's accessibility on
                if app == "codex":
                    _codex_mode(w)
                return w
    raise RuntimeError(f"no {app} window found, or it exposes no page (click into it once); open the app first")


def _wake(w):
    """Chromium sometimes keeps its page tree off until the window is clicked: ask the page widget itself, then
    bring the window forward (attaching to the foreground thread lifts Windows' focus-steal lock)."""
    u32 = ctypes.windll.user32
    if page := u32.FindWindowExW(w.NativeWindowHandle, 0, "Chrome_RenderWidgetHostHWND", None):
        auto.ControlFromHandle(page).GetChildren()
        if _find(w, ctype=DOCUMENT, timeout=4):
            return True
    fg_thread = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    u32.AttachThreadInput(me, fg_thread, True)
    try:
        u32.SetForegroundWindow(w.NativeWindowHandle)
    finally:
        u32.AttachThreadInput(me, fg_thread, False)
    return _find(w, ctype=DOCUMENT, timeout=6) is not None


def _codex_mode(win):
    """Codex projects and threads only show in Codex mode (the ChatGPT / Codex switch at the top left)."""
    is_codex = lambda c: c.Name == "Switch mode, current mode: Codex"
    sw = _find(win, ctype=BUTTON, pred=lambda c: c.Name.startswith("Switch mode, current mode:"), timeout=3)
    if not sw or is_codex(sw):
        return
    ec = sw.GetPattern(auto.PatternId.ExpandCollapsePattern)
    if ec.ExpandCollapseState == auto.ExpandCollapseState.Collapsed:
        ec.Expand()
    item = _find(win, ctype=auto.ControlType.MenuItemControl, pred=lambda c: c.Name.startswith("Codex"), timeout=3)
    if not item:
        raise RuntimeError("no Codex entry in the ChatGPT / Codex mode switch")
    item.GetInvokePattern().Invoke()
    if not _find(win, ctype=BUTTON, pred=is_codex, timeout=8):
        raise RuntimeError("could not switch the app to Codex mode")


def _must(root, name, ctype, timeout=5.0):
    c = _find(root, name=name, ctype=ctype, timeout=timeout)
    if not c:
        raise RuntimeError(f"no {name!r} on screen")
    return c


def _press(c):
    """Menu buttons expand instead of invoking."""
    if p := c.GetInvokePattern():
        p.Invoke()
    else:
        c.GetPattern(auto.PatternId.ExpandCollapsePattern).Expand()


def _doc(win):
    d = _find(win, ctype=DOCUMENT, timeout=2)
    return d.Name if d else ""


def _panes(win):
    """A Claude window can be split: (title, pane) for each chat pane; a new-session page has title ''.
    (Its documents are unnamed or name only one pane, so they cannot tell which chats are on screen.)"""
    out = []
    for e in _all(win, name="Prompt", ctype=EDIT):
        box = e
        while not (t := _find(box, ctype=BUTTON, pred=lambda c: c.Name.endswith(RENAME), timeout=0)):
            up = box.GetParentControl()
            if up is None or up.NativeWindowHandle == win.NativeWindowHandle or len(_all(up, name="Prompt", ctype=EDIT)) > 1:
                break
            box = up
        out.append((t.Name.removesuffix(RENAME) if t else "", box))
    return out


def _view(app, win):
    """Titles of the chats on screen."""
    return [t for t, _ in _panes(win)] if app == "claude_code" else [_doc(win)]


def _root(app, win, title):
    """Where chat `title` ('' = a new chat) takes input: the Codex window, or that chat's Claude pane."""
    if app == "codex":
        return win
    end = time.time() + 5
    while not (box := next((b for t, b in _panes(win) if t == title), None)):
        if time.time() > end:
            raise RuntimeError(f"{title or 'the new session'!r} is not open in any claude_code pane")
        time.sleep(0.5)
    return box


def _sidebar_button(app, win, title, project=""):
    if app == "codex":
        match = lambda c: c.Name == title
    else:  # "Idle <title>", "Running <title>", "Unread response <title>"
        match = lambda c: c.Name.endswith(" " + title) and not c.Name.startswith(("More options for", "New session in"))
    for _ in range(2):
        hits = [c for c in _all(win, ctype=BUTTON) if match(c)]
        # Only chats the sidebar lists count (claude_code: the signed-in account's); a pinned chat shows twice and
        # both rows open it. Same-titled rows of different chats look alike: the bridge cannot click the right one.
        twins = [s["id"] for s in _sessions(app) if s["title"] == title and s.get("shown", True)]
        if hits and len(twins) <= 1:
            return hits[0]
        if hits:
            raise RuntimeError(f"{len(twins)} {app} chats in the sidebar are titled {title!r} ({twins}) and their rows look "
                               "alike; rename the one you mean in the app, then send again. Nothing was sent.")
        folder = _find(win, name=project, ctype=BUTTON, timeout=0.5) if project else None
        ec = folder.GetPattern(auto.PatternId.ExpandCollapsePattern) if folder else None
        if not ec or ec.ExpandCollapseState != auto.ExpandCollapseState.Collapsed:
            break
        ec.Expand()
        time.sleep(1)
    raise RuntimeError(f"{title!r} is not in the {app} sidebar (folder collapsed or chat too old); open it once by hand")


def _open_chat(app, win, title, project=""):
    if title in _view(app, win):  # already on screen, in any pane
        return
    _sidebar_button(app, win, title, project).GetInvokePattern().Invoke()  # opens in the focused pane
    end = time.time() + 10
    while title not in _view(app, win):
        if time.time() > end:
            raise RuntimeError(f"clicked {title!r} in the {app} sidebar but it did not open")
        time.sleep(0.4)


def _pick_folder(win, path):
    """Fill in the Windows folder picker the app just opened (both apps use the same one)."""
    is_dlg = lambda c: c.ClassName == "#32770"
    d = _find(win, ctype=WINDOW, pred=is_dlg, timeout=8)
    if not d:
        raise RuntimeError("the folder picker did not open")
    _find(d, ctype=EDIT, pred=lambda c: c.AutomationId == "1152").GetValuePattern().SetValue(path)  # "Folder:"
    _must(d, "Select Folder", BUTTON).GetInvokePattern().Invoke()
    end = time.time() + 5
    while _find(win, ctype=WINDOW, pred=is_dlg, timeout=0):
        if time.time() > end:
            _must(d, "Cancel", BUTTON).GetInvokePattern().Invoke()  # a left-open picker blocks the whole app
            raise RuntimeError(f"the folder picker did not accept {path}")
        time.sleep(0.3)


def _new_chat(app, win, folder):
    """Start a new chat in `folder` (a _project dict), or with no folder."""
    if app == "codex":
        name = folder and folder["name"]
        if folder and not folder.get("id"):  # Codex has no project for this folder yet: create one
            _must(win, "Add new project", BUTTON).GetInvokePattern().Invoke()
            dlg = _must(win, "Create project", WINDOW)
            _must(dlg, "Add", BUTTON).GetInvokePattern().Invoke()
            _pick_folder(win, folder["path"])
            box = _must(dlg, "Project name", EDIT)
            if not _value(box).strip():
                box.GetValuePattern().SetValue(name)
            name = _value(box).strip()
            _must(dlg, "Create project", BUTTON).GetInvokePattern().Invoke()
        _must(win, f"Start new chat in {name}" if folder else "New chat", BUTTON, timeout=10).GetInvokePattern().Invoke()
        time.sleep(1.5)
        return
    _must(win, "New", BUTTON).GetInvokePattern().Invoke()
    env = _must(_root(app, win, ""), "Local", BUTTON)  # the bar reads: Local, <folder>, branch, Add another folder
    bar = env.GetParentControl()
    _press([c for c in bar.GetChildren() if c.ControlType == BUTTON][1])
    if folder:
        _must(win, "Open folder…", auto.ControlType.MenuItemControl).GetInvokePattern().Invoke()
        _pick_folder(win, folder["path"])
        if trust := _find(win, name="Trust this workspace?", ctype=WINDOW, timeout=1.5):  # trusting is the user's call
            _must(trust, "Cancel", BUTTON).GetInvokePattern().Invoke()
            raise RuntimeError(f"Claude asks whether to trust {folder['path']}; start one session there by hand first")
    else:
        _must(win, "No folder", auto.ControlType.RadioButtonControl).GetInvokePattern().Invoke()
    _must(bar, folder["name"] if folder else "No folder", BUTTON)  # the bar now names the chosen folder


def _is_composer(app, c):
    return c.Name == "Prompt" if app == "claude_code" else "ProseMirror" in c.ClassName and c.Name != "Goal"


def _composer(app, root):
    e = _find(root, ctype=EDIT, pred=lambda c: _is_composer(app, c))
    if not e:
        raise RuntimeError(f"no {app} composer found")
    return e


def _value(e):
    return e.GetValuePattern().Value or ""


def _empty(e):
    """Codex's composer reports its placeholder text as its value."""
    return _ws(_value(e)) in ("", _ws(e.Name))


def _front(win, e):
    win.SetActive()
    e.SetFocus()
    time.sleep(0.3)
    if auto.GetForegroundWindow() != win.NativeWindowHandle:
        raise RuntimeError("could not bring the app to the front, so nothing was typed")


def _put(win, e, text):
    if not _empty(e):
        raise RuntimeError("the composer already holds a draft; not overwriting it")
    e.GetValuePattern().SetValue(text)
    time.sleep(0.4)
    if _ws(text)[:40] in _ws(_value(e)):
        return
    _front(win, e)  # SetValue did not reach the editor: paste instead
    old = auto.GetClipboardText()
    auto.SetClipboardText(text)
    auto.SendKeys("{Ctrl}v", waitTime=0.4)
    auto.SetClipboardText(old or "")
    if _ws(text)[:40] not in _ws(_value(e)):
        raise RuntimeError("could not put the text into the composer")


def _steer(win, text):
    """A busy chat queues the message and shows Steer next to it: click it so the message joins the running turn."""
    hint, best = _ws(text)[:40], None
    end = time.time() + 3
    while best is None and time.time() < end:
        for btn in _all(win, name="Steer", ctype=BUTTON):
            box = btn.GetParentControl()
            for level in range(4):  # the nearest box holding our text is our queued row
                if box is None or (best and level >= best[0]):
                    break
                if any(hint in _ws(t.Name) for t in _all(box)):
                    best = (level, btn)
                    break
                box = box.GetParentControl()
        if best is None:
            time.sleep(0.5)
    if best:
        best[1].GetInvokePattern().Invoke()


def _submit(app, win, root, e, text):
    prev = auto.GetForegroundWindow()
    send = _find(root, ctype=BUTTON, pred=lambda c: c.Name in ("Send", "Queue"), timeout=1)
    queued = send is None or send.Name == "Queue"
    if send:  # clicking needs no focus; a busy Codex chat offers Queue instead of Send
        send.GetInvokePattern().Invoke()
    else:  # e.g. only Stop is shown while a turn runs: Enter queues the message
        _front(win, e)
        auto.SendKeys("{Enter}", waitTime=0.3)
    hint, end = _ws(text)[:40], time.time() + 8
    while True:
        try:  # a new Claude session's pane is rebuilt on send, so look in every composer
            if not any(hint in _ws(_value(c)) for c in _all(win, ctype=EDIT) if _is_composer(app, c)):
                break
        except COMError:  # the composer re-renders after a send
            pass
        if time.time() > end:
            raise RuntimeError("pressed Send but the text is still in the composer")
        time.sleep(0.3)
    if queued:
        _steer(win, text)
    if prev and prev != win.NativeWindowHandle and auto.GetForegroundWindow() != prev:
        auto.SetForegroundWindow(prev)


def _restore(app, win, before):
    """Best effort: put back the chats the user was looking at (`before` = _view)."""
    for _ in range(4):  # right after Send / Save the sidebar re-renders and briefly reads as empty
        lost = [t for t in before if t and t not in _view(app, win)]
        if not lost:
            return
        try:
            _open_chat(app, win, lost[0])  # lands in the focused pane, the one the bridge just used
        except RuntimeError:
            time.sleep(1)


def _gui_send(app, text, title=None, project="", folder=None):
    """Open chat `title` (or a new chat in `folder`), type `text`, press Send, restore the view."""
    win = _window(app)
    before = _view(app, win)
    try:
        if title:
            _open_chat(app, win, title, project)
        else:
            _new_chat(app, win, folder)
        root = _root(app, win, title or "")
        e = _composer(app, root)
        _put(win, e, text)
        _submit(app, win, root, e, text)
    finally:
        _restore(app, win, before)


# ---------- jobs ----------

def _save(job):
    JOBS.mkdir(parents=True, exist_ok=True)
    tmp = JOBS / f"{job['id']}.json.tmp"
    tmp.write_text(json.dumps(job, indent=1), "utf8")
    for _ in range(50):  # a reader (wait_reply) holding the file open blocks the replace on Windows for a moment
        try:
            return tmp.replace(JOBS / f"{job['id']}.json")
        except PermissionError:
            time.sleep(0.1)
    tmp.replace(JOBS / f"{job['id']}.json")


def _load(job_id):
    f = JOBS / f"{job_id}.json"
    if not f.exists():
        raise ValueError(f"no job {job_id}")
    for _ in range(50):  # the file is being replaced by _save at this instant
        try:
            return json.loads(f.read_text("utf8"))
        except PermissionError:
            time.sleep(0.1)
    return json.loads(f.read_text("utf8"))


def _public(job):
    keys = ("id", "status", "app", "chat_id", "title", "into", "reply", "error", "note", "caller", "delivered_at",
            "deliver_error")
    out = {k: job[k] for k in keys if job.get(k) is not None}
    for k in ("sent_at", "finished_at"):
        if job.get(k):
            out[k] = _iso(job[k])
    return out


def _tail(path, n=400_000):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - n))
        return f.read().decode("utf8", "replace")


def _caller(text):
    """Which chat is calling us, so the reply can be typed back into it."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        d = next((s for s in _desktop_sessions() if s["cli_id"] == sid), None)
        return {"app": "claude_code", "chat_id": d["id"], "title": d["title"]} if d else {"app": "claude_cli", "chat_id": sid}
    # Codex: the calling thread's rollout already holds this tool call, arguments included.
    # ponytail: substring heuristic on recent rollouts; switch to an env var if Codex starts passing the thread id.
    plain = max(re.findall(r"[A-Za-z0-9 ,.:;!?-]{16,}", text[:400]), key=len, default="")[:60]
    if not plain:
        return None
    for t in _codex_threads()[:40]:
        p = Path(t["transcript"])
        if p.exists() and time.time() - p.stat().st_mtime < 300 and plain in _tail(p):
            return {"app": "codex", "chat_id": t["id"], "title": t["title"]}
    return None


def _spawn_watch(job_id):
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    log = open(JOBS / f"{job_id}.log", "ab")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):  # outlive the caller's process job when allowed
        try:
            subprocess.Popen([exe, str(Path(__file__).resolve()), "watch", job_id], creationflags=flags | extra,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
            return
        except OSError:
            continue
    raise RuntimeError("could not start the reply watcher")


def _discover(job):
    """Find the chat that a 'new chat' send created."""
    t0 = job["sent_at"] - 5
    here = lambda cwd: job["cwd"] is None or _key(cwd) == _key(job["cwd"])  # None: a chat with no folder
    if job["app"] == "codex":
        new = [t for t in _codex_threads() if t["created"] >= t0 and here(t["cwd"])]
        if new:
            t = min(new, key=lambda t: t["created"])
            job.update(chat_id=t["id"], title=t["title"], transcript=t["transcript"])
    else:
        new = [s for s in _desktop_sessions() if s["created"] >= t0 and here(s["cwd"]) and s["cli_id"]]
        if new:
            s = min(new, key=lambda s: s["created"])
            path = _transcript("claude_code", s)
            job.update(chat_id=s["id"], title=s["title"], transcript=str(path) if path else None)


def _hold(chat, wait=True):
    """Lock `chat` for one claude process (its watcher holds the lock until the process exits): the lock file's
    handle, or None when another process holds it and wait is False."""
    LIVE.mkdir(parents=True, exist_ok=True)
    f = open(LIVE / f"{chat}.lock", "a+")
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            if not wait:
                f.close()
                return None
            time.sleep(0.5)


def _release(f):
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    f.close()


def _to_live(chat, job):
    """Hand a send to the process running `chat`: job 'into' names that run, which reads the text from the job file
    (the inbox holds job ids only, and a run forwards only jobs naming it). False when no process takes input (none
    runs, or it is closing; then the normal resume waits for the chat's lock)."""
    inbox = LIVE / f"{chat}.inbox"
    if not inbox.exists():
        return False
    if lock := _hold(chat, wait=False):  # nothing runs the chat: an inbox left by a killed watcher
        inbox.unlink(missing_ok=True)
        _release(lock)
        return False
    try:
        with open(inbox, "r+b") as f:  # never creates; while it is open the watcher cannot close the inbox
            job["into"] = json.loads(f.readline())["job"]
            _save(job)
            f.seek(0, 2)
            f.write((json.dumps({"job": job["id"]}) + "\n").encode("utf8"))
        return True
    except FileNotFoundError:
        return False


def _say(p, text, send_now=False):
    mid = str(uuid.uuid4())
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
                              "uuid": mid}) + "\n")
    p.stdin.flush()
    if send_now:  # the terminal's send-now key: the running turn takes the message in now (interrupt_send_now_v1)
        time.sleep(0.3)  # the request acts only on a message already at the CLI
        p.stdin.write(json.dumps({"type": "control_request", "request_id": mid,
                                  "request": {"subtype": "interrupt", "send_now": True, "message_uuid": mid}}) + "\n")
        p.stdin.flush()


def _run_cli(job):
    """One headless `claude -p` run in stream-json mode, one process per chat (a resume waits for the chat's lock).
    Messages sent to the chat meanwhile go into this process's stdin (their job ids land in job 'joined'); stdin
    closes once a result has stood GRACE_S with nothing new, and the CLI exits after its last turn."""
    cmd = [shutil.which("claude") or "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--permission-mode", job["permission_mode"]]
    if job.get("chat_id"):
        cmd += ["--resume", job["chat_id"]]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") or k == "CLAUDE_CODE_GIT_BASH_PATH"}
    lock = _hold(job["chat_id"]) if job.get("chat_id") else None
    box, pos, end, o, settled = None, 0, time.time() + WATCH_HOURS * 3600, None, None  # settled: last result, nothing after
    err = tempfile.TemporaryFile()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=job["cwd"] or HOME,
                             env=env, text=True, encoding="utf8", creationflags=subprocess.CREATE_NO_WINDOW)
        lines = queue.Queue()
        threading.Thread(target=lambda: [*map(lines.put, p.stdout), lines.put(None)], daemon=True).start()
        _say(p, job["text"])
        while True:
            if time.time() > end:
                p.kill()
                raise RuntimeError(f"claude ran past {WATCH_HOURS} h")
            if box:
                with open(box, "rb") as f:
                    f.seek(pos)
                    new = f.read()
                new = new[:new.rfind(b"\n") + 1]
                pos += len(new)
                for jid in (json.loads(x)["job"] for x in new.splitlines()):
                    m = _load(jid)
                    if m.get("into") == job["id"]:  # sent through _send to this run
                        _say(p, m["text"], m.get("send_now"))
                        _save(dict(m, forwarded_at=time.time()))
                        job.setdefault("joined", []).append(jid)
                        settled = None
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line is None:
                break
            ev = json.loads(line) if line.startswith("{") else {}
            if ev.get("type") == "system" and ev.get("subtype") == "init" and not box:
                lock = lock or _hold(ev["session_id"])  # a new chat: its id is known from here on
                job.update(chat_id=ev["session_id"], pid=p.pid)  # and it can take mid-run sends
                _save(job)
                box = LIVE / f"{ev['session_id']}.inbox"
                box.write_text(json.dumps({"job": job["id"]}) + "\n", "utf8")
                pos = box.stat().st_size
            elif ev.get("type") == "result":
                o, settled = ev, time.time()
            elif ev.get("type") == "assistant":  # a queued message started a turn of its own
                settled = None
            elif ev.get("type") == "control_response":
                job["control"] = ev.get("response")
            if settled and time.time() - settled >= GRACE_S and not p.stdin.closed:
                if box and box.suffix == ".inbox":
                    try:
                        box = box.replace(box.with_suffix(".closed"))  # fails while a sender holds it open
                    except PermissionError:
                        pass
                    continue  # its last lines are forwarded next round; with none, stdin closes then
                p.stdin.close()
        p.wait(timeout=60)
    finally:
        if box:
            box.unlink(missing_ok=True)
        if lock:
            _release(lock)
    if o is None:
        err.seek(0)
        raise RuntimeError(f"claude exited {p.returncode} without a result: {err.read().decode('utf8', 'replace')[-1500:]}")
    job["chat_id"] = o.get("session_id") or job.get("chat_id")
    if o.get("is_error"):
        raise RuntimeError(str(o.get("result"))[-1500:])
    return o.get("result") or ""


def _deliver(job):
    c = job["caller"]
    if c["app"] not in WINDOW_EXE or (c["app"] == job["app"] and c["chat_id"] == job.get("chat_id")):
        return
    body = job["reply"] if job["status"] == "done" else f"(failed: {job.get('error')})"
    if len(body) > REPLY_CAP:
        body = body[:REPLY_CAP] + f"\n[... cut. Full reply: {JOBS / (job['id'] + '.json')}]"
    msg = (f"[agent_bridge job {job['id']}: reply from {job['app']} chat \"{job.get('title') or job.get('chat_id')}\". "
           f"Typed by the bridge, not by the user.]\n\n{body}")
    for _ in range(30):  # the user may be typing in that chat: retry for ~15 minutes
        try:
            with _gui():
                _gui_send(c["app"], msg, title=_chat(c["app"], c["chat_id"])["title"],
                          project=_chat(c["app"], c["chat_id"])["project"])
            job.update(delivered_at=time.time(), deliver_error=None)
            _save(job)
            return
        except Exception as ex:  # noqa: BLE001 - keep retrying, record why
            job["deliver_error"] = repr(ex)
            _save(job)
            time.sleep(30)


def _watch(job_id):
    job = _load(job_id)
    try:
        if job["app"] == "claude_cli":
            reply = _run_cli(job)
        else:
            reply, end = None, job["sent_at"] + WATCH_HOURS * 3600
            while reply is None and time.time() < end:
                if not job.get("transcript"):
                    _discover(job)
                if job.get("transcript") and Path(job["transcript"]).exists():
                    reply = _reply_since(job["app"], job["transcript"], job.get("offset", 0), job["hint"])
                if reply is None:
                    time.sleep(5)
            if job.get("chat_id"):  # titles are written after the first turn
                job["title"] = next((s["title"] for s in _sessions(job["app"]) if s["id"] == job["chat_id"]), job.get("title"))
        job.update(status="done" if reply is not None else "timeout", reply=reply)
    except Exception as ex:  # noqa: BLE001 - the job file is the only place a detached watcher can report
        job.update(status="error", error=repr(ex))
    job["finished_at"] = time.time()
    _save(job)
    joined = [_load(j) for j in job.get("joined", [])]  # mid-run sends: answered by this run's last result
    for j in joined:
        j.update({k: job.get(k) for k in ("status", "reply", "error", "chat_id", "finished_at")})
        _save(j)
    if job.get("caller") and job["status"] in ("done", "error"):
        _deliver(job)
    for j in joined:
        if j.get("caller") and j["status"] in ("done", "error") and j["caller"] != job.get("caller"):
            _deliver(j)


def _send(app, text, chat=None, project=None, notify_caller=True, permission_mode="auto", send_now=False):
    if app not in APPS:
        raise ValueError(f"app must be one of {APPS}")
    if not text.strip():
        raise ValueError("text is empty")
    if send_now and app != "claude_cli":
        raise ValueError("send_now applies to claude_cli only")
    job = {"id": uuid.uuid4().hex[:12], "app": app, "text": text, "hint": _ws(text)[:50], "sent_at": time.time(),
           "status": "waiting", "caller": _caller(text) if notify_caller else None}
    if app == "claude_cli" and chat and _to_live(chat, dict(job, chat_id=chat, send_now=send_now)):
        return _load(job["id"])  # a running chat named by id: no transcript scan, so it lands within a second
    s = _chat(app, chat, project) if chat else None
    p = _project(app, project) if project and not s else None
    job.update(chat_id=s and s["id"], title=s and s["title"], cwd=s["cwd"] if s else p and p["path"], note=s and s.get("note"))
    if app == "claude_cli":
        job.update(permission_mode=permission_mode, send_now=send_now)
        if s and _to_live(s["id"], job):  # the chat's process is running: no second one, its stdin takes the text
            return job
    else:
        path = _transcript(app, s) if s else None
        job.update(transcript=str(path) if path else None, offset=path.stat().st_size if path else 0)
        with _gui():
            if s:
                _gui_send(app, text, title=s["title"], project=s["project"])
            else:
                _gui_send(app, text, folder=p)
    _save(job)
    _spawn_watch(job["id"])
    return job


# ---------- tools ----------

def _tool(fn):
    """Register fn as a tool. mcp 2.x hides the text of anything but ToolError, so our own refusals become ToolError."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run(*a, **k):
            try:
                return await fn(*a, **k)
            except (ValueError, RuntimeError) as ex:
                raise ToolError(str(ex)) from ex
    else:
        @functools.wraps(fn)
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except (ValueError, RuntimeError) as ex:
                raise ToolError(str(ex)) from ex
    return mcp.tool()(run)


@_tool
def list_projects(app: str) -> list[dict]:
    """List projects (folders) for app = 'codex', 'claude_code' or 'claude_cli', most recently active first."""
    return [{"name": p["name"], "path": p["path"], "chats": p["chats"], "last_active": _iso(p["last_active"])}
            for p in _projects(app)]


@_tool
def list_chats(app: str, project: str, limit: int = 20) -> list[dict]:
    """List chats in a project (name or path from list_projects), newest first.
    codex chats show their goal status; claude chats show busy / idle / closed."""
    p = _project(app, project)
    out = []
    for s in [s for s in _sessions(app) if _in_project(app, s, p)][:limit]:
        title = s["title"] if app != "claude_cli" else _cli_title(s["transcript"])
        row = {"id": s["id"], "title": title, "updated": _iso(s["updated"])}
        row.update({"goal": s["goal"]} if app == "codex" else {"status": s["status"]})
        out.append(row)
    return out


@_tool
def read_chat(app: str, chat: str, last: int = 4, project: str | None = None) -> dict:
    """Read the last messages of a chat (id or exact title): what was asked and each turn's final answer."""
    s = _chat(app, chat, project)
    path = _transcript(app, s)
    msgs = []
    if path and path.exists():
        for kind, text in _events(app, path, max(0, path.stat().st_size - 4_000_000)):
            if text.strip():
                msgs.append({"role": "user" if kind == "user" else "assistant", "text": text[:4000]})
    out = {"id": s["id"], "title": s["title"], "messages": msgs[-last:]}
    if s.get("note"):
        out["note"] = s["note"]
    if app == "codex":
        out["goal"] = _goal(s["id"])
    else:
        out["status"] = s["status"]
    return out


@_tool
def send_message(app: str, text: str, chat: str | None = None, project: str | None = None,
                 notify_caller: bool = True, permission_mode: str = "auto", send_now: bool = False) -> dict:
    """Send `text` into a chat and press Enter.
    chat: id or exact title of an existing chat. Or leave chat empty to start a new chat: in `project` (a project
    name from list_projects, or any folder path; Codex gets a project made for a new folder), or with no folder
    when project is empty too.
    codex / claude_code are driven through the desktop app; a split Claude window is fine (a chat already on screen
    is used in place, otherwise it opens in the focused pane). The view returns to the chats the user was on.
    claude_cli runs `claude -p --resume` headless with permission_mode, in stream-json mode, one process per chat:
    a send to a chat whose run is still going goes into that process's stdin (within a second; the job shows
    'into': the run's job id) and the run takes it in, usually in the same turn; both jobs get the run's last
    result as their reply. send_now (claude_cli): press the CLI's send-now key for it, so the running turn takes it
    in now instead of after the tool call under way. A send to a chat whose process has ended resumes it as usual.
    Returns a job_id. When the answer is ready it is saved; if you are a desktop chat it is also typed into your
    chat (notify_caller). Otherwise call wait_reply(job_id)."""
    job = _send(app, text, chat, project, notify_caller, permission_mode, send_now)
    caller = job.get("caller")
    how = (f"the reply will be typed into your {caller['app']} chat" if caller and caller["app"] in WINDOW_EXE
           else "call wait_reply(job_id) for the reply")
    return {**_public(job), "next": how}


@_tool
async def wait_reply(job_id: str, timeout_s: int = 600) -> dict:
    """Wait until the chat behind job_id has answered (or timeout_s passes) and return the job with its reply."""
    end = time.time() + timeout_s
    while True:
        job = _load(job_id)
        if job["status"] != "waiting" or time.time() >= end:
            return _public(job)
        await anyio.sleep(3)


def main():
    if sys.argv[1:2] == ["watch"]:
        _watch(sys.argv[2])
    else:
        mcp.run()


if __name__ == "__main__":
    main()
