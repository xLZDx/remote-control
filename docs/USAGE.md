# Using RemoteControl

## First-time setup

1. Run `RemoteControlSetup.exe` on **both** PCs. The installer asks for admin (it adds a Windows Firewall inbound rule).
2. Both PCs are now ready. There is no account to create, no service to subscribe to.

## Sharing this PC (host)

1. Launch RemoteControl from the Start Menu, or right-click the tray icon.
2. Click **Share this PC**.
3. The window shows three things:
   - **Address** — the IP and port others use to reach this PC (e.g. `203.0.113.5:7777`)
   - **PIN** — a 6-digit code (regenerated each time the last viewer disconnects)
   - **Fingerprint** — the SHA-256 of the host's TLS cert; clients ask the user to confirm this on first connect

4. Send the address and PIN to the controlling PC's user.

The host keeps running in the system tray. Right-click the tray icon for:
- **Show window** — bring the PIN window back
- **Regenerate PIN** — invalidate the current PIN immediately
- **Quit RemoteControl** — stop hosting

## Connecting to a PC (client)

1. Launch RemoteControl, click **Connect to a PC**.
2. Type the host's address (e.g. `203.0.113.5`), port (default `7777`), and PIN.
3. Click **Connect**.

**On first connect to a new host**, a fingerprint dialog appears. Compare the value with what the host shows. If they match, click **Trust and continue**. The fingerprint is pinned for future connects.

If they don't match, **stop**. Either:
- The host reinstalled RemoteControl, in which case have them confirm and you can reset the pin (delete `%APPDATA%\RemoteControl\pinned_certs.json`)
- Or someone is impersonating the host. Don't proceed.

## Inside the viewer window

- Move/click/scroll/type as if it were your local PC.
- **Disconnect** button to leave cleanly.
- **Fullscreen** button to maximize.
- If the host has multiple monitors, switch between them with the buttons in the toolbar.

## Connecting across the internet without port-forwarding the laptop (Hub mode)

If you have one PC with a stable public IP (the **Hub**) and other PCs (laptops) behind home/cafe NAT, set up Hub mode so any client can reach a laptop without configuring the laptop's router.

### On the Hub PC (the one with the public IP)

1. Launch RemoteControl, choose **Share this PC**.
2. Tray icon -> **Hub Settings...** -> **Run as Hub** tab.
3. Tick **Run Hub broker on this PC**. Default port is `7780` (separate from the host's `7777`).
4. Port-forward TCP/7780 on your router to this PC (in addition to TCP/7777 if you also share this PC directly).
5. Click **Add registration...**, type a name for the laptop (e.g. `work-laptop`).
6. **Copy the displayed token immediately**. It's shown only once. Send it to the laptop user securely.

### On the laptop (behind NAT)

1. Launch RemoteControl, choose **Share this PC**.
2. Tray -> **Hub Settings...** -> **Register with Hub** tab.
3. Enter Hub address (the Hub PC's public IP), Hub port (default 7780), the laptop name agreed with the Hub admin, and the token.
4. Click **Save and connect**. Status should change to `online` within a few seconds.

The laptop now keeps a persistent outbound TLS tunnel to the Hub. There is **no port-forwarding required on the laptop side**.

### To control the laptop from anywhere

1. On the controlling PC, choose **Connect to a PC**.
2. Switch to the **Via Hub** tab.
3. Hub address = the Hub PC's public IP. Hub port = 7780. Laptop name = the registered name. PIN = the laptop's current PIN (shown on the laptop's PIN window).
4. **Connect**.

The Hub relays bytes between you and the laptop. The laptop's host runs the same TLS+PIN auth at its end of the tunnel.

### Hub mode security

In v1, the Hub is **inside** the trust path: the Hub admin can see the bytes flowing through. Use Hub mode only when the Hub PC is owned/trusted by you. End-to-end encryption that hides bytes from the Hub is on the roadmap. See [SECURITY.md](SECURITY.md).

## Hosting from behind a router

This app is built for the case where the host has a **public IP** that doesn't change (a dedicated external IP, a static DDNS, or a port-forwarded home router).

- If the host's IP changes, clients have to be told the new one each time. Use a DDNS provider (DuckDNS, no-ip) for a stable name.
- Inbound TCP on the chosen port (default 7777) must be permitted by:
  - The host PC's Windows Firewall (the installer adds this rule)
  - The router (port-forward TCP/7777 from WAN to the host)
  - Any ISP-side CGNAT (you said you have a dedicated IP, so this isn't an issue for you)

## Multiple viewers

Multiple people can connect to one host at the same time. They all see the same screen. By default, all of them have full control — which means input from different viewers is interleaved, which is rarely what anyone wants. Use the host PIN window to mark specific viewers **View-only**:
- The PIN window's **Connected viewers** list has a checkbox per viewer.
- View-only viewers can see but not move the mouse or send keys.

## Settings

Most defaults are sensible. To change them:

`%APPDATA%\RemoteControl\config.json` — port, FPS, bitrate cap, static PIN, autostart.

## Limitations

- **Windows-only**, both ends.
- Cannot capture or inject into UAC prompts / secure-desktop screens (no kernel driver).
- ~30 FPS at 1080p in pure-Python; for higher framerates, run the host on a beefier CPU.
- TCP-based — head-of-line blocking on a lossy WAN can cause brief stutters. LAN and dedicated-IP-to-good-WAN are smooth.
- No audio yet (planned in Phase 5).
- No file transfer yet (planned in Phase 5).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Client times out connecting | Port not open on router; host firewall rule missing |
| "Bad PIN" repeatedly | PIN regenerated on the host; ask for the current one |
| "Fingerprint mismatch" error | Host reinstalled - delete pinned_certs.json for that host |
| AV flags the .exe | Whitelist it; the binaries are unsigned |
| Black screen, no video | Multi-GPU laptop on the host? Switch to integrated graphics for capture |
| Wrong screen captured | Use the monitor selector in the viewer toolbar |
| Window doesn't close on Quit | The asyncio loop has hung; force-kill via Task Manager |
