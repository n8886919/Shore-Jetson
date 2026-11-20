"""Read files from the mounted video card without following links."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat


class CardFiles:
    def __init__(self, root: Path = Path('/mnt/shore-video')) -> None:
        self.root = root

    @contextmanager
    def open_path(self, relative: str, *, directory: bool = False):
        if len(relative) > 4096 or '\0' in relative or relative.startswith('/'):
            raise ValueError('invalid path')
        parts = relative.split('/') if relative else []
        if any(part in {'', '.', '..'} for part in parts):
            raise ValueError('invalid path')
        if not os.path.ismount(self.root):
            raise OSError('video card is not mounted')
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.root, flags | os.O_DIRECTORY)
        try:
            device = os.fstat(fd).st_dev
            if not os.path.ismount(self.root) or self.root.stat().st_dev != device:
                raise OSError('video card mount changed')
            for index, part in enumerate(parts):
                is_directory = directory or index < len(parts) - 1
                next_fd = os.open(part, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=fd)
                os.close(fd)
                fd = next_fd
                if os.fstat(fd).st_dev != device:
                    raise PermissionError('not on the video card')
            info = os.fstat(fd)
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                raise ValueError('not a regular file or directory')
            yield fd, info
        finally:
            os.close(fd)

    def listing(self, relative: str, offset: int = 0) -> dict:
        if not 0 <= offset <= 1000000:
            raise ValueError('invalid offset')
        entries = []
        truncated = False
        with self.open_path(relative, directory=True) as (fd, _):
            disk = os.fstatvfs(fd)
            with os.scandir(fd) as children:
                for child in children:
                    try:
                        child.name.encode('utf-8')
                        info = child.stat(follow_symlinks=False)
                    except (OSError, UnicodeError):
                        continue
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        continue
                    entries.append({'name': child.name,
                                    'path': '/'.join(filter(None, (relative, child.name))),
                                    'type': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                                    'size': info.st_size if stat.S_ISREG(info.st_mode) else None,
                                    'modified': info.st_mtime})
                    if len(entries) == 10000:
                        truncated = True
                        break
        entries.sort(key=lambda entry: (entry['type'] != 'directory', entry['name'].casefold()))
        return {'path': relative, 'parent': relative.rpartition('/')[0] if relative else None,
                'entries': entries[offset:offset + 200], 'total': len(entries),
                'offset': offset, 'limit': 200, 'truncated': truncated,
                'storage': {'total': disk.f_blocks * disk.f_frsize,
                            'used': (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
                            'free': disk.f_bavail * disk.f_frsize}}


def byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value)
    if not match or not any(match.groups()) or size == 0 or len(value) > 100:
        raise ValueError('invalid byte range')
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    else:
        length = int(last)
        if length == 0:
            raise ValueError('invalid suffix')
        start, end = max(0, size - length), size - 1
    if start >= size or end < start:
        raise ValueError('unsatisfiable byte range')
    return start, end, True
