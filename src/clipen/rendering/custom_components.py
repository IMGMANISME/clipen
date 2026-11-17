"""Graphics an Agent designs freely as a web page (HTML, CSS, SVG, Canvas, JS).

The page fills the whole picture and is transparent wherever it draws nothing.
Frames are drawn by ``custom_render`` in a sealed headless Chrome, cached on
disk by content, and composited like any other programme graphic, so the
preview, the film and the Resolve export show the same picture.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .titles import TitleRenderError

MAX_HTML_BYTES = 200_000
FRAME_TIMEOUT_SECONDS = 10
CUSTOM = "custom"
_locks = {}
_guard = threading.Lock()


def is_custom(profile):
    return profile.get("component_id") == CUSTOM


def cache_root():
    configured = os.environ.get("CLIPEN_CUSTOM_COMPONENT_CACHE")
    if configured:
        return Path(configured)
    from clipen.bootstrap import clipen_data_root

    return clipen_data_root() / "cache" / "custom-components"


def _fps(timeline):
    return timeline.frame_rate.numerator / timeline.frame_rate.denominator


def _edits(profile):
    edits = profile.get("edits") or {}
    return {kind: dict(sorted((edits.get(kind) or {}).items())) for kind in ("texts", "colors") if edits.get(kind)}


def _bundled():
    """Fonts Clipen carries itself (pinned in fonts/manifest.json), by family name: nothing to install."""
    from importlib.resources import files

    manifest = json.loads(files("clipen.rendering").joinpath("fonts", "manifest.json").read_text(encoding="utf-8"))
    return {entry["family"]: entry for entry in manifest.values()}


BUNDLED = _bundled()
# Typefaces a look may use: what every Mac has, plus the fonts Clipen carries.
_SYSTEM = {
    "PingFang TC": "蘋方",
    "Heiti TC": "黑體",
    "Songti TC": "宋體",
    "Hiragino Sans": "冬青黑體",
    "Hiragino Mincho ProN": "冬青明朝",
    "Helvetica Neue": "Helvetica Neue",
    "Avenir Next": "Avenir Next",
    "Futura": "Futura",
    "Gill Sans": "Gill Sans",
    "Optima": "Optima",
    "Didot": "Didot",
    "Baskerville": "Baskerville",
    "Georgia": "Georgia",
    "Rockwell": "Rockwell",
    "Copperplate": "Copperplate",
    "Impact": "Impact",
    "Arial Black": "Arial Black",
    "Chalkduster": "Chalkduster",
    "Marker Felt": "Marker Felt",
    "Noteworthy": "Noteworthy",
    "Menlo": "Menlo",
}
FONT_CHOICES = {**{family: entry["label"] for family, entry in BUNDLED.items()}, **_SYSTEM}
# Where a typeface lacks a character (Japanese kana in a Chinese hand, Chinese in a Japanese one,
# anything in a Latin face) the next face of the same feel draws it, so a line keeps its style.
_HAND = ["Clipen Chenyu", "Yomogi", "Clipen Iansui"]
FALLBACKS = {
    "Clipen Chenyu": ["Yomogi", "Clipen Iansui"],
    "Clipen Iansui": [],
    "Yusei Magic": ["Clipen Chenyu", "Clipen Iansui"],
    "Yomogi": ["Clipen Chenyu", "Clipen Iansui"],
    "Zen Kurenaido": ["Clipen Iansui"],
    "Hachi Maru Pop": ["Yomogi", "Clipen Chenyu", "Clipen Iansui"],
    "Dela Gothic One": ["Heiti TC"],
    "RocknRoll One": ["Zen Maru Gothic", "PingFang TC"],
    "Zen Maru Gothic": ["PingFang TC"],
    "Bebas Neue": ["Heiti TC"],
    "Anton": ["Heiti TC"],
    "Caveat": _HAND,
    "Playfair Display": ["Songti TC"],
    "Chalkduster": _HAND,
    "Marker Felt": _HAND,
    "Noteworthy": _HAND,
    "Songti TC": ["Hiragino Mincho ProN"],
    "Hiragino Mincho ProN": ["Songti TC"],
    "Didot": ["Songti TC"],
    "Baskerville": ["Songti TC"],
    "Georgia": ["Songti TC"],
}


def font_stack(family):
    """The CSS font list for a typeface: itself, faces of the same feel for missing characters, then PingFang."""
    chain = [family, *FALLBACKS.get(family, []), "PingFang TC"]
    return ", ".join(f'"{name}"' for name in dict.fromkeys(chain)) + ", sans-serif"


def look_css(look):
    """The CSS a look gives a page: --look-font-<key> and --look-<key> variables on :root, with the faces
    Clipen carries declared for every typeface the look may draw with, fallbacks included."""
    if not look:
        return ""
    used = dict.fromkeys(name for f in look["fonts"] for name in [f["family"], *FALLBACKS.get(f["family"], [])])
    faces = "".join(
        f'@font-face {{ font-family: "{family}"; src: url(https://clipen.local/fonts/{BUNDLED[family]["file"]}); }}\n'
        for family in used
        if family in BUNDLED
    )
    names = [f"--look-font-{f['key']}: {font_stack(f['family'])};" for f in look["fonts"]]
    names += [f"--look-{c['key']}: {c['value']};" for c in look["colors"]]
    return faces + ":root { " + " ".join(names) + " }"


def _key(profile, timeline, duration):
    material = json.dumps(
        [
            profile["html"],
            timeline.width,
            timeline.height,
            timeline.frame_rate.numerator,
            timeline.frame_rate.denominator,
            duration,
            _edits(profile),
            look_css(profile.get("look")),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _lock(key):
    with _guard:
        return _locks.setdefault(key, threading.Lock())


class CustomRenderError(TitleRenderError):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.detail = detail


def frames(timeline, item, profile, wanted, cancel=None):
    """Paths of the requested relative frames, drawing the missing ones in one Chrome run."""
    duration = item.duration_frames
    wanted = sorted({min(max(0, int(n)), duration - 1) for n in wanted})
    key = _key(profile, timeline, duration)
    folder = cache_root() / key
    paths = {n: folder / f"f{n:05d}.png" for n in wanted}
    with _lock(key):
        missing = [n for n, path in paths.items() if not path.is_file()]
        if missing:
            folder.mkdir(parents=True, exist_ok=True)
            job = {
                "html": profile["html"],
                "width": timeline.width,
                "height": timeline.height,
                "fps": _fps(timeline),
                "duration": duration,
                "frames": missing,
                "out": str(folder),
                "frame_timeout": FRAME_TIMEOUT_SECONDS,
                "edits": _edits(profile),
                "look": look_css(profile.get("look")),
            }
            _run(job, timeout=30 + FRAME_TIMEOUT_SECONDS + len(missing) * 1.5, cancel=cancel)
    return paths


def fields(timeline, item, profile):
    """The words and colours the page marked as editable, with the values the design gives them."""
    plain = {key: value for key, value in profile.items() if key != "edits"}
    key = _key(plain, timeline, item.duration_frames)
    folder = cache_root() / key
    path = folder / "fields.json"
    with _lock(key):
        if not path.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            _run(
                {
                    "html": profile["html"],
                    "width": timeline.width,
                    "height": timeline.height,
                    "fps": _fps(timeline),
                    "duration": item.duration_frames,
                    "frames": [],
                    "out": str(folder),
                    "frame_timeout": FRAME_TIMEOUT_SECONDS,
                    "describe": True,
                    "look": look_css(profile.get("look")),
                },
                timeout=30 + FRAME_TIMEOUT_SECONDS,
            )
    found = json.loads(path.read_text(encoding="utf-8"))
    # Bounded so a page cannot flood the review panel.
    return {
        "texts": [f for f in found.get("texts", []) if isinstance(f.get("key"), str)][:12],
        "colors": [f for f in found.get("colors", []) if isinstance(f.get("key"), str)][:8],
        "fonts": [f for f in found.get("fonts", []) if isinstance(f, str)][:20],
    }


def check_fonts(timeline, item, profile):
    """A graphic that uses a look draws its text only in that look's typefaces."""
    look = profile.get("look")
    if not look:
        return
    allowed = {f["family"] for f in look["fonts"]}
    stray = [family for family in fields(timeline, item, profile)["fonts"] if family not in allowed]
    if stray:
        raise CustomRenderError("custom_component_font_outside_look", "、".join(stray))


