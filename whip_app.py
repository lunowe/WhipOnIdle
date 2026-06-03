#!/usr/bin/env python3
"""WhipOnIdle tray app — runs in the system tray / menu bar.

Cross-platform: macOS menu bar via pystray (NSStatusItem backend), Windows
system tray via pystray (Win32 shell tray backend).

Menu:
  - Status / whip count
  - Pausieren / Fortsetzen
  - Idle-Schwelle submenu (1/5/10/15/30 min)
  - Meeting-Unterdrückung submenu (frontmost / running / never)
  - Test Peitsche
  - Statistik anzeigen…
  - Beenden

Stays running until the user clicks "Beenden" in the menu.
"""

from __future__ import annotations

import math
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Lazy-imported so --help/--stats/--test still work without pystray installed:
#   pystray, PIL.Image, PIL.ImageDraw

from whip_on_idle import (
    DEFAULT_MEETING_APPS,
    IS_MAC,
    IS_WINDOWS,
    Watcher,
    _parse_ts,
    load_config,
    load_stats,
    save_config,
    whips_today_count,
)


# ---------------------------------------------------------------------------
# Tray icon graphic — a simple whip drawn with PIL
# ---------------------------------------------------------------------------
def make_icon_image(size: int = 128):
    """Return a PIL.Image of a stylized whip on a transparent background.

    Rendered at 4× and downsampled with LANCZOS so the curves and the star
    come out smoothly anti-aliased at any tray size.
    """
    from PIL import Image, ImageDraw

    ss = 4                      # supersampling factor
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    s = big / 64.0              # scale factor (coords stay in a 64-unit grid)

    # Handle (lower-left): leather-brown rounded rectangle with a soft highlight.
    d.rounded_rectangle(
        [8 * s, 38 * s, 22 * s, 56 * s],
        radius=int(3 * s), fill=(120, 78, 34, 255),
    )
    d.rounded_rectangle(
        [9.5 * s, 39.5 * s, 13 * s, 55 * s],
        radius=int(2 * s), fill=(150, 100, 48, 255),
    )
    # Handle wrap (a couple of darker stripes)
    for y in (42, 47, 52):
        d.line(
            [(9 * s, y * s), (21 * s, y * s)],
            fill=(70, 40, 15, 255), width=max(1, int(s)),
        )

    # Lash: curve sweeping up-and-right with tapering width
    points: list[tuple[float, float]] = []
    for i in range(48):
        t = i / 47
        # Bezier-ish curve from (22, 42) up to (58, 8)
        x = 22 * s + t * 36 * s
        y = 42 * s - t * 30 * s + 4 * s * math.sin(t * math.pi * 2.5)
        points.append((x, y))

    # Dark underlay first for a touch of depth, then the graded lash on top.
    for i in range(len(points) - 1):
        f = i / (len(points) - 1)
        width = max(1, int((6.0 - 4.0 * f) * s))
        d.line([points[i], points[i + 1]], fill=(40, 24, 10, 180), width=width)

    for i in range(len(points) - 1):
        f = i / (len(points) - 1)
        width = max(1, int((4.5 - 3.5 * f) * s))
        # Color: brown near handle, lighter brown to cream at tip
        if f < 0.5:
            color = (165, 108, 46, 255)
        elif f < 0.85:
            color = (215, 168, 92, 255)
        else:
            color = (255, 242, 205, 255)
        d.line([points[i], points[i + 1]], fill=color, width=width)

    # Crack: a soft warm glow under a bright yellow star at the tip.
    cx, cy = points[-1]
    for r, alpha in ((11, 60), (7, 110)):
        rr = r * s
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(255, 225, 120, alpha))

    star_r = 7 * s
    inner_r = 3 * s
    star_pts: list[tuple[float, float]] = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        r = star_r if k % 2 == 0 else inner_r
        star_pts.append((cx + math.cos(ang) * r, cy + math.sin(ang) * r))
    d.polygon(star_pts, fill=(255, 224, 86, 255), outline=(255, 255, 255, 255))

    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Stats popup helpers (cross-platform)
