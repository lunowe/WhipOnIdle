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
    """Return a PIL.Image of a stylized whip on transparent background."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    s = size / 64.0  # scale factor

    # Handle (lower-left): leather-brown rounded rectangle
    handle_color = (110, 70, 30, 255)
    d.rounded_rectangle(
        [8 * s, 38 * s, 22 * s, 56 * s],
        radius=int(3 * s), fill=handle_color,
    )
    # Handle wrap (a couple of darker stripes)
    for y in (42, 47, 52):
        d.line(
            [(9 * s, y * s), (21 * s, y * s)],
            fill=(70, 40, 15, 255), width=max(1, int(s)),
        )

    # Lash: curve sweeping up-and-right with tapering width
    points: list[tuple[float, float]] = []
    for i in range(40):
        t = i / 39
        # Bezier-ish curve from (22, 42) up to (58, 8)
        x = 22 * s + t * 36 * s
        y = 42 * s - t * 30 * s + 4 * s * math.sin(t * math.pi * 2.5)
        points.append((x, y))

    for i in range(len(points) - 1):
        f = i / (len(points) - 1)
        width = max(1, int((4.5 - 3.5 * f) * s))
        # Color: brown near handle, lighter brown to cream at tip
        if f < 0.5:
            color = (160, 105, 45, 255)
        elif f < 0.85:
            color = (210, 165, 90, 255)
        else:
            color = (255, 240, 200, 255)
        d.line([points[i], points[i + 1]], fill=color, width=width)

    # Crack: bright yellow star at the tip
    tip = points[-1]
    cx, cy = tip
    star_r = 7 * s
    inner_r = 3 * s
    star_color = (255, 220, 80, 255)
    star_pts: list[tuple[float, float]] = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        r = star_r if k % 2 == 0 else inner_r
        star_pts.append((cx + math.cos(ang) * r, cy + math.sin(ang) * r))
    d.polygon(star_pts, fill=star_color, outline=(255, 255, 255, 255))

    return img


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
        f"Aufzeichnung seit: {stats.get('started_first', '?')}",
        f"Peitschenhiebe gesamt: {len(whips)}",
        f"Heute: {len(whips_today_list)}",
        f"Letzte 7 Tage: {len(whips_week)}",
        f"Unterdrückt: {len(suppressed)} (Meeting/disarmed)",
    ]
    if whips:
        hours = Counter(_parse_ts(e).hour for e in whips)
        top_hour, top_count = hours.most_common(1)[0]
        avg_idle = sum(e.get("idle_seconds", 0) for e in whips) / len(whips)
        last = _parse_ts(whips[-1])
        lines += [
            "",
            f"Schlimmste Stunde: {top_hour:02d}:00–{top_hour:02d}:59 ({top_count}×)",
            f"Ø Idle bei Hieb: {avg_idle:.0f}s",
            f"Letzter Hieb: {last.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
    if suppressed:
        reasons = Counter(e.get("reason", "?") for e in suppressed)
        top = ", ".join(f"{r} ×{c}" for r, c in reasons.most_common(3))
        lines += ["", f"Top-Unterdrückungen: {top}"]
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
