# ratecap - compress video / audio / image to a target file size
# Uses FFmpeg + nice console GUI
#
# REQUIREMENTS:
#   pip install rich questionary
#   FFmpeg & ffprobe in PATH (https://www.gyan.dev/ffmpeg/builds/ - full build recommended)

from __future__ import annotations

# Python 3.15+ (PEP 810): these modules load on first use instead of at startup.
# On older Pythons this is just an unused list and everything imports normally,
# so the script runs everywhere (a literal `lazy import` would be a SyntaxError <3.15).
__lazy_modules__ = [
    "json", "re", "shutil", "tempfile", "time", "urllib.parse",
    "questionary",
    "rich.markup", "rich.panel", "rich.progress", "rich.table",
]

import calendar
import functools
import json
import math
import os
import re
import shutil
import threading
import subprocess
import sys
import tempfile
import time
import queue
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import questionary
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from questionary.prompts.common import InquirerControl, create_inquirer_layout
from rich.console import Console
from rich.markup import escape
from rich.console import Group
from rich.panel import Panel
from rich.table import Column, Table
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn,
)

console = Console()

CONFIG_FILE = Path(__file__).with_name("config.json")
LEGACY_CONFIG_FILE = Path(__file__).with_name("compressor_config.json")   # read once if config.json is missing
DEFAULT_OUTPUT_FOLDER = Path(__file__).with_name("output")

# ──────────────────────────────────────────────────────────────────────────────
# FORMATS - each one only appears for inputs it can actually be made from
#   kind: what the OUTPUT is (video / audio / image)
# ──────────────────────────────────────────────────────────────────────────────
FORMATS = [
    {"name": "MP4",      "ext": ".mp4",  "kind": "video", "group": "Moderate efficiency • Compatible with everything"},
    {"name": "WebM",     "ext": ".webm", "kind": "video", "group": "High efficiency • Good compatibility"},
    {"name": "AV1 MKV",  "ext": ".mkv",  "kind": "video", "group": "Ultra high efficiency (AV1) • Modern (Discord friendly)"},
    {"name": "MP3",      "ext": ".mp3",  "kind": "audio", "group": "Very poor efficiency • Universal"},
    {"name": "M4A",      "ext": ".m4a",  "kind": "audio", "group": "Moderate efficiency • Compatible with everything"},
    {"name": "OGG",      "ext": ".ogg",  "kind": "audio", "group": "High efficiency • Good compatibility"},
    {"name": "PNG/JPEG", "ext": ".png",  "kind": "image", "group": "Moderate efficiency • PNG if it fits, else JPEG"},
    {"name": "WebP",     "ext": ".webp", "kind": "image", "group": "High efficiency • Modern"},
    {"name": "AVIF",     "ext": ".avif", "kind": "image", "group": "Ultra high efficiency (AV1) • Modern browsers"},
]

COMPRESSION_LEVELS = ["fastest", "fast", "normal", "maximum"]

# frame rate choices: "original" keeps the source, "auto" lets ratecap decide, otherwise an exact rate
FPS_CHOICES = [
    ("Keep original", "original"),
    ("Adaptive (ratecap picks based on the target size)", "auto"),
    ("60 fps", Fraction(60)),
    ("59.94 fps (NTSC)", Fraction(60000, 1001)),
    ("50 fps (PAL)", Fraction(50)),
    ("30 fps", Fraction(30)),
    ("29.97 fps (NTSC)", Fraction(30000, 1001)),
    ("25 fps (PAL)", Fraction(25)),
    ("24 fps (film)", Fraction(24)),
    ("23.976 fps (NTSC film)", Fraction(24000, 1001)),
]
ADAPTIVE_MIN_SCALE = 0.7     # adaptive fps drops frames before letting resolution fall below 70%
ADAPTIVE_MIN_FPS = 24

# bulk folder scan (.ts is left out on purpose, it matches TypeScript files)
VIDEO_EXTS = {".mts", ".m2ts", ".m2t", ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".mpg", ".mpeg",
              ".wmv", ".webm", ".3gp", ".flv", ".mxf"}
# what an MP4 can hold without re-encoding
MP4_VIDEO_OK = {"h264", "hevc", "av1", "mpeg4", "vp9"}
MP4_AUDIO_COPY = {"aac", "ac3", "eac3", "mp3", "alac"}

# name -> (video codec, audio codec, family)
VIDEO_CODECS = {
    "MP4":     ("libx264",    "aac",     "x264"),
    "WebM":    ("libvpx-vp9", "libopus", "vp9"),
    "AV1 MKV": ("libsvtav1",  "libopus", "svtav1"),
}
# minimum bits per pixel per frame we want before we start shrinking resolution
MIN_BPP = {"x264": 0.07, "nvenc": 0.09, "vp9": 0.06, "svtav1": 0.05}

# name -> (audio codec, min kbps, max kbps)
AUDIO_CODECS = {
    "MP3": ("libmp3lame", 32, 320),
    "M4A": ("aac",        32, 320),
    "OGG": ("libopus",    24, 256),
}

NULL_DEVICE = "NUL" if os.name == "nt" else "/dev/null"


class FFmpegError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _int(x, default=None):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def ask(question):
    """questionary returns None on Ctrl+C - turn that into a real KeyboardInterrupt."""
    answer = question.ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


_PATH_LOCK = threading.Lock()


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """A free path under `folder`. Reserves it immediately (empty file) so two concurrent jobs never collide,
    even on the same stem; every caller replaces this placeholder with the real file right after."""
    with _PATH_LOCK:
        p = folder / f"{stem}{ext}"
        n = 1
        while p.exists():
            p = folder / f"{stem}_{n}{ext}"
            n += 1
        p.touch()
    return p


def load_config() -> dict:
    for path in (CONFIG_FILE, LEGACY_CONFIG_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def save_config(data: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        console.print("[yellow]Warning: could not save config[/yellow]")


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        console.print("[bold red]FFmpeg / ffprobe not found in PATH[/bold red]")
        console.print("Download full build → https://www.gyan.dev/ffmpeg/builds/")
        sys.exit(1)


@functools.cache
def nvenc_works() -> bool:
    """`ffmpeg -encoders` lists h264_nvenc even with no NVIDIA GPU, so actually try it."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ──────────────────────────────────────────────────────────────────────────────
# PROBING
# ──────────────────────────────────────────────────────────────────────────────

def get_media_info(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(r.stdout)


def _is_cover(s: dict) -> bool:
    return bool(s.get("disposition", {}).get("attached_pic"))


def video_stream(info: dict) -> dict | None:
    return next((s for s in info.get("streams", [])
                 if s.get("codec_type") == "video" and not _is_cover(s)), None)


def audio_stream(info: dict) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)


def detect_type(info: dict) -> str:
    v, a = video_stream(info), audio_stream(info)
    if v is None:
        return "audio" if a else "unknown"      # mp3/m4a with cover art stays "audio"
    if a:
        return "video"
    fmt = info.get("format", {})
    frames = _int(v.get("nb_frames"))
    duration = _float(fmt.get("duration"), 0.0)
    name = fmt.get("format_name", "")
    if frames == 1 or "_pipe" in name or name == "image2" or (frames is None and duration < 0.2):
        return "image"
    return "video"                               # animated gif / silent clip → treated as video


def get_duration(info: dict) -> float:
    d = _float(info.get("format", {}).get("duration"))
    if not d:
        for s in info.get("streams", []):
            d = _float(s.get("duration"))
            if d:
                break
    if not d or d <= 0:
        raise RuntimeError("Could not read the duration of this file")
    return d


def get_fps(v: dict) -> Fraction:
    """Exact source frame rate (e.g. 30000/1001), 30 if ffprobe gives nothing sane."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = str(v.get(key, "0/1")).split("/")
            fps = Fraction(int(n), int(d))
            if 0 < fps <= 240:
                return fps.limit_denominator(100000)
        except (ValueError, ZeroDivisionError):
            pass
    return Fraction(30)


def pick_fps(src: Fraction, mode, video_kbps: float, w: int, h: int, family: str) -> tuple:
    """Output frame rate for the chosen mode, plus a short note (may be empty)."""
    if mode in (None, "original"):
        return src, ""
    if mode == "auto":
        def scale_at(f):                # resolution factor the bitrate allows at frame rate f
            return math.sqrt(video_kbps * 1000 / (w * h * float(f) * MIN_BPP[family]))
        # only clean divisions (60 -> 30, 50 -> 25, 59.94 -> 29.97) so motion stays even
        ladder = [src / k for k in (1, 2, 4) if k == 1 or src / k >= ADAPTIVE_MIN_FPS]
        pick = ladder[-1]
        for f in ladder:
            if scale_at(f) >= ADAPTIVE_MIN_SCALE:
                pick = f
                break
        if pick == src:
            return src, ""
        return pick, f"{float(src):g} -> {float(pick):g} fps"
    if mode > src * Fraction(101, 100):
        return src, f"kept {float(src):g} fps (source is lower)"
    return mode, ""


def collect_videos(folder: Path, skip: Path) -> list:
    """All video files under `folder` (AVCHD card copies included), natural sort, ignoring our output folder."""
    skip, root = skip.resolve(), folder.resolve()
    # ratecap's own results are only ignored when scanning a folder that CONTAINS the output folder,
    # a folder inside it (for example output/youtube/My Mix) is a normal input
    ignore_output = skip != root and skip not in root.parents
    found = []
    for p in folder.rglob("*"):
        if p.suffix.lower() in VIDEO_EXTS and p.is_file() and not any(part.startswith(".tmp_") for part in p.parts):
            if not (ignore_output and skip in p.resolve().parents):
                found.append(p)
    return sorted(found, key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))])


def is_interlaced(path: Path, v: dict) -> bool:
    """Camcorder footage (AVCHD .MTS) is usually interlaced, which looks combed if re-encoded as is."""
    fo = v.get("field_order", "unknown")
    if fo in ("tt", "bb", "tb", "bt"):
        return True
    if fo == "progressive":
        return False
    try:    # ffprobe doesn't always know for transport streams, so look at the first frames
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path), "-map", "0:v:0",
                            "-vf", "idet", "-frames:v", "150", "-an", "-f", "null", NULL_DEVICE],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    # ffmpeg prints the idet stats twice (an empty warm-up block first), the last one is the real result
    found = re.findall(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)", r.stderr)
    if not found:
        return False
    tff, bff, prog = map(int, found[-1])
    return tff + bff > 3 * prog         # clearly interlaced, not just a few ambiguous frames


# ──────────────────────────────────────────────────────────────────────────────
# FFmpeg RUNNER (live progress, real error messages)
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# CONSOLE LOOK: themed prompts, section headers, aligned key/value lines, per-file progress
# ──────────────────────────────────────────────────────────────────────────────

_QSTYLE = None


def _style():
    global _QSTYLE
    if _QSTYLE is None:
        _QSTYLE = questionary.Style([
            ("qmark", "fg:#00d7ff bold"), ("question", "bold"), ("answer", "fg:#00d7ff bold"),
            ("pointer", "fg:#00d7ff bold"), ("highlighted", "fg:#00d7ff bold"), ("selected", "fg:#5fd75f"),
            ("separator", "fg:#6c6c6c"), ("instruction", "fg:#808080"), ("text", ""),
            ("disabled", "fg:#858585 italic"),
        ])
    return _QSTYLE


def qselect(*a, **k):
    k.setdefault("style", _style())
    k.setdefault("pointer", ">")
    return questionary.select(*a, **k)


def qcheckbox(*a, **k):
    k.setdefault("style", _style())
    k.setdefault("pointer", ">")
    return questionary.checkbox(*a, **k)


def qtext(*a, **k):
    k.setdefault("style", _style())
    return questionary.text(*a, **k)


def qconfirm(*a, **k):
    k.setdefault("style", _style())
    return questionary.confirm(*a, **k)


def qpath(*a, **k):
    k.setdefault("style", _style())
    return questionary.path(*a, **k)


# ──────────────────────────────────────────────────────────────────────────────
# GOING BACK: every multi-question flow below (compress options, YouTube quality, convert, burn-in
# quality) can return to its previous question instead of forcing a restart of the whole program.
# ──────────────────────────────────────────────────────────────────────────────

