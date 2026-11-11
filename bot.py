#!/usr/bin/env python3
"""
claude-tg — drive Claude Code on a VPS from Telegram, tuned for bug bounty work.

One Telegram chat = one operator. Each chat has:
  * targets   -> a working folder per program (scope.md, secrets/, uploads/, findings/)
  * sessions  -> named Claude Code conversations inside a target (resumable)
  * settings  -> model / effort / permission mode per session
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets as pysecrets
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

# ─────────────────────────── config ───────────────────────────
load_dotenv(Path(__file__).with_name(".env"))
for _k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
    if not os.environ.get(_k):
        os.environ.pop(_k, None)  # empty values confuse the CLI's auth detection

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x}
BASE_DIR = Path(os.environ.get("BASE_DIR", str(Path.home() / "bounty"))).expanduser()
STATE_FILE = BASE_DIR / ".claude-tg-state.json"
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet")
DEFAULT_EFFORT = os.environ.get("DEFAULT_EFFORT", "high")
DEFAULT_MODE = os.environ.get("DEFAULT_MODE", "default")
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_PER_TURN_USD", "0") or 0)
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT_SEC", "600"))
SH_TIMEOUT = int(os.environ.get("SH_TIMEOUT_SEC", "300"))
ENABLE_CHROME = os.environ.get("ENABLE_CHROME", "0").lower() in ("1", "true", "yes")
# Use the installed `claude` (shares /login, MCP servers like burp, and the Chrome connection);
# fall back to the SDK's bundled CLI if none is on PATH.
CLI_PATH = os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude")
# Tools that never need a Telegram approval (comma separated; trailing * = prefix match), e.g.
# AUTO_ALLOW_TOOLS=mcp__claude-in-chrome__*,mcp__burp__*
AUTO_ALLOW = [t.strip() for t in os.environ.get("AUTO_ALLOW_TOOLS", "").split(",") if t.strip()]
IS_WINDOWS = sys.platform.startswith("win")
# autopilot: when on and you send no instructions, Claude picks its own next step and keeps going.
AUTOPILOT_MAX_STEPS = int(os.environ.get("AUTOPILOT_MAX_STEPS", "40"))   # auto-steps before it pauses for you
AUTOPILOT_DELAY = float(os.environ.get("AUTOPILOT_DELAY_SEC", "4"))       # pause between auto-steps
AUTOPILOT_BUDGET_USD = float(os.environ.get("AUTOPILOT_BUDGET_USD", "0") or 0)  # 0 = no cap (API billing only)
# auto-resume after a Claude usage/rate limit resets
RESUME_POLL_SEC = int(os.environ.get("RESUME_POLL_SEC", "900"))     # recheck interval when reset time is unknown
RESUME_BUFFER_SEC = int(os.environ.get("RESUME_BUFFER_SEC", "45"))  # wait this long past the reset before retrying
# auto-recovery: on a turn error, start a fresh session on the same target and resume from saved files
RECOVER_MAX = int(os.environ.get("RECOVER_MAX", "3"))        # consecutive auto-restarts before giving up
RECOVER_DELAY = float(os.environ.get("RECOVER_DELAY_SEC", "5"))

MODELS = ["opus", "sonnet", "haiku", "claude-fable-5-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODES = ["default", "acceptEdits", "plan", "bypassPermissions"]
MODE_HELP = {
    "default": "asks you in Telegram before running tools",
    "acceptEdits": "file edits auto, commands still ask",
    "plan": "read-only planning, no execution",
    "bypassPermissions": "runs everything without asking",
}
SAFE_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch", "WebFetch", "NotebookRead"}

AUTOPILOT_PROMPT = (
    "[autopilot] No new instructions from the operator. Keep the engagement on '{target}' moving yourself.\n"
    "1. Review where things stand: read scope.md, and skim recon/ and findings/ for what you've already done.\n"
    "2. Pick the single most useful next step and do it — e.g. probe an endpoint you haven't tested, follow up "
    "a lead from earlier output, widen recon, or verify a suspected issue.\n"
    "3. Stay STRICTLY inside the scope in scope.md; never touch out-of-scope assets. Keep every action "
    "non-destructive and within the program's rate limits. No account takeover of real users, no data "
    "exfiltration beyond a minimal proof.\n"
    "4. Write each confirmed issue to its own file in findings/.\n"
    "If you've genuinely run out of productive, in-scope avenues, or you hit something that needs an operator "
    "decision, say so in one line and stop — don't invent risky work to stay busy."
)

RECOVER_PROMPT = (
    "[auto-recovery] The previous session errored or was cut off. This is a fresh session on "
    "'{target}'. Do the METHODOLOGY session-start protocol now: read scope.md and PROGRESS.md, "
    "skim recon/ and findings/ and .last.md if present, then FIRST update PROGRESS.md so nothing "
    "is lost, and continue the engagement from exactly where it left off. Open with one short line "
    "on the current state and your next step."
)

ADVISOR_PROMPT = (
    "[advisor] Step back and act as a senior bug-bounty advisor for '{target}'. Read scope.md, "
    "PROGRESS.md, recon/ and findings/. Do NOT run active tests in this reply. Give me, briefly: "
    "(1) where the highest-value bugs likely are given the program's bounty tiers and focus areas; "
    "(2) what looks under-tested or skipped so far; (3) the 3-5 next actions you'd prioritise, in "
    "order, and why; (4) any scope or rule risks to watch. Keep it tight and specific to what the "
    "files actually show."
)

TG_LIMIT = 3900
log = logging.getLogger("claude-tg")


# ─────────────────────────── state ────────────────────────────
@dataclass
class Session:
    name: str
    target: str
    session_id: str | None = None
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    mode: str = DEFAULT_MODE
    cost: float = 0.0
    turns: int = 0
    created: float = field(default_factory=time.time)
    always_allow: list[str] = field(default_factory=list)


@dataclass
class ChatState:
    target: str = "general"
    active: str = "main"
    sessions: dict[str, Session] = field(default_factory=dict)
    verbose: bool = True
    show_thinking: bool = False
    show_output: bool = False
    autopilot: bool = False
    auto_resume: bool = True
    auto_recover: bool = True


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.chats: dict[int, ChatState] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        for cid, c in raw.items():
            sess = {k: Session(**v) for k, v in c.pop("sessions", {}).items()}
            self.chats[int(cid)] = ChatState(sessions=sess, **c)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for cid, c in self.chats.items():
            d = asdict(c)
            data[str(cid)] = d
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def chat(self, cid: int) -> ChatState:
        if cid not in self.chats:
            self.chats[cid] = ChatState()
        c = self.chats[cid]
        if c.active not in c.sessions:
            c.sessions[c.active] = Session(name=c.active, target=c.target)
        ensure_target(c.target)
        return c

    def session(self, cid: int) -> Session:
        c = self.chat(cid)
        return c.sessions[c.active]


STORE: Store = None  # type: ignore  # initialised in main()


# ─────────────────────────── targets ──────────────────────────
SLUG = re.compile(r"[^a-zA-Z0-9._-]+")


def slug(s: str) -> str:
    return SLUG.sub("-", s.strip()).strip("-.")[:48] or "x"


def tdir(target: str) -> Path:
    return BASE_DIR / "targets" / target


def ensure_target(target: str) -> Path:
    d = tdir(target)
    for sub in ("uploads", "findings", "recon", "secrets"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(d / "secrets", 0o700)
    scope = d / "scope.md"
    if not scope.exists():
        scope.write_text(f"# Scope — {target}\n\n## In scope\n\n## Out of scope\n\n## Program rules\n")
    return d


def secrets_file(target: str) -> Path:
    return tdir(target) / "secrets" / "auth.json"


def load_secrets(target: str) -> dict[str, Any]:
    f = secrets_file(target)
    return json.loads(f.read_text()) if f.exists() else {"cookies": {}, "headers": {}, "notes": {}}


def save_secrets(target: str, data: dict[str, Any]) -> None:
    f = secrets_file(target)
    f.write_text(json.dumps(data, indent=2))
    os.chmod(f, 0o600)


def mask(v: str) -> str:
    return v if len(v) <= 8 else f"{v[:4]}…{v[-3:]} ({len(v)} chars)"


# Claude Pro/Max usage-limit and rate-limit detection. The CLI's wording isn't guaranteed stable,
# so match broadly and, when present, pull a reset time out of the message (unix epoch, or a clock time).
_LIMIT_RE = re.compile(
    r"usage limit|rate limit|limit reached|limit will reset|limit resets|too many requests|\b429\b|"
    r"out of .*(credit|usage)|quota (?:exceeded|reached)", re.I)
_EPOCH_RE = re.compile(r"\b(1\d{12}|1\d{9})\b")                 # 13-digit ms or 10-digit seconds
_CLOCK_RE = re.compile(r"reset[^0-9]{0,24}?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def limit_reset_from(text: str) -> tuple[bool, float | None]:
    """(is_limit, reset_epoch_or_None) from an error/result string."""
    if not text or not _LIMIT_RE.search(text):
        return (False, None)
    m = _EPOCH_RE.search(text)
    if m:
        v = int(m.group(1))
        return (True, v / 1000 if v > 1_000_000_000_000 else float(v))
    m = _CLOCK_RE.search(text)
    if m:
        try:
            hh = int(m.group(1)) % 24
            mm = int(m.group(2) or 0)
            ap = (m.group(3) or "").lower()
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            now = time.localtime()
            cand = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0,
                                     now.tm_wday, now.tm_yday, now.tm_isdst))
            epoch = time.mktime(cand)
            if epoch <= time.time():        # already passed today -> next day
                epoch += 86400
            return (True, epoch)
        except Exception:
            pass
    return (True, None)


def fmt_eta(until: float | None) -> str:
    if not until:
        return "unknown reset time"
    mins = max(0, int((until - time.time()) / 60))
    return f"~{time.strftime('%H:%M', time.localtime(until))} local (in ~{mins} min)"


def system_append(s: Session) -> str:
    d = tdir(s.target)
    return f"""
