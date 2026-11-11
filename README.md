# claude-bounty

Run Claude Code on a Windows VM and drive it from Telegram for **authorized** bug bounty work.
One Telegram chat controls everything — sessions, model, permissions, scope, credentials, files —
while Claude drives Chrome and Burp on the VM, follows a methodology on every target, works
autonomously when told to, and recovers itself from errors and usage limits.

```
 Telegram  ──►  bot.py  ──►  Claude Code (your Pro/Max login)
                               │
                               ├── Chrome (Claude extension) ──┐
                               ├── Burp Suite (MCP) ───────────┤──► the target, in scope
                               └── recon tools (subfinder…)  ──┘
                               │
   C:\bounty\targets\<program>\  scope.md · PROGRESS.md · recon/ · findings/ · secrets/ · uploads/
```

---

## Quick start

**1. Prereqs on the VM (once):**

| Need | Install |
|---|---|
| Python 3.12+ | `winget install Python.Python.3.12` |
| Claude Code | `irm https://claude.ai/install.ps1 \| iex` → then run `claude` and `/login` |
| Git for Windows | `winget install Git.Git` |

Sign in with `/login` (Pro/Max). Do **not** set an API key — it turns the Chrome integration off.

**2. Install everything (admin PowerShell, in this folder):**

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

One script installs the recon toolkit, the bot's Python env, the methodology, the `.env` template
and an autostart task.

**3. Configure and launch:**

```powershell
notepad .env                 # TELEGRAM_BOT_TOKEN + ALLOWED_USER_IDS (leave the Claude token blank)
Start-ScheduledTask claude-tg
```

Open Burp (enable the **MCP** tab and its auto-approve) and start the proxied Chrome. Then in
Telegram: `/start` → `/target <name>`.

> Full VM/Chrome/Burp walkthrough is in the setup guide shared separately.

---

## A target, start to finish

```
/target klaviyo                      # make/switch the workspace
(send the brief as a .txt, caption:  "build scope.md and read the scope back")
                                     # → Claude writes scope.md, confirms in/out of scope
/advise                              # where the best bugs likely are, what to do first
/auto on                             # autopilot: it works through the methodology on its own
…                                    # recon → mapping → testing, writing findings/ as it goes
/findings   →   /get findings/<file>
```

Later, same target, fresh chat: `/target klaviyo` → it reads `PROGRESS.md` and picks up where it
stopped. Nothing is lost between sessions.

---

## Commands

| Group | Commands |
|---|---|
| **Sessions** | `/new` `/sessions` `/switch` `/rename` `/del` `/stop` `/status` |
| **Settings** | `/model` `/effort` `/mode` `/verbose` `/output` `/thinking` `/allowed` |
| **Autonomy** | `/auto on\|off` · `/yolo` (autopilot + no approvals) · `/advise` (strategy review) |
| **Resilience** | `/resume on\|off` (wait out usage limits) · `/recover on\|off` (restart on errors) |
| **Bug bounty** | `/target` `/scope` `/cookie` `/header` `/note` `/creds` `/findings` `/get` `/ls` `/sh` |

Plain text = a prompt to Claude · a file = saved to `uploads/` (caption = instruction) ·
`//cmd` = a Claude Code slash command (e.g. `//compact`).

---

## How it keeps going on its own

- **Methodology** (`C:\bounty\METHODOLOGY.md`, loaded via `CLAUDE.md` for every target): a standing
  process — session-start protocol, scope discipline, recon → mapping → testing by bug class,
  bypass thinking, and `PROGRESS.md` tracking.
- **PROGRESS.md** is the memory. State lives in files, not the chat, so a wedged chat, a reboot or
  `/new` never loses the thread.
- **Auto-recovery** (`/recover`): on a turn error it starts a fresh conversation on the same target
  and resumes from the files — capped at a few tries so it never spins, and a genuine refusal still
  stops it.
- **Auto-resume** (`/resume`): on a Pro/Max usage limit it waits for the reset and continues.

---

## Gotchas (things that bit during setup)

- **Pasting a whole program page is unreliable** — Telegram splits it and the pieces arrive as
  separate turns. Send the brief as a **.txt file**, or have Claude read the engagement URL in
  Chrome. Don't combine `/target` and the paste in one message.
- **`BASE_DIR` must be set** in `.env` (to `C:\bounty`). If it's blank the bot works in
  `C:\Users\<you>\bounty` instead, and your tools/methodology won't load. `/status` shows the live
  folder — check it's under `C:\bounty`.
- **Two approval layers.** `AUTO_ALLOW_TOOLS` silences the *bot's* Telegram prompt; Burp's MCP tab
  has its *own* approval that you also disable there. `/yolo` drops the bot's shell prompts too.
- **Leaving the VM:** close the RDP window, **don't sign out** — signing out kills the bot, Chrome
  and Burp.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't reply | `bot.log`; `Start-ScheduledTask claude-tg`; confirm your id is in `ALLOWED_USER_IDS` |
| No browser tools | `claude` → `/chrome` → Reconnect; restart Chrome; `ENABLE_CHROME=1`, no token set |
| `burp` fails in `/mcp` | Burp not open or MCP tab not Enabled; try the stdio jar |
| Still asked to approve Burp calls | enable auto-approve in Burp's MCP tab **and** restart the bot |
| HTTPS warnings in Chrome | install Burp's CA into Trusted Root |
| Can't find a target folder | it's under `BASE_DIR`; check `/status` |
| Runs cost a lot | it's an API-equivalent meter, not a charge on Pro; drop `/effort`, switch `/model` |

---

## Files

| File | What it is |
|---|---|
| `setup.ps1` | one-shot installer — **the only file you run** |
| `bot.py` | the Telegram ↔ Claude Code bridge |
| `scripts/install-tools.ps1` | recon toolkit installer (called by setup) |
| `config/METHODOLOGY.md` | the process Claude follows per target |
| `.env.example` | configuration template |
| `start-bot.bat` | run/restart wrapper for the autostart task |
| `ROADMAP.md` | suggested next features |

---

## Safety

- Only `ALLOWED_USER_IDS` can drive the bot — turn on Telegram two-step verification.
- **`scope.md` is the only thing keeping Claude on authorized assets.** Set it before testing. On a
  strict program, prefer `/auto on` (approvals on) over `/yolo` until you trust the behavior.
- Auto-recovery recovers from errors; it does not bypass Claude's guardrails — each fresh session
  has them in full.
- Use short-lived **test-account** cookies only; Telegram bot chats aren't end-to-end encrypted.
- Only test programs you're authorized on, within their rules. This tool does nothing to change
  what's legal — scope and program policy are yours to honor.
