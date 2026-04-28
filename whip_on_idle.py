#!/usr/bin/env python3
"""WhipOnIdle — knallt die Peitsche wenn du zu lange untätig bist.

Cross-platform (macOS + Windows). Watches OS idle time, suppresses the whip
during meetings (Zoom/Teams/etc.), tracks stats. Stdlib only for the core
behaviour — the tray UI in `whip_app.py` adds pystray + pillow.

Usage (CLI / dev mode):
    python3 whip_on_idle.py                       # 5 min default, German
    python3 whip_on_idle.py --idle 60             # whip after 60s idle
    python3 whip_on_idle.py --test                # fire immediately
    python3 whip_on_idle.py --stats               # print stats and exit
    python3 whip_on_idle.py --suppress-when never # whip even in meetings
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Platform + path setup
# ---------------------------------------------------------------------------
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def _resources_dir() -> Path:
    """Read-only assets bundled with the script (or app bundle when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _state_dir() -> Path:
    """Writable per-user state directory."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        d = Path(base) / "WhipOnIdle"
    elif IS_MAC:
        d = Path.home() / "Library" / "Application Support" / "WhipOnIdle"
    else:
        d = Path.home() / ".whip_on_idle"
    d.mkdir(parents=True, exist_ok=True)
    return d


RESOURCES_DIR = _resources_dir()
STATE_DIR = _state_dir()

# Bundled defaults (read-only; both formats so each platform can pick)
BUNDLED_MP3 = RESOURCES_DIR / "universfield-whip-06-487886.mp3"
BUNDLED_WAV = RESOURCES_DIR / "whip.wav"

# Writable state
SYNTH_SOUND = STATE_DIR / "whip.wav"   # synthesized fallback if no bundled WAV
STATS_FILE = STATE_DIR / "stats.json"
CONFIG_FILE = STATE_DIR / "config.json"


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------
def get_idle_seconds() -> float:
    """Seconds since last keyboard/mouse input."""
    if IS_MAC:
        return _idle_mac()
    if IS_WINDOWS:
        return _idle_windows()
    return 0.0


def _idle_mac() -> float:
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"], text=True, timeout=2
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return 0.0
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                ns = int(line.split("=", 1)[1].strip())
                return ns / 1_000_000_000.0
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def _idle_windows() -> float:
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return max(0.0, millis / 1000.0)


# ---------------------------------------------------------------------------
# Audio playback (cross-platform)
# ---------------------------------------------------------------------------
# Windows-only flag to suppress console pop-up windows when spawning subprocesses.
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def play_audio(path: Path) -> None:
    """Play the given audio file in the background."""
    if IS_MAC:
        subprocess.Popen(
            ["afplay", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    elif IS_WINDOWS:
        if path.suffix.lower() == ".wav":
            import winsound
            winsound.PlaySound(
                str(path),
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        else:
            # PowerShell MediaPlayer for MP3 / other formats.
            uri = str(path).replace("'", "''")
            ps = (
                "Add-Type -AssemblyName presentationCore;"
                "$p = New-Object System.Windows.Media.MediaPlayer;"
                f"$p.open([uri]'{uri}');"
                "$p.Play();"
                "Start-Sleep -Seconds 3"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                creationflags=_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


def speak(text: str, voice: str | None = None) -> None:
    """Speak the given text via the OS TTS in the background."""
    if not text:
        return
    if IS_MAC:
        cmd = ["say"]
        if voice:
            cmd.extend(["-v", voice])
        cmd.append(text)
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    elif IS_WINDOWS:
        safe = text.replace("'", "''")
        if voice:
            select = f"try {{$s.SelectVoice('{voice.replace(chr(39), chr(39)*2)}')}} catch {{}};"
        else:
            # Auto-pick any installed German voice; fall back to system default.
            select = (
                "$g = $s.GetInstalledVoices() | "
                "Where-Object { $_.VoiceInfo.Culture.Name -like 'de*' } | "
                "Select-Object -First 1; "
                "if ($g) { try { $s.SelectVoice($g.VoiceInfo.Name) } catch {} };"
            )
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"{select}"
            f"$s.Speak('{safe}');"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


# ---------------------------------------------------------------------------
# Synthesized fallback whip (stdlib only)
# ---------------------------------------------------------------------------
def synthesize_whip_wav(path: Path, sample_rate: int = 44100) -> Path:
    """Generate a passable whip-crack .wav using stdlib wave/struct/math.

    Three layers: a descending pitched whoosh, filtered noise tail, and a
    sharp transient burst at t=0.28s.
    """
    duration = 0.55
    n = int(sample_rate * duration)
    samples = bytearray()

    noise = [random.uniform(-1, 1) for _ in range(n)]
    smoothed = [0.0] * n
    smoothed[0] = noise[0]
    for i in range(1, n):
        smoothed[i] = 0.6 * smoothed[i - 1] + 0.4 * noise[i]

    crack_at = 0.28
    for i in range(n):
        t = i / sample_rate
        sweep_freq = 200 + 1600 * math.exp(-6 * t)
        whoosh_env = math.exp(-3.5 * t) * (1.0 - math.exp(-50 * t))
        whoosh = math.sin(2 * math.pi * sweep_freq * t) * whoosh_env * 0.35

        if t < crack_at:
            noise_env = (t / crack_at) ** 2 * 0.7
        else:
            noise_env = math.exp(-9 * (t - crack_at)) * 0.7
        air = smoothed[i] * noise_env * 0.55

        crack_t = t - crack_at
        if 0 <= crack_t < 0.06:
            crack = (
                random.uniform(-1, 1)
                * math.exp(-90 * crack_t)
                * (1.0 + 0.5 * math.sin(2 * math.pi * 80 * crack_t))
            )
        else:
            crack = 0.0

        s = max(-1.0, min(1.0, whoosh + air + crack))
        samples.extend(struct.pack("<h", int(s * 30000)))

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(samples))
    return path


def ensure_whip_sound(custom: Path | None) -> Path | None:
    """Resolve which sound file to use for the whip.

    Mac prefers MP3 (richer, afplay handles it). Windows prefers WAV (winsound
    is fast/reliable). Falls back to the bundled other format, then to a
    synthesized WAV in the state dir.
    """
    if custom is not None:
        return custom if custom.exists() else None

    if IS_WINDOWS:
        candidates = [BUNDLED_MP3, BUNDLED_WAV]
    else:
        candidates = [BUNDLED_MP3, BUNDLED_WAV]
    for c in candidates:
        if c.exists():
            return c

    if not SYNTH_SOUND.exists():
        try:
            synthesize_whip_wav(SYNTH_SOUND)
            print(f"[whip] generated fallback {SYNTH_SOUND}")
        except Exception as e:  # noqa: BLE001
            print(f"[whip] could not synthesize whip.wav: {e}", file=sys.stderr)
            return None
    return SYNTH_SOUND


def play_whip_sound(
    sound_path: Path | None, motivational: str, voice: str | None = None
) -> None:
    """Trigger the whip sound + spoken phrase asynchronously."""
    def _play_sound() -> None:
        if sound_path and sound_path.exists():
            play_audio(sound_path)

    def _speak() -> None:
        if motivational:
            # Tiny delay so the whip "lands" before the voice starts.
            time.sleep(0.45)
            speak(motivational, voice)

    threading.Thread(target=_play_sound, daemon=True).start()
    threading.Thread(target=_speak, daemon=True).start()


# ---------------------------------------------------------------------------
# Meeting detection (frontmost / running app heuristic)
# ---------------------------------------------------------------------------
# Lowercase substrings — match against app/process names case-insensitively.
# "zoom" matches both "zoom.us" (Mac) and "zoom.exe" (Windows). "teams"
# matches "Microsoft Teams" / "Teams.exe" / "ms-teams.exe".
DEFAULT_MEETING_APPS = [
    "zoom",
    "teams",
    "webex",
    "facetime",
    "gotomeeting",
    "bluejeans",
    "skype",
    "discord",
    "google meet",
]


def get_frontmost_app() -> str:
    if IS_MAC:
        return _frontmost_mac()
    if IS_WINDOWS:
        return _frontmost_windows()
    return ""


def _frontmost_mac() -> str:
    try:
        out = subprocess.check_output(
            [
                "osascript", "-e",
                'tell application "System Events" to get name of '
                'first application process whose frontmost is true',
            ],
            text=True, timeout=2, stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def _frontmost_windows() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        ):
            return ""
        return Path(buf.value).name  # e.g. "Teams.exe"
    finally:
        kernel32.CloseHandle(handle)


def get_running_apps() -> list[str]:
    if IS_MAC:
        return _running_mac()
    if IS_WINDOWS:
        return _running_windows()
    return []


def _running_mac() -> list[str]:
    try:
        out = subprocess.check_output(
            [
                "osascript", "-e",
                'tell application "System Events" to get name of '
                'every application process whose background only is false',
            ],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
        return [n.strip() for n in out.split(",") if n.strip()]
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def _running_windows() -> list[str]:
    """List process names of windows that are visible and have a title."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    seen: set[str] = set()

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not handle:
            return True
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                seen.add(Path(buf.value).name)
        finally:
            kernel32.CloseHandle(handle)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return sorted(seen)


def is_meeting_active(
    suppress_when: str, meeting_apps: list[str]
) -> tuple[bool, str]:
    """Return (suppress, reason). suppress_when in {'frontmost','running','never'}."""
    if suppress_when == "never" or not meeting_apps:
        return False, ""
    needles = [a.lower() for a in meeting_apps]

    if suppress_when == "frontmost":
        front = get_frontmost_app()
        front_lc = front.lower()
        for n in needles:
            if n in front_lc:
                return True, f"frontmost-app:{front}"
        return False, ""

    for app in get_running_apps():
        app_lc = app.lower()
        for n in needles:
            if n in app_lc:
                return True, f"running-app:{app}"
    return False, ""


# ---------------------------------------------------------------------------
# Stats — append-only event log + summary printer.
# ---------------------------------------------------------------------------
MAX_EVENTS = 10000


def _empty_stats() -> dict:
    return {
        "version": 1,
        "started_first": datetime.now().isoformat(timespec="seconds"),
        "events": [],
    }


def load_stats() -> dict:
    if not STATS_FILE.exists():
        return _empty_stats()
    try:
        with STATS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("version", 1)
        data.setdefault("events", [])
        data.setdefault(
            "started_first", datetime.now().isoformat(timespec="seconds")
        )
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[whip] stats file unreadable ({e}); starting fresh.", file=sys.stderr)
        return _empty_stats()


def save_stats(stats: dict) -> None:
    if len(stats["events"]) > MAX_EVENTS:
        stats["events"] = stats["events"][-MAX_EVENTS:]
    try:
        tmp = STATS_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        tmp.replace(STATS_FILE)
    except OSError as e:
        print(f"[whip] could not save stats: {e}", file=sys.stderr)


def record_event(
    stats: dict, event_type: str, idle_seconds: float, **details
) -> None:
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "type": event_type,
        "idle_seconds": round(idle_seconds, 1),
        **details,
    }
    stats["events"].append(event)
    save_stats(stats)