BACK = object()          # sentinel: "go back one question" in one of those flows


def qselect_back(message: str, choices: list, back_ok: bool = True, default=None):
    """qselect with a "<< Back" choice appended when back_ok. `choices` may hold raw values, (label, value)
    pairs, or questionary.Choice objects; returns BACK if the person picks it instead of a real answer."""
    opts = []
    for c in choices:
        if isinstance(c, questionary.Choice):
            opts.append(c)
        elif isinstance(c, tuple):
            opts.append(questionary.Choice(c[0], c[1]))
        else:
            opts.append(questionary.Choice(str(c), c))
    if back_ok:
        opts.append(questionary.Choice("<< Back", BACK))
    return ask(qselect(message, choices=opts, default=default))


def back_validate(fn):
    """Wrap a qtext validator so typing b/back always passes, letting qtext_back return BACK for it."""
    return lambda t: True if t.strip().lower() in ("b", "back") else fn(t)


def qtext_back(message: str, default: str = "", validate=None, back_ok: bool = True) -> str:
    """Text input where typing b or back returns BACK. The field starts EMPTY (not pre-filled with
    `default`), so b/back is always one clean keystroke rather than needing to clear a pre-filled value
    first; pressing Enter with nothing typed returns `default` itself, when there's no stricter validator
    saying otherwise."""
    parts = []
    if default and not validate:
        parts.append(f"Enter for {default}")
    if back_ok:
        parts.append("b to go back")
    hint = f"  [{', '.join(parts)}]" if parts else ""
    v = back_validate(validate) if validate else None
    raw = ask(qtext(f"{message}{hint}", default="", validate=v) if v else qtext(f"{message}{hint}", default=""))
    if back_ok and raw.strip().lower() in ("b", "back"):
        return BACK
    return raw if raw.strip() else default


def qcheckbox_range(message: str, labels: list, back_ok: bool = True):
    """Like qcheckbox, with two extras: Shift+Down/Shift+Up drag-selects a range from the last place Space
    was pressed (like Shift-click in a file manager), and Escape returns BACK instead of the selection,
    when back_ok. Returns the chosen indexes (sorted), or BACK."""
    choices = [questionary.Choice(lbl, i) for i, lbl in enumerate(labels)]
    ic = InquirerControl(choices, None, pointer=">")
    anchor = ic.pointed_at

    def instruction():
        return ("(Space selects, Shift+Up/Down extends the selection, a = all, i = invert, "
                f"Enter = confirm{', Esc = back' if back_ok else ''})")

    def get_prompt_tokens():
        tokens = [("class:qmark", "?"), ("class:question", f" {message} ")]
        if ic.is_answered:
            tokens.append(("class:answer", f"done ({len(ic.selected_options)} selected)"))
        else:
            tokens.append(("class:instruction", instruction()))
        return tokens

    def selected_values():
        return [c.value for c in ic.get_selected_values()]

    def extend_to(target: int):
        lo, hi = sorted((anchor, target))
        for i in range(lo, hi + 1):
            v = ic.choices[i].value
            if v not in ic.selected_options:
                ic.selected_options.append(v)

    bindings = KeyBindings()

    @bindings.add(Keys.ControlQ, eager=True)
    @bindings.add(Keys.ControlC, eager=True)
    def _ctrlc(event):
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    if back_ok:
        @bindings.add(Keys.Escape, eager=True)
        def _esc(event):
            ic.is_answered = True
            event.app.exit(result=BACK)

    @bindings.add(" ", eager=True)
    def _toggle(event):
        nonlocal anchor
        v = ic.get_pointed_at().value
        if v in ic.selected_options:
            ic.selected_options.remove(v)
        else:
            ic.selected_options.append(v)
        anchor = ic.pointed_at

    @bindings.add("a", eager=True)
    def _all(event):
        if len(ic.selected_options) == len(ic.choices):
            ic.selected_options = []
        else:
            ic.selected_options = [c.value for c in ic.choices]

    @bindings.add("i", eager=True)
    def _invert(event):
        ic.selected_options = [c.value for c in ic.choices if c.value not in ic.selected_options]

    @bindings.add(Keys.Down, eager=True)
    @bindings.add("j", eager=True)
    def _down(event):
        ic.select_next()

    @bindings.add(Keys.Up, eager=True)
    @bindings.add("k", eager=True)
    def _up(event):
        ic.select_previous()

    @bindings.add(Keys.ShiftDown, eager=True)
    def _shift_down(event):
        ic.select_next()
        extend_to(ic.pointed_at)

    @bindings.add(Keys.ShiftUp, eager=True)
    def _shift_up(event):
        ic.select_previous()
        extend_to(ic.pointed_at)

    @bindings.add(Keys.ControlM, eager=True)
    def _enter(event):
        ic.is_answered = True
        event.app.exit(result=selected_values())

    @bindings.add(Keys.Any)
    def _other(event):
        pass

    app = Application(layout=create_inquirer_layout(ic, get_prompt_tokens),
                      key_bindings=bindings, style=_style())
    result = questionary.Question(app).ask()
    return result if result is BACK else sorted(result)





