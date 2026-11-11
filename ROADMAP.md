# Roadmap — suggested features

Ordered roughly by value-to-effort. Ask and I'll build any of these.

## High value
- **`/intake <engagement-url>`** — one command: create the target, drive Chrome to the Bugcrowd/
  HackerOne brief, parse it into `scope.md`, and read the scope back. Removes the paste problem
  entirely.
- **Idle-guard for autopilot** — pause autopilot automatically when a turn produces no tool calls
  and no file changes (true "no progress" detection), as a belt-and-suspenders to the methodology
  rule. Stops any future spin-loop regardless of what the prompt says.
- **Finding → report formatter** — `/report <finding>` turns a `findings/*.md` into the program's
  expected submission format (clear repro, impact, CVSS), ready to paste into Bugcrowd.
- **Usage-limit visibility** — show real Pro/Max limit status in `/status` instead of (or beside)
  the API-equivalent dollar meter, if the SDK/CLI exposes it; otherwise parse it from limit errors.
- **Per-target scope guard in the tool layer** — before any Bash/browser/burp call, check the
  target/URL against `scope.md` in `can_use_tool` and hard-deny out-of-scope hosts. Makes scope
  enforcement mechanical, not just an instruction. Big safety win for `/yolo`.

## Medium
- **Scheduled recon** — a nightly job that re-runs passive recon on active targets and pings you
  on new subdomains/endpoints (subdomain-takeover watch).
- **Dedupe against known issues** — pull the program's known-issues/changelog and have Claude
  check findings against them before you submit.
- **Multiple operators / per-chat isolation** — already keyed by chat id; add named profiles and
  per-target credentials vaulting.
- **`/diff`** — show what changed in `recon/` and `findings/` since you last looked.
- **Richer secrets** — store and auto-inject Authorization/refresh tokens, rotate test-account
  sessions, and wire them into tool commands automatically.

## Nice to have
- **Voice notes → prompts** (Telegram voice message transcription).
- **Inline screenshots** from `gowitness`/Chrome sent to the chat for fast triage.
- **Cost/effort auto-tuning** — drop to Haiku/medium for grunt work, escalate to Opus/max for
  analysis, automatically.
- **Web dashboard** — a small local page (served on the VM) showing live progress, findings and
  session costs, as an alternative to Telegram for when you're at the desk.

## Workflow improvements (no code)
- Settle on the browser-read or file intake over pasting — it's what caused the early errors.
- Keep `/effort medium` as the default for recon; reserve `high`/`max` for analysis and exploitation.
- Run strict programs in `/auto on` (approvals on), not `/yolo`, until you trust the behavior.