# ---------------------------------------------------------------------------
def format_stats_text(stats: dict) -> str:
    events = stats.get("events", [])
    whips = [e for e in events if e.get("type") == "whipped"]
    suppressed = [e for e in events if e.get("type") == "suppressed"]

    now = datetime.now()
    today = now.date()
    week_ago = now - timedelta(days=7)
    whips_today_list = [e for e in whips if _parse_ts(e).date() == today]
    whips_week = [e for e in whips if _parse_ts(e) >= week_ago]

    lines = [
        "🐎  WhipOnIdle — Statistik",
        f"     seit {stats.get('started_first', '?')}",
        "",
        f"💥  Peitschenhiebe gesamt:  {len(whips)}",
        f"📅  Heute:                  {len(whips_today_list)}",
        f"🗓  Letzte 7 Tage:          {len(whips_week)}",
        f"🤫  Unterdrückt:            {len(suppressed)}  (Meeting / pausiert)",
    ]
    if whips:
        hours = Counter(_parse_ts(e).hour for e in whips)
        top_hour, top_count = hours.most_common(1)[0]
        avg_idle = sum(e.get("idle_seconds", 0) for e in whips) / len(whips)
        last = _parse_ts(whips[-1])
        lines += [
            "",
            f"🕒  Schlimmste Stunde:  {top_hour:02d}:00–{top_hour:02d}:59  ({top_count}×)",
            f"⏱  Ø Idle bei Hieb:    {avg_idle:.0f}s",
            f"🔚  Letzter Hieb:       {last.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
    if suppressed:
        reasons = Counter(e.get("reason", "?") for e in suppressed)
        top = ", ".join(f"{r} ×{c}" for r, c in reasons.most_common(3))
        lines += ["", f"🛑  Top-Unterdrückungen:  {top}"]
    return "\n".join(lines)


def show_message_box(title: str, text: str) -> None:
    """Show an OS-native message dialog. Non-blocking."""
    if IS_MAC:
        safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display dialog "{safe_text}" with title "{safe_title}" '
            f'buttons {{"OK"}} default button "OK" with icon note'
        )
        subprocess.Popen(["osascript", "-e", script])
    elif IS_WINDOWS:
        # Run MessageBoxW in a thread so it doesn't block the tray loop.
        def _show():
            import ctypes
            MB_ICONINFORMATION = 0x40
            ctypes.windll.user32.MessageBoxW(0, text, title, MB_ICONINFORMATION)
        threading.Thread(target=_show, daemon=True).start()
    else:
        print(f"\n=== {title} ===\n{text}\n")


# ---------------------------------------------------------------------------
# Input dialogs (modal, blocking — call from a worker thread, not the tray loop)
# ---------------------------------------------------------------------------
def prompt_text(title: str, prompt: str, default: str = "") -> str | None:
    """Modal single-line text input. Returns the entered string, or None on cancel."""
    if IS_MAC:
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        safe_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"')
        safe_default = default.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display dialog "{safe_prompt}" with title "{safe_title}" '
            f'default answer "{safe_default}" '
            f'buttons {{"Abbrechen", "OK"}} '
            f'default button "OK" cancel button "Abbrechen"'
        )
        try:
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return None  # user cancelled
        # osascript output: "button returned:OK, text returned:<value>\n"
        marker = "text returned:"
        idx = out.find(marker)
        if idx == -1:
            return None
        return out[idx + len(marker):].rstrip("\r\n")
    if IS_WINDOWS:
        # Tk inline: pystray on Windows doesn't conflict with a fresh Tk root,
        # as long as we create + destroy it on the same thread.
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return simpledialog.askstring(
                title, prompt, initialvalue=default, parent=root,
            )
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
    return None


def prompt_open_file(title: str, extensions: list[str] | None = None) -> str | None:
    """Modal open-file dialog. Returns absolute path, or None on cancel.

    `extensions` is a list of file extensions without the dot, e.g. ["wav", "mp3"].
    """
    extensions = extensions or []
    if IS_MAC:
        # AppleScript `choose file` filters by uniform-type identifier or extension.
        type_clause = ""
        if extensions:
            quoted = ", ".join(f'"{e}"' for e in extensions)
            type_clause = f" of type {{{quoted}}}"
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'POSIX path of (choose file with prompt "{safe_title}"{type_clause})'
        )
        try:
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return None  # user cancelled
        return out.strip() or None
    if IS_WINDOWS:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            filetypes: list[tuple[str, str]] = []
            if extensions:
                pattern = " ".join(f"*.{e}" for e in extensions)
                filetypes.append(("Audio", pattern))
            filetypes.append(("Alle Dateien", "*.*"))
            path = filedialog.askopenfilename(
                title=title, parent=root, filetypes=filetypes,
            )
            return path or None
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
    return None


