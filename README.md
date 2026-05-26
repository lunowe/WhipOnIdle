# WhipOnIdle

> Knallt die Peitsche wenn du zu lange untätig bist.

A tiny menu-bar / tray app that watches your idle time. If you sit still for too
long, it plays a whip-crack sound, sweeps an animated whip across the screen,
and yells at you (in German, by default) to get back to work.

Meetings are detected and respected — no Zoom-call whiplash. Cross-platform:
macOS menu bar + Windows system tray.

---

## What it does

- Idle watcher uses native APIs — `IOHIDSystem` on macOS, `GetLastInputInfo` on
  Windows. No keylogging, no input hooking.
- After `idle_seconds` of no input (default 5 min), it cracks the whip:
  - plays a whip sample (bundled MP3 / WAV, or a stdlib-synthesized fallback)
  - draws a full-screen animated whip overlay
  - speaks a motivational phrase via the OS TTS (`say` / `System.Speech`)
- Re-whips after another full idle interval if you stubbornly keep idling.
- Meeting suppression: skips the whip if Zoom / Teams / Webex / Meet / Discord /
  etc. is frontmost (or merely running — your choice).
- Tracks stats locally — whips today, top "worst hour", average idle-at-crack,
  suppressed events.
- Everything configurable from the tray menu: idle threshold, suppression mode,
  custom sound file, headline / spoken phrase / dismiss-hint text, pause.

State and config live under `~/Library/Application Support/WhipOnIdle/` (macOS)
or `%LOCALAPPDATA%\WhipOnIdle\` (Windows). Nothing leaves the machine.

---

## Install

### Pre-built binaries

Grab the latest zip from the [Releases](https://github.com/lunowe/WhipOnIdle/releases)
page:

- **macOS:** `WhipOnIdle-mac.zip` → unzip → drag `WhipOnIdle.app` into
  `/Applications`. First launch: right-click → *Open* (unsigned build,
  Gatekeeper will complain otherwise).
- **Windows:** `WhipOnIdle-windows.zip` → unzip anywhere → run
  `WhipOnIdle.exe`.

### From source (dev mode)

```bash
git clone https://github.com/lunowe/WhipOnIdle.git
cd WhipOnIdle

# macOS / Linux
./run.command

# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python whip_app.py
```

Python 3.10+ recommended. `tkinter` must be present (it ships with the standard
python.org installers; on Linux: `apt install python3-tk`).

---

## Tray menu

```
 🟢  3 Hiebe heute
 Idle-Schwelle: 5 Min
 ──────────────────────────
 Aktiv                      (toggle pause)
 Idle-Schwelle      ▸ 1 / 5 / 10 / 15 / 30 Min
 Meeting-Unterdrückung ▸ frontmost / running / never
 Peitschen-Sound    ▸ Standard / eigene Datei…
 Texte anpassen     ▸ Nachricht / Überschrift / Hinweistext
 ──────────────────────────
 Test Peitsche
 Statistik anzeigen…
 ──────────────────────────
 Beenden
```

---

## CLI

For headless use, dev work, or scripting `whip_on_idle.py` directly:

```bash
python3 whip_on_idle.py                          # default: 5 min idle, German
python3 whip_on_idle.py --idle 60                # whip after 60s
python3 whip_on_idle.py --test                   # fire immediately
python3 whip_on_idle.py --stats                  # print the stats summary
python3 whip_on_idle.py --suppress-when never    # whip even in meetings
python3 whip_on_idle.py --message "Get back to it" --voice Daniel
```

`python3 whip_on_idle.py --help` shows all flags.

---

## macOS permissions

The first time the app queries the frontmost application for meeting detection,
macOS prompts to allow *System Events* automation. Approve it, otherwise
suppression silently no-ops and the app warns on stderr. Use
`--suppress-when never` if you don't want to grant that permission.

The bundled `.app` is unsigned, so the first launch needs a right-click → *Open*
to get past Gatekeeper.

---

## Build it yourself

```bash
# macOS — produces dist/WhipOnIdle.app + dist/WhipOnIdle-mac.zip
./build_mac.sh

# Windows — produces dist\WhipOnIdle\WhipOnIdle.exe + dist\WhipOnIdle-windows.zip
build_windows.bat
```

Both scripts spin up a venv, install `requirements-dev.txt` (adds PyInstaller),
and run `pyinstaller whip_app.spec`.

CI builds for both platforms on every push to `main` and on every `v*` tag —
tagged builds attach the zips to a GitHub release automatically. See
[`.github/workflows/build.yml`](.github/workflows/build.yml).

---

## Project layout

| File | Purpose |
|---|---|
| `whip_on_idle.py` | Core: idle detection, audio, meeting detection, stats, Tk whip overlay, headless CLI |
| `whip_app.py` | Tray / menu-bar app (pystray) — depends on the core module |
| `whip_app.spec` | PyInstaller spec — bundles the sound assets into the binary |
| `build_mac.sh` / `build_windows.bat` | Local build scripts |
| `run.command` | Dev-mode launcher (no PyInstaller) |
| `universfield-whip-06-487886.mp3` / `whip.wav` | Bundled whip samples |

Core has no third-party dependencies beyond the Python stdlib. The tray app
adds `pystray` and `Pillow`.

---

## Credits

Whip MP3: *"Whip 06"* by Universfield (universfield-whip-06-487886.mp3).