## Operator context (Telegram-controlled bug bounty workstation)
You are running on the operator's VPS and are controlled through a Telegram chat. Keep replies
compact and phone-readable; put long output into files and mention the path.

Current program/target: {s.target}
Working directory: {d}
- scope.md: the program's scope and rules. READ IT before any active testing. Only interact with
  assets listed as in scope; never touch out-of-scope assets; obey the program's rules
  (rate limits, banned techniques, required headers). If scope is empty or unclear, ask first.
- secrets/auth.json: session cookies / auth headers per domain supplied by the operator. Load them
  from the file when needed, preferably through the shell (e.g. jq piped into curl -b/-H) so the
  values don't get echoed into the conversation; never print full values back into chat.
- uploads/: files the operator sent from Telegram (Burp requests, JS, screenshots, wordlists).
- recon/: save recon output here. findings/: write each confirmed issue as its own markdown file
  (title, severity estimate, affected asset, steps to reproduce, impact, evidence).

Testing etiquette: prefer low-noise, non-destructive checks; no DoS, no mass-scanning beyond
program limits, no data exfiltration beyond the minimum needed to prove impact, no actions against
other users' accounts. Ask the operator before anything high-impact or irreversible.
"""


# ─────────────────────────── helpers ──────────────────────────
def authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED)


def guard(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not authorized(update):
            if update.effective_user:
                log.warning("blocked user %s (%s)", update.effective_user.id, update.effective_user.username)
            return
        try:
            return await fn(update, ctx)
        except Exception as e:  # keep the bot alive, report to the operator
            log.exception("handler error")
            await reply(update, f"⚠️ {type(e).__name__}: {e}")
    wrapper.__name__ = fn.__name__
    return wrapper


def chunks(text: str, n: int = TG_LIMIT) -> list[str]:
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > n:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:n])
            line = line[n:]
        if len(buf) + len(line) > n:
            out.append(buf)
            buf = ""
        buf += line
    if buf.strip():
        out.append(buf)
    return out


async def send_text(bot, chat_id: int, text: str, file_name: str = "output.md") -> None:
    text = text.strip()
    if not text:
        return
    if len(text) > TG_LIMIT * 4:
        p = BASE_DIR / ".outbox" / f"{int(time.time())}-{file_name}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        await bot.send_message(chat_id, text[:600] + "\n…\n(full output attached)")
        with p.open("rb") as fh:
            await bot.send_document(chat_id, fh, filename=file_name)
        return
    for part in chunks(text):
        await bot.send_message(chat_id, part, disable_web_page_preview=True)


async def reply(update: Update, text: str, **kw) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(text, disable_web_page_preview=True, **kw)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in r] for r in rows])


def auto_allowed(tool: str) -> bool:
    return any(tool.startswith(p[:-1]) if p.endswith("*") else tool == p for p in AUTO_ALLOW)


def tool_summary(name: str, inp: dict[str, Any]) -> str:
    if name in ("Bash", "PowerShell"):
        s = inp.get("command", "")
    elif name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        s = inp.get("file_path", "")
    elif name in ("Grep", "Glob"):
        s = f"{inp.get('pattern', '')} {inp.get('path', '')}"
    elif name == "WebFetch":
        s = inp.get("url", "")
    elif name == "WebSearch":
        s = inp.get("query", "")
    elif name in ("Task", "Agent"):
        s = inp.get("description", "")
    else:
        s = json.dumps(inp)[:300]
    s = str(s).strip().replace("\n", " ⏎ ")
    return f"{name}: {s[:350]}"


# ─────────────────────────── runner ───────────────────────────
class Runner:
    """Owns the live Claude Code process for one Telegram chat."""

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.client: ClaudeSDKClient | None = None
        self.key: tuple | None = None
        self.task: asyncio.Task | None = None
        self.queue: list[str] = []   # messages sent while Claude was busy
        self.notes: list[str] = []   # bot notices prepended to the next prompt
        self.auto_steps = 0          # consecutive autopilot steps since last human input
        self.limit_hit = False       # last run stopped on a usage/rate limit
        self.limit_until = None      # epoch the limit is expected to reset, if known
        self.errored = False         # last run ended in a (non-limit) error
        self.recover_count = 0       # consecutive auto-recoveries since last clean turn
        self.bot = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client, self.key = None, None

    async def _client_for(self, s: Session) -> ClaudeSDKClient:
        key = (s.name, s.target, s.session_id, s.model, s.effort, s.mode)
        if self.client and self.key and self.key[:2] == key[:2] and self.key[3:] == key[3:]:
            return self.client  # same session and settings; session_id updates are internal
        await self.close()
        opts = ClaudeAgentOptions(
            cwd=str(tdir(s.target)),
            model=s.model,
            effort=s.effort,  # type: ignore[arg-type]
            permission_mode=s.mode,  # type: ignore[arg-type]
            resume=s.session_id,
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_append(s)},
            setting_sources=["user", "project", "local"],
            can_use_tool=self._make_approver(s),
            max_budget_usd=MAX_BUDGET_USD or None,
            cli_path=CLI_PATH,
            extra_args={"chrome": None} if ENABLE_CHROME else {},
        )
        self.client = ClaudeSDKClient(options=opts)
        await self.client.connect()
        self.key = key
        return self.client

    def _make_approver(self, s: Session):
        async def approve(tool: str, inp: dict[str, Any], ctx: ToolPermissionContext):
            if tool in SAFE_TOOLS or tool in s.always_allow or auto_allowed(tool):
                return PermissionResultAllow()
            rid = pysecrets.token_hex(4)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            PENDING[rid] = (fut, self.chat_id, s.name, tool)
            text = f"🔐 Approve?\n\n{tool_summary(tool, inp)}"
            if tool in ("Bash", "PowerShell") and len(inp.get("command", "")) > 350:
                text = f"🔐 Approve {tool}?\n\n{inp['command'][:3000]}"
            await self.bot.send_message(
                self.chat_id,
                text,
                reply_markup=kb([[("✅ Allow", f"ap:{rid}:y"), (f"✅ Always {tool}", f"ap:{rid}:a")],
                                 [("❌ Deny", f"ap:{rid}:n"), ("🛑 Deny + stop", f"ap:{rid}:s")]]),
            )
            try:
                ans = await asyncio.wait_for(fut, APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                ans = "n"
                await self.bot.send_message(self.chat_id, f"⌛ No answer — denied {tool}.")
            finally:
                PENDING.pop(rid, None)
            if ans == "a":
                if tool not in s.always_allow:
                    s.always_allow.append(tool)
                    STORE.save()
                return PermissionResultAllow()
            if ans == "y":
                return PermissionResultAllow()
            return PermissionResultDeny(message="Operator denied this action.", interrupt=(ans == "s"))
        return approve

    async def run(self, bot, prompt: str) -> None:
        self.bot = bot
        st = STORE.chat(self.chat_id)
        s = STORE.session(self.chat_id)
        started = time.time()
        self.limit_hit, self.limit_until = False, None
        self.errored = False
        try:
            (tdir(s.target) / ".last.md").write_text(
                f"# Last activity\n- {time.strftime('%Y-%m-%d %H:%M:%S')}\n- session: {s.name}\n\n{prompt[:2000]}\n")
        except Exception:
            pass
        status = await bot.send_message(self.chat_id, f"⏳ {s.target}/{s.name} · {s.model} · {s.effort}")
        last_edit = 0.0
        tools_used = 0

        async def set_status(line: str) -> None:
            nonlocal last_edit
            if time.time() - last_edit < 2.5:
                return
            last_edit = time.time()
            el = int(time.time() - started)
            try:
                await status.edit_text(f"⏳ {el}s · {tools_used} tools\n{line[:300]}")
            except Exception:
                pass

        try:
            client = await self._client_for(s)
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            await send_text(bot, self.chat_id, b.text)
                        elif isinstance(b, ThinkingBlock) and st.show_thinking and b.thinking.strip():
                            await send_text(bot, self.chat_id, "💭 " + b.thinking, "thinking.md")
                        elif isinstance(b, ToolUseBlock):
                            tools_used += 1
                            line = "🔧 " + tool_summary(b.name, b.input)
                            if st.verbose:
                                await bot.send_message(self.chat_id, line, disable_web_page_preview=True)
                            else:
                                await set_status(line)
                elif isinstance(msg, UserMessage) and st.show_output and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock):
                            body = b.content if isinstance(b.content, str) else "\n".join(
                                x.get("text", "") for x in (b.content or []) if isinstance(x, dict))
                            body = (body or "(no output)").strip()
                            if len(body) > 1500:
                                body = body[:700] + f"\n… [{len(body) - 1400} chars cut] …\n" + body[-700:]
                            await bot.send_message(self.chat_id, ("❗ " if b.is_error else "📤 ") + body,
                                                   disable_web_page_preview=True)
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        s.session_id = msg.session_id
                        self.key = (s.name, s.target, s.session_id, s.model, s.effort, s.mode)
                    s.cost += msg.total_cost_usd or 0
                    s.turns += 1
                    STORE.save()
                    cost = f" · ${msg.total_cost_usd:.3f}" if msg.total_cost_usd else ""
                    if msg.is_error:
                        txt = " ".join(str(x) for x in (msg.result, msg.subtype, getattr(msg, "errors", None),
                                                        getattr(msg, "api_error_status", None)) if x)
                        hit, reset = limit_reset_from(txt)
                        if hit:
                            self.limit_hit, self.limit_until = True, reset
                        else:
                            self.errored = True
                            await bot.send_message(self.chat_id,
                                f"⚠️ {msg.subtype or 'error'} {msg.num_turns} steps · {int(time.time() - started)}s{cost}")
                    else:
                        await bot.send_message(self.chat_id,
                            f"✓ {msg.num_turns} steps · {int(time.time() - started)}s{cost}")
        except asyncio.CancelledError:
            await self.close()
            await bot.send_message(self.chat_id, "🛑 Stopped.")
            raise
        except Exception as e:
            log.exception("run failed")
            await self.close()
            hit, reset = limit_reset_from(f"{type(e).__name__}: {e}")
            if hit:
                self.limit_hit, self.limit_until = True, reset
            else:
                self.errored = True
                await bot.send_message(self.chat_id, f"⚠️ Claude error: {type(e).__name__}: {e}")
        finally:
            try:
                await status.delete()
            except Exception:
                pass

    async def loop(self, bot, prompt: str) -> None:
        """Run a turn; drain queued messages; then, if autopilot is on, keep going on its own."""
        while True:
            if self.notes:
                prompt = "\n".join(self.notes) + "\n\n" + prompt
                self.notes.clear()
            await self.run(bot, prompt)
            if self.limit_hit:
                self.limit_hit = False
                retry = await self._handle_limit(bot)
                if retry:
                    continue          # same prompt, once the limit is back
                return
            if self.errored:
                self.errored = False
                nxt = await self._recover(bot)
                if nxt is not None:
                    prompt = nxt      # fresh session resumes from saved state
                    continue
                return
            self.recover_count = 0    # a clean turn clears the recovery budget
            if self.queue:
                prompt = "\n\n".join(self.queue)
                n = len(self.queue)
                self.queue.clear()
                self.auto_steps = 0   # a human message resets the autopilot budget
                await bot.send_message(self.chat_id, f"📨 Delivering {n} queued message{'s' if n > 1 else ''} to Claude…")
                continue
            nxt = await self._autopilot_next(bot)
            if nxt is None:
                return
            prompt = nxt

    async def _autopilot_next(self, bot) -> str | None:
        """Return the next self-directed prompt, or None to stop looping."""
        st = STORE.chat(self.chat_id)
        if not st.autopilot:
            return None
        self.auto_steps += 1
        if self.auto_steps > AUTOPILOT_MAX_STEPS:
            st.autopilot = False
            self.auto_steps = 0
            STORE.save()
            await bot.send_message(self.chat_id,
                                   f"🛑 Autopilot paused after {AUTOPILOT_MAX_STEPS} steps. /auto on to resume.")
            return None
        if AUTOPILOT_BUDGET_USD and STORE.session(self.chat_id).cost >= AUTOPILOT_BUDGET_USD:
            st.autopilot = False
            self.auto_steps = 0
            STORE.save()
            await bot.send_message(self.chat_id,
                                   f"🛑 Autopilot paused: session hit ${AUTOPILOT_BUDGET_USD:.2f} budget. /auto on to resume.")
            return None
        await asyncio.sleep(AUTOPILOT_DELAY)
        if not STORE.chat(self.chat_id).autopilot:   # toggled off during the pause
            return None
        return AUTOPILOT_PROMPT.format(target=st.target)

    async def _handle_limit(self, bot) -> bool:
        """Notify about a usage/rate limit; if auto-resume is on, wait for reset and return True to retry."""
        st = STORE.chat(self.chat_id)
        eta = fmt_eta(self.limit_until)
        if not st.auto_resume:
            await bot.send_message(self.chat_id,
                f"🚦 Claude usage limit reached ({eta}). Auto-resume is off — resend when it's back, "
                f"or /resume on to have me wait and continue automatically.")
            return False
        await bot.send_message(self.chat_id,
            f"🚦 Claude usage limit reached. I'll wait and pick up where we left off — reset {eta}. "
            f"/stop to cancel, /resume off to stop waiting.")
        while True:
            now = time.time()
            if self.limit_until and self.limit_until > now:
                delay = (self.limit_until - now) + RESUME_BUFFER_SEC
            else:
                delay = RESUME_POLL_SEC
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if not STORE.chat(self.chat_id).auto_resume:
                await bot.send_message(self.chat_id, "Stopped waiting for the limit reset.")
                return False
            if self.limit_until and time.time() >= self.limit_until:
                break
            if not self.limit_until:
                break   # unknown reset: poll once, then retry; if still limited we'll wait again
        await bot.send_message(self.chat_id, "▶️ Limit should be back — resuming.")
        return True

    async def _recover(self, bot) -> str | None:
        """On a turn error: start a fresh conversation on the same target, resume from files."""
        st = STORE.chat(self.chat_id)
        if not st.auto_recover:
            return None
        self.recover_count += 1
        if self.recover_count > RECOVER_MAX:
            self.recover_count = 0
            await bot.send_message(self.chat_id,
                f"♻️ Auto-recovery gave up after {RECOVER_MAX} tries on {st.target} — something is "
                f"persistently failing (check bot.log). Stopping so it doesn't spin. /auto on to retry.")
            return None
        s = STORE.session(self.chat_id)
        s.session_id = None           # drop the errored conversation; next connect is fresh context
        await self.close()
        STORE.save()
        await bot.send_message(self.chat_id,
            f"♻️ Session errored — starting a fresh conversation on {st.target} and resuming from "
            f"saved state (scope.md / PROGRESS.md). Try {self.recover_count}/{RECOVER_MAX}.")
        await asyncio.sleep(RECOVER_DELAY)
        return RECOVER_PROMPT.format(target=st.target)

    async def interrupt(self) -> None:
        """Stop the current step but keep the queue, so queued messages go in right away."""
        if self.busy and self.client:
            try:
                await asyncio.wait_for(self.client.interrupt(), 5)
            except Exception:
                pass

    async def stop(self) -> bool:
        st = STORE.chat(self.chat_id)
        if st.autopilot:
            st.autopilot = False
            STORE.save()
        self.auto_steps = 0
        if not self.busy:
            return False
        dropped = len(self.queue)
        self.queue.clear()
        if dropped:
            await self.bot.send_message(self.chat_id, f"Dropped {dropped} queued message(s).")
        for rid, (fut, cid, _, _) in list(PENDING.items()):
            if cid == self.chat_id and not fut.done():
                fut.set_result("s")
        try:
            if self.client:
                await asyncio.wait_for(self.client.interrupt(), 5)
                await asyncio.wait_for(asyncio.shield(self.task), 10)
                return True
        except Exception:
            pass
        self.task.cancel()
        return True


RUNNERS: dict[int, Runner] = {}
PENDING: dict[str, tuple[asyncio.Future, int, str, str]] = {}


def runner(cid: int) -> Runner:
    if cid not in RUNNERS:
        RUNNERS[cid] = Runner(cid)
    return RUNNERS[cid]


async def start_turn(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    cid = update.effective_chat.id
    r = runner(cid)
    if r.busy:
        r.queue.append(prompt)
        await reply(update, "📨 Queued. Claude gets it as soon as the current step finishes.",
                    reply_markup=kb([[("⚡ Interrupt & send now", "int:now")]]))
        return
    r.auto_steps = 0
    r.task = asyncio.create_task(r.loop(ctx.bot, prompt))


async def secret_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE, what: str, note: str) -> None:
    """Tell Claude where the new secret lives (never its value) and offer to continue."""
    cid = update.effective_chat.id
    r = runner(cid)
    notice = (f"[bot] The operator just saved {what} in secrets/auth.json. Use it from that file via the "
              f"shell (e.g. jq into curl) rather than printing it.")
    if r.busy:
        r.queue.append(notice)
        return await ctx.bot.send_message(cid, f"✅ Saved {what}{note}. Claude will be told after its current step.")
    r.notes.append(notice)
    await ctx.bot.send_message(cid, f"✅ Saved {what}{note}.\nYour next message tells Claude where it is, or:",
                               reply_markup=kb([[("▶ Tell Claude & continue", "cont:go")]]))


# ─────────────────────────── commands ─────────────────────────
HELP = """🤖 Claude Code over Telegram