def split_paths(raw: str) -> list:
    """Dragging several files into the window pastes them as one line: "a.mp4" "b.mp4" ... Split that into paths."""
    raw = raw.strip()
    one = raw.strip("\"'")
    if not raw or Path(one).expanduser().exists() or parse_link(one):
        return [one]
    parts = [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', raw)]
    if len(parts) > 1 and any(Path(x).expanduser().exists() for x in parts):
        return parts
    return [one]


def section(title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]{title}[/]", style="dim cyan", align="left")


def kv(label: str, value: str) -> None:
    console.print(f"  [dim]{label:<12}[/] {value}")


def fmt_secs(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def fmt_bytes(n: float) -> str:
    return f"{n / 1_073_741_824:.2f} GiB" if n >= 1_073_741_824 else f"{n / 1_048_576:.2f} MiB"


def cpu_count() -> int:
    return os.cpu_count() or 4


def ffmpeg_threads(workers: int) -> int:
    """Threads for ONE ffmpeg when `workers` of them run at once, so together they don't oversubscribe the CPU."""
    return max(1, cpu_count() // max(1, workers))


class UI:
    """Coordinates a batch: the overall bar and file counter shared by every lane, plus one lane per
    worker. workers=1 (the default) gives a single lane, used exactly like the old one-bar UI."""

    def __init__(self, progress: Progress, total: int = 1, workers: int = 1):
        self.p, self.total = progress, total
        self.done_n = 0
        self._lock = threading.Lock()
        self._seen = set()
        self.overall = progress.add_task("", total=total) if total > 1 else None
        self.lanes = [Lane(self, progress.add_task("", total=100)) for _ in range(max(1, workers))]
        self._refresh()

    def lane(self, n: int = 0) -> "Lane":
        """The n-th worker's bar. Each concurrent worker must use its own lane; nothing else is shared state."""
        return self.lanes[n % len(self.lanes)]

    def _num(self, i: int) -> str:
        return f"[dim]{i:>{len(str(self.total))}}/{self.total}[/]" if self.total > 1 else ""

    def _refresh(self) -> None:
        if self.overall is not None:
            extra = sum(l._frac for l in self.lanes)
            self.p.update(self.overall, completed=min(self.total, self.done_n + extra),
                          description=f"[bold]Overall[/]  {self.done_n}/{self.total} files")

    def _print(self, text: str) -> None:
        with self._lock:
            self.p.console.print(text)

    def _once(self, key: str, text: str) -> None:
        with self._lock:
            fresh = key not in self._seen
            if fresh:
                self._seen.add(key)
        if fresh:
            self._print(text)

    def _advance(self) -> None:
        with self._lock:
            self.done_n += 1
        self._refresh()


class Lane:
    """One live bar for whichever file this worker currently has. Passed into the compress/convert/burn/
    download functions exactly like the old single UI object: begin/stage/tick/note/once/tag/done/fail/name.
    A lane is only ever touched by the one thread that owns it, so none of its own state needs a lock."""

    def __init__(self, batch: UI, task):
        self.batch, self.task = batch, task
        self.name, self.prefix, self.tags, self._frac = "", "", [], 0.0

    def begin(self, i: int, name: str) -> None:
        self.name = escape(name if len(name) <= 46 else name[:43] + "...")
        self.prefix = f"{i}/{self.batch.total}  " if self.batch.total > 1 else ""
        self.tags, self._frac = [], 0.0
        self.stage("starting")

    def stage(self, text: str) -> None:
        self.batch.p.update(self.task, description=f"{self.prefix}{self.name}  [dim]{text}[/]", completed=0)

    def tick(self, frac: float) -> None:
        self._frac = max(0.0, min(1.0, frac))
        self.batch.p.update(self.task, completed=self._frac * 100)
        self.batch._refresh()

    def note(self, text: str) -> None:
        self.batch._print(text)

    def once(self, key: str, text: str) -> None:
        self.batch._once(key, text)

    def tag(self, text: str) -> None:
        if text and text not in self.tags:
            self.tags.append(text)

    def done(self, i: int, name: str, dst: Path | None, secs: float, src_size: int | None = None) -> None:
        size = f"  {fmt_bytes(dst.stat().st_size):>10}" if dst and dst.exists() else ""
        if size and src_size:
            size = f"  [dim]{fmt_bytes(src_size):>10} \u2192[/]{size[1:]}"
        tags = f"  [dim]({', '.join(self.tags)})[/]" if self.tags else ""
        lead = f"{self.batch._num(i)}  " if self.batch.total > 1 else ""
        self.batch._print(f"[green]\u2713[/] {lead}{escape(name)}  [green]done in {fmt_secs(secs)}[/]{size}{tags}")
        self._frac = 0.0
        self.batch._advance()

    def fail(self, i: int, name: str, reason: str, secs: float) -> None:
        first = reason.strip().splitlines()[-1] if reason.strip() else "failed"
        lead = f"{self.batch._num(i)}  " if self.batch.total > 1 else ""
        self.batch._print(f"[red]\u2717[/] {lead}{escape(name)}  [red]failed after {fmt_secs(secs)}[/]\n"
                          f"      {escape(first[:200])}")
        self._frac = 0.0
        self.batch._advance()


def choose_items(labels: list, noun: str) -> list:
    """For bulk jobs: process everything or pick specific items. Returns the chosen indexes in order.
    On the pick list: Space selects, Shift+Up/Down drag-selects a range, Escape goes back to this question."""
    n = len(labels)
    if n < 2:
        return list(range(n))
    while True:
        mode = ask(qselect(f"Process all {n} {noun}, or choose specific ones?", choices=[
            questionary.Choice(f"All {n} {noun}", "all"),
            questionary.Choice("Choose specific ones", "pick"),
        ]))
        if mode == "all":
            return list(range(n))
        while True:
            picked = qcheckbox_range("Pick which ones", labels)
            if picked is BACK:
                break                 # back to "all or pick"
            if picked:
                return picked
            console.print("[yellow]Nothing selected, pick at least one[/yellow]")


# With parallel jobs, several ffmpeg processes can be in flight from different threads at once, so Ctrl+C
# cannot rely on a single local `proc` the way one-job-at-a-time code could: the main thread's
# KeyboardInterrupt only unwinds its own stack. kill_active() lets it reach every running ffmpeg immediately.
_ACTIVE_PROCS: set = set()
_ACTIVE_LOCK = threading.Lock()


def kill_active() -> None:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except OSError:
            pass


def run_ffmpeg(args: list, ui: UI | None = None, duration: float | None = None, cwd=None) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1", *map(str, args)]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, cwd=cwd,
                                text=True, encoding="utf-8", errors="replace")
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS.add(proc)
        try:
            for line in proc.stdout:
                if ui and duration and line.startswith("out_time_us="):
                    us = _float(line.split("=", 1)[1])
                    if us is not None:
                        ui.tick(us / 1e6 / duration)
            rc = proc.wait()
        finally:
            if proc.poll() is None:          # Ctrl+C etc. → don't leave ffmpeg running
                proc.kill()
                proc.wait()
            with _ACTIVE_LOCK:
                _ACTIVE_PROCS.discard(proc)
        if rc != 0:
            err.seek(0)
            raise FFmpegError(err.read().strip()[-800:] or f"ffmpeg exited with code {rc}")


# ──────────────────────────────────────────────────────────────────────────────
# VIDEO
# ──────────────────────────────────────────────────────────────────────────────

def video_enc_args(family: str, vcodec: str, level: str, kbps: int, threads: int | None = None) -> list:
    i = COMPRESSION_LEVELS.index(level)
    a = ["-c:v", vcodec, "-b:v", f"{kbps}k", "-pix_fmt", "yuv420p"]
    if family == "x264":
        a += ["-preset", ("ultrafast", "veryfast", "medium", "veryslow")[i]]
    elif family == "nvenc":
        a += ["-preset", ("p1", "p3", "p5", "p7")[i], "-rc", "vbr"]
    elif family == "vp9":
        a += ["-deadline", "good", "-cpu-used", str((5, 3, 2, 1)[i]), "-row-mt", "1"]
    else:  # svtav1
        a += ["-preset", str((12, 8, 5, 2)[i])]
    if threads:      # capped when several encodes run at once, so they share the CPU instead of fighting for it
        a += ["-threads", str(threads)]
    return a


def compress_video(inp: Path, dst_dir: Path, stem: str, target_mib: int, level: str, fmt: dict,
                   info: dict, saturation: float, use_2pass: bool, use_gpu: bool, fps_mode, ui: UI,
                   burn: dict | None = None, threads: int | None = None) -> Path:
    v = video_stream(info)
    if v is None:
        raise RuntimeError("No video stream in this file")
    has_audio = audio_stream(info) is not None
    duration = get_duration(info)
    target_bytes = int(target_mib * 1_048_576)

    # ── bitrate budget (3% kept back for container overhead) ──
    budget_kbps = target_bytes * 8 / 1000 / duration * 0.97
    audio_kbps = min(128, max(32, int(budget_kbps * 0.12))) if has_audio else 0
    video_kbps = int(budget_kbps - audio_kbps)
    if video_kbps < 30:
        raise RuntimeError(f"{target_mib} MiB is too small for {duration:.0f}s of video - pick a bigger target")
    audio_bytes = audio_kbps * 1000 / 8 * duration

    # ── codec ──
    vcodec, acodec, family = VIDEO_CODECS[fmt["name"]]
    if use_gpu and fmt["name"] == "MP4":
        vcodec, family = "h264_nvenc", "nvenc"
    two_pass = use_2pass and family != "nvenc"

    # ── resolution: shrink only as far as needed to keep enough bits per pixel ──
    w, h = _int(v.get("width"), 1920), _int(v.get("height"), 1080)
    sar_filter = None
    if burn:                    # anamorphic clips (for example 1440x1080) need square pixels for the text to look right
        f = sar_factor(v)
        if f != 1.0:
            w = max(2, round(w * f / 2) * 2)
            sar_filter = f"scale={w}:{h},setsar=1"
    src_fps = get_fps(v)
    out_fps, fps_note = pick_fps(src_fps, fps_mode, video_kbps, w, h, family)
    if fps_note:
        ui.tag(fps_note)
    factor = math.sqrt(video_kbps * 1000 / (w * h * min(float(out_fps), 120.0) * MIN_BPP[family]))
    factor = min(1.0, max(factor, min(1.0, 240 / min(w, h))))     # never below 240p short side, never upscale
    new_w, new_h = max(2, int(w * factor) // 2 * 2), max(2, int(h * factor) // 2 * 2)

    pre, post = [], []
    if is_interlaced(inp, v):
        pre.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
        ui.tag("deinterlaced")
    if sar_filter:
        pre.append(sar_filter)
    if saturation != 100:
        pre.append(f"eq=saturation={saturation / 100:.2f}")
    if out_fps != src_fps:
        post.append(f"fps={out_fps.numerator}/{out_fps.denominator}")
    if (new_w, new_h) != (w, h):
        post.append(f"scale={new_w}:{new_h}:flags=lanczos")
    vf_args = []
    # the date is burned in at full resolution, then scaled together with the picture
    src = ["-i", str(inp), *video_graph_args(pre, burn, post, w, h), "-map", "0:a:0?", "-dn"]
    audio_args = ["-c:a", acodec, "-b:a", f"{audio_kbps}k", "-ac", "2"] if has_audio else []
    mux_args = ["-movflags", "+faststart"] if fmt["ext"] == ".mp4" else []

    with tempfile.TemporaryDirectory(dir=dst_dir, prefix=".tmp_compress_") as td:
        td = Path(td)
        if burn:
            shutil.copy(burn["font"], td / "font.ttf")
        log = str(td / "pass")
        stats_ready = False
        best = smallest = None            # (size, path)
        kbps = video_kbps
        max_tries = 4

        for attempt in range(1, max_tries + 1):
            out_try = td / f"try{attempt}{fmt['ext']}"
            tag = f"Try {attempt}/{max_tries}"

            if two_pass and (not stats_ready or family == "svtav1"):
                ui.stage(f"{tag} · pass 1/2")
                try:
                    run_ffmpeg([*src, *vf_args, *video_enc_args(family, vcodec, level, kbps, threads),
                                "-pass", "1", "-passlogfile", log, "-an", "-f", "null", NULL_DEVICE],
                               ui, duration, cwd=td)
                    stats_ready = True
                except FFmpegError:
                    ui.once("no2pass", "[yellow]2-pass is not supported by this encoder/build, using single pass[/yellow]")
                    two_pass = False

            if two_pass:
                ui.stage(f"{tag} · pass 2/2 · {kbps} kbps")
                run_ffmpeg([*src, *vf_args, *video_enc_args(family, vcodec, level, kbps, threads),
                            "-pass", "2", "-passlogfile", log, *audio_args, *mux_args, out_try],
                           ui, duration, cwd=td)
            else:
                ui.stage(f"{tag} · {kbps} kbps")
                run_ffmpeg([*src, *vf_args, *video_enc_args(family, vcodec, level, kbps, threads),
                            *audio_args, *mux_args, out_try], ui, duration, cwd=td)

            size = out_try.stat().st_size
            if smallest is None or size < smallest[0]:
                smallest = (size, out_try)
            if size <= target_bytes and (best is None or size > best[0]):
                best = (size, out_try)
            if target_bytes * 0.93 <= size <= target_bytes:
                break                                   # close enough, under the limit

            # proportional correction on the VIDEO part only (audio + overhead are ~fixed)
            video_bytes = max(size - audio_bytes, 1)
            kbps = int(kbps * (target_bytes * 0.975 - audio_bytes) / video_bytes)
            if kbps < 30:
                break
            ui.note(f"[dim]  {ui.name}: {tag}: {size / 1_048_576:.2f} MiB ({size / target_bytes:.0%} of target), "
                    f"trying again at {kbps} kbps[/dim]")

        chosen = best or smallest
        ui.tag(f"{chosen[0] / target_bytes:.0%} of target")
        if best is None:
            ui.note(f"[yellow]Could not get under {target_mib} MiB "
                    f"(best: {chosen[0] / 1_048_576:.2f} MiB) - try a bigger target or lower resolution[/yellow]")
        dst = unique_path(dst_dir, stem, fmt["ext"])
        os.replace(chosen[1], dst)
        if burn:
            st = inp.stat()
            os.utime(dst, (st.st_atime, st.st_mtime))      # keep the recording time as the file date
        return dst


# ──────────────────────────────────────────────────────────────────────────────
# AUDIO
# ──────────────────────────────────────────────────────────────────────────────

def compress_audio(inp: Path, dst_dir: Path, stem: str, target_mib: int, level: str, fmt: dict,
                   info: dict, ui: UI) -> Path:
    duration = get_duration(info)
    target_bytes = int(target_mib * 1_048_576)
    codec, lo, hi = AUDIO_CODECS[fmt["name"]]

    kbps = int(target_bytes * 8 / 1000 / duration * 0.97)
    a = audio_stream(info)
    if a is None:
        raise RuntimeError("This file has no audio stream")
    src_kbps = _float(a.get("bit_rate"))
    if src_kbps:
        kbps = min(kbps, max(lo, int(src_kbps / 1000)))      # no point going above the source
    kbps = max(lo, min(hi, kbps))

    mux = ["-movflags", "+faststart"] if fmt["ext"] == ".m4a" else []
    with tempfile.TemporaryDirectory(dir=dst_dir, prefix=".tmp_compress_") as td:
        out_try = Path(td) / f"out{fmt['ext']}"
        for attempt in range(1, 4):
            ui.stage(f"Encoding · {kbps} kbps")
            run_ffmpeg(["-i", str(inp), "-map", "0:a:0", "-vn", "-sn", "-c:a", codec,
                        "-b:a", f"{kbps}k", *mux, out_try], ui, duration)
            size = out_try.stat().st_size
            if size <= target_bytes:
                break
            new_kbps = max(lo, int(kbps * target_bytes * 0.97 / size))
            if new_kbps >= kbps:                              # already at the codec minimum
                ui.note(f"[yellow]Smallest possible is {size / 1_048_576:.2f} MiB - can't reach {target_mib} MiB[/yellow]")
                break
            kbps = new_kbps
        dst = unique_path(dst_dir, stem, fmt["ext"])
        os.replace(out_try, dst)
        return dst


# ──────────────────────────────────────────────────────────────────────────────
# IMAGE
# ──────────────────────────────────────────────────────────────────────────────

def compress_image(inp: Path, dst_dir: Path, stem: str, target_mib: int, level: str, fmt: dict,
                   info: dict, saturation: float, ui: UI) -> Path:
    target_bytes = int(target_mib * 1_048_576)
    li = COMPRESSION_LEVELS.index(level)
    v = video_stream(info)
    w, h = _int(v.get("width"), 1920), _int(v.get("height"), 1080)
    name = fmt["name"]

    # codec settings: qualities are ordered BEST → WORST
    if name == "WebP":
        ext, qs = ".webp", list(range(100, 4, -3))
        enc = lambda q: ["-c:v", "libwebp", "-quality", str(q), "-compression_level", str((0, 2, 4, 6)[li])]
    elif name == "AVIF":
        ext, qs = ".avif", list(range(10, 64, 2))
        enc = lambda q: ["-c:v", "libaom-av1", "-still-picture", "1", "-cpu-used", str((8, 6, 4, 2)[li]),
                         "-crf", str(q), "-b:v", "0", "-pix_fmt", "yuv420p"]
    else:  # PNG/JPEG → JPEG
        ext, qs = ".jpg", list(range(2, 32))
        enc = lambda q: ["-c:v", "mjpeg", "-q:v", str(q), "-pix_fmt", "yuvj420p"]

    def encode(scale: float, codec_args: list, out: Path) -> int:
        nw, nh = max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)
        vf = []
        if (nw, nh) != (w, h):
            vf.append(f"scale={nw}:{nh}:flags=lanczos")
        if saturation != 100:
            vf.append(f"eq=saturation={saturation / 100:.2f}")
        run_ffmpeg(["-i", str(inp), "-map", "0:v:0", "-frames:v", "1",
                    *(["-vf", ",".join(vf)] if vf else []), *codec_args, out])
        return out.stat().st_size

    with tempfile.TemporaryDirectory(dir=dst_dir, prefix=".tmp_compress_") as td:
        td = Path(td)
        n = 0

        def finish(path: Path, final_ext: str) -> Path:
            dst = unique_path(dst_dir, stem, final_ext)
            os.replace(path, dst)
            return dst

        if name == "PNG/JPEG":                      # lossless first - keep it if it already fits
            ui.stage("Trying lossless PNG")
            png = td / "try.png"
            if encode(1.0, ["-c:v", "png"], png) <= target_bytes:
                return finish(png, ".png")

        scale = 1.0
        for round_ in range(1, 8):
            lo_i, hi_i = 0, len(qs) - 1
            best = None
            best_i = 0
            smallest = None
            while lo_i <= hi_i:
                mid = (lo_i + hi_i) // 2
                n += 1
                ui.stage(f"Round {round_} · scale {scale:.0%} · quality step {mid + 1}/{len(qs)}")
                out_try = td / f"try{n}{ext}"
                size = encode(scale, enc(qs[mid]), out_try)
                smallest = size if smallest is None else min(smallest, size)
                if size <= target_bytes:
                    best, best_i = out_try, mid
                    hi_i = mid - 1                  # fits → try better quality
                else:
                    lo_i = mid + 1
            if best:
                if scale < 0.999:
                    ui.note(f"[yellow]Image was resized to {scale:.0%} to fit {target_mib} MiB[/yellow]")
                if best_i >= len(qs) * 0.8:
                    ui.note("[yellow]Quality is very low to fit this target, result may look degraded[/yellow]")
                return finish(best, ext)
            scale *= max(0.3, min(0.9, math.sqrt(target_bytes / smallest) * 0.95))   # nothing fit → shrink

        raise RuntimeError(f"Could not get the image under {target_mib} MiB")


# ──────────────────────────────────────────────────────────────────────────────
# LOSSLESS CONVERT (no re-encoding)
# ──────────────────────────────────────────────────────────────────────────────

def stream_hash(path: Path, kind: str, index: int, tmp: Path, ui: UI, duration: float) -> str:
    """MD5 of the fully decoded video/audio stream, independent of container, timestamps and packet layout."""
    out = tmp / f"hash_{kind}{index}.txt"
    if kind == "v":
        args = ["-i", str(path), "-map", f"0:v:{index}", "-an", "-sn", "-dn",
                "-c:v", "rawvideo", "-fps_mode", "passthrough"]
    else:
        args = ["-i", str(path), "-map", f"0:a:{index}", "-vn", "-sn", "-dn",
                "-af", "aformat=sample_fmts=s32", "-c:a", "pcm_s32le"]
    run_ffmpeg([*args, "-f", "hash", "-hash", "md5", out], ui, duration)
    return out.read_text().strip()


def convert_lossless(inp: Path, dst_dir: Path, stem: str, info: dict, container: str,
                     audio_mode: str, verify: bool, ui: UI) -> Path:
    """Re-wrap video and audio into a new container. Video is never re-encoded."""
    v = video_stream(info)
    if v is None:
        raise RuntimeError("No video stream in this file")
    audios = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    acodecs = [a.get("codec_name", "") for a in audios]
    vcodec = v.get("codec_name", "")
    duration = get_duration(info)

    if container == "mp4" and vcodec not in MP4_VIDEO_OK:
        ui.note(f"[yellow]  {escape(vcodec or 'This video codec')} can't go into MP4 cleanly, using MKV for this file[/yellow]")
        container = "mkv"
    a_args, lossy_audio, bad = plan_audio(acodecs, container, audio_mode)
    if bad:
        ui.note(f"[yellow]  {escape(bad)} audio can't go into MP4 losslessly, using MKV for this file[/yellow]")
        container = "mkv"
        a_args, lossy_audio, _ = plan_audio(acodecs, container, audio_mode)
    ext = ".mp4" if container == "mp4" else ".mkv"

    subs = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    sub_args = ["-map", "0:s?", "-c:s", "copy"] if container == "mkv" and subs else []
    if subs and container == "mp4":
        ui.once("pgs", "[dim]Note: the camera's on-screen date track can't go into MP4, use the date burn-in option to keep the date visible[/dim]")

    st = inp.stat()
    extra = creation_meta(info, st)     # AVCHD files carry no date in the container, keep the recording time
    if container == "mp4" and vcodec == "hevc":
        extra += ["-tag:v", "hvc1"]

    with tempfile.TemporaryDirectory(dir=dst_dir, prefix=".tmp_convert_") as td:
        td = Path(td)
        out_try = td / f"out{ext}"
        ui.stage(f"Converting to {ext[1:].upper()}")
        run_ffmpeg(["-i", str(inp), "-map", "0:v:0", "-map", "0:a?", *sub_args, "-dn",
                    "-c:v", "copy", *a_args, *extra, out_try], ui, duration)

        if verify:
            ui.stage("Verifying video")
            if stream_hash(inp, "v", 0, td, ui, duration) != stream_hash(out_try, "v", 0, td, ui, duration):
                raise RuntimeError("Verification failed: the converted video differs from the original (original is untouched)")
            if not lossy_audio:
                for i in range(len(audios)):
                    ui.stage(f"Verifying audio {i + 1}/{len(audios)}")
                    if stream_hash(inp, "a", i, td, ui, duration) != stream_hash(out_try, "a", i, td, ui, duration):
                        raise RuntimeError(f"Verification failed: audio track {i + 1} differs from the original (original is untouched)")
            ui.tag("verified identical" if not lossy_audio else "video verified, audio converted by choice")

        dst = unique_path(dst_dir, stem, ext)
        os.replace(out_try, dst)
    os.utime(dst, (st.st_atime, st.st_mtime))       # keep the file's date
    return dst


def ask_convert_options(config: dict) -> dict:
    """Container, audio handling and verification. Any question can go back to the previous one."""
    YES_NO = [("Yes", True), ("No", False)]
    STAGES = ["container", "audio", "verify"]
    ans: dict = {}
    i, direction = 0, 1
    while 0 <= i < len(STAGES):
        stage = STAGES[i]
        back_ok = i > 0

        if stage == "container":
            val = qselect_back("Convert to", [
                questionary.Choice("MP4  -  plays almost everywhere", "mp4"),
                questionary.Choice("MKV  -  keeps every stream exactly as recorded", "mkv"),
            ], back_ok=back_ok, default=ans.get("container", config.get("convert_container", "mp4")))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["container"] = val

        elif stage == "audio":
            if ans["container"] != "mp4":
                ans["audio"] = "keep"
                i += direction; continue
            val = qselect_back("Audio", [
                questionary.Choice("Keep original audio (lossless)", "keep"),
                questionary.Choice("Convert to AAC 256 kbps (plays on more devices, tiny quality change)", "aac"),
            ], back_ok=back_ok, default=ans.get("audio", config.get("convert_audio", "keep")))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["audio"] = val

        else:  # verify
            val = qselect_back("Verify each file is identical to the original? (decodes both, slower)", YES_NO,
                               back_ok=back_ok, default=ans.get("verify", bool(config.get("verify", True))))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["verify"] = val

        direction = 1
        i += 1

    config.update({"convert_container": ans["container"], "convert_audio": ans["audio"], "verify": ans["verify"]})
    save_config(config)
    return {"container": ans["container"], "audio": ans["audio"], "verify": ans["verify"]}


def convert_one(path: Path, dest: Path, c: dict, info: dict, ui: UI) -> Path:
    return convert_lossless(path, dest, path.stem, info, c["container"], c["audio"], c["verify"], ui)


# ──────────────────────────────────────────────────────────────────────────────
# AVCHD DATE/TIME BURN-IN
# ──────────────────────────────────────────────────────────────────────────────
# A Sony Handycam stores the recording time twice inside every .MTS clip:
#   1. as a picture-subtitle track (the exact "Data Code" graphic the camera shows)
#   2. as MDPM metadata in the H.264 stream (a real date and time every ~0.5 s)
# The CPI/MPL files next to the clip are not needed for the date.

MTS_EXTS = {".mts", ".m2ts"}
MDPM_UUID = bytes.fromhex("17ee8c60f84d11d98cd60800200c9a66")
# Handycam on-screen Data Code, measured from a capture of the camera's own playback (fractions of the frame):
# two right-aligned lines (date over time), medium-weight white digits with a thin black outline, no background box.
# The camera spaces its characters differently from a normal font, so each group (day, month, year, hours,
# minutes, seconds) is placed on its own at the position measured from that capture.
DC_SIZE = 0.0702        # font size / frame height (Arial Regular digits are 0.716 of this)
DC_RIGHT = 0.917        # right edge of the digits / frame width
DC_DATE_TOP = 0.7009    # top of the date digits / frame height
DC_TIME_TOP = 0.7824    # top of the time digits / frame height
DC_BEARING = 0.05       # a glyph's right side bearing in font sizes (turns an ink edge into a text origin)
DC_DASH_DROP = 0.0298   # a lone hyphen is placed by its own top edge, so it is dropped to where the camera draws it
# right ink edge of each group, as a fraction of the frame height, counted left from the right edge of the text
DC_X_DATE = {"year": 0.0, "dash2": -0.1616, "month": -0.1871, "dash1": -0.2700, "day": -0.2962}
DC_X_TIME = {"sec": 0.0, "min": -0.0928, "hour": -0.1885}

BURN_QUALITY = [("High quality (looks identical to the original, recommended)", 18),
                ("Very high quality (bigger files)", 14),
                ("Lossless (huge files, some players can't open them)", 0)]
BURN_SPEEDS = [("Fast", "veryfast"), ("Balanced", "medium"), ("Slow (smaller file)", "slow")]


def find_avchd_stream(path: Path) -> Path | None:
    """The STREAM folder of an AVCHD recording (a card or a copy of it), or None."""
    cands = [path / rel for rel in ("PRIVATE/AVCHD/BDMV/STREAM", "AVCHD/BDMV/STREAM", "BDMV/STREAM", "STREAM")]
    if path.name.upper() == "STREAM":
        cands.append(path)
    for d in cands:
        try:
            if d.is_dir() and any(p.suffix.lower() in MTS_EXTS for p in d.iterdir()):
                return d
        except OSError:
            pass
    return None


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 15)


def parse_mdpm(data: bytes):
    """Decode Sony's MDPM block (what follows the SEI UUID). Returns (datetime, camera model) or None."""
    if data[:4] != b"MDPM":
        return None
    tags = {}
    for k in range(data[4]):
        e = data[5 + 5 * k: 10 + 5 * k]
        if len(e) < 5:
            break
        tags[e[0]] = e[1:]
    t18, t19 = tags.get(0x18), tags.get(0x19)      # 0x18 = zone/year/month, 0x19 = day/hour/minute/second (BCD)
    if not t18 or not t19:
        return None
    try:
        dt = datetime(_bcd(t18[1]) * 100 + _bcd(t18[2]), _bcd(t18[3]), _bcd(t19[0]),
                      _bcd(t19[1]), _bcd(t19[2]), _bcd(t19[3]))
    except ValueError:
        return None
    model = b"".join(tags.get(t, b"") for t in (0xE4, 0xE5, 0xE6)).rstrip(b"\0").decode("ascii", "replace")
    return dt, model


def _ts_payloads(f, pid: int):
    """Payload of every transport stream packet for one PID (handles 188 and 192 byte packets)."""
    head = f.read(400)
    f.seek(0)
    size = 192 if head[4:5] == b"G" and head[196:197] == b"G" else 188
    skip = size - 188
    while True:
        chunk = f.read(size * 2048)
        if not chunk:
            return
        for i in range(0, len(chunk) - size + 1, size):
            p = chunk[i + skip:i + size]
            if p[0] != 0x47 or (((p[1] & 0x1F) << 8) | p[2]) != pid:
                continue
            afc = (p[3] >> 4) & 3
            if not afc & 1:
                continue
            yield bool(p[1] & 0x40), p[(5 + p[4]) if afc & 2 else 4:]


def _pes_pts(b: bytes) -> int:
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def read_mdpm(path: Path, pid: int, start_time: float, max_hits: int = 12) -> tuple:
    """First few MDPM stamps as ([(seconds from the start of the file, datetime)], camera model)."""
    samples, model = [], None
    buf, pts = bytearray(), None

    def flush():
        nonlocal model
        if pts is None or not buf:
            return
        i = buf.find(MDPM_UUID)
        if i >= 0:
            r = parse_mdpm(bytes(buf[i + 16:i + 16 + 200]))
            if r:
                samples.append((pts / 90000 - start_time, r[0]))
                model = model or r[1]

    with open(path, "rb") as f:
        for pusi, payload in _ts_payloads(f, pid):
            if pusi:
                flush()
                buf.clear()
                pts = None
                if payload[:3] != b"\x00\x00\x01" or len(payload) < 15:
                    continue
                if payload[7] & 0x80:
                    pts = _pes_pts(payload[9:14])
                payload = payload[9 + payload[8]:]
            if len(buf) < 1024:             # the stamp sits in the first bytes of each picture
                buf += payload
            if len(samples) >= max_hits:
                break
        else:
            flush()
    return samples, model


def inspect_clip(path: Path, info: dict) -> dict:
    """The recording date of a clip, read from the Sony MDPM stamps inside its video stream."""
    out = {"first": None, "offset": None, "model": None,
           "duration": _float(info.get("format", {}).get("duration"), 0.0)}
    v = video_stream(info)
    try:
        pid = int(str(v.get("id")), 0) if v else None
    except ValueError:
        pid = None
    if not pid or "mpegts" not in info.get("format", {}).get("format_name", ""):
        return out
    try:
        samples, out["model"] = read_mdpm(path, pid, _float(info["format"].get("start_time"), 0.0))
    except OSError:
        return out
    if samples:
        out["first"] = samples[0][1]
        # each stamp says "the clock showed S during this frame", so the clock's phase must fit all of them
        pairs = [(p, calendar.timegm(dt.timetuple())) for p, dt in samples]
        lo = max(s - p for p, s in pairs)
        hi = min(s + 1 - p for p, s in pairs)
        out["offset"] = (lo + hi) / 2 if lo < hi else lo
    return out


def find_font() -> Path | None:
    if os.name == "nt":
        base = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        names = ["arial.ttf", "segoeui.ttf", "tahoma.ttf", "verdana.ttf", "arialbd.ttf"]
        bases = [base]
    else:
        names = ["LiberationSans-Regular.ttf", "Arial.ttf", "Arial Unicode.ttf", "DejaVuSans.ttf", "Helvetica.ttc"]
        bases = [Path(p) for p in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
                                   "/usr/share/fonts/truetype/liberation", "/Library/Fonts",
                                   "/System/Library/Fonts/Supplemental", "/System/Library/Fonts")]
    for n in names:             # the preferred font wins, whichever folder it is in
        for b in bases:
            if (b / n).is_file():
                return b / n
    return None


