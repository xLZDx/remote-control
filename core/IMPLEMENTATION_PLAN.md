# RemoteControl — Comprehensive Implementation Plan

> **Status:** Living document. Updated as phases complete.
> **Scope (approved):** Self-contained Windows-to-Windows remote desktop tool.
> Two standalone EXEs (no Python required on either PC). Direct TLS-over-TCP,
> PIN-based mutual authentication, multi-viewer, with all extras (clipboard,
> file transfer, audio later). Self-hosted on a PC with a dedicated public IP.

---

## 0. Goals and non-goals

### Goals
- **Zero external services** — no STUN, no TURN, no signaling broker, no relay.
- **Two installable Windows EXEs** with no end-user prerequisites.
- **3-step pairing** — install, show PIN on host, enter PIN on client.
- **Multi-viewer** — host accepts N concurrent viewers, encode-once-fan-out.
- **Full control** — keyboard, mouse, scroll, modifiers, all special keys.
- **Reasonable security** — TLS 1.2+, PIN never in plaintext, cert pinning.

### Non-goals
- Linux/Mac client (Windows-only).
- Kernel driver for secure-desktop input (UAC/Ctrl+Alt+Del bypass).
- Code signing of binaries (unsigned ship; user may need to whitelist in AV).
- WebRTC, mDNS auto-discovery, or any decentralized rendezvous.
- Audited security review (personal use, not commercial product).

---

## 1. Architecture overview

```
+--------------------------+               +---------------------------+
|   Host PC                |               |   Client PC               |
|   ---------------------- |               |   ----------------------- |
|   dxcam capture          |               |   PyQt6 viewer window     |
|       v                  |   TLS/TCP     |       ^                   |
|   PyAV H.264 encoder     |  port 7777    |   PyAV H.264 decoder      |
|       v                  |  (1 conn per  |       ^                   |
|   FrameBroadcaster ----->|   viewer)     |<----- HostClient (asyncio)|
|       ^   |              |               |       |                   |
|   InputInjector <--------|               |       v                   |
|   (SendInput)            |               |   InputCapture (Qt)       |
|       ^                  |               |       v                   |
|   pystray + PIN window   |               |   Connect dialog + tray   |
+--------------------------+               +---------------------------+
```

**Single TLS connection per viewer carries all message types:**
HELLO, AUTH, VIDEO_CONFIG, VIDEO_FRAME, INPUT_EVENT, CLIPBOARD, FILE_*, PING.

---

## 2. Tech stack

| Layer | Library | Why |
|---|---|---|
| Capture | `dxcam` 0.0.5 | DXGI Desktop Duplication, ~240 FPS capable |
| Encode/decode | `av` (PyAV) 13.x | FFmpeg bindings, H.264 baseline/main |
| Network | stdlib `asyncio` + `ssl` | No third-party network deps |
| TLS cert | `cryptography` 43.x | Self-signed cert generation |
| Input injection | `ctypes` + Win32 `SendInput` | Native, no extra DLLs |
| Client UI | `PyQt6` 6.7.x | QPainter/QImage video rendering, modern Qt |
| Host tray | `pystray` 0.19.x + Pillow | Minimal Win32 tray |
| Auth | stdlib `hashlib`/`hmac`/`secrets` | PBKDF2-HMAC-SHA256, HMAC challenge |
| Packaging | `PyInstaller` 6.x | Onefile EXE per role |
| Installer | Inno Setup 6 | Single combined `.exe` installer |
| Tests | `pytest`, `pytest-asyncio` | Unit tests for protocol/auth/injector |

