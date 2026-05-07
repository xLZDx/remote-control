# Building RemoteControl from source

## Prerequisites

- Windows 10 or 11 (64-bit)
- Python 3.11 - 3.14 (3.14 verified working on this project)
- (Optional, for full installer) Inno Setup 6: https://jrsoftware.org/isdl.php

## One-shot build

From the project root:

```powershell
.\build.ps1
```

This will:
1. Create a venv if it doesn't exist
2. Install all runtime + build deps with `--no-cache-dir`
3. Run the test suite
4. Run PyInstaller against `installer/RemoteControl.spec`
5. Compile the Inno Setup installer if `iscc.exe` is found

Outputs:

- `dist/RemoteControl/RemoteControl.exe` — the standalone application directory
- `installer/Output/RemoteControlSetup.exe` — single-file installer (if Inno Setup available)

## Manual build

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q

# Build standalone EXE
pyinstaller --clean installer\RemoteControl.spec

# Build installer (requires Inno Setup)
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" installer\RemoteControl.iss
```

## Run from source

```powershell
.\venv\Scripts\Activate.ps1

# Show role picker
python -m app.main

# Or skip the picker
python -m app.main host
python -m app.main client
```

## Skipping steps

```powershell
.\build.ps1 -SkipTests       # skip pytest
.\build.ps1 -SkipInstaller   # skip Inno Setup
```

## Test from a fresh Windows install

To verify the produced EXE runs with no Python prerequisite:

1. Copy `installer\Output\RemoteControlSetup.exe` to a clean Windows VM.
2. Install. Launch from Start Menu.
3. Confirm the role picker appears, the host shows a PIN, and the client can connect.

If the launched EXE crashes on a clean machine, check `dist\RemoteControl\_internal\` for missing DLLs (especially Qt6 plugins under `PyQt6\Qt6\plugins\` and FFmpeg DLLs under `av.libs\`). Add them as `datas` or `binaries` in `installer/RemoteControl.spec`.

## Output size

Roughly:
- `RemoteControl.exe` (launcher) — ~6 MB
- `dist/RemoteControl/` (full bundle) — ~210 MB (mostly Qt6 + PyAV + Python)
- `RemoteControlSetup.exe` (LZMA-compressed) — ~80-100 MB