def date_segments(offset: float, duration: float) -> list:
    """The calendar date for each stretch of the clip, split where midnight passes: [(from s, to s or None, date)]."""
    t, out = 0.0, []
    while True:
        wall = datetime.fromtimestamp(offset + t, timezone.utc)
        midnight = (wall.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp() - offset
        if midnight > duration + 1:
            out.append((t, None, wall))
            return out
        out.append((t, midnight, wall))
        t = midnight


def datecode_filters(burn: dict, w: int, h: int) -> list:
    """Draw the Handycam Data Code: date over time, right aligned, white digits with a thin black outline.
    The drawtext filters read font.ttf from ffmpeg's working folder."""
    size = max(12, round(h * DC_SIZE))
    emb = max(0, round(h / 1080))       # a white border of this width thickens the regular font to the camera's weight
    outl = max(2, round(h / 720))       # width of the black outline (thin, like the camera)

    def piece(text: str, x_off: float, top: float, extra: str = "") -> tuple:
        x = f"x='w*{DC_RIGHT}+{x_off * h + DC_BEARING * size:.2f}-text_w'"
        base = f"drawtext=fontfile=font.ttf:fontsize={size}:text='{text}':{x}:y='h*{top}'{extra}"
        if emb:
            return (f"{base}:fontcolor=black:borderw={emb + outl}:bordercolor=black",
                    f"{base}:fontcolor=white:borderw={emb}:bordercolor=white")
        return (None, f"{base}:fontcolor=white:borderw={outl}:bordercolor=black")

    def lay(pieces: list) -> list:      # all outlines first, so a neighbour's outline never covers a digit
        return [f for f, _ in pieces if f] + [f for _, f in pieces]

    segs = date_segments(burn["offset"], burn["duration"])
    date = []
    for a, b, wall in segs:
        when = "" if len(segs) == 1 else f":enable='between(t,{a:.3f},{b if b is not None else 1e9:.3f})'"
        date += [piece(f"{wall.year}", DC_X_DATE["year"], DC_DATE_TOP, when),
                 piece("-", DC_X_DATE["dash2"], DC_DATE_TOP + DC_DASH_DROP, when),
                 piece(f"{wall.month}", DC_X_DATE["month"], DC_DATE_TOP, when),
                 piece("-", DC_X_DATE["dash1"], DC_DATE_TOP + DC_DASH_DROP, when),
                 piece(f"{wall.day}", DC_X_DATE["day"], DC_DATE_TOP, when)]
    whole = math.floor(burn["offset"])
    frac = burn["offset"] - whole

    def clock(fmt: str) -> str:         # the running time from the clip's own timestamps
        return "%{pts\\:gmtime\\:" + str(whole) + "\\:" + fmt + "}"

    time_ = lay([piece(clock("%H"), DC_X_TIME["hour"], DC_TIME_TOP),
                 piece("\\:" + clock("%M"), DC_X_TIME["min"], DC_TIME_TOP),      # a literal colon needs \: even in quotes
                 piece("\\:" + clock("%S"), DC_X_TIME["sec"], DC_TIME_TOP)])
    if frac > 0.001:        # line the clock's second boundaries up with the camera's, then undo the shift
        time_ = [f"setpts=PTS+{frac:.4f}/TB", *time_, f"setpts=PTS-{frac:.4f}/TB"]
    return [*lay(date), *time_]


def sar_factor(v: dict) -> float:
    """Pixel aspect ratio of an anamorphic clip (1440x1080 AVCHD is 4:3), 1.0 for normal square pixels."""
    try:
        n, d = str(v.get("sample_aspect_ratio", "1:1")).split(":")
        f = int(n) / int(d)
    except (ValueError, ZeroDivisionError):
        return 1.0
    return f if f > 0 and abs(f - 1) > 0.01 else 1.0


def video_graph_args(pre: list, burn: dict | None, post: list, w: int, h: int) -> list:
    """ffmpeg arguments that pick the video stream, run the filters and burn the date in."""
    chain = list(pre)
    if burn:
        chain += datecode_filters(burn, w, h)
    chain += post
    return ["-map", "0:v:0", *(["-vf", ",".join(chain)] if chain else [])]


def plan_audio(acodecs: list, container: str, audio_mode: str) -> tuple:
    """Per-stream audio args that never lose anything unless AAC was asked for.
    Returns (args, lossy, first codec MP4 can't hold or None)."""
    args, lossy, bad = [], False, None
    for i, c in enumerate(acodecs):
        if container == "mp4" and audio_mode == "aac" and c != "aac":
            args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "256k"]
            lossy = True
        elif c.startswith("pcm"):           # LPCM (some Sony models) can't be copied as is, so store it losslessly
            args += [f"-c:a:{i}", "alac" if container == "mp4" else "flac"]
        else:
            if container == "mp4" and c not in MP4_AUDIO_COPY:
                bad = bad or c
            args += [f"-c:a:{i}", "copy"]
    return args, lossy, bad


