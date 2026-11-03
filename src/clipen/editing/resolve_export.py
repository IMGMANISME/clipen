"""Hand the current film to DaVinci Resolve for hands-on editing.

The package holds an FCPXML timeline that points at the original footage, the
on-screen components as transparent PNGs, the captions as SRT, and the music.
Shots whose look cannot be rebuilt from the original footage (mosaic, speed
ramps) are rendered to ProRes first so nothing hidden by a mosaic is revealed.
Everything Resolve cannot take from FCPXML is left as a marker and in 說明.txt.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from clipen.domain import MediaClip, Track, TrackKind
from clipen.projects import ProjectError

# Effects that change which pixels or frames appear, so the shot is rendered as is.
BURNED = frozenset({'video.mosaic', 'video.speed_ramp'})
_FRAMING = frozenset({'video.punch_in', 'video.reframe', 'video.slow_push'})
_UNSAFE_NAME = re.compile(r'[\x00-\x1f/:\\]+')


def seconds(value: Fraction) -> str:
    """An exact FCPXML time value."""
    if value == 0:
        return '0s'
    return f'{value.numerator}s' if value.denominator == 1 else f'{value.numerator}/{value.denominator}s'


def timecode_seconds(timecode: str, rate: Fraction) -> Fraction:
    """Seconds from a file's start timecode, so Resolve and the FCPXML agree on source time."""
    match = re.fullmatch(r'(\d{1,2}):(\d{2}):(\d{2})([:;.])(\d{2,3})', timecode.strip())
    if not match:
        return Fraction(0)
    hours, minutes, secs, separator, frames = match.groups()
    nominal = max(1, round(rate))
    count = ((int(hours) * 60 + int(minutes)) * 60 + int(secs)) * nominal + int(frames)
    if separator in ';.' and rate.denominator == 1001:
        dropped = nominal // 15
        total_minutes = int(hours) * 60 + int(minutes)
        count -= dropped * (total_minutes - total_minutes // 10)
    return count / rate


def probe(path: Path) -> dict:
    """Timecode, rotation and display size, which the project inventory does not keep."""
    completed = subprocess.run(
        ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_entries',
         'format_tags=timecode:stream=index,codec_type,width,height:stream_tags=timecode,rotate:stream_side_data=rotation',
         str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise ProjectError('resolve_source_unreadable', f'無法讀取素材 {path.name}，請確認檔案還在。', status_code=409)
    data = json.loads(completed.stdout or '{}')
    streams = data.get('streams', [])
    timecode = data.get('format', {}).get('tags', {}).get('timecode') or next(
        (s.get('tags', {}).get('timecode') for s in streams if s.get('tags', {}).get('timecode')), None)
    return {'timecode': timecode, 'streams': {s.get('index'): s for s in streams}}


def _rotation(stream: dict) -> int:
    for side in stream.get('side_data_list', []):
        if 'rotation' in side:
            return int(float(side['rotation']))
    return int(float(stream.get('tags', {}).get('rotate', 0) or 0))


def _safe_name(value: str) -> str:
    return _UNSAFE_NAME.sub(' ', value).strip(' .') or 'Clipen'


def _unique(directory: Path, name: str) -> Path:
    candidate, number = directory / name, 2
    while candidate.exists():
        stem, suffix = os.path.splitext(name)
        candidate, number = directory / f'{stem} ({number}){suffix}', number + 1
    return candidate


def _srt_time(value: Fraction) -> str:
    milliseconds = round(value * 1000)
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, milliseconds = divmod(rest, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}'


def _lanes(intervals, first: int, step: int) -> list[int]:
    """Give overlapping items their own lane, reusing lanes once an item has ended."""
    ends: list[int] = []
    lanes = []
    for start, end in intervals:
        for index, last in enumerate(ends):
            if last <= start:
                ends[index] = end
                lanes.append(first + index * step)
                break
        else:
            ends.append(end)
            lanes.append(first + (len(ends) - 1) * step)
    return lanes


def _framing(item: MediaClip):
    """Clipen's zoom and crop window as Resolve zoom and position (FCPXML position is % of frame height)."""
    scale, center_x, center_y, push = 1.0, .5, .5, None
    for effect in item.effects:
        p = effect.parameters
        if effect.effect_id == 'video.punch_in':
            scale = min(2.0, max(1.01, float(p.get('scale', 1.08))))
            center_x = min(1.0, max(0.0, float(p.get('center_x', .5))))
            center_y = min(1.0, max(0.0, float(p.get('center_y', .5))))
        elif effect.effect_id == 'video.reframe':
            scale = 1.08
            center_x = min(1.0, max(0.0, float(p.get('x', .5))))
            center_y = min(1.0, max(0.0, float(p.get('y', .5))))
        elif effect.effect_id == 'video.slow_push':
            push = (min(2.0, max(1.0, float(p.get('scale_from', 1.0)))), min(2.0, max(1.0, float(p.get('scale_to', 1.08)))))
    return scale, center_x, center_y, push


class _Builder:
    def __init__(self, timeline, name):
        self.timeline = timeline
        self.fps = Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
        self.root = ET.Element('fcpxml', version='1.10')
        self.resources = ET.SubElement(self.root, 'resources')
        self.formats: dict[tuple, str] = {}
        self.assets: dict[str, str] = {}
        self.count = 0
        self.sequence_format = self.format(timeline.width, timeline.height, 1 / self.fps, 'Clipen')
        library = ET.SubElement(self.root, 'library')
        event = ET.SubElement(library, 'event', name='Clipen')
        project = ET.SubElement(event, 'project', name=name)
        self.sequence = ET.SubElement(project, 'sequence', format=self.sequence_format,
                                      duration=seconds(self.time(timeline.duration_frames)),
                                      tcStart='0s', tcFormat='NDF', audioLayout='stereo', audioRate='48k')
        self.spine = ET.SubElement(self.sequence, 'spine')
        self.parents: list[tuple[int, int, ET.Element, Fraction]] = []

    def time(self, frames) -> Fraction:
        return Fraction(frames) / self.fps

    def ident(self) -> str:
        self.count += 1
        return f'r{self.count}'

    def format(self, width, height, frame_duration: Fraction | None, name=None) -> str:
        key = (width, height, frame_duration)
        if key not in self.formats:
            ident = self.ident()
            attributes = {'id': ident, 'width': str(width), 'height': str(height)}
            if frame_duration is None:
                attributes['name'] = 'FFVideoFormatRateUndefined'
            else:
                attributes['frameDuration'] = seconds(frame_duration)
                if name:
                    attributes['name'] = name
            ET.SubElement(self.resources, 'format', attributes)
            self.formats[key] = ident
        return self.formats[key]

    def asset(self, path: Path, name: str, start: Fraction, duration: Fraction, *, video=None, audio=None) -> str:
        key = str(path)
        if key in self.assets:
            return self.assets[key]
        ident = self.ident()
        attributes = {'id': ident, 'name': name, 'start': seconds(start), 'duration': seconds(duration)}
        if video:
            attributes.update(hasVideo='1', videoSources='1', format=video)
        if audio:
            channels, rate = audio
            attributes.update(hasAudio='1', audioSources='1', audioChannels=str(channels), audioRate=str(rate))
        element = ET.SubElement(self.resources, 'asset', attributes)
        ET.SubElement(element, 'media-rep', kind='original-media', src=path.resolve().as_uri())
        self.assets[key] = ident
        return ident

    def place(self, element, offset_frames, duration_frames, local_start: Fraction):
        self.spine.append(element)
        self.parents.append((offset_frames, duration_frames, element, local_start))

    def gap(self, offset_frames, duration_frames):
        element = ET.Element('gap', name='Gap', offset=seconds(self.time(offset_frames)),
                             start='0s', duration=seconds(self.time(duration_frames)))
        self.place(element, offset_frames, duration_frames, Fraction(0))

    def connect(self, tag, attributes, frame):
        """Attach an item at a timeline frame to the storyline clip under it."""
        parent = next((p for p in self.parents if p[0] <= frame < p[0] + p[1]), self.parents[-1])
        offset_frames, _, element, local_start = parent
        attributes = {**attributes, 'offset': seconds(local_start + self.time(frame - offset_frames))}
        return ET.SubElement(element, tag, attributes)

    def marker(self, element, local_start: Fraction, frames, value, note=''):
        ET.SubElement(element, 'marker', start=seconds(local_start + self.time(frames)),
                      duration=seconds(1 / self.fps), value=value, **({'note': note} if note else {}))

    def write(self, path: Path):
        ET.indent(self.root)
        body = ET.tostring(self.root, encoding='unicode')
        path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + '\n', encoding='utf-8')


def _is_caption(track, item, styles) -> bool:
    return track.track_id == 'agent-subtitles' or styles.get(item.style_id, {}).get('kind') == 'program_subtitle'


def export_folder(repository, project_id: str) -> Path:
    """「Clipen 匯出」inside the footage folder: the App Store Resolve reads only folders the user opens,
    so one 'Locate' on the footage folder lets it find the footage and the package together."""
    from clipen.media.discovery import EXPORT_FOLDER
    from clipen.projects import DirectoryRole
    source = next((d for d in repository.projects.get_project(project_id).directories if d.role is DirectoryRole.SOURCE), None)
    if source is None or not Path(source.canonical_path).is_dir():
        raise ProjectError('resolve_source_offline', '找不到毛片資料夾，請確認外接硬碟已連接。', status_code=409)
    folder = Path(source.canonical_path) / EXPORT_FOLDER
    folder.mkdir(exist_ok=True)
    return folder


def export_resolve(repository, project_id: str, run_id: str, destination: Path | None = None, cancel: threading.Event | None = None) -> dict:
    """Write a Resolve package into a new folder (beside the footage unless told otherwise) and describe it."""
    cancel = cancel or threading.Event()
    destination = destination or export_folder(repository, project_id)
    revision = repository.current_revision(project_id, run_id)
    timeline = revision.timeline
    video = next((t for t in timeline.tracks if t.kind is TrackKind.VIDEO and t.items), None)
    if video is None:
        raise ProjectError('resolve_video_required', '這一版還沒有畫面，無法匯出。', status_code=409)
    project_name = repository.projects.get_project(project_id).name
    name = _safe_name(f'{project_name} {datetime.now():%Y-%m-%d %H%M}')
    package = _unique(destination, f'{name} DaVinci Resolve')
    package.mkdir()
    try:
        return _write(repository, project_id, package, name, timeline, video, cancel)
    except Exception:
        shutil.rmtree(package, ignore_errors=True)
        raise


def _write(repository, project_id, package, name, timeline, video, cancel):
    from clipen.finishing.audio import open_asset
    from clipen.media.preview import MediaPreviewRepository
    from clipen.rendering.renderer import FfmpegPreviewRenderer, PreviewRenderError
    from clipen.rendering.titles import TitleCardRenderer, TitleRenderError
    from .production_media import AgentAudioStore, load_text_styles
    media = MediaPreviewRepository(repository.projects)
    styles = load_text_styles(repository.projects, project_id)
    build = _Builder(timeline, name)
    notes: list[str] = []
    probes: dict[Path, dict] = {}
    burned = 0

    # 1. Shots on the main storyline.
    cursor = 0
    for index, item in enumerate(sorted(video.items, key=lambda i: i.start_frame), 1):
        if cancel.is_set():
            raise ProjectError('cancelled', '已取消匯出。', status_code=409)
        if item.start_frame > cursor:
            build.gap(cursor, item.start_frame - cursor)
        duration = build.time(item.duration_frames)
        clock = f'{int(build.time(item.start_frame) // 60)}:{int(build.time(item.start_frame) % 60):02d}'
        if any(e.effect_id in BURNED for e in item.effects):
            directory = package / '已處理片段'
            directory.mkdir(exist_ok=True)
            output = directory / f'{index:03d} {clock.replace(":", "m")}s.mov'
            kept = tuple(e for e in item.effects if e.effect_id in BURNED | _FRAMING)
            single = replace(timeline, duration_frames=item.duration_frames, markers=(),
                             tracks=(Track(video.track_id, TrackKind.VIDEO, (replace(item, start_frame=0, effects=kept),)),))
            try:
                FfmpegPreviewRenderer(media, master=True)._render_media(project_id, single, output, cancel, lambda _: None, preserve_audio_gain=True)
            except PreviewRenderError as error:
                raise ProjectError('resolve_render_failed', f'{clock} 的片段無法輸出（{error.code}）。', status_code=500) from error
            info = probe(output)
            has_audio = any(s.get('codec_type') == 'audio' for s in info['streams'].values())
            ref = build.asset(output, output.stem, Fraction(0), duration,
                              video=build.sequence_format, audio=(2, 48000) if has_audio else None)
            start = Fraction(0)
            burned += 1
            what = '、'.join(label for effect, label in (('video.mosaic', '馬賽克'), ('video.speed_ramp', '變速'))
                            if any(e.effect_id == effect for e in item.effects))
            notes.append(f'{clock} 的鏡頭已先輸出成 ProRes（含{what}與構圖），放在「已處理片段」；原始素材不會出現在時間軸上。')
            framing = None
            label = f'{clock} 已處理'
        else:
            path, streams = media.locate(project_id, item.source.span.version_id)
            span = item.source.span
            stream = streams.get(span.stream_index, {})
            if path not in probes:
                probes[path] = probe(path)
            info = probes[path]
            shown = info['streams'].get(span.stream_index, {})
            try:
                rate = Fraction(stream.get('r_frame_rate') or stream.get('avg_frame_rate') or '30')
            except (ValueError, ZeroDivisionError):
                rate = Fraction(0)
            rate = rate or build.fps
            width, height = shown.get('width') or stream.get('width'), shown.get('height') or stream.get('height')
            if _rotation(shown) % 180:
                width, height = height, width
            time_base = Fraction(span.time_base.numerator, span.time_base.denominator)
            stream_start = int(stream.get('start_pts', 0) or 0)
            origin = timecode_seconds(info['timecode'], rate) if info['timecode'] else Fraction(0)
            length = int(stream['duration_ts']) * Fraction(stream['time_base']) if stream.get('duration_ts') and stream.get('time_base') else duration
            audio = next((s for s in streams.values() if s['kind'] == 'audio'), None)
            ref = build.asset(path, path.stem, origin, length,
                              video=build.format(width, height, 1 / rate),
                              audio=(audio.get('channels', 2), audio.get('sample_rate', 48000)) if audio else None)
            start = origin + (int(span.start_pts) - stream_start) * time_base
            framing = _framing(item)
            label = path.stem
        clip = ET.Element('asset-clip', ref=ref, offset=seconds(build.time(item.start_frame)), name=label,
                          start=seconds(start), duration=seconds(duration), tcFormat='NDF', audioRole='dialogue')
        ET.SubElement(clip, 'adjust-conform', type='fit')
        if framing:
            scale, center_x, center_y, push = framing
            shift_x = (scale - 1) * timeline.width * (.5 - center_x) / timeline.height * 100
            shift_y = -(scale - 1) * (.5 - center_y) * 100 + 0.0  # no '-0.0000'
            if scale != 1 or push:
                transform = ET.SubElement(clip, 'adjust-transform', position=f'{shift_x:.4f} {shift_y:.4f}', scale=f'{scale:.4f} {scale:.4f}')
                if push:
                    animation = ET.SubElement(ET.SubElement(transform, 'param', name='scale'), 'keyframeAnimation')
                    for at, value in ((start, push[0] * scale), (start + duration - 1 / build.fps, push[1] * scale)):
                        ET.SubElement(animation, 'keyframe', time=seconds(at), value=f'{value:.4f} {value:.4f}')
        policy = next((e.parameters for e in item.effects if e.effect_id == 'audio.source'), None)
        if policy:
            gain = -96 if policy['mode'] == 'mute' else float(policy['gain_db'])
            ET.SubElement(clip, 'adjust-volume', amount=f'{gain:g}dB')
        build.place(clip, item.start_frame, item.duration_frames, start)

        # Looks Resolve cannot take from FCPXML stay as markers on the shot.
        if framing is None:
            build.marker(clip, start, 0, 'Clipen 已處理片段', '這段已套用馬賽克或變速並輸出成 ProRes；原始素材不在時間線上。')
        if policy and policy['mode'] == 'dialogue':
            build.marker(clip, start, 0, 'Clipen 對白處理', '高通 85 Hz、低通 11 kHz、輕微壓縮；請在 Fairlight 重做。')
        grade = next((e.parameters for e in item.effects if e.effect_id == 'video.grade'), None)
        if grade:
            build.marker(clip, start, 0, 'Clipen 調色',
                         f'對比 {float(grade["contrast"]):.2f}、飽和度 {float(grade["saturation"]):.2f}、亮度 {float(grade["brightness"]):+.2f}；請在調色頁重調。')
            notes.append(f'{clock} 的鏡頭有調色，數值記在標記裡。')
        for effect in item.effects:
            p = effect.parameters
            if effect.effect_id == 'video.transition' and p.get('kind') != 'none':
                color = '白' if p['kind'] == 'dip_white' else '黑'
                build.marker(clip, start, 0, f'Clipen 轉場：淡入淡出{color}場', f'共 {p["duration_frames"]} 格，前後兩個鏡頭各一半。')
                notes.append(f'{clock} 有淡入淡出{color}場轉場，請在 Resolve 加上 Dip to Color Dissolve。')
            elif effect.effect_id == 'video.transition_out':
                color = '白' if p.get('kind') == 'dip_white' else '黑'
                build.marker(clip, start, max(0, item.duration_frames - p['duration_frames'] // 2), f'Clipen 轉場：淡出到{color}場', f'共 {p["duration_frames"] // 2} 格。')
            elif effect.effect_id == 'video.fade':
                if p.get('fade_in_frames'):
                    build.marker(clip, start, 0, 'Clipen 淡入', f'從黑場淡入 {p["fade_in_frames"]} 格。')
                if p.get('fade_out_frames'):
                    build.marker(clip, start, item.duration_frames - p['fade_out_frames'], 'Clipen 淡出', f'淡出到黑場 {p["fade_out_frames"]} 格。')
                if p.get('fade_in_frames') or p.get('fade_out_frames'):
                    notes.append(f'{clock} 的鏡頭有淡入或淡出，請在 Resolve 補上。')
        cursor = item.start_frame + item.duration_frames
    if cursor < timeline.duration_frames:
        build.gap(cursor, timeline.duration_frames - cursor)

    # 2. Components and titles as transparent stills; captions as an editable SRT.
    captions, overlays = [], []
    for track in timeline.tracks:
        if track.kind is not TrackKind.TITLE:
            continue
        for item in track.items:
            (captions if _is_caption(track, item, styles) else overlays).append((track, item))
    rendered, editable = [], 0
    if overlays:
        directory = package / '元件'
        directory.mkdir()
        with tempfile.TemporaryDirectory(dir=package) as temporary:
            images = []
            try:
                # The renderer caps one pass for FFmpeg's sake; stills for Resolve go batch by batch.
                size = TitleCardRenderer._MAX_TITLES
                for first in range(0, len(overlays), size):
                    batch = overlays[first:first + size]
                    only = replace(timeline, tracks=tuple(Track(f'overlay-{n}', TrackKind.TITLE, (item,)) for n, (_, item) in enumerate(batch)))
                    folder = Path(temporary) / str(first)
                    folder.mkdir()
                    images += TitleCardRenderer(styles).render(only, folder)
            except TitleRenderError as error:
                raise ProjectError('resolve_overlay_failed', f'元件圖片無法產生（{error.code}）。', status_code=500) from error
            for number, ((_, item), image) in enumerate(zip(overlays, images), 1):
                label = _safe_name(item.text.split('\n', 1)[0])[:40]
                target = directory / f'{number:03d} {label}.png'
                shutil.move(str(image.path), target)
                rendered.append((item, target, label))
        editable = _editable_titles(directory, rendered, styles, timeline)
        still = build.format(timeline.width, timeline.height, None)
        for (item, target, label), lane in zip(rendered, _lanes([(i.start_frame, i.start_frame + i.duration_frames) for i, _, _ in rendered], 1, 1)):
            ref = build.asset(target, target.stem, Fraction(0), Fraction(0), video=still)
            build.connect('video', {'ref': ref, 'lane': str(lane), 'name': label, 'start': '0s',
                                    'duration': seconds(build.time(item.duration_frames))}, item.start_frame)
        notes.append(f'畫面元件 {len(rendered)} 個先以透明 PNG 放在影片上層，位置和外觀與 Clipen 相同，彈出動畫不會保留。'
                     + (f'其中 {editable} 個可以換成能改字的 DaVinci 標題，做法見上面「想在 Resolve 改元件的字」。' if editable else ''))
    if captions:
        lines = []
        for number, (_, item) in enumerate(sorted(captions, key=lambda pair: pair[1].start_frame), 1):
            lines += [str(number), f'{_srt_time(build.time(item.start_frame))} --> {_srt_time(build.time(item.start_frame + item.duration_frames))}', item.text.strip(), '']
        (package / '字幕.srt').write_text('\n'.join(lines), encoding='utf-8')

    # 3. Music: the stem Clipen mixed (ducked under speech), plus each source muted for rework.
    layers = [(i, l) for t in timeline.tracks for i in t.items if isinstance(i, MediaClip)
              for e in i.effects if e.effect_id == 'audio.layers' for l in e.parameters['layers']]
    if layers:
        from .program_audio import render_music_stem
        directory = package / '音樂'
        directory.mkdir()
        stem = directory / '配樂（Clipen 混音）.wav'
        settings, _ = render_music_stem(media, project_id, timeline, stem, cancel)
        total = build.time(timeline.duration_frames)
        ref = build.asset(stem, stem.stem, Fraction(0), total, audio=(2, 48000))
        build.connect('asset-clip', {'ref': ref, 'lane': '-1', 'name': stem.stem, 'start': '0s',
                                     'duration': seconds(total), 'audioRole': 'music'}, 0)
        store = AgentAudioStore(repository.projects, project_id)
        copies: dict[str, tuple[Path, dict]] = {}
        spans = [(clip.start_frame + layer['start_frame'], clip.start_frame + layer['start_frame'] + layer['duration_frames']) for clip, layer in layers]
        for (clip, layer), setting, lane in zip(layers, settings, _lanes(spans, -2, -1)):
            if layer['asset_id'] not in copies:
                fd, asset = open_asset(store.context(layer['asset_id']), layer['asset_id'])
                with os.fdopen(fd, 'rb') as source:
                    target = _unique(directory, _safe_name(Path(asset['relative_path']).name))
                    with target.open('wb') as copy:
                        shutil.copyfileobj(source, copy)
                copies[layer['asset_id']] = (target, asset)
            target, asset = copies[layer['asset_id']]
            length = Fraction(round(asset['duration_seconds'] * 48000), 48000)
            ref = build.asset(target, target.stem, Fraction(0), length, audio=(2, 48000))
            element = build.connect('asset-clip', {'ref': ref, 'lane': str(lane), 'name': f'{target.stem}（原始）', 'enabled': '0',
                                                   'start': seconds(Fraction(round(layer['source_offset_seconds'] * 48000), 48000)),
                                                   'duration': seconds(build.time(layer['duration_frames'])), 'audioRole': 'music'},
                                    clip.start_frame + layer['start_frame'])
            ET.SubElement(element, 'adjust-volume', amount=f'{setting["normalization_db"] + layer["gain_db"]:.2f}dB')
        notes.append('配樂分兩層：「配樂（Clipen 混音）」是 Clipen 混好的成品（講話時自動壓低、含淡入淡出）；下方停用的「原始」音軌是各段原始音樂，要重新混音時啟用它們並關掉混音層。')

    fcpxml = package / f'{name}.fcpxml'
    build.write(fcpxml)
    _guide(package, fcpxml, bool(captions), editable, notes)
    return {'folder': str(package), 'fcpxml': str(fcpxml), 'shots': len(video.items), 'burned': burned,
            'overlays': len(rendered), 'editable_overlays': editable, 'captions': len(captions),
            'music_layers': len(layers), 'notes': notes}


def _editable_titles(directory: Path, rendered, styles, timeline) -> int:
    """Record each component's type beside its PNG so a .drt from Resolve can carry it as editable titles."""
    from clipen.rendering import program_creator
    from clipen.rendering.titles import TitleCardRenderer, TitleRenderError
    from .resolve_titles import MANIFEST
    font_path = TitleCardRenderer._font_path()
    manifest = {}
    for item, target, _ in rendered:
        profile = styles.get(item.style_id)
        if not profile or profile.get('kind') != 'program_component' or not program_creator.is_creator(profile):
            continue
        try:
            plan = program_creator.editable(font_path, timeline, item, profile)
        except TitleRenderError:
            plan = None
        if plan is None:
            continue
        picture = None
        if plan['picture']:
            picture = f'{target.stem}（底圖）.png'
            plan['picture'].save(directory / picture, 'PNG', optimize=True)
        manifest[target.name] = {'rows': plan['rows'], 'picture': picture, 'width': timeline.width, 'height': timeline.height}
    if manifest:
        (directory / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    return len(manifest)


RESOLVE_DEFAULT_FOLDER = Path.home() / 'Movies' / 'DaVinci Resolve'


def _exported_timeline(package: Path, since: float = 0.) -> Path | None:
    """The newest .drt saved from this package's timeline after since (a time stamp).

    Resolve saves exports to its own folder unless told otherwise, so both that folder and the
    package are searched; a .drt counts when its clips point at this package's components."""
    from .resolve_titles import uses
    candidates = [p for folder in (package, RESOLVE_DEFAULT_FOLDER) for p in folder.glob('*.drt') if '（可改字）' not in p.stem]
    candidates = [p for p in candidates if p.stat().st_mtime > since]
    return next((p for p in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True) if uses(p, package / '元件')), None)


def convert_timeline(package: Path, source: Path | None = None) -> tuple[Path, int]:
    """Write a copy of the .drt exported from this package's timeline with editable component titles."""
    from .resolve_titles import MANIFEST, convert
    package = Path(package)
    if not (package / '元件' / MANIFEST).is_file():
        raise ProjectError('resolve_titles_missing', '這份匯出沒有可以換成文字的元件。', status_code=409)
    source = source or _exported_timeline(package)
    if source is None:
        raise ProjectError('resolve_timeline_missing', '還找不到這份匯出的 .drt。請先在 DaVinci Resolve 用「檔案 > 匯出 > 時間線…」把時間線存成 .drt。', status_code=409)
    target = _unique(package, f'{source.stem}（可改字）.drt')
    temporary = target.with_suffix('.partial')
    try:
        replaced = convert(source, temporary)
        if not replaced:
            raise ProjectError('resolve_titles_unmatched', f'「{source.name}」裡找不到這份匯出的元件。請確認匯出的是從這個資料夾匯入的時間線。', status_code=409)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, replaced


def watch_timeline(package: Path, since: float, stop: threading.Event | None = None,
                   timeout: float = 4 * 3600, interval: float = 2.) -> Path | None:
    """Wait for the person to save this package's timeline as .drt in Resolve, then convert and show it.

    A save counts once its size holds still for one interval, so a half-written file is never read.
    Gives up after timeout or when stop is set; returns the converted timeline."""
    stop = stop or threading.Event()
    deadline, seen = time.monotonic() + timeout, None
    while not stop.wait(interval) and time.monotonic() < deadline:
        source = _exported_timeline(package, since)
        if source is None:
            continue
        size = source.stat().st_size
        if seen != (source, size):
            seen = (source, size)
            continue
        try:
            target, _ = convert_timeline(package, source)
        except ProjectError:
            return None
        reveal(target)
        return target
    return None


def start_watching(package: Path) -> threading.Thread:
    """Watch for the .drt in the background so the person never has to come back to Clipen for it."""
    since = time.time()
    thread = threading.Thread(target=watch_timeline, args=(Path(package), since), name='resolve-titles', daemon=True)
    thread.start()
    return thread


def reveal(path: Path) -> None:
    """Show the new timeline file in Finder; a missing Finder only skips this courtesy."""
    if os.uname().sysname == 'Darwin':
        subprocess.run(['/usr/bin/open', '-R', str(path)], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)


def _guide(package: Path, fcpxml: Path, captions: bool, editable: int, notes: list[str]):
    steps = [
        '在 DaVinci Resolve 匯入',
        '',
        f'1. 選「檔案 > 匯入 > 時間線…」（File > Import > Timeline），選擇「{fcpxml.name}」。',
        '2. 匯入視窗保持預設，確認「自動將素材匯入媒體池」有勾選。',
    ]
    if captions:
        steps += ['3. 選「檔案 > 匯入 > 字幕…」（File > Import > Subtitle），選擇「字幕.srt」，再把它拖到時間線最上方的字幕軌。',
                  '   字幕會變成 Resolve 的字幕軌，可以直接改字和時間；外觀請在字幕軌的檢查器設定。']
    steps += ['', '如果 Resolve 顯示「clips were not yet found」，按「Locate」（尋找），選擇毛片資料夾（也就是「Clipen 匯出」所在的資料夾）。',
              'App Store 版的 Resolve 只能讀取你打開過的資料夾，選一次毛片資料夾，素材和這裡的元件、音樂就都找得到。',
              '', '影片片段直接指向原始素材，前後保留可延長的長度。若搬動了素材，Resolve 會請你重新連結。',
              '這個資料夾裡的元件、音樂和已處理片段也被時間線使用，請保持資料夾完整。']
    if editable:
        steps += ['', '想在 Resolve 改元件的字（需要時再做）', '',
                  f'匯入後元件已經和 Clipen 一模一樣，只是以圖片呈現。要改其中的字，再做兩步，{editable} 個元件會一起換成可以改字的標題：',
                  '1. 選「檔案 > 匯出 > 時間線…」（File > Export > Timeline），格式選「DaVinci Resolve Timeline Files (*.drt)」，按儲存。',
                  '   Clipen 開著的話會自動做好一份名稱有「（可改字）」的 .drt，並在 Finder 打開它；沒出現的話，到 Clipen 的「到 DaVinci Resolve 自己剪」按「把元件換成可改字的標題」。',
                  '2. 選「檔案 > 匯入 > 時間線…」，選那份「（可改字）」的 .drt。選取元件後，在右邊「檢查器」改字。',
                  '   改字只會改文字本身；底框、細線這類圖形是固定的圖片，字數差很多時可能要自己調整位置。']
    if notes:
        steps += ['', '需要留意', ''] + [f'・{note}' for note in notes]
    steps += ['', '時間線上的標記（Marker）記下了 Clipen 有、但 Resolve 無法直接帶入的處理，例如調色數值、轉場與淡入淡出。']
    (package / '說明.txt').write_text('\n'.join(steps) + '\n', encoding='utf-8')