# ---------------------------------------------------------------------------
# Tray app
# ---------------------------------------------------------------------------
IDLE_PRESETS = [
    ("1 Minute", 60),
    ("5 Minuten", 300),
    ("10 Minuten", 600),
    ("15 Minuten", 900),
    ("30 Minuten", 1800),
]

SUPPRESS_PRESETS = [
    ("Nur wenn Meeting im Vordergrund", "frontmost"),
    ("Wenn Meeting-App läuft", "running"),
    ("Niemals unterdrücken", "never"),
]


class WhipTrayApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.stats = load_stats()
        self.watcher = Watcher(
            self.config, self.stats, on_event=self._on_watcher_event
        )
        self.icon = None  # set in run()
        self._refresh_thread: threading.Thread | None = None
        self._refresh_stop = threading.Event()

    # ----- watcher callbacks -----
    def _on_watcher_event(self, kind: str, _data: dict) -> None:
        # Refresh menu so the "Hiebe heute" counter updates after a whip.
        if kind in ("whipped", "suppressed") and self.icon:
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    # ----- menu actions -----
    def _toggle_pause(self, _icon=None, _item=None) -> None:
        self.config["paused"] = not self.config.get("paused", False)
        save_config(self.config)
        self.watcher.update_config(paused=self.config["paused"])
        if self.icon:
            self.icon.update_menu()

    def _set_idle_seconds(self, seconds: int):
        def _action(_icon=None, _item=None):
            self.config["idle_seconds"] = seconds
            save_config(self.config)
            self.watcher.update_config(idle_seconds=seconds)
            if self.icon:
                self.icon.update_menu()
        return _action

    def _set_suppress(self, mode: str):
        def _action(_icon=None, _item=None):
            self.config["suppress_when"] = mode
            save_config(self.config)
            self.watcher.update_config(suppress_when=mode)
            if self.icon:
                self.icon.update_menu()
        return _action

    def _apply_config_change(self, **changes) -> None:
        """Persist config + push to watcher + refresh menu. Thread-safe."""
        self.config.update(changes)
        save_config(self.config)
        self.watcher.update_config(**changes)
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def _set_sound(self, path: str):
        def _action(_icon=None, _item=None):
            self._apply_config_change(sound=path)
        return _action

    def _pick_custom_sound(self, _icon=None, _item=None) -> None:
        # Run the file picker off the tray thread so the menu can dismiss.
        def _do() -> None:
            path = prompt_open_file(
                "Peitschen-Sound auswählen",
                extensions=["wav", "mp3"],
            )
            if path:
                self._apply_config_change(sound=path)
        threading.Thread(target=_do, daemon=True).start()

    def _edit_text(self, key: str, title: str, prompt: str):
        """Returns a menu action that opens an input dialog for `config[key]`."""
        def _action(_icon=None, _item=None):
            def _do() -> None:
                current = self.config.get(key, "")
                new = prompt_text(title, prompt, default=current)
                if new is not None:
                    self._apply_config_change(**{key: new})
            threading.Thread(target=_do, daemon=True).start()
        return _action

    def _test_whip(self, _icon=None, _item=None) -> None:
        # Reuse the watcher's subprocess invocation so behaviour matches a
        # real fire (same args, same animation/sound/voice).
        self.watcher._fire_whip_subprocess()  # noqa: SLF001 — intentional

    def _show_stats(self, _icon=None, _item=None) -> None:
        # Reload from disk so we get any events written since launch.
        self.stats = load_stats()
        text = format_stats_text(self.stats)
        show_message_box("WhipOnIdle Statistik", text)

    def _quit(self, _icon=None, _item=None) -> None:
        self._refresh_stop.set()
        self.watcher.stop()
        if self.icon:
            self.icon.stop()

    # ----- menu construction -----
    def _build_menu(self):
        import pystray
        from pystray import Menu, MenuItem

        whips_today = whips_today_count(self.stats)
        paused = self.config.get("paused", False)
        current_idle = self.config.get("idle_seconds", 300)
        current_suppress = self.config.get("suppress_when", "frontmost")

        idle_items = [
            MenuItem(
                label,
                self._set_idle_seconds(secs),
                checked=lambda _i, s=secs: self.config.get("idle_seconds") == s,
                radio=True,
            )
            for label, secs in IDLE_PRESETS
        ]

        suppress_items = [
            MenuItem(
                label,
                self._set_suppress(mode),
                checked=lambda _i, m=mode: self.config.get("suppress_when") == m,
                radio=True,
            )
            for label, mode in SUPPRESS_PRESETS
        ]

        # Sound submenu: "Standard" + currently-picked custom + file picker.
        sound_items = [
            MenuItem(
                "Standard",
                self._set_sound(""),
                checked=lambda _i: not self.config.get("sound"),
                radio=True,
            )
        ]
        custom_sound = self.config.get("sound", "")
        if custom_sound:
            sound_items.append(
                MenuItem(
                    Path(custom_sound).name or custom_sound,
                    self._set_sound(custom_sound),  # re-selects, effectively no-op
                    checked=lambda _i: bool(self.config.get("sound")),
                    radio=True,
                )
            )
        sound_items.append(Menu.SEPARATOR)
        sound_items.append(MenuItem("Eigene Datei wählen…", self._pick_custom_sound))

        # Texts submenu: edit the spoken phrase, the visual headline, and the hint.
        text_items = [
            MenuItem(
                "Nachricht ändern…",
                self._edit_text(
                    "message",
                    "WhipOnIdle: Nachricht",
                    "Gesprochene Nachricht beim Peitschenhieb:",
                ),
            ),
            MenuItem(
                "Überschrift ändern…",
                self._edit_text(
                    "headline",
                    "WhipOnIdle: Überschrift",
                    "Große Überschrift im Whip-Overlay:",
                ),
            ),
            MenuItem(
                "Hinweistext ändern…",
                self._edit_text(
                    "dismiss_hint",
                    "WhipOnIdle: Hinweistext",
                    "Kleiner Hinweistext unter der Überschrift:",
                ),
            ),
        ]

        # Header line — disabled, just shows status + count
        status_emoji = "⏸" if paused else "🟢"
        header = f"{status_emoji}  {whips_today} Hiebe heute"

        return Menu(
            MenuItem(header, None, enabled=False),
            MenuItem(
                f"Idle-Schwelle: {current_idle // 60} Min",
                None, enabled=False,
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Pausiert" if paused else "Aktiv",
                self._toggle_pause,
                checked=lambda _i: self.config.get("paused", False),
            ),
            MenuItem("Idle-Schwelle", Menu(*idle_items)),
            MenuItem("Meeting-Unterdrückung", Menu(*suppress_items)),
            MenuItem("Peitschen-Sound", Menu(*sound_items)),
            MenuItem("Texte anpassen", Menu(*text_items)),
            Menu.SEPARATOR,
            MenuItem("Test Peitsche", self._test_whip),
            MenuItem("Statistik anzeigen…", self._show_stats),
            Menu.SEPARATOR,
            MenuItem("Beenden", self._quit),
        )

    # ----- menu refresh loop (updates "Hiebe heute" minutely) -----
    def _periodic_refresh(self) -> None:
        while not self._refresh_stop.wait(60):
            # Reload stats from disk in case the whip subprocess wrote some.
            self.stats = load_stats()
            if self.icon:
                try:
                    self.icon.update_menu()
                except Exception:  # noqa: BLE001
                    pass

    # ----- run -----
    def run(self) -> int:
        import pystray

        self.icon = pystray.Icon(
            "WhipOnIdle",
            make_icon_image(),
            "WhipOnIdle",
            self._build_menu(),
        )

        self._refresh_thread = threading.Thread(
            target=self._periodic_refresh, name="MenuRefresh", daemon=True
        )
        self._refresh_thread.start()

        self.watcher.start()
        try:
            self.icon.run()  # blocks
        finally:
            self._refresh_stop.set()
            self.watcher.stop()
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# CLI flags that should bypass the tray and run the headless whip_on_idle CLI
# (used internally by the watcher to fire the whip in a subprocess, plus for
# `--stats` / `--help`).
_CLI_FLAGS = {
    "--whip-once", "--test", "--stats", "--help", "-h",
}


def main() -> int:
    if any(arg in _CLI_FLAGS for arg in sys.argv[1:]):
        from whip_on_idle import main as core_main
        return core_main()

    app = WhipTrayApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
