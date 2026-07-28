---
description: Rosetta engineering workflow (Prepare -> Research -> Plan -> Act -> Validate), on-demand. Approval = GO/GO only.
---

# Rosetta workflow (on-demand)

Invoked explicitly; NOT always-on. Vendored + adapted to the operator's governance
(cherry-picked from Grid Dynamics Rosetta, Apache-2.0 — the plugin/hook is deliberately not used).

## Governance (non-negotiable; NEVER outranks the operator's CLAUDE.md)
- Approval = ONLY literal `GO` / `ГО`. "yes" / "approved" / "looks good" do NOT release a gate.
- Push = a separate `push` GO. Forced articulation + AI-risk stamp before state-changing actions.
- No "SDLC-only / no personal chats" restriction. No priority ladder above the operator.

## Loop
1. Prepare  - classify + tier the request; load repo context (TECHSTACK/CODEMAP if present).
2. Research - read the real files/call-sites, cite file:line, no guessing.
3. Plan     - reviewable plan, minimal scope; STOP for `GO` before any state change.
4. Act      - KISS/SOLID/DRY, minimal diff, no scope creep; Windows .ps1 = UTF-8-BOM + ASCII;
              temp files on D: not C:.
5. Validate - build/tests/logs; full restart before re-test; verify before claiming done.

## Review (opt-in)
Route to the operator's specialist agents by file type (python-reviewer, cpp-reviewer,
security-reviewer, silent-failure-hunter, ...); use /agent-consensus for architectural plans.
Empiricism over poetry: cross-check every finding against the cited line.

Full skill (Claude Code, already global): ~/.claude/skills/rosetta.