def creation_meta(info: dict, st) -> list:
    """creation_time from the file date, when the container has none (AVCHD never does)."""
    tags = {k.lower() for k in (info.get("format", {}).get("tags") or {})}
    if "creation_time" in tags:
        return []
    stamp = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return ["-metadata", f"creation_time={stamp}"]


def scan_clips(files: list, infos: dict) -> dict:
    return {f: inspect_clip(f, infos[f]) for f in files}


def ask_burn(clips: dict) -> bool:
    """Offer to burn the recording date into the video, and say clearly what was or wasn't found."""
    dated = [c["first"] for c in clips.values() if c["first"]]
    if dated:
        a, b = min(dated), max(dated)
        model = next((c["model"] for c in clips.values() if c["model"]), "")
        span = f"{a:%d-%m-%Y %H:%M:%S}" if a == b else f"{a:%d-%m-%Y %H:%M} to {b:%d-%m-%Y %H:%M}"
        kv("Recorded", f"{span}   {escape(model)}")
    else:
        console.print("[yellow]No recording date was found in the video of these files.[/yellow]")
    if find_font() is None:
        console.print("[yellow]No usable font was found on this computer, the date can't be drawn.[/yellow]")
        return False
    return ask(qconfirm("Burn the recording date and time into the video (Handycam style)?", default=bool(dated)))


