"""
StwDO Smart Scheduler
=====================
Automatically runs the dorm bot ONLY during the windows when
StwDO publishes new room listings:

  - Montag    10:00 Uhr  ->  Dienstag   12:00 Uhr  (Berlin)
  - Mittwoch  10:00 Uhr  ->  Donnerstag 12:00 Uhr  (Berlin)

Run this script ONCE and leave it running -- it will sleep
outside the windows and auto-wake at the right time.

Usage:
    python scheduler.py
"""

from __future__ import annotations

import time
import subprocess
import sys
import os
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv
from rich.console import Console
from rich.rule import Rule

load_dotenv()
console = Console()
BERLIN_TZ = pytz.timezone("Europe/Berlin")


# ── Active listing windows (Berlin local time) ────────────────────────────────
# Each entry: (start_weekday, start_hour, end_weekday, end_hour)
# weekday: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, ...
WINDOWS = [
    # Montag 10:00 -> Dienstag 12:00
    (0, 10, 1, 12),
    # Mittwoch 10:00 -> Donnerstag 12:00
    (2, 10, 3, 12),
]


def now_berlin() -> datetime:
    return datetime.now(BERLIN_TZ)


def in_active_window(dt: datetime) -> bool:
    """Return True if dt (Berlin time) falls inside an active listing window."""
    wd = dt.weekday()  # 0=Mon ... 6=Sun
    h  = dt.hour
    m  = dt.minute

    for start_wd, start_h, end_wd, end_h in WINDOWS:
        t   = wd * 10000 + h * 100 + m
        t_s = start_wd * 10000 + start_h * 100
        t_e = end_wd   * 10000 + end_h   * 100
        if t_s <= t < t_e:
            return True
    return False


def seconds_until_next_window(dt: datetime) -> float:
    """Return seconds from dt until the next window opens."""
    for days_ahead in range(8):
        candidate = dt + timedelta(days=days_ahead)
        for start_wd, start_h, end_wd, end_h in WINDOWS:
            if candidate.weekday() == start_wd:
                window_start = candidate.replace(
                    hour=start_h, minute=0, second=0, microsecond=0
                )
                if window_start > dt:
                    return (window_start - dt).total_seconds()
    return 7 * 24 * 3600  # fallback: 1 week


def window_end_time(dt: datetime) -> datetime:
    """Return the end datetime of the currently active window."""
    wd = dt.weekday()
    for start_wd, start_h, end_wd, end_h in WINDOWS:
        t   = wd * 10000 + dt.hour * 100 + dt.minute
        t_s = start_wd * 10000 + start_h * 100
        t_e = end_wd   * 10000 + end_h   * 100
        if t_s <= t < t_e:
            days_to_end = (end_wd - dt.weekday()) % 7
            end = (dt + timedelta(days=days_to_end)).replace(
                hour=end_h, minute=0, second=0, microsecond=0
            )
            return end
    return dt


def run_bot_for_window(end_time: datetime) -> None:
    """Run dorm_agent.py until window closes."""
    remaining_min = int((end_time - now_berlin()).total_seconds() / 60)
    remaining_min = max(remaining_min, 1)

    console.print(
        f"[bold green]Starting bot -- will run for {remaining_min} min "
        f"until window closes[/]"
    )

    env = os.environ.copy()
    env["MAX_RUNTIME_MINUTES"]      = str(remaining_min)
    env["POLLING_INTERVAL_SECONDS"] = "20"   # faster polling during window

    proc = subprocess.Popen([sys.executable, "dorm_agent.py"], env=env)

    while proc.poll() is None:
        if now_berlin() >= end_time:
            console.print("[yellow]Window closed -- stopping bot.[/]")
            proc.terminate()
            proc.wait()
            break
        time.sleep(10)

    console.print("[bold]Bot session ended.[/]")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    console.print(Rule("[bold blue]StwDO Smart Scheduler[/]"))
    console.print("[cyan]Active windows (Berlin time):[/]")
    console.print("  * Montag    10:00  ->  Dienstag   12:00")
    console.print("  * Mittwoch  10:00  ->  Donnerstag 12:00")
    console.print()

    while True:
        now = now_berlin()
        console.print(f"[dim]{now.strftime('%A %Y-%m-%d %H:%M:%S %Z')}[/]")

        if in_active_window(now):
            end = window_end_time(now)
            console.print(
                f"[bold green]ACTIVE WINDOW![/] Running until "
                f"[bold]{end.strftime('%A %H:%M')} Berlin[/]"
            )
            run_bot_for_window(end)
            time.sleep(60)

        else:
            secs = seconds_until_next_window(now)
            wake = now + timedelta(seconds=secs)
            h    = int(secs) // 3600
            m    = (int(secs) % 3600) // 60

            console.print(
                f"[yellow]Outside window.[/] Next window: "
                f"[bold]{wake.strftime('%A %d.%m %H:%M')} Berlin[/] "
                f"(in {h}h {m}m)"
            )

            sleep_chunk = min(secs, 1800)  # max 30 min sleep chunk
            time.sleep(sleep_chunk)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold red]Scheduler stopped.[/]")
