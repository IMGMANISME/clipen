"""Terminal-owned lifetime; never write UI credentials into MCP streams."""
from contextlib import contextmanager
import os
import select
import signal
import stat


def announce_in_terminal(url):
    """Best effort to the current controlling terminal, not captured stderr.

    Some AI hosts detach their MCP children. Do not search other terminals or
    fall back to logging the credential when there is no controlling terminal.
    """
    fd = None
    try:
        fd = os.open('/dev/tty', os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        if not stat.S_ISCHR(os.fstat(fd).st_mode) or not os.isatty(fd):
            return False
        message = ('\r\n[Clipen] Local UI / 本機介面：\r\n' + url +
                   '\r\n本次 AI 執行期間可重開；關閉 terminal 即停止 UI。請勿分享此連結。\r\n')
        data = message.encode('utf-8')
        while data:
            written = os.write(fd, data)
            if not written:
                return False
            data = data[written:]
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def stop_on_signals(stop):
    """Set a flag only: cleanup/SQLite work must not run inside a signal handler."""
    previous = {}
    try:
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def input_lines(source, stop, parent_pid=None):
    """Read bounded stdio messages without blocking shutdown on an open pipe."""
    try:
        fd = source.fileno()
    except (AttributeError, OSError, ValueError):
        # In-memory fixtures, not the live stdio transport.
        while not stop.is_set():
            line = source.readline(65537)
            if not line:
                return
            yield line
        return
    terminal_fd = None
    if parent_pid is not None:
        try:
            terminal_fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            pass  # Detached hosts still use parent/EOF/signal monitoring.
    try:
        yield from pipe_lines(fd, stop, parent_pid, terminal_fd)
    finally:
        if terminal_fd is not None:
            os.close(terminal_fd)


def pipe_lines(fd, stop, parent_pid, terminal_fd):
    pending = bytearray()
    while not stop.is_set():
        owner_gone = parent_pid is not None and os.getppid() != parent_pid
        if terminal_fd is not None:
            try:
                # macOS /dev/tty still answers tcgetpgrp after hangup, and
                # rejects kqueue registration. A zero-byte write reports EIO
                # after hangup without reading keys or changing the display.
                os.write(terminal_fd, b'')
            except OSError:
                owner_gone = True  # Terminal hung up even if the AI ignored HUP.
        if owner_gone:
            stop.set()
            return
        readable, _, _ = select.select([fd], [], [], 0.2)
        if not readable:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        pending.extend(chunk)
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending = bytearray(rest)
            yield bytes(line) + b'\n'
            if stop.is_set():
                return
        if len(pending) > 65536:
            yield bytes(pending)  # Caller reports the bounded-size error.
            return