def _run(job, timeout, cancel=None):
    """Draws in a child Chrome run; a cancelled render stops it instead of waiting for every frame."""
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")])
        ),
    }
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "clipen.rendering.custom_render"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            env=environment,
            cwd=tempfile.gettempdir(),
        )
        try:
            process.stdin.write(json.dumps(job).encode())
            process.stdin.close()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise CustomRenderError("cancelled")
                if time.monotonic() >= deadline:
                    raise CustomRenderError("custom_component_frame_timeout")
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        errors.seek(0)
        stderr = errors.read().decode("utf-8", "replace")
    if process.returncode != 0:
        try:
            problem = json.loads(stderr.strip().splitlines()[-1])
        except (ValueError, IndexError):
            problem = {"code": "custom_component_render_failed", "detail": stderr[-400:]}
        raise CustomRenderError(problem.get("code", "custom_component_render_failed"), problem.get("detail", ""))


def _image(path):
    from PIL import Image

    with Image.open(path) as image:
        return image.convert("RGBA")


def _offset(timeline, profile):
    return round(profile.get("x", 0) * timeline.width), round(profile.get("y", 0) * timeline.height)


def _resting_frame(item):
    return item.duration_frames // 2


def bounds(timeline, item, profile, relative_frame=None):
    """Where the graphic draws something, in pixels, after the user's move."""
    frame = _resting_frame(item) if relative_frame is None else relative_frame
    box = (
        _image(frames(timeline, item, profile, [frame])[min(max(0, frame), item.duration_frames - 1)])
        .getchannel("A")
        .getbbox()
    )
    if box is None:
        return None
    dx, dy = _offset(timeline, profile)
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def draw(draw_, timeline, item, profile, relative_frame=None):
    frame = _resting_frame(item) if relative_frame is None else min(max(0, relative_frame), item.duration_frames - 1)
    picture = _image(frames(timeline, item, profile, [frame])[frame])
    dx, dy = _offset(timeline, profile)
    target = draw_._image
    # Crop whatever a move pushed off the picture; alpha_composite takes no negative offsets.
    left, top = max(0, -dx), max(0, -dy)
    right, bottom = min(picture.width, timeline.width - dx), min(picture.height, timeline.height - dy)
    if right > left and bottom > top:
        piece = picture.crop((left, top, right, bottom))
        if target.mode == "RGBA":
            target.alpha_composite(piece, dest=(dx + left, dy + top))
        else:
            target.paste(piece, (dx + left, dy + top), piece)
    box = picture.getchannel("A").getbbox()
    return None if box is None else (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def changes(timeline, item, profile, cancel=None):
    """Absolute frames where the picture differs from the frame before, so still stretches cost one image."""
    paths = frames(timeline, item, profile, range(item.duration_frames), cancel)
    marks, previous = {item.start_frame, item.start_frame + item.duration_frames}, None
    for n in range(item.duration_frames):
        digest = hashlib.sha256(_image(paths[n]).tobytes()).digest()
        if digest != previous:
            marks.add(item.start_frame + n)
        previous = digest
    return marks


def check(timeline, item, profile):
    """Technical checks only: the page loads, runs and draws something at the start, middle and end."""
    if not isinstance(profile.get("html"), str) or not profile["html"].strip():
        raise CustomRenderError("custom_component_html_missing")
    if len(profile["html"].encode()) > MAX_HTML_BYTES:
        raise CustomRenderError("custom_component_html_too_large")
    sample = sorted({0, _resting_frame(item), item.duration_frames - 1})
    paths = frames(timeline, item, profile, sample)
    if all(_image(paths[n]).getchannel("A").getbbox() is None for n in sample):
        raise CustomRenderError("custom_component_empty")
