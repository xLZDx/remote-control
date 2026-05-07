# Security model

## What RemoteControl protects against

**Passive eavesdropping on the network.**
All traffic is wrapped in TLS 1.2+. The host generates a self-signed
RSA-2048 certificate on first launch and presents it to clients. The
client refuses TLS versions below 1.2.

**MITM after first connect.**
On the first successful connect to a given host, the client stores
(pins) the host's certificate fingerprint in
`%APPDATA%\RemoteControl\pinned_certs.json`. Subsequent connects must
present the same fingerprint or the client refuses. This means: even
if an attacker hijacks DNS / routing later, they can't impersonate the
host without also stealing the host's private key.

**PIN brute-force.**
The PIN never traverses the wire. Authentication is a challenge-response:
the host sends `salt` and `nonce`, the client computes
`HMAC(PBKDF2-HMAC-SHA256(pin, salt, 200_000 iterations), nonce)` and
sends the proof. PBKDF2 makes online brute-force expensive (one
round-trip + ~10ms of CPU per attempt). 6-digit PIN gives 10^6 search
space; 8-digit available in settings for paranoid setups.

**Replay.**
The challenge salt and nonce are random per-connection (16 bytes each
from `os.urandom`). A replayed proof from a previous session will not
authenticate.

## What RemoteControl does NOT protect against

**MITM on the very first connect.**
The user is asked to confirm the certificate fingerprint shown by the
client matches what the host displays. If they click through without
checking, the attacker pins their own cert. **Always compare the
fingerprint the first time.**

**A compromised host PC.**
If the attacker has code execution on the host, they have full keyboard
+ screen access already. RemoteControl provides them no additional
power.

**Secure desktop / UAC prompts.**
Windows isolates UAC prompts and the Ctrl+Alt+Del screen on a separate
"secure desktop" that user-mode programs cannot capture or send input
to. RemoteControl is a user-mode process. If you need to click through
a UAC prompt remotely, run the host elevated as Administrator (then
captured screen and input work for elevated UI, but the secure desktop
is still off-limits — only a kernel driver can do that).

**Forward secrecy in the strict cryptographic sense.**
TLS provides forward secrecy on the transport (ephemeral DH). However,
the PIN-derived authentication key is not rotated within a session. If
the PIN itself is compromised, all past sessions captured wholesale
could be decrypted by re-doing the handshake — but only the
authentication, not the payload, since the TLS session keys are
ephemeral. A compromised PIN allows future impersonation. Regenerate
PIN per session (default behavior) to limit exposure.

**A malicious viewer.**
Once authenticated, a viewer can capture the screen and inject input
freely. There is no per-application permission scope. View-only mode
on a per-viewer basis is supported and blocks input injection. For
defense-in-depth, do not give the PIN to anyone you don't fully trust.

**Antivirus false positives.**
The shipped binaries are **not code-signed**. PyInstaller bundles +
SendInput-style input injection are common false-positive triggers.
If your AV flags `RemoteControl.exe`, you can either whitelist it or
build from source yourself.

## Threat-model summary

| Threat | Covered? |
|---|---|
| Passive network eavesdropping | Yes (TLS) |
| Active MITM after first connect | Yes (cert pinning) |
| Active MITM on first connect (no out-of-band check) | No (user must verify fingerprint) |
| Wire PIN sniffing | Yes (PIN never sent in plaintext) |
| Online PIN brute-force | Mitigated (PBKDF2 + per-session PIN regen) |
| Offline PIN brute-force | Not applicable (no recoverable handshake transcript on disk) |
| Replay attack | Yes (per-session salt + nonce) |
| Bypassing UAC / secure desktop | No (run as admin to control elevated UIs) |
| Compromised host | Out of scope |
| AV / Defender false positives | Whitelist or build from source |

## Best-practice configuration

- Use a 6-digit PIN by default; bump to 8 if exposing the port to the
  open internet for long periods.
- Keep "regenerate PIN on disconnect" enabled (default).
- Verify the host's cert fingerprint the first time you connect.
- If you ever reinstall the host, the cert changes — clients will refuse
  to connect until you delete `pinned_certs.json` for that host.
- Don't bind the host to `0.0.0.0` on a hostile network unless you've
  put TCP/7777 behind a firewall rule that allow-lists known clients.
