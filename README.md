# Remote Control

A self-contained Windows-to-Windows remote desktop tool. Two standalone EXEs (no Python required on either machine), TLS-encrypted direct TCP connection, PIN-based authentication.

## Quick start

1. Install `RemoteControlSetup.exe` on both PCs.
2. On the PC you want to control: launch and pick **Share this PC**. The window shows your address and a 6-digit PIN.
3. On the controlling PC: launch and pick **Connect to a PC**, type the address + PIN, click **Connect**.

## Architecture

- Host opens a TLS-encrypted TCP listener (default port `7777`).
- Self-signed certificate generated on first launch; client pins it after the first successful connect.
- Authentication via PIN-derived key + challenge-response (PIN never sent in plaintext).
- Single H.264 encode broadcast to all connected viewers.
- Input events flow client -> host as binary messages over the same connection.
- No third-party servers, no signaling, no STUN/TURN.

## Requirements

- Windows 10/11 on both PCs.
- Host PC must be reachable from the client (this app is built for the case where the host has a public/dedicated IP, or both PCs are on the same LAN).
- Inbound TCP on the chosen port allowed by Windows Firewall (the installer adds a rule).

## Build from source

See [docs/BUILD.md](docs/BUILD.md).

## Security notes

See [docs/SECURITY.md](docs/SECURITY.md).