def _parse_ts(e: dict) -> datetime:
    try:
        return datetime.fromisoformat(e["ts"])
    except (KeyError, ValueError):
        return datetime.min


def whips_today_count(stats: dict) -> int:
    today = datetime.now().date()
    return sum(
        1 for e in stats.get("events", [])
        if e.get("type") == "whipped" and _parse_ts(e).date() == today
    )


def print_stats_summary(stats: dict) -> None:
    events = stats.get("events", [])
    whips = [e for e in events if e.get("type") == "whipped"]
    suppressed = [e for e in events if e.get("type") == "suppressed"]

    now = datetime.now()
    today = now.date()
    week_ago = now - timedelta(days=7)
    whips_today_list = [e for e in whips if _parse_ts(e).date() == today]
    whips_week = [e for e in whips if _parse_ts(e) >= week_ago]
    started = stats.get("started_first", "?")

    print()
    print("┌─ WhipOnIdle Statistik ─────────────────────────")
    print(f"│ Aufzeichnung seit:  {started}")
    print(f"│ Peitschenhiebe ges.:{len(whips):>5}")
    print(f"│ Heute:              {len(whips_today_list):>5}")
    print(f"│ Letzte 7 Tage:      {len(whips_week):>5}")
    print(f"│ Unterdrückt:        {len(suppressed):>5}  (Meeting/disarmed)")
    if whips:
        hours = Counter(_parse_ts(e).hour for e in whips)
        top_hour, top_count = hours.most_common(1)[0]
        print(f"│ Schlimmste Stunde:  {top_hour:02d}:00–{top_hour:02d}:59 ({top_count}×)")
        avg_idle = sum(e.get("idle_seconds", 0) for e in whips) / len(whips)
        print(f"│ Ø Idle bei Hieb:    {avg_idle:.0f}s")
        last = _parse_ts(whips[-1])
        print(f"│ Letzter Hieb:       {last.strftime('%Y-%m-%d %H:%M:%S')}")
    if suppressed:
        reasons = Counter(e.get("reason", "?") for e in suppressed)
        top_reasons = ", ".join(f"{r} ×{c}" for r, c in reasons.most_common(3))
        print(f"│ Top-Unterdrückungen: {top_reasons}")
    print("└────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# Config persistence (used by the tray app; CLI ignores it)
# ---------------------------------------------------------------------------
def default_config() -> dict:
    """The factory defaults the tray app starts with."""
    return {
        "idle_seconds": 300,
        "poll_seconds": 2.0,
        "cooldown_seconds": 45,
        "message": "Hey! Zurück an die Arbeit!",
        "headline": "ZURÜCK AN DIE ARBEIT!",
        "dismiss_hint": "(beliebige Taste zum Schließen)",
        "voice": "Anna" if IS_MAC else "",
        "sound": "",  # path to a custom .wav/.mp3; empty → bundled default
        "suppress_when": "frontmost",
        "meeting_apps": list(DEFAULT_MEETING_APPS),
        "paused": False,
    }


def load_config() -> dict:
    cfg = default_config()
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[whip] config unreadable ({e}); using defaults.", file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        tmp.replace(CONFIG_FILE)
    except OSError as e:
        print(f"[whip] could not save config: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Whip animation — Tk overlay (works on macOS + Windows)
# ---------------------------------------------------------------------------
class WhipAnimation:
    """Borderless top-most overlay that draws a whip cracking across the screen."""

    DURATION_MS = 950

    def __init__(
        self,
        message_top: str = "ZURÜCK AN DIE ARBEIT!",
        dismiss_hint: str = "(beliebige Taste zum Schließen)",
    ) -> None:
        self.root = tk.Tk()
        self.root.title("WhipOnIdle")

        self.w = self.root.winfo_screenwidth()
        self.h = self.root.winfo_screenheight()
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.w}x{self.h}+0+0")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.88)
        self.root.configure(bg="black")

        self.canvas = tk.Canvas(
            self.root,
            width=self.w, height=self.h,
            bg="black", highlightthickness=0, cursor="none",
        )
        self.canvas.pack(fill="both", expand=True)

        self.canvas.create_text(
            self.w / 2, self.h * 0.16,
            text=message_top, fill="#ff2a2a",
            font=("Helvetica", 96, "bold"),
        )
        self.canvas.create_text(
            self.w / 2, self.h * 0.16 + 90,
            text=dismiss_hint, fill="#888888",
            font=("Helvetica", 18),
        )

        self._whip_items: list[int] = []
        self._start_time = time.time()
        self.root.bind("<Key>", lambda _e: self._close())
        self.root.bind("<Button>", lambda _e: self._close())
        self._closed = False

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _draw_whip(self, t: float) -> None:
        for item in self._whip_items:
            self.canvas.delete(item)
        self._whip_items.clear()

        head_x = -150 + (self.w + 300) * t
        head_y = self.h * 0.55 + math.sin(t * math.pi) * (self.h * 0.04)
        tail_length = self.w * 0.55

        segs = 48
        points: list[tuple[float, float]] = []
        for i in range(segs + 1):
            f = i / segs
            amp = (1 - t) * 90 + f * 130
            phase = f * 6.5 + t * 9.5
            x = head_x - tail_length * f
            y = head_y + amp * math.sin(phase)
            points.append((x, y))

        ghost_dx = -40
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            f = i / segs
            width = max(1, 12 * (1 - f) + 2)
            ghost = self.canvas.create_line(
                x1 + ghost_dx, y1, x2 + ghost_dx, y2,
                width=max(1, width - 4), fill="#3a2a10", capstyle="round",
            )
            self._whip_items.append(ghost)

        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            f = i / segs
            width = max(1, 14 * (1 - f) + 2)
            color = "#fff5d0" if f < 0.05 else ("#e0b070" if f < 0.5 else "#8a5a20")
            seg = self.canvas.create_line(
                x1, y1, x2, y2, width=width, fill=color, capstyle="round",
            )
            self._whip_items.append(seg)

        if 0.42 < t < 0.62:
            cx, cy = points[0]
            flash_progress = (t - 0.42) / 0.20
            for r, col in ((220, "#fff7c0"), (140, "#ffffff"), (60, "#ffffff")):
                fr = r * (1.0 + flash_progress * 0.8)
                self._whip_items.append(
                    self.canvas.create_oval(
                        cx - fr, cy - fr, cx + fr, cy + fr,
                        outline="", fill=col,
                    )
                )
            for ang_deg in range(0, 360, 30):
                ang = math.radians(ang_deg)
                r1 = 80 + flash_progress * 50
                r2 = 200 + flash_progress * 120
                self._whip_items.append(
                    self.canvas.create_line(
                        cx + math.cos(ang) * r1, cy + math.sin(ang) * r1,
                        cx + math.cos(ang) * r2, cy + math.sin(ang) * r2,
                        width=4, fill="#ffe680",
                    )
                )

    def _tick(self) -> None:
        if self._closed:
            return
        elapsed_ms = (time.time() - self._start_time) * 1000
        t = min(1.0, elapsed_ms / self.DURATION_MS)
        self._draw_whip(t)
        if t >= 1.0:
            self.root.after(180, self._close)
            return
        self.root.after(16, self._tick)

    def run(self) -> None:
        self.root.after(20, self._tick)
        self.root.after(self.DURATION_MS + 800, self._close)
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def trigger_whip(
    sound_path: Path | None,
    message: str,
    headline: str,
    voice: str | None = None,
    dismiss_hint: str = "(beliebige Taste zum Schließen)",
) -> None:
    print(f"[whip] KNALL! ({time.strftime('%H:%M:%S')})", flush=True)
    play_whip_sound(sound_path, message, voice=voice)
    WhipAnimation(message_top=headline, dismiss_hint=dismiss_hint).run()


# ---------------------------------------------------------------------------
# Watcher — long-lived background-thread idle watcher used by the tray app.
# ---------------------------------------------------------------------------
EventCallback = Callable[[str, dict], None]


class Watcher:
    """Idle watcher that runs in a background thread.

    Fires the whip animation in a *subprocess* so the Tk main loop doesn't
    fight with whatever main loop owns the tray app's UI thread.
    """

    def __init__(
        self,
        config: dict,
        stats: dict,
        on_event: EventCallback | None = None,
    ) -> None:
        self.config = dict(config)
        self.stats = stats
        self.on_event = on_event
        self._stop_event = threading.Event()
        self._wakeup = threading.Event()  # bumps the loop when config changes
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ----- lifecycle -----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="WhipWatcher", daemon=True
        )
        self._thread.start()
        self._emit("started", {})

    def stop(self) -> None:
        self._stop_event.set()
        self._wakeup.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._emit("stopped", {})

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ----- config / runtime control -----
    def update_config(self, **changes) -> None:
        with self._lock:
            self.config.update(changes)
        self._wakeup.set()

    def set_paused(self, paused: bool) -> None:
        self.update_config(paused=bool(paused))

    # ----- internals -----
    def _emit(self, kind: str, data: dict) -> None:
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(kind, data)
        except Exception as e:  # noqa: BLE001
            print(f"[whip] on_event raised: {e}", file=sys.stderr)

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep up to N seconds; returns True if stop was requested."""
        # Wait on either stop or wakeup; ignore wakeup, just rebound back.
        triggered = self._stop_event.wait(timeout=seconds)
        return triggered

    def _cfg_get(self, key: str, default=None):
        with self._lock:
            return self.config.get(key, default)

    def _fire_whip_subprocess(self) -> None:
        """Fire the whip animation in a subprocess (avoids Tk-on-main-thread issues)."""
        cmd: list[str]
        if getattr(sys, "frozen", False):
            # Frozen .app/.exe: run ourselves with --whip-once to play once.
            cmd = [sys.executable, "--whip-once"]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "--test"]

        cmd += [
            "--message", self._cfg_get("message", ""),
            "--headline", self._cfg_get("headline", ""),
            "--dismiss-hint", self._cfg_get("dismiss_hint", ""),
        ]
        voice = self._cfg_get("voice", "")
        if voice:
            cmd += ["--voice", voice]

        sound = self._cfg_get("sound", "")
        if sound:
            cmd += ["--sound", sound]

        try:
            subprocess.Popen(
                cmd,
                creationflags=_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            print(f"[whip] failed to spawn whip subprocess: {e}", file=sys.stderr)

    def _run(self) -> None:
        armed = True
        last_suppression_log = 0.0

        while not self._stop_event.is_set():
            if self._cfg_get("paused"):
                if self._interruptible_sleep(1.0):
                    return
                continue

            idle_threshold = float(self._cfg_get("idle_seconds", 300))
            poll = float(self._cfg_get("poll_seconds", 2.0))
            cooldown = float(self._cfg_get("cooldown_seconds", 45))
            suppress_when = self._cfg_get("suppress_when", "frontmost")
            meeting_apps = list(self._cfg_get("meeting_apps", DEFAULT_MEETING_APPS))

            idle = get_idle_seconds()

            if armed and idle >= idle_threshold:
                in_meeting, reason = is_meeting_active(suppress_when, meeting_apps)
                if in_meeting:
                    now = time.time()
                    if now - last_suppression_log > cooldown:
                        print(f"[whip] suppressed — {reason}", flush=True)
                        record_event(self.stats, "suppressed", idle, reason=reason)
                        self._emit("suppressed", {"reason": reason})
                        last_suppression_log = now
                    armed = False
                    # Wait for meeting to end + fresh activity, then cooldown.
                    while not self._stop_event.is_set():
                        if not is_meeting_active(suppress_when, meeting_apps)[0]:
                            break
                        if self._interruptible_sleep(3.0):
                            return
                    while not self._stop_event.is_set() and get_idle_seconds() >= 5:
                        if self._interruptible_sleep(2.0):
                            return
                    if self._interruptible_sleep(cooldown):
                        return
                    armed = True
                else:
                    self._fire_whip_subprocess()
                    record_event(
                        self.stats, "whipped", idle,
                        frontmost=get_frontmost_app(),
                    )
                    self._emit("whipped", {"idle": idle})
                    armed = False
                    # If the user keeps idling, whip again once another
                    # full idle interval has elapsed — don't just wait
                    # for activity. If they do wake up during the wait,
                    # honor the cooldown as a grace period.
                    deadline = time.time() + idle_threshold
                    became_active = False
                    while not self._stop_event.is_set():
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        if get_idle_seconds() < 5:
                            became_active = True
                            break
                        if self._interruptible_sleep(min(poll, remaining)):
                            return
                    if became_active and self._interruptible_sleep(cooldown):
                        return
                    armed = True

            if self._interruptible_sleep(poll):
                return


# ---------------------------------------------------------------------------
# CLI entry point (kept for headless use, dev mode, and subprocess fire-once)
# ---------------------------------------------------------------------------
def watch_loop(args: argparse.Namespace, sound_path: Path | None, stats: dict) -> int:
    """Foreground watcher (the original CLI behaviour). Used for `python3 whip_on_idle.py` runs."""
    cfg = default_config()
    cfg.update({
        "idle_seconds": args.idle,
        "poll_seconds": args.poll,
        "cooldown_seconds": args.cooldown,
        "message": args.message,
        "headline": args.headline,
        "dismiss_hint": args.dismiss_hint,
        "voice": args.voice,
        "suppress_when": args.suppress_when,
        "meeting_apps": [a.strip() for a in args.meeting_apps.split(",") if a.strip()],
        "sound": args.sound,
    })

    print(
        f"[whip] watching: trigger after {args.idle}s idle "
        f"(poll {args.poll}s, cooldown {args.cooldown}s, "
        f"suppress={args.suppress_when}). Ctrl+C to stop."
    )

    if IS_MAC and args.suppress_when != "never" and not get_frontmost_app():
        print(
            "[whip] WARNING: could not query the frontmost app. Meeting "
            "suppression won't work until Terminal has Automation permission "
            "for 'System Events'. Approve the prompt or use --suppress-when never.",
            file=sys.stderr,
        )

    watcher = Watcher(cfg, stats)
    watcher.start()
    try:
        while watcher.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[whip] stopping.")
    finally:
        watcher.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--idle", type=int, default=300,
        help="Idle seconds before the whip triggers (default: 300)",
    )
    parser.add_argument(
        "--poll", type=float, default=2.0,
        help="How often to check idle state, seconds (default: 2)",
    )
    parser.add_argument(
        "--cooldown", type=int, default=45,
        help="Seconds to wait after re-activity before re-arming (default: 45)",
    )
    parser.add_argument(
        "--message", type=str, default="Hey! Zurück an die Arbeit!",
        help="Spoken motivational phrase (German default).",
    )
    parser.add_argument(
        "--headline", type=str, default="ZURÜCK AN DIE ARBEIT!",
        help="Big headline drawn on the overlay.",
    )
    parser.add_argument(
        "--dismiss-hint", type=str,
        default="(beliebige Taste zum Schließen)",
        help="Smaller hint shown beneath the headline.",
    )
    parser.add_argument(
        "--voice", type=str, default=("Anna" if IS_MAC else ""),
        help="TTS voice name. Mac: 'Anna', 'Markus', etc. "
             "Windows: leave empty to auto-pick a German voice.",
    )
    parser.add_argument(
        "--sound", type=str, default="",
        help="Path to a custom whip sound (.wav/.mp3). "
             "If unset, the bundled sound is used (or a synthesized fallback).",
    )
    parser.add_argument(
        "--suppress-when", type=str, default="frontmost",
        choices=["frontmost", "running", "never"],
        help="When to suppress the whip in a meeting.",
    )
    parser.add_argument(
        "--meeting-apps", type=str,
        default=",".join(DEFAULT_MEETING_APPS),
        help="Comma-separated, case-insensitive substrings of meeting app names.",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Trigger the whip immediately and exit.",
    )
    parser.add_argument(
        "--whip-once", action="store_true",
        help=argparse.SUPPRESS,  # internal: same as --test, used by tray app
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print stats summary and exit.",
    )
    args = parser.parse_args(argv)

    if args.stats:
        print_stats_summary(load_stats())
        return 0

    custom_sound = Path(args.sound).expanduser() if args.sound else None
    sound_path = ensure_whip_sound(custom_sound)
    if custom_sound and not custom_sound.exists():
        print(
            f"[whip] WARNING: custom sound not found: {custom_sound}",
            file=sys.stderr,
        )

    stats = load_stats()

    if args.test or args.whip_once:
        trigger_whip(
            sound_path, args.message, args.headline,
            voice=args.voice or None,
            dismiss_hint=args.dismiss_hint,
        )
        record_event(
            stats, "whipped", 0.0,
            frontmost=get_frontmost_app(), test=True,
        )
        return 0

    try:
        return watch_loop(args, sound_path, stats) or 0
    except KeyboardInterrupt:
        print("\n[whip] stopping.")
        print_stats_summary(stats)
        return 0


if __name__ == "__main__":
    sys.exit(main())
