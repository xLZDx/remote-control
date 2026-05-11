> **Inherits global rules from `D:\test 2\CLAUDE.md`** — approval gate, no-guessing, regression tests, git lifecycle (including todo-in-commits), shell pre-approval, D:-drive-only disk policy. Read that file too.

# Remote Control — Project Context

## Layout
- Working directory: `D:\test 2\Remote control`
- Python venv: `venv\`
- App package: `app/` — host (`host/`), client (`client/`), shared (`shared/protocol.py`, `shared/crypto.py`)
- Tests: `tests/` (pytest)
- Build scripts: `build.ps1` (PowerShell)
- Installer output: `installer/`, `dist/`, `build/`
- Plans & docs: `core/` (canonical implementation plan), `docs/`

## What this is
Self-contained Windows-to-Windows remote desktop tool. Two standalone EXEs (no Python required on either machine).
- TLS-encrypted direct TCP, default port `7777`.
- Self-signed cert generated on first launch; client pins it after first successful connect.
- Auth: PIN-derived key + challenge-response (PIN never sent in plaintext).
- Single H.264 encode broadcast to all connected viewers.
- No third-party servers, no signaling, no STUN/TURN.
- Target: host has public IP, or both PCs on same LAN.

## Defaults
- Both ends must be Windows 10/11.
- Inbound TCP on the chosen port must be allowed by Windows Firewall (installer adds the rule).
- Self-signed certs: never reuse keys across machines.

## Tests
- `tests/test_protocol.py`, `tests/test_crypto.py`, `tests/test_handshake.py`.
- `pytest.ini` configured; run `venv\Scripts\python.exe -m pytest tests/ -x` after every change.
- 0 failures gate every push.

## Plan Source of Truth
- `core/IMPLEMENTATION_PLAN.md` — phased implementation plan. Update when scope changes.
- All planning docs go under `core/`.
- Build runbook: `docs/BUILD.md`. Security notes: `docs/SECURITY.md`.

## Security-sensitive code
This project handles cryptography, TLS, and remote code paths. Apply extra rigor on:
- Any change to `shared/crypto.py` or `shared/protocol.py` needs a regression test that exercises the wire format end-to-end (handshake, key derivation, frame encryption).
- Never weaken the TLS posture (no self-signed-auto-accept on the host, no key reuse).
- PIN entropy must remain ≥6 digits; never log raw PINs.