All native dependencies (PyAV's FFmpeg DLLs, dxcam's CFFI, PyQt6's Qt DLLs)
are picked up automatically by PyInstaller hooks.

---

## 3. On-the-wire protocol

**Frame layout** (over TLS, length-prefixed):
```
+--------+--------+----------+---------------------+
| type:1 | len:4  | payload  |  (next message...)  |
+--------+--------+----------+---------------------+
```
- `type` is `MessageType` (uint8).
- `len` is uint32 big-endian, payload length in bytes.
- `MAX_FRAME_BYTES = 16 MiB` hard cap.

**Message types** (see [app/shared/protocol.py](app/shared/protocol.py)):

| # | Name | Direction | Payload | Notes |
|---|---|---|---|---|
| 1 | HELLO | C->S | JSON {version, client_name} | First message |
| 2 | HELLO_ACK | S->C | JSON {version, host_name, monitors} | After HELLO |
| 3 | AUTH_CHALLENGE | S->C | JSON {salt, nonce} (hex) | Random per session |
| 4 | AUTH_RESPONSE | C->S | JSON {proof} (hex) | HMAC(PBKDF2(pin,salt), nonce) |
| 5 | AUTH_OK | S->C | JSON {view_only} | Begin streaming |
| 6 | AUTH_FAIL | S->C | JSON {reason} | Connection closes after |
| 7 | ERROR | both | JSON {reason} | Fatal error |
| 8 | PING / 9 PONG | both | empty | Keepalive |
| 10 | VIDEO_CONFIG | S->C | JSON {width, height, fps, codec} | Sent on monitor change |
| 11 | VIDEO_FRAME | S->C | binary header + H.264 NALu(s) | See below |
| 12 | INPUT_EVENT | C->S | JSON event | Mouse/key, see Phase 3 |
| 13 | CLIPBOARD | both | JSON {text} | Sync trigger |
| 14 | FILE_OFFER | both | JSON {id, name, size} | Phase 5 |
| 15 | FILE_CHUNK | both | binary | Phase 5 |
| 16 | FILE_ACK | both | JSON {id, offset} | Phase 5 |
| 17 | BYE | both | empty | Graceful disconnect |
| 18 | REKEY | reserved | - | Future |
| 19 | SET_MONITOR | C->S | JSON {index} | Switch monitor |
| 20 | SET_VIEW_ONLY | C->S | JSON {view_only} | Per-client perms |

**VIDEO_FRAME payload format:**
```
+---------------+--------+--------------------+
| ts:8 BE us    | flag:1 | h264 NALu bytes    |
+---------------+--------+--------------------+
```
`flag bit 0` = keyframe (IDR).

---

## 4. Authentication

PIN never traverses the wire.

1. Server sends `AUTH_CHALLENGE { salt: 16B, nonce: 16B }`.
2. Client computes `key = PBKDF2-HMAC-SHA256(pin, salt, iter=200_000, dklen=32)`.
3. Client sends `AUTH_RESPONSE { proof: HMAC-SHA256(key, nonce) }`.
4. Server recomputes the proof (it knows the PIN) and compares with `compare_digest`.
5. On success: `AUTH_OK`. On failure: `AUTH_FAIL` and the connection drops.

**TLS layer** is independent: a self-signed cert protects against passive
sniffing of the entire conversation. The client pins the cert fingerprint
on first connect and refuses subsequent connects on mismatch.

**Brute-force resistance:** PIN is regenerated on disconnect by default. PIN
is 6 digits = 10^6 space; PBKDF2 200k makes online brute force ~30 days at
0.4 attempts/sec (TLS handshake overhead). Fast enough for personal use,
slow enough to be safe; 8-digit PIN option available in settings.

---

## 5. Phases and deliverables

Each phase ends with a git commit. Tests added/updated per CLAUDE.md rule.

### Phase 1 — Network + auth foundation [STATUS: DONE — commit ea48c74, 25 tests passing]

**Files**
- `app/shared/config.py` — paths, constants, AppConfig
- `app/shared/protocol.py` — frame format, MessageType, read/write
- `app/shared/crypto.py` — PIN, PBKDF2, HMAC challenge, cert gen, fingerprint
- `app/host/server.py` — `HostServer`, `ClientSession`, multi-client loop
- `app/host/main.py` — host entry point
- `app/client/tcp_client.py` — `HostClient` with cert pinning + handshake
- `app/client/main.py` — client entry point (CLI smoke test)

**Tests**
- `tests/test_protocol.py` — round-trip framing, oversize rejection
- `tests/test_crypto.py` — PIN gen, PBKDF2 determinism, proof verify, cert
- `tests/test_handshake.py` — full HELLO->AUTH_OK over loopback

**Acceptance**
- A client can connect, authenticate with the right PIN, and stay connected
  on the keepalive loop. Wrong PIN -> `AUTH_FAIL`. Wrong cert -> abort.
- `pytest` passes 100%.

---

### Phase 2 — Video pipeline [STATUS: DONE — commit 12dbbbf, 8 tests passing]

**Files**
- `app/host/capture.py` — `ScreenCapture`: dxcam loop, monitor enumeration,
  monitor switch, region cropping. Exposes async iterator of BGR frames.
- `app/host/encoder.py` — `H264Encoder`: PyAV-backed, configurable bitrate
  / FPS / GOP. Forces keyframe on demand (e.g. new viewer joins).
- `app/host/frame_broadcaster.py` — `FrameBroadcaster`: holds last keyframe
  buffer; new subscribers receive keyframe first, then live stream. One
  encode shared across N viewers.
- `app/client/decoder.py` — `H264Decoder`: PyAV decode -> RGBA QImage frame.
- Wires capture -> encoder -> broadcaster into `HostServer.broadcast`.

**Tests**
- `tests/test_encoder.py` — encode 30 synthesized frames, decode them back,
  verify frame count + dimensions.
- `tests/test_broadcaster.py` — N subscribers each receive keyframe+stream.

**Acceptance**
- Headless smoke test: host streams a synthetic test pattern at 30 FPS;
  client decodes; bytes-in == bytes-out within +/-1 frame.
- 2 concurrent viewers can receive the same stream.

---

### Phase 3 — Input injection + capture [STATUS: DONE — commit 7033a9e, 30 tests passing]

**Files**
- `app/host/input_injector.py` — `InputInjector` with:
  - `move_mouse(x, y, abs=True)` -> `SendInput` MOUSEEVENTF_ABSOLUTE
  - `mouse_button(button, down)` for L/M/R/X1/X2
  - `mouse_wheel(delta_x, delta_y)`
  - `key(vk, scan, down, extended)` -> `SendInput` KEYBDINPUT with `KEYEVENTF_SCANCODE`
  - Full Windows VK -> scancode map for special keys (F1-F24, arrows, modifiers, etc.)
- `app/client/input_capture.py` — Qt event filter: `QMouseEvent`,
  `QKeyEvent`, `QWheelEvent` -> wire-format INPUT_EVENT JSON. Maps Qt
  keycodes to Windows VK + scancode.
- Wires `HostServer.on_input_event` -> injector. Skips when `view_only`.

**Tests**
- `tests/test_input_injector.py` — patches `SendInput` ctypes call, verifies
  correct INPUT struct layout and flags for representative events.

**Acceptance**
- Manual: mouse and keyboard from client window control host PC. Modifier
  combinations (Alt+Tab, Ctrl+Shift+arrow), function keys, and arrow keys
  all work.
- View-only mode blocks all input even if events sent.

---

### Phase 4 — UI [STATUS: DONE — commit 5c0dbb7, 9 tests passing]

**Files**
- `app/host/tray.py` — `pystray` icon. Menu: Show PIN window, Regenerate PIN,
  Open settings, Quit. Tooltip shows connected-viewer count.
- `app/host/ui_pin.py` — Qt window: shows "Address: <ip>:<port>", "PIN:
  <digits>", cert fingerprint short form, "Regenerate" / "Copy" buttons,
  list of currently connected viewers (per-client view-only toggle).
- `app/client/connect_dialog.py` — Qt dialog: address+port+PIN entry, history
  dropdown, "Trust new fingerprint?" prompt for first-connect.
- `app/client/viewer_window.py` — Qt window:
  - QLabel/QWidget with `paintEvent` rendering the latest decoded QImage
  - Toolbar: disconnect, monitor selector, view-only toggle, fullscreen, FPS counter
  - Captures input events via event filters, forwards to client
  - Adaptive scaling: maintains aspect ratio, scales QImage on resize
- `app/shared/role_picker.py` — first-launch role picker dialog
  ("Share this PC" / "Connect to a PC")

**Tests**
- `tests/test_input_capture.py` — Qt-event -> wire-format mapping (no Qt
  app needed; test the pure mapping function).

**Acceptance**
- Both EXEs runnable end-to-end with full UX: install -> launch ->
  share/connect -> live screen + control.
- Connection status visible on both ends; clean disconnect on close.

---

### Phase 5 — Extras [STATUS: DEFERRED — clipboard, file transfer, settings dialog, multi-monitor switch wiring not yet implemented]

**Files**
- Clipboard sync: extend `tray.py` and `viewer_window.py` to watch clipboard
  changes (`QClipboard.dataChanged`) and emit/consume `CLIPBOARD` messages.
- File transfer: drag-and-drop on viewer window initiates `FILE_OFFER` ->
  chunked `FILE_CHUNK` (64 KiB chunks) -> `FILE_ACK`. Saves to host
  Downloads folder; reverse direction supported.
- `app/host/settings_window.py` — Qt dialog: port, default PIN behavior,
  bitrate cap, autostart-with-Windows toggle (`HKCU\...\Run` registry).
- Multi-monitor selector: capture module already supports; UI exposes
  toolbar dropdown synced via `SET_MONITOR`.
- (Optional) Audio: WASAPI loopback -> Opus track over the same connection.

**Tests**
- `tests/test_file_transfer.py` — chunked transfer round-trip in-memory.
- `tests/test_clipboard.py` — text payload framing.

**Acceptance**
- Copy on host, paste on client, and vice versa.
- File drop on viewer pops up "saving to <path>" toast on host.
- Settings persist across restart in `%APPDATA%\RemoteControl\config.json`.

---

### Phase 6 — Package + tests + docs [STATUS: DONE — commit ebf416b; PyInstaller bundle builds (5.9 MB exe, 210 MB onedir) and the EXE successfully spawns the role picker on this machine]

**Files**
- `installer/host.spec` — PyInstaller spec for `RemoteControl.exe` (single
  EXE, role chosen at launch via `app/main.py`).
- `installer/installer.iss` — Inno Setup script:
  - Installs to `Program Files\RemoteControl`
  - Adds Start Menu shortcut
  - Adds Windows Firewall inbound rule for chosen port (via `netsh advfirewall`)
  - Optional Run-at-startup checkbox -> `HKCU\...\Run`
  - Per-user install option (no admin needed) for client-only use
- `build.ps1` — one-command: `pip install -r requirements.txt`, run pytest,
  `pyinstaller installer/host.spec`, `iscc installer/installer.iss`.
- `docs/BUILD.md` — build instructions.
- `docs/SECURITY.md` — threat model, mitigations, what is NOT covered.
- `docs/USAGE.md` — screenshots, walkthrough.

**Tests**
- Smoke: install on a fresh Windows VM (or alternate user), launch host
  and client, verify successful pairing with no Python on system.
  (Manual; documented in BUILD.md.)

**Acceptance**
- `RemoteControlSetup.exe` installs cleanly on a fresh Windows 11.
- No Python interpreter present on system; app still runs.
- Firewall rule auto-added; uninstaller removes rule and registry.

---

## 6. Operational details

### Performance budget (1080p target)
| Stage | Budget |
|---|---|
| Capture (dxcam) | 5 ms / frame |
| Encode (PyAV libx264 ultrafast) | 15 ms / frame |
| Network (LAN) | < 5 ms |
| Network (WAN) | depends on RTT; adaptive bitrate |
| Decode + paint (Qt) | 10 ms / frame |
| **Target** | **30 FPS @ 1080p, 4 Mbps default bitrate** |

### Adaptive bitrate (Phase 2 light, Phase 5 hardened)
- TCP backpressure detector: if `writer.transport.get_write_buffer_size()`
  > threshold for N seconds -> drop bitrate by 25%, force keyframe.
- Recover by 10% / 5s when buffer drains.

### Threading model
- One asyncio loop on host: server accepts, per-client tasks, capture+encode
  task, broadcaster fan-out task.
- Capture loop runs in a thread (dxcam is sync) — produces frames into an
  asyncio Queue via `loop.call_soon_threadsafe`.
- Input injection runs in the asyncio loop (SendInput is non-blocking).
- Client: asyncio loop in a worker thread; Qt main thread paints frames
  delivered via Qt signal cross-thread.

### Failure modes covered
- Wrong PIN -> AUTH_FAIL, drop, allow re-enter (rate-limited 1/s on host).
- Cert fingerprint mismatch -> client refuses, surfaces to user.
- Slow client -> backpressure -> bitrate adapts; if write buffer exceeds
  hard cap (e.g. 64 MB) the host drops that client.
- Capture failure (DXGI lost) -> capture loop reinitializes dxcam.
- Network blip -> client attempts auto-reconnect with same pinned cert.

### What we don't cover (and why)
- **Symmetric NAT** — your host has a public IP; not relevant.
- **UAC secure desktop** — would need a kernel driver. Documented limitation.
- **Multi-user simultaneous control** — only one input injector; concurrent
  control would create race conditions. UI prevents it (one client owns
  control at a time; others can be view-only).
- **Account/identity** — PIN + fingerprint is the identity. No accounts.

---

## 7. Project layout (final)

```
D:\test 2\Remote control\
├── app\
│   ├── __init__.py
│   ├── main.py                   role picker + dispatch
│   ├── host\
│   │   ├── __init__.py
│   │   ├── main.py               host orchestrator
│   │   ├── server.py             [Phase 1] TLS server, multi-client
│   │   ├── capture.py            [Phase 2] dxcam capture loop
│   │   ├── encoder.py            [Phase 2] PyAV H.264 encoder
│   │   ├── frame_broadcaster.py  [Phase 2] one-encode-fan-out
│   │   ├── input_injector.py     [Phase 3] SendInput
│   │   ├── tray.py               [Phase 4] pystray icon
│   │   ├── ui_pin.py             [Phase 4] PIN window
│   │   └── settings_window.py    [Phase 5] settings dialog
│   ├── client\
│   │   ├── __init__.py
│   │   ├── main.py               client orchestrator
│   │   ├── tcp_client.py         [Phase 1] TLS client + pinning
│   │   ├── decoder.py            [Phase 2] PyAV H.264 decoder
│   │   ├── input_capture.py      [Phase 3] Qt -> wire-format input
│   │   ├── connect_dialog.py     [Phase 4] connect dialog
│   │   └── viewer_window.py      [Phase 4] viewer window
│   └── shared\
│       ├── __init__.py
│       ├── config.py             [Phase 1] paths + AppConfig
│       ├── protocol.py           [Phase 1] frame + message types
│       ├── crypto.py             [Phase 1] auth + cert
│       └── role_picker.py        [Phase 4] first-launch dialog
├── tests\
│   ├── conftest.py
│   ├── test_protocol.py          [Phase 1]
│   ├── test_crypto.py            [Phase 1]
│   ├── test_handshake.py         [Phase 1]
│   ├── test_encoder.py           [Phase 2]
│   ├── test_broadcaster.py       [Phase 2]
│   ├── test_input_injector.py    [Phase 3]
│   ├── test_input_capture.py     [Phase 4]
│   ├── test_clipboard.py         [Phase 5]
│   └── test_file_transfer.py     [Phase 5]
├── installer\
│   ├── host.spec                 [Phase 6] PyInstaller
│   └── installer.iss             [Phase 6] Inno Setup
├── docs\
│   ├── IMPLEMENTATION_PLAN.md    (this file)
│   ├── BUILD.md
│   ├── SECURITY.md
│   └── USAGE.md
├── build.ps1                     [Phase 6]
├── requirements.txt
├── README.md
├── .gitignore
└── CLAUDE.md
```

---

## 8. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| AV flags PyInstaller binaries | High | Medium | Document whitelist steps; consider code signing later |
| FFmpeg DLLs missing in onefile bundle | Medium | High | Use `--collect-all av` in PyInstaller spec |
| dxcam DXGI errors on multi-GPU laptops | Medium | Medium | Catch and reinit; fallback to GDI capture path |
| TCP head-of-line stutter on lossy WAN | Medium | Low | Document; future UDP+DTLS data path option |
| PIN brute-force | Low | High | PBKDF2 + per-session PIN regeneration; rate limit |
| Cert mismatch confuses user | Medium | Low | Friendly "First connect to this PC?" dialog |
| Pyqt6 + PyAV DLL clash in onefile | Medium | High | Test bundle on clean Win11 VM in Phase 6 |

---

## 9. Build, run, and test commands

```powershell
# from D:\test 2\Remote control\

# dev setup
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt

# run from source
python -m app.main host         # share this PC
python -m app.main client       # connect to a PC

# tests
pytest -q

# build standalone EXE + installer (Phase 6)
.\build.ps1
```

---

## 10. Commit cadence

Per CLAUDE.md ("git commit before any new implementation phase"):

- `scaffold:` initial layout + shared modules (DONE)
- `phase1:` network + auth foundation (in progress)
- `phase2:` video pipeline
- `phase3:` input injection + capture
- `phase4:` UI (host tray + PIN window, client connect + viewer)
- `phase5:` clipboard + file transfer + settings
- `phase6:` PyInstaller spec + Inno Setup installer + final docs

Each commit must pass `pytest`. Failing tests block the commit.