Chat: just type, and Claude works in the current target folder.
//cmd  → passes a Claude Code slash command (e.g. //compact, //cost)

Sessions
/new [name] · start a fresh conversation
/sessions · list and switch
/switch <name> · /rename <new> · /del <name>
/stop · interrupt the running task
/status · current target, session, model, cost

Settings (per session)
/model [opus|sonnet|haiku|<full-model-id>]
/effort [low|medium|high|xhigh|max]
/mode [default|acceptEdits|plan|bypassPermissions]
/verbose · show every tool call on/off
/output · show command output on/off (terminal view)
/thinking · show thinking on/off
Messages sent while Claude works are queued; tap ⚡ to interrupt and send now.
/allowed · tools you approved with “Always” (/allowed reset)

Autonomy
/auto [on|off] · autopilot: Claude keeps picking its own next in-scope step
/yolo · full-permission autopilot: bypass mode (no approvals) + autopilot on
/resume [on|off] · on a usage limit, wait and continue automatically (on by default)
/recover [on|off] · on an error, start a fresh session on the same target and resume (on by default)
/advise · senior-advisor review: where the best bugs are and what to do next (no active testing)
On a usage limit or error I message you; auto-resume waits out limits, auto-recover restarts clean.

Bug bounty
/target [name] · switch or create a program workspace
/scope · show · /scope add <lines> · /scope set <text> · /scope clear
/cookie <domain> <cookie-string>
/header <domain> <Name: value>
/note <key> <value> · other secrets (API keys, test creds)
/creds · masked list · /creds rm <domain|key> · /creds clear
Send any file or photo → saved to uploads/ (caption = prompt)
/findings · list findings · /get <path> · /ls [path]
/sh <command> · run a shell command directly, no Claude
"""


@guard
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STORE.chat(update.effective_chat.id)
    STORE.save()
    await reply(update, HELP)


@guard
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c, s = STORE.chat(cid), STORE.session(cid)
    total = sum(x.cost for x in c.sessions.values())
    await reply(update, (
        f"🎯 target: {c.target}\n💬 session: {s.name} ({'resumable' if s.session_id else 'new'})\n"
        f"🧠 model: {s.model} · effort: {s.effort}\n🔐 mode: {s.mode} — {MODE_HELP.get(s.mode, '')}\n"
        f"📂 {tdir(c.target)}\n💸 session ${s.cost:.3f} · all sessions ${total:.3f} · {s.turns} turns\n"
        f"{'⏳ busy' if runner(cid).busy else '💤 idle'} · autopilot {'ON' if c.autopilot else 'off'} · resume {'on' if c.auto_resume else 'off'} · recover {'on' if c.auto_recover else 'off'}\n"
        f"verbose {'on' if c.verbose else 'off'} · output {'on' if c.show_output else 'off'} · thinking {'on' if c.show_thinking else 'off'}"
    ))


@guard
async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    if runner(cid).busy:
        return await reply(update, "Busy — /stop first.")
    cur = c.sessions[c.active]
    name = slug(ctx.args[0]) if ctx.args else time.strftime("s%m%d-%H%M")
    if name in c.sessions:
        return await reply(update, f"Session '{name}' exists — /switch {name}")
    c.sessions[name] = Session(name=name, target=c.target, model=cur.model, effort=cur.effort, mode=cur.mode)
    c.active = name
    await runner(cid).close()
    STORE.save()
    await reply(update, f"🆕 New session '{name}' in {c.target} ({cur.model}/{cur.effort}/{cur.mode}).")


@guard
async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    items = sorted(c.sessions.values(), key=lambda x: -x.created)[:20]
    rows = [[(f"{'▶ ' if x.name == c.active else ''}{x.name} · {x.target} · ${x.cost:.2f}", f"sw:{x.name}")] for x in items]
    await reply(update, "Sessions (tap to switch):", reply_markup=kb(rows))


async def do_switch(cid: int, name: str) -> str:
    c = STORE.chat(cid)
    if name not in c.sessions:
        return f"No session '{name}'."
    if runner(cid).busy:
        return "Busy — /stop first."
    c.active = name
    c.target = c.sessions[name].target
    ensure_target(c.target)
    await runner(cid).close()
    STORE.save()
    s = c.sessions[name]
    return f"💬 Switched to '{name}' ({s.target}, {s.model}/{s.effort}/{s.mode})."


@guard
async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await cmd_sessions(update, ctx)
    await reply(update, await do_switch(update.effective_chat.id, ctx.args[0]))


@guard
async def cmd_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    if not ctx.args:
        return await reply(update, "Usage: /rename <new-name>")
    new = slug(ctx.args[0])
    if new in c.sessions:
        return await reply(update, "Name taken.")
    s = c.sessions.pop(c.active)
    s.name = new
    c.sessions[new] = s
    c.active = new
    await runner(cid).close()
    STORE.save()
    await reply(update, f"Renamed to '{new}'.")


@guard
async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    if not ctx.args or ctx.args[0] not in c.sessions:
        return await reply(update, "Usage: /del <session-name>")
    name = ctx.args[0]
    if name == c.active:
        if runner(cid).busy:
            return await reply(update, "Busy — /stop first.")
        await runner(cid).close()
    del c.sessions[name]
    if c.active == name:
        c.active = "main" if name != "main" else "main2"
    STORE.chat(cid)
    STORE.save()
    await reply(update, f"🗑 Deleted '{name}'. Active: {c.active}")


async def apply_setting(cid: int, field_: str, value: str) -> str:
    s = STORE.session(cid)
    setattr(s, field_, value)
    if not runner(cid).busy:
        await runner(cid).close()  # reconnect with new settings on next message
        when = "now"
    else:
        when = "from the next message"
    STORE.save()
    extra = f" — {MODE_HELP[value]}" if field_ == "mode" else ""
    return f"✅ {field_} = {value}{extra} ({when})."


def picker(field_: str, opts: list[str], current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for o in opts:
        row.append((("● " if o == current else "") + o, f"set:{field_}:{o}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(rows)


@guard
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if ctx.args:
        return await reply(update, await apply_setting(cid, "model", ctx.args[0]))
    s = STORE.session(cid)
    await reply(update, f"Model (now {s.model}). Or /model <full-model-id>", reply_markup=picker("model", MODELS, s.model))


@guard
async def cmd_effort(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if ctx.args:
        if ctx.args[0] not in EFFORTS:
            return await reply(update, "Effort: " + " | ".join(EFFORTS))
        return await reply(update, await apply_setting(cid, "effort", ctx.args[0]))
    s = STORE.session(cid)
    await reply(update, f"Effort (now {s.effort}):", reply_markup=picker("effort", EFFORTS, s.effort))


@guard
async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if ctx.args:
        if ctx.args[0] not in MODES:
            return await reply(update, "Mode: " + " | ".join(MODES))
        return await reply(update, await apply_setting(cid, "mode", ctx.args[0]))
    s = STORE.session(cid)
    desc = "\n".join(f"• {m}: {d}" for m, d in MODE_HELP.items())
    await reply(update, f"Permission mode (now {s.mode}):\n{desc}", reply_markup=picker("mode", MODES, s.mode))


@guard
async def cmd_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    which = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
    if which == "verbose":
        c.verbose = not c.verbose
        val = c.verbose
    elif which == "output":
        c.show_output = not c.show_output
        val = c.show_output
    else:
        c.show_thinking = not c.show_thinking
        val = c.show_thinking
    STORE.save()
    await reply(update, f"{which}: {'on' if val else 'off'}")


@guard
async def cmd_allowed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = STORE.session(update.effective_chat.id)
    if ctx.args and ctx.args[0] == "reset":
        s.always_allow.clear()
        STORE.save()
        return await reply(update, "Cleared always-allowed tools for this session.")
    await reply(update, "Always allowed: " + (", ".join(s.always_allow) or "none") + "\n(auto-safe: " + ", ".join(sorted(SAFE_TOOLS)) + ")")


@guard
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    arg = ctx.args[0].lower() if ctx.args else ("off" if c.auto_resume else "on")
    if arg not in ("on", "off"):
        return await reply(update, "Usage: /resume on | off")
    c.auto_resume = (arg == "on")
    STORE.save()
    await reply(update, "▶️ Auto-resume ON — if a usage limit is hit, I wait and continue when it resets."
                if c.auto_resume else
                "Auto-resume OFF — on a usage limit I notify you and stop; resend to continue.")


@guard
async def cmd_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    arg = ctx.args[0].lower() if ctx.args else ("off" if c.autopilot else "on")
    if arg not in ("on", "off"):
        return await reply(update, "Usage: /auto on | off")
    c.autopilot = (arg == "on")
    STORE.save()
    if not c.autopilot:
        return await reply(update, "🤖 Autopilot OFF. The current step (if any) finishes, then it waits for you.")
    r = runner(cid)
    r.auto_steps = 0
    note = "" if c.sessions[c.active].mode == "bypassPermissions" else \
        f"\nTool calls still ask for approval (mode: {c.sessions[c.active].mode}). /yolo for no prompts."
    await reply(update, (f"🤖 Autopilot ON for '{c.target}'. Claude keeps choosing its own next in-scope step; "
                         f"pauses after {AUTOPILOT_MAX_STEPS} steps or on /auto off / /stop.{note}"))
    if not r.busy:
        r.task = asyncio.create_task(r.loop(ctx.bot, AUTOPILOT_PROMPT.format(target=c.target)))


@guard
async def cmd_yolo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    if runner(cid).busy:
        return await reply(update, "Busy — /stop first, then /yolo.")
    await apply_setting(cid, "mode", "bypassPermissions")
    c.autopilot = True
    STORE.save()
    r = runner(cid)
    r.auto_steps = 0
    await reply(update, ("⚡ YOLO: full permission (no approval prompts) + autopilot on for '" + c.target + "'.\n"
                         "Claude runs every tool without asking and keeps working on its own, strictly within "
                         "scope.md. Make sure scope is set. /stop or /auto off to pause, /mode default to require "
                         "approvals again."))
    if not r.busy:
        r.task = asyncio.create_task(r.loop(ctx.bot, AUTOPILOT_PROMPT.format(target=c.target)))


@guard
async def cmd_advise(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    await start_turn(update, ctx, ADVISOR_PROMPT.format(target=c.target))


@guard
async def cmd_recover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    arg = ctx.args[0].lower() if ctx.args else ("off" if c.auto_recover else "on")
    if arg not in ("on", "off"):
        return await reply(update, "Usage: /recover on | off")
    c.auto_recover = (arg == "on")
    STORE.save()
    await reply(update, "♻️ Auto-recovery ON — on an error I start a fresh conversation on the same "
                f"target and resume from saved state (up to {RECOVER_MAX} tries)."
                if c.auto_recover else
                "Auto-recovery OFF — on an error I just notify you and stop.")


@guard
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await runner(update.effective_chat.id).stop()
    if not ok:
        await reply(update, "Nothing running.")


# ── targets / scope ──
@guard
async def cmd_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    c = STORE.chat(cid)
    if not ctx.args:
        existing = sorted(p.name for p in (BASE_DIR / "targets").iterdir() if p.is_dir())
        rows = [[(("● " if t == c.target else "") + t, f"tg:{t}")] for t in existing[:30]]
        return await reply(update, "Targets (tap to switch, or /target <new-name>):", reply_markup=kb(rows))
    await reply(update, await do_target(cid, slug(ctx.args[0])))


async def do_target(cid: int, t: str) -> str:
    c = STORE.chat(cid)
    if runner(cid).busy:
        return "Busy — /stop first."
    ensure_target(t)
    c.target = t
    # reuse the most recent session for this target, or start one
    mine = [s for s in c.sessions.values() if s.target == t]
    if mine:
        c.active = max(mine, key=lambda s: s.created).name
    else:
        cur = c.sessions[c.active]
        name = f"{t}-main" if f"{t}-main" not in c.sessions else time.strftime(f"{t}-%m%d%H%M")
        c.sessions[name] = Session(name=name, target=t, model=cur.model, effort=cur.effort, mode=cur.mode)
        c.active = name
    await runner(cid).close()
    STORE.save()
    scope = (tdir(t) / "scope.md").read_text()
    hint = "" if "\n- " in scope or "*." in scope else "\n⚠️ scope.md is empty — add scope with /scope add …"
    return f"🎯 Target '{t}', session '{c.active}'.{hint}"


@guard
async def cmd_scope(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    f = tdir(c.target) / "scope.md"
    text = update.effective_message.text
    body = text.split(None, 2)[2] if len(text.split(None, 2)) > 2 else ""
    sub = ctx.args[0] if ctx.args else ""
    if sub == "add" and body:
        cur = f.read_text()
        lines = "\n".join(l if l.startswith(("-", "#")) else f"- {l}" for l in body.splitlines() if l.strip())
        cur = cur.replace("## In scope\n", f"## In scope\n{lines}\n", 1) if "## In scope\n" in cur else cur + "\n" + lines + "\n"
        f.write_text(cur)
    elif sub == "set" and body:
        f.write_text(body + "\n")
    elif sub == "clear":
        f.unlink()
        ensure_target(c.target)
    elif sub:
        return await reply(update, "Usage: /scope · /scope add <lines> · /scope set <full text> · /scope clear")
    await send_text(ctx.bot, update.effective_chat.id, f.read_text(), "scope.md")


# ── secrets ──
async def _delete_secret_msg(update: Update) -> str:
    try:
        await update.effective_message.delete()
        return " (your message was deleted from the chat)"
    except Exception:
        return " — delete your message manually, it contains the secret"


@guard
async def cmd_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.effective_message.text.split(None, 2)
    if len(parts) < 3:
        return await reply(update, "Usage: /cookie <domain> <name=value; name2=value2>")
    c = STORE.chat(update.effective_chat.id)
    data = load_secrets(c.target)
    val = parts[2].strip()
    val = re.sub(r"^cookie:\s*", "", val, flags=re.I)
    data["cookies"][parts[1].lower()] = val
    save_secrets(c.target, data)
    note = await _delete_secret_msg(update)
    await secret_saved(update, ctx, f"cookies for {parts[1].lower()} (auth.json → cookies[\"{parts[1].lower()}\"])", note)


@guard
async def cmd_header(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.effective_message.text.split(None, 2)
    if len(parts) < 3 or ":" not in parts[2]:
        return await reply(update, "Usage: /header <domain> <Header-Name: value>")
    c = STORE.chat(update.effective_chat.id)
    data = load_secrets(c.target)
    hname, hval = parts[2].split(":", 1)
    data["headers"].setdefault(parts[1].lower(), {})[hname.strip()] = hval.strip()
    save_secrets(c.target, data)
    note = await _delete_secret_msg(update)
    await secret_saved(update, ctx, f"header {hname.strip()} for {parts[1].lower()} (auth.json → headers)", note)


@guard
async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.effective_message.text.split(None, 2)
    if len(parts) < 3:
        return await reply(update, "Usage: /note <key> <value>  (e.g. /note testacct user@x.com:Passw0rd)")
    c = STORE.chat(update.effective_chat.id)
    data = load_secrets(c.target)
    data.setdefault("notes", {})[parts[1]] = parts[2]
    save_secrets(c.target, data)
    note = await _delete_secret_msg(update)
    await secret_saved(update, ctx, f"secret note '{parts[1]}' (auth.json → notes)", note)


@guard
async def cmd_creds(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    data = load_secrets(c.target)
    if ctx.args and ctx.args[0] == "clear":
        save_secrets(c.target, {"cookies": {}, "headers": {}, "notes": {}})
        return await reply(update, f"Cleared all secrets for {c.target}.")
    if len(ctx.args) >= 2 and ctx.args[0] == "rm":
        k = ctx.args[1]
        for sec in ("cookies", "headers", "notes"):
            data.get(sec, {}).pop(k, None)
        save_secrets(c.target, data)
        return await reply(update, f"Removed '{k}'.")
    lines = [f"Secrets for {c.target}:"]
    for d, v in data.get("cookies", {}).items():
        lines.append(f"🍪 {d}: {mask(v)}")
    for d, hs in data.get("headers", {}).items():
        for h, v in hs.items():
            lines.append(f"🔑 {d}: {h}: {mask(v)}")
    for k, v in data.get("notes", {}).items():
        lines.append(f"📝 {k}: {mask(v)}")
    await reply(update, "\n".join(lines) if len(lines) > 1 else f"No secrets saved for {c.target}.")


# ── files / shell ──
def safe_path(target: str, rel: str) -> Path:
    base = tdir(target).resolve()
    p = (base / rel).resolve() if not rel.startswith("/") else Path(rel).resolve()
    if not (p == base or base in p.parents or BASE_DIR.resolve() in p.parents):
        raise ValueError("path outside the bounty workspace")
    return p


@guard
async def cmd_get(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await reply(update, "Usage: /get <path relative to target>")
    c = STORE.chat(update.effective_chat.id)
    p = safe_path(c.target, " ".join(ctx.args))
    if "secrets" in p.parts:
        return await reply(update, "Not sending secrets files back over Telegram.")
    if not p.is_file():
        return await reply(update, "Not a file.")
    if p.stat().st_size > 49 * 1024 * 1024:
        return await reply(update, "Too big for Telegram (50 MB).")
    with p.open("rb") as fh:
        await ctx.bot.send_document(update.effective_chat.id, fh, filename=p.name)


@guard
async def cmd_ls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    p = safe_path(c.target, " ".join(ctx.args) if ctx.args else ".")
    if not p.is_dir():
        return await reply(update, "Not a directory.")
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))[:80]
    out = [f"📂 {p}"] + [f"{'📁' if e.is_dir() else '📄'} {e.name}" + ("" if e.is_dir() else f" ({e.stat().st_size} B)") for e in entries]
    await reply(update, "\n".join(out))


@guard
async def cmd_findings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = STORE.chat(update.effective_chat.id)
    fs = sorted((tdir(c.target) / "findings").glob("*.md"), key=lambda p: -p.stat().st_mtime)
    if not fs:
        return await reply(update, "No findings yet.")
    await reply(update, "Findings (newest first):\n" + "\n".join(f"• findings/{p.name}" for p in fs[:40]) + "\n\n/get findings/<file>")


@guard
async def cmd_sh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    cmd = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
    if not cmd:
        return await reply(update, "Usage: /sh <command>")
    c = STORE.chat(update.effective_chat.id)
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(tdir(c.target)), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), SH_TIMEOUT)
        rc = proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        out, rc = b"(timed out)", -1
    body = out.decode(errors="replace") or "(no output)"
    await send_text(ctx.bot, update.effective_chat.id, f"$ {cmd}\n{body}\n[exit {rc}]", "sh-output.txt")


@guard
async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    c = STORE.chat(update.effective_chat.id)
    up = tdir(c.target) / "uploads"
    if msg.document:
        tgf = await msg.document.get_file()
        name = slug(msg.document.file_name or "file")
    elif msg.photo:
        tgf = await msg.photo[-1].get_file()
        name = f"photo-{int(time.time())}.jpg"
    else:
        return
    dest = up / name
    if dest.exists():
        dest = up / f"{dest.stem}-{int(time.time())}{dest.suffix}"
    await tgf.download_to_drive(str(dest))
    rel = dest.relative_to(tdir(c.target))
    if msg.caption:
        await start_turn(update, ctx, f"[Operator uploaded a file: {rel}]\n\n{msg.caption}")
    else:
        await reply(update, f"📥 Saved {rel}. Mention it in your next message, or resend with a caption to act on it now.")


@guard
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    if text.startswith("//"):
        text = text[1:]  # pass through as a Claude Code slash command
    await start_turn(update, ctx, text)


# ── callbacks ──
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorized(update):
        return await q.answer("Not allowed", show_alert=True)
    cid = update.effective_chat.id
    data = q.data or ""
    kind, _, rest = data.partition(":")
    if kind == "ap":
        rid, _, ans = rest.partition(":")
        item = PENDING.get(rid)
        if not item or item[0].done():
            await q.answer("Expired")
            return await q.edit_message_reply_markup(None)
        item[0].set_result(ans)
        label = {"y": "✅ allowed", "a": f"✅ always allowed {item[3]}", "n": "❌ denied", "s": "🛑 denied + stopped"}[ans]
        await q.answer()
        return await q.edit_message_text(f"{q.message.text}\n\n→ {label}")
    if kind == "int":
        await q.answer("Interrupting…")
        await q.edit_message_reply_markup(None)
        r = runner(cid)
        if r.busy and r.queue:
            r.queue[0] = "[Operator interrupted you to say:]\n" + r.queue[0]
        return await r.interrupt()
    if kind == "cont":
        await q.answer()
        await q.edit_message_reply_markup(None)
        r = runner(cid)
        if r.busy:
            return
        r.task = asyncio.create_task(r.loop(ctx.bot, "Continue with the task using the credentials I just saved."))
        return
    if kind == "set":
        f, _, v = rest.partition(":")
        await q.answer()
        return await q.edit_message_text(await apply_setting(cid, f, v))
    if kind == "sw":
        await q.answer()
        return await q.edit_message_text(await do_switch(cid, rest))
    if kind == "tg":
        await q.answer()
        return await q.edit_message_text(await do_target(cid, rest))
    await q.answer()


# ─────────────────────────── main ─────────────────────────────
COMMANDS = [
    ("new", "start a fresh Claude session"), ("sessions", "list/switch sessions"),
    ("stop", "interrupt running task"), ("status", "current settings & cost"),
    ("model", "change model"), ("effort", "change effort"), ("mode", "permission mode"),
    ("target", "switch program workspace"), ("scope", "show/edit scope"),
    ("cookie", "save cookies for a domain"), ("header", "save auth header"),
    ("creds", "list saved secrets (masked)"), ("findings", "list findings"),
    ("get", "download a file"), ("ls", "list files"), ("sh", "run shell command"),
    ("verbose", "toggle tool-call messages"), ("output", "toggle command output"),
    ("thinking", "toggle showing thinking"), ("auto", "autopilot on/off"),
    ("yolo", "full-permission autopilot"), ("resume", "auto-resume after usage limit"),
    ("advise", "strategic advisor on this target"), ("recover", "auto-restart on errors"), ("help", "help"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


def main() -> None:
    global STORE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not BOT_TOKEN or not ALLOWED:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS in .env")
    (BASE_DIR / "targets").mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(BASE_DIR, 0o700)
    STORE = Store(STATE_FILE)

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    h = [
        (["start", "help"], cmd_start), (["status"], cmd_status), (["new"], cmd_new),
        (["sessions"], cmd_sessions), (["switch"], cmd_switch), (["rename"], cmd_rename), (["del"], cmd_del),
        (["model"], cmd_model), (["effort"], cmd_effort), (["mode"], cmd_mode),
        (["verbose", "thinking", "output"], cmd_toggle), (["allowed"], cmd_allowed), (["stop"], cmd_stop),
        (["auto"], cmd_auto), (["yolo"], cmd_yolo), (["resume"], cmd_resume),
        (["advise", "advisor"], cmd_advise), (["recover"], cmd_recover),
        (["target"], cmd_target), (["scope"], cmd_scope), (["cookie"], cmd_cookie),
        (["header"], cmd_header), (["note"], cmd_note), (["creds"], cmd_creds),
        (["get"], cmd_get), (["ls"], cmd_ls), (["findings"], cmd_findings), (["sh"], cmd_sh),
    ]
    for names, fn in h:
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    # plain text, plus "//slash" passthrough (which Telegram would otherwise treat as an unknown command)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND | filters.Regex(r"^//")), on_text))
    log.info("claude-tg up · base %s · users %s · cli %s · chrome %s", BASE_DIR, sorted(ALLOWED),
             CLI_PATH or "bundled", "on" if ENABLE_CHROME else "off")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