def plan_burn(files: list, clips: dict) -> dict:
    """Burn settings per clip. Clips with no usable date are never given a made-up one silently."""
    font = find_font()
    out, missing = {}, []
    for f in files:
        c = clips[f]
        if c["offset"] is not None:
            out[f] = {"offset": c["offset"], "font": font, "duration": c["duration"]}
        else:
            missing.append(f)
    if missing:
        names = ", ".join(m.name for m in missing[:5]) + ("..." if len(missing) > 5 else "")
        console.print(f"[yellow]{len(missing)} clip(s) contain no recording date: {escape(names)}[/yellow]")
        options = [questionary.Choice("Use the file's modified date (it is when the recording ENDED, may be off)", "mtime"),
                   questionary.Choice("Skip these clips", "skip")]
        if len(missing) == 1:
            options.insert(1, questionary.Choice("Type the start date and time myself", "manual"))
        how = ask(qselect("What should be used for them?", choices=options))
        for f in missing:
            if how == "skip":
                out[f] = None
                continue
            if how == "manual":
                s = ask(qtext("Start date and time (YYYY-MM-DD HH:MM:SS)",
                              validate=lambda t: _valid_stamp(t) or "Use the format 2026-06-19 17:01:32"))
                offset = calendar.timegm(datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").timetuple())
            else:
                end = datetime.fromtimestamp(f.stat().st_mtime)
                offset = calendar.timegm(end.timetuple()) - clips[f]["duration"]
            out[f] = {"offset": offset, "font": font, "duration": clips[f]["duration"]}
    return out


def _valid_stamp(t: str) -> bool:
    try:
        datetime.strptime(t.strip(), "%Y-%m-%d %H:%M:%S")
        return True
    except ValueError:
        return False


def ask_burn_quality(config: dict) -> dict:
    """Quality, encode speed and audio handling for the high-quality burn-in. Any question can go back."""
    STAGES = ["crf", "preset", "audio"]
    ans: dict = {}
    i = 0
    while 0 <= i < len(STAGES):
        stage = STAGES[i]
        back_ok = i > 0

        if stage == "crf":
            val = qselect_back("Quality", BURN_QUALITY, back_ok=back_ok,
                               default=ans.get("crf", config.get("burn_crf", 18)))
            if val is BACK:
                i -= 1; continue
            ans["crf"] = val

        elif stage == "preset":
            val = qselect_back("Encoding speed", BURN_SPEEDS, back_ok=back_ok,
                               default=ans.get("preset", config.get("burn_preset", "medium")))
            if val is BACK:
                i -= 1; continue
            ans["preset"] = val

        else:  # audio
            val = qselect_back("Audio", [
                questionary.Choice("Keep original audio", "keep"),
                questionary.Choice("Convert to AAC 256 kbps (plays on more devices)", "aac")],
                back_ok=back_ok, default=ans.get("audio", config.get("convert_audio", "keep")))
            if val is BACK:
                i -= 1; continue
            ans["audio"] = val

        i += 1

    config.update({"burn_crf": ans["crf"], "burn_preset": ans["preset"], "convert_audio": ans["audio"]})
    save_config(config)
    return {"crf": ans["crf"], "preset": ans["preset"], "audio": ans["audio"]}


def burn_quality(inp: Path, dst_dir: Path, stem: str, info: dict, burn: dict, crf: int, preset: str,
                 audio_mode: str, ui: UI, threads: int | None = None) -> Path:
    """Re-encode to H.264 MP4 with the date burned in, keeping resolution, frame rate and all audio."""
    v = video_stream(info)
    if v is None:
        raise RuntimeError("No video stream in this file")
    duration = get_duration(info)
    w, h = _int(v.get("width"), 1920), _int(v.get("height"), 1080)
    acodecs = [s.get("codec_name", "") for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    a_args, _, bad = plan_audio(acodecs, "mp4", audio_mode)
    if bad:
        ui.note(f"[yellow]  {escape(bad)} audio can't go into MP4, converting the audio to AAC 256 kbps[/yellow]")
        a_args, _, _ = plan_audio(acodecs, "mp4", "aac")

    pre = []
    if is_interlaced(inp, v):
        pre.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")     # keeps the frame rate (29.97i -> 29.97p)
        ui.tag("deinterlaced")
    f = sar_factor(v)
    if f != 1.0:                # anamorphic clips (for example 1440x1080) need square pixels for the text to look right
        w = max(2, round(w * f / 2) * 2)
        pre.append(f"scale={w}:{h},setsar=1")
    st = inp.stat()
    with tempfile.TemporaryDirectory(dir=dst_dir, prefix=".tmp_burn_") as td:
        td = Path(td)
        shutil.copy(burn["font"], td / "font.ttf")
        out_try = td / "out.mp4"
        ui.stage("encoding")
        run_ffmpeg(["-i", str(inp), *video_graph_args(pre, burn, [], w, h), "-map", "0:a?", "-dn",
                    "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                    *(["-threads", str(threads)] if threads else []),
                    *a_args, "-movflags", "+faststart", *creation_meta(info, st), out_try],
                   ui, duration, cwd=td)
        dst = unique_path(dst_dir, stem, ".mp4")
        os.replace(out_try, dst)
    os.utime(dst, (st.st_atime, st.st_mtime))
    return dst


# ──────────────────────────────────────────────────────────────────────────────
# YOUTUBE DOWNLOAD (yt-dlp)
# ──────────────────────────────────────────────────────────────────────────────

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240]
BIG_PLAYLIST = 50


class DownloadFailed(RuntimeError):
    pass


def parse_link(raw: str) -> dict | None:
    """Return {url, video_id, list_id} for a YouTube link, else None."""
    raw = raw.strip()
    if raw.lower().startswith(tuple(h + "/" for h in YT_HOSTS)):
        raw = "https://" + raw
    try:
        u = urlparse(raw)
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or host not in YT_HOSTS:
        return None
    qs = parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    video_id = None
    if host == "youtu.be":
        video_id = parts[0] if parts else None
    elif parts and parts[0] == "watch":
        video_id = (qs.get("v") or [None])[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
        video_id = parts[1]
    list_id = (qs.get("list") or [None])[0]
    if not video_id and not list_id:
        return None
    return {"url": raw, "video_id": video_id, "list_id": list_id}


def single_video_url(link: dict) -> str:
    """The link with the playlist part removed."""
    return f"https://www.youtube.com/watch?v={link['video_id']}"


def _ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        return None


class _QuietLogger:
    """yt-dlp would print straight to the terminal and fight the progress bar; we show errors ourselves."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl_base() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger()}
    runtimes = {n: {} for n in ("deno", "node", "bun") if shutil.which(n)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    return opts


def _clean_err(e: Exception) -> str:
    msg = str(e).replace("ERROR: ", "").strip()
    return msg[-500:] + "\n(YouTube changes often - try: pip install -U \"yt-dlp[default]\")"


def fetch_info(url: str, *, playlist: bool = False) -> dict:
    yt = _ytdlp()
    opts = {**_ydl_base(), "skip_download": True, "noplaylist": not playlist}
    if playlist:
        opts["extract_flat"] = "in_playlist"
    try:
        with yt.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt.utils.YoutubeDLError as e:
        raise DownloadFailed(_clean_err(e)) from None


def fmt_duration(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _res(f: dict) -> int | None:
    w, h = f.get("width"), f.get("height")
    return min(w, h) if w and h else h      # same meaning as YouTube's "1080p" (short side)


def build_selection(plan: dict) -> dict:
    """yt-dlp format options for the chosen mode/quality."""
    if plan["audio_only"]:
        abr = plan["abr"]
        return {"format": f"bestaudio[abr<={abr}]/worstaudio/best" if abr else "bestaudio/best"}
    cap = f"res:{plan['height']}" if plan["height"] else "res"
    return {"format": "bv*+ba/b", "format_sort": [cap, "vcodec:h264", "acodec:aac"]}


def download_one(url: str, dest_dir: Path, prefix: str, plan: dict, ui: UI, label: str) -> Path:
    yt = _ytdlp()
    state = {"downloading": False}

    def on_progress(d):
        if d["status"] == "downloading":
            if not state["downloading"]:
                state["downloading"] = True
                ui.stage(f"{label} · downloading")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                ui.tick(d.get("downloaded_bytes", 0) / total)
        elif d["status"] == "finished":
            state["downloading"] = False

    def on_pp(d):
        if d["status"] == "started":
            ui.stage(f"{label} · processing")
            ui.tick(0.99)

    opts = {
        **_ydl_base(),
        **build_selection(plan),
        "noplaylist": True,
        "paths": {"home": str(dest_dir)},
        "outtmpl": {"default": f"{prefix}%(title).120s.%(ext)s"},
        "windowsfilenames": True,
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_pp],
        "merge_output_format": "mkv" if plan["compress"] else "mp4",
    }
    if plan["audio_only"] and not plan["compress"]:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}]

    try:
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt.utils.YoutubeDLError as e:
        raise DownloadFailed(_clean_err(e)) from None
    done = (info.get("requested_downloads") or [{}])[0].get("filepath")
    if not done or not Path(done).is_file():
        raise DownloadFailed("Download finished but the file wasn't found")
    return Path(done)


def youtube_flow(link: dict) -> dict | None:
    """Ask everything about a YouTube link. Returns a plan, or None to go back to the input prompt."""
    if _ytdlp() is None:
        console.print('[red]yt-dlp is not installed →[/red] pip install -U "yt-dlp[default]"')
        return None
    if not shutil.which("deno") and not shutil.which("node") and not shutil.which("bun"):
        console.print("[dim]Tip: yt-dlp wants a JavaScript runtime (Deno recommended) for full YouTube support - "
                      "some qualities may be missing without one.[/dim]")

    url = link["url"]
    entries = None          # playlist entries, or None for a single video
    folder = None

    # ── playlist check ───────────────────────────────────────────────────────
    if link["list_id"]:
        try:
            with console.status("[bold green]Reading playlist..."):
                pl = fetch_info(url, playlist=True)
            items = [e for e in (pl.get("entries") or []) if e and e.get("id")]
        except DownloadFailed as e:
            items, pl = [], {}
            if link["video_id"]:
                console.print(f"[yellow]Couldn't read the playlist - downloading just this video.[/yellow]\n[dim]{escape(str(e))}[/dim]")
            else:
                console.print(f"[red]Couldn't read the playlist:[/red] {escape(str(e))}")
                return None

        if items:
            n = len(items)
            title = pl.get("title") or "playlist"
            kind_txt = "auto-generated mix" if link["list_id"].startswith(("RD", "UL")) else "playlist"
            console.print(f"\n[bold yellow]! This link contains a whole {kind_txt}:[/] {escape(title)}  [bold]{n} videos[/]")
            console.print("[yellow]Downloading all of it can take a long time and a lot of disk space.[/yellow]")
            whole = ask(qconfirm("Download from the playlist?", default=False))
            if whole:
                labels = [f"{i + 1:>3}. {(e.get('title') or e['id'])[:70]}"
                          + (f"  ({fmt_duration(e['duration'])})" if e.get("duration") else "")
                          for i, e in enumerate(items)]
                items = [items[i] for i in choose_items(labels, "videos")]
                if len(items) > BIG_PLAYLIST:
                    whole = ask(qconfirm(f"That's {len(items)} videos, are you really sure?", default=False))
            if whole:
                entries = [{"url": f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or e["id"]}
                           for e in items]
                yt = _ytdlp()
                folder = yt.utils.sanitize_filename(title, restricted=False)[:100].strip(" .") or "playlist"
            elif link["video_id"]:
                url = single_video_url(link)
                kv("Playlist", "removed from the link, downloading just the video")
            else:
                console.print("[yellow]This link is a playlist only, there's no single video in it.[/yellow]")
                return None
        elif link["video_id"]:
            url = single_video_url(link)
        else:
            console.print("[yellow]That playlist looks empty.[/yellow]")
            return None

    # ── single video info ────────────────────────────────────────────────────
    heights = []
    if entries is None:
        try:
            with console.status("[bold green]Reading video info..."):
                info = fetch_info(url)
        except DownloadFailed as e:
            console.print(f"[red]Can't read that video:[/red] {escape(str(e))}")
            return None
        if info.get("is_live"):
            console.print("[yellow]Live streams aren't supported.[/yellow]")
            return None
        title = info.get("title") or "video"
        kv("Video", f"{escape(title)}  [dim]({fmt_duration(info.get('duration'))})[/]")
        entries = [{"url": url, "title": title}]
        heights = sorted({r for f in info.get("formats", []) if f.get("vcodec") not in (None, "none")
                          for r in [_res(f)] if r})

    # ── questions (mode -> quality -> compress, any of them can go back to the previous one) ──────────────
    best = max(heights) if heights else None
    caps = [h for h in VIDEO_HEIGHTS if best is None or h < best]
    best_txt = f"Best available ({best}p)" if best else "Best available"
    suffix = "" if len(entries) == 1 else "  (per video, lower if it has less)"

    STAGES = ["mode", "quality", "compress"]
    ans: dict = {}
    i = 0
    while 0 <= i < len(STAGES):
        stage = STAGES[i]
        back_ok = i > 0

        if stage == "mode":
            val = qselect_back("Download as", [
                questionary.Choice("Video (with audio)", "video"),
                questionary.Choice("Audio only", "audio"),
            ], back_ok=back_ok, default=ans.get("mode"))
            if val is BACK:
                i -= 1; continue
            ans["mode"] = val

        elif stage == "quality":
            if ans["mode"] == "audio":
                val = qselect_back("Audio quality", [
                    questionary.Choice("Best available", 0),
                    questionary.Choice("Standard (up to 128 kbps)", 128),
                    questionary.Choice("Small (up to 64 kbps)", 64),
                ], back_ok=back_ok, default=ans.get("abr", 0))
                if val is BACK:
                    i -= 1; continue
                ans["abr"], ans["height"] = val, 0
            else:
                val = qselect_back("Video quality", [
                    questionary.Choice(best_txt + suffix, 0),
                    *[questionary.Choice(f"{h}p" + suffix, h) for h in caps],
                ], back_ok=back_ok, default=ans.get("height", 0))
                if val is BACK:
                    i -= 1; continue
                ans["height"], ans["abr"] = val, 0

        else:  # compress
            val = qselect_back("Compress it after downloading?", [("Yes", True), ("No", False)],
                               back_ok=back_ok, default=ans.get("compress", True))
            if val is BACK:
                i -= 1; continue
            ans["compress"] = val

        i += 1

    return {"entries": entries, "folder": folder, "audio_only": ans["mode"] == "audio",
            "height": ans["height"], "abr": ans["abr"], "compress": ans["compress"]}


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def allowed_formats(kind: str, has_audio: bool) -> list:
    kinds = {"video"} | ({"audio"} if has_audio else set()) if kind == "video" else {kind}
    return [f for f in FORMATS if f["kind"] in kinds]


def ask_options(kind: str, has_audio: bool, config: dict, can_back: bool = False) -> dict:
    """Target size, format, level and (for video/image) encoder/frame-rate/saturation questions.
    Any question can go back to the previous one (type b, or pick << Back) instead of restarting.
    With can_back, going back out of the very first question (size) returns BACK instead of asking it,
    so the caller can send the person back to whatever came before this group of questions."""
    size_choices = [
        questionary.Choice("1 MiB", 1),
        questionary.Choice("8 MiB", 8),
        questionary.Choice("10 MiB   \u2190 most popular for short clips", 10),
        questionary.Choice("20 MiB", 20),
        questionary.Choice("25 MiB", 25),
        questionary.Choice("50 MiB   \u2190 good balance", 50),
        questionary.Choice("100 MiB  \u2190 high quality longer content", 100),
        questionary.Choice("500 MiB", 500),
        questionary.Choice("Custom...", "custom"),
    ]
    YES_NO = [("Yes", True), ("No", False)]
    STAGES = ["size", "format", "level", "gpu", "two_pass", "fps", "saturation"]
    ans: dict = {}
    i, direction = 0, 1
    while i < len(STAGES):
        if i < 0:
            return BACK
        stage = STAGES[i]
        back_ok = i > 0 or (i == 0 and can_back)

        if stage == "size":
            val = qselect_back("Target size (MiB)", size_choices, back_ok=back_ok)
            if val is BACK:
                direction = -1; i -= 1; continue
            if val == "custom":
                raw = qtext_back("Custom size (MiB)", default="10",
                                 validate=lambda t: (t.isdigit() and int(t) > 0) or "Enter a whole number above 0",
                                 back_ok=True)
                if raw is BACK:
                    continue                      # re-show the size menu, not a stage further back
                val = int(raw)
            ans["target_mib"] = val

        elif stage == "format":
            fmts = allowed_formats(kind, has_audio)
            val = qselect_back("Output format",
                               [questionary.Choice(f"{f['name']}  \u2022  {f['group']}", f) for f in fmts],
                               back_ok=back_ok)
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["fmt"] = val

        elif stage == "level":
            val = qselect_back("Compression level (speed \u2194 quality)", COMPRESSION_LEVELS, back_ok=back_ok,
                               default=ans.get("level"))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["level"] = val

        elif stage == "gpu":
            fmt = ans["fmt"]
            if not (fmt["kind"] == "video" and fmt["name"] == "MP4" and nvenc_works()):
                ans["use_gpu"] = False
                i += direction; continue
            val = qselect_back("GPU (NVENC) encoding?", YES_NO, back_ok=back_ok,
                               default=ans.get("use_gpu", bool(config.get("gpu", False))))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["use_gpu"] = val
            config["gpu"] = val
            if val:
                console.print("[dim]GPU encoding is single-pass[/dim]")

        elif stage == "two_pass":
            fmt = ans["fmt"]
            if fmt["kind"] != "video" or ans.get("use_gpu"):
                ans["use_2pass"] = False
                i += direction; continue
            val = qselect_back("2-pass encoding? (slower, more accurate size)", YES_NO, back_ok=back_ok,
                               default=ans.get("use_2pass", bool(config.get("two_pass", True))))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["use_2pass"] = val
            config["two_pass"] = val

        elif stage == "fps":
            fmt = ans["fmt"]
            if fmt["kind"] != "video":
                ans["fps"] = "original"
                i += direction; continue
            val = qselect_back("Frame rate", FPS_CHOICES, back_ok=back_ok, default=ans.get("fps"))
            if val is BACK:
                direction = -1; i -= 1; continue
            ans["fps"] = val

        else:  # saturation
            fmt = ans["fmt"]
            if fmt["kind"] not in ("video", "image"):
                ans["saturation"] = 100.0
                i += direction; continue
            raw = qtext_back("Saturation multiplier (%)", default="100", back_ok=back_ok)
            if raw is BACK:
                direction = -1; i -= 1; continue
            try:
                ans["saturation"] = max(0.0, min(200.0, float(raw)))
            except ValueError:
                console.print("[yellow]Invalid value \u2192 using 100%[/yellow]")
                ans["saturation"] = 100.0

        direction = 1
        i += 1

    save_config(config)
    return {"target_mib": ans["target_mib"], "fmt": ans["fmt"], "level": ans["level"],
            "use_gpu": ans["use_gpu"], "use_2pass": ans["use_2pass"], "fps": ans["fps"],
            "saturation": ans["saturation"]}


def compress_one(path: Path, dest: Path, o: dict, info: dict, ui: UI, burn: dict | None = None,
                 threads: int | None = None) -> Path:
    fmt, stem = o["fmt"], path.stem + "_compressed"
    if fmt["kind"] == "video":
        return compress_video(path, dest, stem, o["target_mib"], o["level"], fmt, info,
                              o["saturation"], o["use_2pass"], o["use_gpu"], o["fps"], ui, burn=burn, threads=threads)
    if fmt["kind"] == "audio":
        return compress_audio(path, dest, stem, o["target_mib"], o["level"], fmt, info, ui)
    return compress_image(path, dest, stem, o["target_mib"], o["level"], fmt, info, o["saturation"], ui)


def output_category(job: str, plan: dict | None, local: list) -> str:
    """Which subfolder of the output folder a job's results go into."""
    if plan:
        return "youtube_compressed" if plan["compress"] else "youtube"
    avchd = bool(local) and all(f.suffix.lower() in MTS_EXTS for f in local)     # Handycam clips
    if job == "convert":
        return "avchd_converted" if avchd else "converted"
    if job == "burn_hq":
        return "avchd_datecode"
    return "avchd_compressed" if avchd else "compressed"


def fmt_rate(bps: float) -> str:
    return f"{bps / 1e6:.1f} Mb/s" if bps >= 1e6 else f"{bps / 1e3:.0f} kb/s"


def media_lines(info: dict, path: Path, kind: str) -> list:
    """Compact technical summary of a file: [(label, text)] for Video / Audio / Image / File."""
    fmt = info.get("format", {})
    size = path.stat().st_size
    dur = _float(fmt.get("duration"))
    total = _float(fmt.get("bit_rate")) or (size * 8 / dur if dur else None)
    audios = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    a_bps = sum(_float(a.get("bit_rate"), 0.0) for a in audios)
    rows = []
    v = video_stream(info)
    if v and kind == "image":
        rows.append(("Image", f"{v.get('codec_name', '?')}, {v.get('width')}x{v.get('height')}, {v.get('pix_fmt', '')}".rstrip(", ")))
    elif v:
        bits = [f"{v.get('codec_name', '?')}{' ' + v['profile'] if v.get('profile') else ''}",
                f"{v.get('width')}x{v.get('height')}",
                f"{float(get_fps(v)):.3f}".rstrip("0").rstrip(".") + " fps"]
        scan = {"progressive": "progressive"}.get(v.get("field_order"), "interlaced" if v.get("field_order") in ("tt", "bb", "tb", "bt") else "")
        if scan:
            bits[-1] += f" {scan}"
        if v.get("pix_fmt"):
            bits.append(v["pix_fmt"])
        vb = _float(v.get("bit_rate"))
        if vb:
            bits.append(fmt_rate(vb))
        elif total and total > a_bps:           # many containers (MPEG-TS) don't state it, so estimate from the total
            bits.append("~" + fmt_rate(total - a_bps))
        rows.append(("Video", ", ".join(bits)))
    if audios:
        a = audios[0]
        ch = _int(a.get("channels"))
        layout = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}ch" if ch else "")
        bits = [a.get("codec_name", "?"), layout]
        if _float(a.get("sample_rate")):
            bits.append(f"{float(a['sample_rate']) / 1000:g} kHz")
        if _float(a.get("bit_rate")):
            bits.append(fmt_rate(float(a["bit_rate"])))
        extra = f"  (+{len(audios) - 1} more)" if len(audios) > 1 else ""
        rows.append(("Audio", ", ".join(b for b in bits if b) + extra))
    names = fmt.get("format_name", "")
    box = path.suffix[1:].upper() + (" (MPEG-TS)" if "mpegts" in names else "")
    parts = [box, fmt_bytes(size)]
    if dur and kind != "image":
        parts.append(fmt_duration(dur))
    if total and kind != "image":
        parts.append(fmt_rate(total))
    rows.append(("File", ", ".join(p for p in parts if p.strip())))
    return rows


def print_banner() -> None:
    tree = ("AVCHD\n"
            "[dim]\u2514\u2500\u2500\u2500[/]BDMV\n"
            "    [dim]\u251c\u2500\u2500\u2500[/]CLIPINF\n"
            "    [dim]\u251c\u2500\u2500\u2500[/]PLAYLIST\n"
            "    [dim]\u2514\u2500\u2500\u2500[/]STREAM")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", no_wrap=True)
    grid.add_column()
    grid.add_row("Single file", "drag in the video to compress or convert it")
    grid.add_row("Bulk", "drag in the folder with the videos (all or pick)")
    grid.add_row("YouTube", "paste a link, playlists too (all or pick)")
    grid.add_row("Handycam", "drag in the whole AVCHD folder to convert,")
    grid.add_row("", "compress or burn in the recording date.")
    grid.add_row("", "It must contain this folder tree:")
    grid.add_row("", tree)
    console.print(Panel(
        Group("[bold cyan]RATECAP[/]",
              "[dim]compress | convert | YouTube | AVCHD date burn-in | bulk[/]",
              "", grid),
        border_style="cyan", padding=(0, 2), expand=False))


def _label_clip(f: Path, clips: dict, root: Path | None) -> str:
    c = clips.get(f)
    when = f"{c['first']:%d-%m-%Y %H:%M:%S}" if c and c["first"] else "no date"
    dur = fmt_duration(c["duration"]) if c else ""
    return f"{f.name:<12} {when:<19} {dur:>7}  {fmt_bytes(f.stat().st_size):>10}"


def ask_parallel(n: int, job: str, plan: dict | None, config: dict) -> int:
    """How many files to process at once in a batch of n. Returns 1 for the plain one-at-a-time behavior."""
    if n < 2:
        return 1
    cpus = cpu_count()
    if plan:                      # downloads: limited by bandwidth, not CPU; keep it modest either way
        suggested = min(4, n)
    elif job == "convert":        # lossless: a stream copy barely touches the CPU, so this scales further
        suggested = min(8, cpus, n)
    else:                         # compress / burn in date: a real re-encode per file, don't oversubscribe the CPU
        suggested = min(4, max(1, cpus // 2), n)
    if suggested < 2:
        return 1
    noun = "downloads" if plan else "files"
    saved = config.get("parallel", 1)
    choices = [questionary.Choice("One at a time", 1),
              questionary.Choice(f"{suggested} at a time  (suggested for this computer)", suggested)]
    if saved not in (1, suggested) and saved <= n:
        choices.append(questionary.Choice(f"{saved} at a time  (last used)", saved))
    choices.append(questionary.Choice("Custom...", "custom"))
    workers = ask(qselect(f"Process how many {noun} at once?", choices=choices,
                          default=saved if saved in (1, suggested) else 1))
    if workers == "custom":
        cap = min(n, 16)
        workers = int(ask(qtext(f"How many at once? (1-{cap})", default=str(min(suggested, cap)),
                                validate=lambda t: (t.isdigit() and 1 <= int(t) <= cap) or f"Enter 1-{cap}")))
    workers = max(1, min(workers, n))
    config["parallel"] = workers
    save_config(config)
    if workers > 1:
        kv("Parallel", f"{workers} at a time")
    return workers


def run_job(config: dict) -> None:
    """One full job: pick the input (file, folder or link), choose what to do, process. Settings live in `config`."""
    out_folder = Path(config.get("output_folder") or DEFAULT_OUTPUT_FOLDER)
    plan = info = folder_name = root = None
    local, clips, infos = [], {}, {}

    # ── 1  Input ─────────────────────────────────────────────────────────────
    section("1  Input")
    kv("Output", f"{escape(str(out_folder))}  [dim](Enter with nothing typed = change it)[/]")
    while True:
        typed = ask(qpath("Video, folder or YouTube link", default="")).strip()
        parts = split_paths(typed)
        raw = parts[0] if len(parts) == 1 else typed

        if not raw:
            new = ask(qtext("New output folder (Enter = keep current)", default="")).strip().strip("\"'")
            if new:
                p = Path(new).expanduser().resolve()
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    console.print(f"[red]Cannot use that folder:[/] {escape(str(e))}")
                else:
                    out_folder = p
                    config["output_folder"] = str(p)
                    save_config(config)
                    kv("Output", f"{escape(str(p))}  [green]saved[/]")
            continue

        link = parse_link(raw) if len(parts) == 1 else None
        if link:
            plan = youtube_flow(link)
            if plan is None:
                continue
            kind = "audio" if plan["audio_only"] else "video"
            break

        if len(parts) > 1:                          # several files (or folders) dragged in together
            dropped = [Path(x).expanduser() for x in parts]
            gone = [d.name or str(d) for d in dropped if not d.exists()]
            if gone:
                console.print(f"[red]Not found:[/red] {escape(', '.join(gone[:5]))}{'...' if len(gone) > 5 else ''}")
                continue
            files = []
            for d in dropped:
                files += collect_videos(d, out_folder) if d.is_dir() else [d]
            kept, skipped = [], []
            with console.status("[bold green]Reading files..."):
                for f in dict.fromkeys(files):      # drop duplicates, keep the order
                    try:
                        i_ = get_media_info(f)
                    except (subprocess.CalledProcessError, ValueError):
                        skipped.append(f.name)
                        continue
                    if detect_type(i_) == "video":
                        kept.append(f)
                        infos[f] = i_
                    else:
                        skipped.append(f.name)
            if skipped:
                console.print(f"[yellow]Skipped {len(skipped)} file(s) that are not videos: "
                              f"{escape(', '.join(skipped[:4]))}{'...' if len(skipped) > 4 else ''}[/yellow]")
            if not kept:
                console.print("[red]No video files in that selection[/red]")
                continue
            local, kind = kept, "video"
            parents = {f.parent for f in kept}
            if len(parents) == 1:                   # all from one folder: behave like dragging that folder in
                root = next(iter(parents))
                folder_name = re.sub(r'[<>:"/\\|?*]', "_", root.name) or "videos"
            kv("Source", f"{len(kept)} files dragged in")
            kv("Found", f"{len(kept)} video file(s), {fmt_bytes(sum(f.stat().st_size for f in kept))}")
            break

        path = Path(raw).expanduser()
        if path.is_dir():
            stream = find_avchd_stream(path)        # a camcorder card or a copy of it
            with console.status("[bold green]Scanning folder..."):
                if stream:
                    found = sorted((p for p in stream.iterdir() if p.suffix.lower() in MTS_EXTS), key=lambda p: p.name.lower())
                else:
                    found = collect_videos(path, out_folder)
            if not found:
                console.print("[red]No video files found in that folder[/red]")
                continue
            local, kind, root = found, "video", path
            folder_name = re.sub(r'[<>:"/\\|?*]', "_", path.name) or "videos"
            if stream and folder_name.upper() in {"VIDEOS", "STREAM", "BDMV", "AVCHD", "PRIVATE"}:
                folder_name = "handycam"
            kv("Source", escape(str(path)))
            kv("Found", f"{'[bold]AVCHD recording[/], ' if stream else ''}{len(found)} video file(s), "
                        f"{fmt_bytes(sum(f.stat().st_size for f in found))}")
            break
        if not path.is_file():
            console.print("[red]File or folder not found, try again[/red]")
            continue
        try:
            info = get_media_info(path)
        except (subprocess.CalledProcessError, ValueError):
            console.print("[red]ffprobe can't read that file, is it really media?[/red]")
            continue
        kind = detect_type(info)
        if kind == "unknown":
            console.print("[red]No audio/video/image stream found in that file[/red]")
            continue
        local = [path]
        kv("Source", escape(path.name))
        for label, text in media_lines(info, path, kind):
            kv(label, text)
        break

    # Handycam clips: read their recording dates now, so the pick list can show them
    is_mts = plan is None and bool(local) and all(f.suffix.lower() in MTS_EXTS for f in local)
    if is_mts:
        with console.status("[bold green]Reading recording dates..."):
            for f in local:
                try:
                    infos[f] = infos.get(f) or (info if (len(local) == 1 and info) else get_media_info(f))
                except (subprocess.CalledProcessError, ValueError):
                    pass            # reported later like any unreadable file
            clips = scan_clips([f for f in local if f in infos], infos)
    if plan is None and len(local) > 1:
        labels = [_label_clip(f, clips, root) if is_mts else f"{f.relative_to(root) if root else f.name}   "
                  f"{fmt_bytes(f.stat().st_size)}" for f in local]
        chosen = choose_items(labels, "clips" if is_mts else "videos")
        local = [local[i] for i in chosen]
        if len(local) < len(labels):
            kv("Selected", f"{len(local)} of {len(labels)}")

    # ── 2  Options ───────────────────────────────────────────────────────────
    section("2  Options")
    job, burn_map, bq = "compress", {}, None
    if is_mts and ask_burn({f: clips[f] for f in local if f in clips}):
        job = ask(qselect("How should it be saved?", choices=[
            questionary.Choice("Keep high quality (no size limit)", "burn_hq"),
            questionary.Choice("Compress to a target size", "compress"),
        ]))
        burn_map = plan_burn([f for f in local if f in clips], clips)
        skipped = [f for f in local if f in burn_map and burn_map[f] is None]
        if skipped:
            local = [f for f in local if f not in skipped]
            console.print(f"[yellow]Skipping {len(skipped)} clip(s) without a recording date[/yellow]")
        if not local:
            console.print("[yellow]Nothing left to process[/yellow]")
            return
    elif plan is None and kind == "video":
        job = ask(qselect("What do you want to do?", choices=[
            questionary.Choice("Compress to a target size", "compress"),
            questionary.Choice("Convert without re-encoding (lossless, e.g. camcorder MTS to MP4)", "convert"),
        ]))

    opts = copts = None
    if job == "convert":
        copts = ask_convert_options(config)
    elif job == "burn_hq":
        bq = ask_burn_quality(config)
    elif plan is None or plan["compress"]:
        has_audio = True if (plan or root or len(local) > 1) else audio_stream(info) is not None
        while True:
            opts = ask_options(kind, has_audio, config, can_back=bool(plan))
            if opts is not BACK:
                break
            # back out of the YouTube quality/compress choice instead of restarting the whole program
            plan = youtube_flow(link)
            if plan is None:
                return
            kind = "audio" if plan["audio_only"] else "video"
            has_audio = True
            if not plan["compress"]:
                opts = None
                break
        if opts and len(local) == 1 and local[0].stat().st_size <= opts["target_mib"] * 1_048_576:
            console.print(f"[yellow]Note: the input is already under {opts['target_mib']} MiB, "
                          f"re-encoding may not make it smaller[/yellow]")

    # dest is computed here, not earlier, so it reflects job/plan as they stand after any "go back" above
    dest = out_folder / output_category(job, plan, local)
    sub = plan["folder"] if plan and plan["folder"] else folder_name      # playlist or input folder name
    if sub:
        dest = dest / sub
    dest.mkdir(parents=True, exist_ok=True)

    # ── 3  Processing ────────────────────────────────────────────────────────
    items = plan["entries"] if plan else [{"url": None, "path": f, "title": f.name} for f in local]
    workers = ask_parallel(len(items), job, plan, config)
    enc_threads = ffmpeg_threads(workers) if workers > 1 else None

    section("3  Processing")
    if plan:
        what = "audio only" if plan["audio_only"] else (f"video up to {plan['height']}p" if plan["height"] else "video, best quality")
        kv("Job", f"YouTube download, {what}{', then compress' if plan['compress'] else ''}")
    elif job == "convert":
        kv("Job", f"Lossless convert to {copts['container'].upper()}, "
                  f"audio {'kept' if copts['audio'] == 'keep' else 'to AAC'}, verify {'on' if copts['verify'] else 'off'}")
    elif job == "burn_hq":
        kv("Job", f"Burn in recording date, H.264 MP4, CRF {bq['crf']}, {bq['preset']}, audio {'kept' if bq['audio'] == 'keep' else 'to AAC'}")
    else:
        extra = ", 2-pass" if opts["use_2pass"] else ", GPU" if opts["use_gpu"] else ""
        fps = "" if opts["fps"] == "original" else ", adaptive fps" if opts["fps"] == "auto" else f", {float(opts['fps']):g} fps"
        sat = "" if opts["saturation"] == 100 else f", saturation {opts['saturation']:g}%"
        kv("Job", f"{'Burn in date and ' if burn_map else ''}compress to {opts['target_mib']} MiB, "
                  f"{opts['fmt']['name']}, {opts['level']}{extra}{fps}{sat}")
    kv("Files", f"{len(items)}")
    kv("Output", escape(str(dest)))
    console.print()

    results, failures = [], []
    results_lock = threading.Lock()
    tmp_root = Path(tempfile.mkdtemp(dir=dest, prefix=".tmp_download_")) if plan and opts else None
    t0 = time.perf_counter()
    progress = Progress(
        SpinnerColumn("dots12"),
        TextColumn("{task.description}", table_column=Column(no_wrap=True, overflow="ellipsis", ratio=1)),
        BarColumn(bar_width=26), TaskProgressColumn(), TimeElapsedColumn(), TimeRemainingColumn(),
        console=console, transient=True, expand=True)

    def process_item(i: int, item: dict, lane: "Lane") -> None:
        lane.begin(i, item["title"])
        t_file = time.perf_counter()
        dst = src_size = None
        try:
            if plan:
                prefix = f"{i:02d} - " if len(items) > 1 else ""
                path = download_one(item["url"], tmp_root or dest, prefix, plan, lane, lane.name)
                if opts is None:
                    dst = path
                else:
                    src_size = path.stat().st_size
                    file_info = get_media_info(path)
            else:
                path = item["path"]
                src_size = path.stat().st_size
                file_info = infos.get(path) or (info if len(local) == 1 and info else get_media_info(path))
            if dst is None:
                if job == "convert":
                    dst = convert_one(path, dest, copts, file_info, lane)
                elif job == "burn_hq":
                    dst = burn_quality(path, dest, path.stem + "_datecode", file_info, burn_map[path],
                                       bq["crf"], bq["preset"], bq["audio"], lane, threads=enc_threads)
                else:
                    dst = compress_one(path, dest, opts, file_info, lane, burn_map.get(path), threads=enc_threads)
                if plan:
                    path.unlink(missing_ok=True)       # keep only the compressed copy
            with results_lock:
                results.append(dst)
            lane.done(i, item["title"], dst, time.perf_counter() - t_file, src_size)
        except subprocess.CalledProcessError:
            with results_lock:
                failures.append(item["title"])
            lane.fail(i, item["title"], "Can't read this file (corrupt or not a video)", time.perf_counter() - t_file)
        except (RuntimeError, OSError, ValueError) as e:
            with results_lock:
                failures.append(item["title"])
            lane.fail(i, item["title"], str(e), time.perf_counter() - t_file)

    try:
        with progress:
            ui = UI(progress, len(items), workers=workers)
            if workers <= 1:
                lane = ui.lane()
                for i, item in enumerate(items, 1):
                    process_item(i, item, lane)
            else:
                work = queue.Queue()
                for i, item in enumerate(items, 1):
                    work.put((i, item))
                cancel = threading.Event()      # set on Ctrl+C: stop pulling new items once the current one ends

                def run_lane(n: int) -> None:
                    lane = ui.lane(n)
                    while not cancel.is_set():
                        try:
                            i, item = work.get_nowait()
                        except queue.Empty:
                            return
                        process_item(i, item, lane)
                        work.task_done()

                lane_threads = [threading.Thread(target=run_lane, args=(w,), daemon=True) for w in range(workers)]
                for t in lane_threads:
                    t.start()
                try:
                    for t in lane_threads:
                        t.join()
                except KeyboardInterrupt:
                    cancel.set()                # no more queued files are picked up
                    kill_active()               # ...and whichever file each lane is mid-encode on is stopped too
                    for t in lane_threads:
                        t.join(timeout=5)
                    raise
    finally:
        if tmp_root:
            shutil.rmtree(tmp_root, ignore_errors=True)

    # ── Result ───────────────────────────────────────────────────────────────
    color = "green" if not failures else "yellow"
    kv("Finished", f"[{color}]{len(results)} of {len(items)} file(s)[/] in {fmt_secs(time.perf_counter() - t0)}"
                   + (f"   [red]{len(failures)} failed[/]" if failures else ""))
    if results:
        kv("Total size", fmt_bytes(sum(r.stat().st_size for r in results if r.exists())))
    kv("Output", escape(str(dest)))


def main() -> None:
    check_ffmpeg()
    print_banner()

    config = load_config()
    while True:                     # output folder, 2-pass and GPU choices carry over between jobs
        run_job(config)
        console.print()
        if not ask(qconfirm("Process something else?", default=True)):
            console.print("[dim]Goodbye![/dim]")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]Unexpected error:[/] {escape(str(e))}")