"""Explicit, local-only material inventory. Never uploads, decodes, or edits media."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .store import Rejected

MAX_FILES = 500
MAX_NODES = 4000
MAX_DEPTH = 8
PROBE_SECONDS = 15

DEMUXERS = {
    '.mov': 'mov', '.mp4': 'mov', '.m4v': 'mov', '.m4a': 'mov',
    '.mkv': 'matroska,webm', '.webm': 'matroska,webm', '.avi': 'avi',
    '.mxf': 'mxf', '.mts': 'mpegts', '.m2ts': 'mpegts',
    '.wav': 'wav', '.mp3': 'mp3', '.aac': 'aac', '.flac': 'flac',
    '.aif': 'aiff', '.aiff': 'aiff',
}
IMAGES = {'.png', '.jpg', '.jpeg'}
UNSUPPORTED = {'.wmv', '.flv', '.ogv', '.ts', '.vob', '.gif', '.bmp', '.tif', '.tiff',
               '.raw', '.braw', '.r3d', '.heic', '.heif', '.webp'}
MEDIA_EXTENSIONS = set(DEMUXERS) | IMAGES | UNSUPPORTED


class ScanCancelled(Exception):
    pass


def _directory(path_text):
    if not isinstance(path_text, str) or not path_text or not os.path.isabs(path_text):
        raise Rejected('material_directory_must_be_absolute', 422)
    if any(ord(char) < 32 or ord(char) == 127 for char in path_text):
        raise Rejected('invalid_material_directory', 422)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path_text, flags)
    except OSError:
        raise Rejected('material_directory_unavailable', 422) from None
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise Rejected('material_directory_unavailable', 422)
    return fd


def validate_roots(source, reference=''):
    """Validate only after explicit confirmation; do not walk either directory."""
    fds = []
    try:
        fds.append(_directory(source))
        if reference:
            fds.append(_directory(reference))
    finally:
        for fd in fds:
            os.close(fd)


def _probe(fd, extension, cancel, executable):
    if not executable:
        return None, 'probe_unavailable'
    output = tempfile.TemporaryFile()
    errors = tempfile.TemporaryFile()
    args = [executable, '-v', 'error', '-hide_banner', '-protocol_whitelist', 'file',
            '-format_whitelist', DEMUXERS[extension], '-analyzeduration', '20000000',
            '-probesize', '20000000']
    if extension in {'.mov', '.mp4', '.m4v', '.m4a'}:
        args += ['-enable_drefs', '0', '-use_absolute_path', '0']
    args += ['-select_streams', 'v:0', '-show_entries',
             'format=duration:stream=codec_type,codec_name,width,height',
             '-of', 'json', f'/dev/fd/{fd}']
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                               close_fds=True, pass_fds=(fd,))
    deadline = time.monotonic() + PROBE_SECONDS
    try:
        while process.poll() is None:
            if cancel.wait(0.05):
                raise ScanCancelled()
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                return None, 'probe_timed_out'
        if process.returncode:
            return None, 'metadata_unreadable'
        output.seek(0)
        raw = output.read(65537)
        if len(raw) > 65536:
            return None, 'metadata_too_large'
        data = json.loads(raw)
        stream = (data.get('streams') or [{}])[0]
        duration = float((data.get('format') or {}).get('duration') or 0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError
        return {'duration': round(duration, 3), 'codec': stream.get('codec_name'),
                'width': stream.get('width'), 'height': stream.get('height')}, None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, 'metadata_unreadable'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        output.close()
        errors.close()


def _probe_image(fd, extension):
    """Read only bounded headers; inventory does not decode image pixels."""
    try:
        data = os.pread(fd, 1024 * 1024, 0)
        if extension == '.png':
            if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
                raise ValueError
            return {'width': int.from_bytes(data[16:20], 'big'),
                    'height': int.from_bytes(data[20:24], 'big')}, None
        if len(data) < 4 or data[:2] != b'\xff\xd8':
            raise ValueError
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xff:
                index += 1; continue
            marker = data[index + 1]; index += 2
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], 'big')
            if length < 2 or index + length > len(data):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                return {'height': int.from_bytes(data[index + 3:index + 5], 'big'),
                        'width': int.from_bytes(data[index + 5:index + 7], 'big')}, None
            index += length
    except (OSError, ValueError):
        pass
    return None, 'metadata_unreadable'


def scan_materials(source, reference, cancel, progress=None, ffprobe=None):
    """Return a bounded inventory. Paths are relative to their confirmed roots."""
    executable = shutil.which('ffprobe') if ffprobe is None else ffprobe
    entries, nodes = [], 0

    def visit(root_fd, role, directory_fd, prefix, depth):
        nonlocal nodes
        if cancel.is_set():
            raise ScanCancelled()
        try:
            children = sorted(os.scandir(directory_fd), key=lambda item: item.name.casefold())
        except OSError:
            entries.append({'role': role, 'path': prefix or '.', 'kind': 'issue',
                            'issue': 'directory_unreadable'})
            return
        for child in children:
            if cancel.is_set():
                raise ScanCancelled()
            nodes += 1
            if nodes > MAX_NODES:
                raise Rejected('material_node_limit', 422)
            relative = f'{prefix}/{child.name}' if prefix else child.name
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                entries.append({'role': role, 'path': relative, 'kind': 'issue', 'issue': 'unreadable'})
                continue
            if stat.S_ISLNK(info.st_mode):
                if Path(child.name).suffix.lower() in MEDIA_EXTENSIONS:
                    entries.append({'role': role, 'path': relative, 'kind': 'issue', 'issue': 'symlink_skipped'})
                continue
            if stat.S_ISDIR(info.st_mode):
                if depth >= MAX_DEPTH:
                    entries.append({'role': role, 'path': relative, 'kind': 'issue', 'issue': 'directory_depth_limit'})
                    continue
                try:
                    child_fd = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                       dir_fd=directory_fd)
                except OSError:
                    entries.append({'role': role, 'path': relative, 'kind': 'issue', 'issue': 'directory_unreadable'})
                    continue
                try:
                    visit(root_fd, role, child_fd, relative, depth + 1)
                finally:
                    os.close(child_fd)
                continue
            extension = Path(child.name).suffix.lower()
            if not stat.S_ISREG(info.st_mode) or extension not in MEDIA_EXTENSIONS:
                continue
            if len(entries) >= MAX_FILES:
                raise Rejected('material_file_limit', 422)
            item = {'role': role, 'path': relative, 'size': info.st_size}
            if extension in UNSUPPORTED:
                item.update(kind='issue', issue='unsupported_format')
            else:
                try:
                    media_fd = os.open(child.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                       dir_fd=directory_fd)
                except OSError:
                    item.update(kind='issue', issue='unreadable')
                else:
                    try:
                        if not stat.S_ISREG(os.fstat(media_fd).st_mode):
                            item.update(kind='issue', issue='not_regular_file')
                        else:
                            metadata, issue = (_probe_image(media_fd, extension) if extension in IMAGES
                                               else _probe(media_fd, extension, cancel, executable))
                            if issue:
                                item.update(kind='issue', issue=issue)
                            else:
                                item.update(kind='image' if extension in IMAGES else 'media',
                                            duration=0 if extension in IMAGES else metadata['duration'],
                                            **{key:value for key,value in metadata.items() if key != 'duration'})
                    finally:
                        os.close(media_fd)
            entries.append(item)
            if progress:
                progress(entries)

    for role, root in (('source', source), ('reference', reference)):
        if not root:
            continue
        root_fd = _directory(root)
        try:
            visit(root_fd, role, root_fd, '', 0)
        finally:
            os.close(root_fd)
    return entries


class DirectoryPicker:
    """One native macOS picker; the fixed JXA script accepts no user-controlled code."""
    SCRIPT = '''ObjC.import("AppKit");
const panel = $.NSOpenPanel.openPanel;
panel.canChooseFiles = false;
panel.canChooseDirectories = true;
panel.allowsMultipleSelection = false;
panel.message = "選擇 Clipen 要使用的本機目錄";
panel.prompt = "選擇";
const result = panel.runModal;
result === $.NSModalResponseOK ? ObjC.unwrap(panel.URL.path) : "__CLIPEN_CANCELLED__";'''

    def __init__(self, platform=None):
        self.available = (platform or sys.platform) == 'darwin' and bool(shutil.which('osascript'))
        self.lock = threading.Lock()
        self.state = {'id': None, 'role': None, 'status': 'idle', 'path': None}
        self.process = None
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, role):
        if role not in ('source', 'reference'):
            raise Rejected('invalid_directory_role', 422)
        with self.lock:
            if not self.available:
                raise Rejected('directory_picker_unavailable', 409)
            if self.state['status'] == 'picking':
                raise Rejected('directory_picker_busy')
            pick_id = 'pick_' + secrets.token_hex(12)
            self.state = {'id': pick_id, 'role': role, 'status': 'picking', 'path': None}
            self.thread = threading.Thread(target=self._run, args=(pick_id,), daemon=False)
            self.thread.start()
            return {'status': 'picking', 'pick_id': pick_id}

    def _run(self, pick_id):
        try:
            process = subprocess.Popen(['osascript', '-l', 'JavaScript', '-e', self.SCRIPT], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            with self.lock:
                if self.state['id'] != pick_id:
                    process.terminate()
                self.process = process
            output, _ = process.communicate(timeout=300)
            value = output.rstrip('\r\n')
            if process.returncode:
                status, path = 'failed', None
            elif value == '__CLIPEN_CANCELLED__':
                status, path = 'cancelled', None
            else:
                status, path = 'chosen', value
            if path:
                fd = _directory(path)
                os.close(fd)
        except (OSError, subprocess.TimeoutExpired, Rejected):
            status, path = 'failed', None
            if 'process' in locals() and process.poll() is None:
                process.kill(); process.wait()
        with self.lock:
            if self.state['id'] == pick_id:
                self.state.update(status=status, path=path)
            self.process = None

    def close(self):
        with self.lock:
            process, thread = self.process, self.thread
            self.state = {'id': None, 'role': None, 'status': 'idle', 'path': None}
        if process and process.poll() is None:
            process.terminate()
        if thread and thread.ident is not None:
            thread.join(timeout=2)
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if thread and thread.is_alive():
            thread.join(timeout=2)
