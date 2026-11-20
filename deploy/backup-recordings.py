#!/usr/bin/env python3
"""Archive finalized Nano event clips. Process finalized files serially on recording completion or startup."""
from __future__ import annotations

from configparser import ConfigParser
from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo
import math
import sqlite3
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import shutil
import tomllib


BASE = Path.home() / '.local/share/shore-tracker'
CONFIG = Path.home() / '.config/shore-tracker/backup-drive.conf'
CARD = Path('/mnt/shore-video')
RCLONE = BASE / 'backup/rclone'


@cache
def drive_folder_id():
    config = ConfigParser(interpolation=None)
    with CONFIG.open() as handle:
        config.read_file(handle)
    folder = config.get('shore-backup', 'root_folder_id', fallback='').strip()
    if not folder:
        raise ValueError('shore-backup requires root_folder_id in the local rclone config')
    return folder


def remote(*args):
    cmd = [str(RCLONE), '--config', str(CONFIG), '--drive-root-folder-id', drive_folder_id(),
           '--retries', '2', '--low-level-retries', '3', '--contimeout', '15s',
           '--timeout', '90s', *args]
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True).stdout


def identity(path):
    s = path.stat(follow_symlinks=False)
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def save_receipt(path, receipt):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        try:
            json.dump(receipt, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def reserve_name(directory, started_at, transfer):
    start = datetime.fromisoformat(started_at)
    if start.tzinfo is None:
        raise ValueError('recording start has no timezone')
    day = start.astimezone(ZoneInfo('Asia/Taipei')).strftime('%y%m%d')
    db = sqlite3.connect(directory.parent / '.drive-names.sqlite3')
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, day TEXT, n INTEGER, UNIQUE(day,n))')
            row = db.execute('SELECT day,n FROM clips WHERE id=?', (directory.name,)).fetchone()
            if row:
                return f'{row[0]}_{row[1]:03d}'
            transfer('mkdir', 'shore-backup:' + day)
            names = transfer('lsf', 'shore-backup:' + day, '--files-only', '--recursive', '--max-depth', '2',
                             '--include', day + '_*.mp4').splitlines()
            local = db.execute('SELECT COALESCE(MAX(n),0) FROM clips WHERE day=?', (day,)).fetchone()[0]
            numbers = [int(m[1]) for name in names
                       if (m := re.fullmatch(r'(?:fail/)?' + day + r'_(\d{3,})\.mp4', name))]
            number = max([local, *numbers]) + 1
            db.execute('INSERT INTO clips VALUES (?,?,?)', (directory.name, day, number))
            return f'{day}_{number:03d}'
    finally:
        db.close()


def midpoint_jpeg(clip, dest, rotation):
    probe = subprocess.check_output(['ffprobe','-v','error','-show_entries',
        'format=duration','-of','default=nw=1:nk=1',str(clip)], text=True)
    duration = float(probe.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('missing recording duration')
    filters = ['hflip','vflip'] if rotation == 180 else []
    filters.append("scale=w='min(640,iw)':h=-2")
    subprocess.run(['ffmpeg','-nostdin','-v','error','-ss',f'{duration/2:.6f}',
        '-i',str(clip),'-map','0:v:0','-frames:v','1','-vf',','.join(filters),
        '-q:v','6','-threads','1','-y',str(dest)],check=True)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise ValueError('midpoint frame unavailable')


def verified_copy(local, dest, size, md5, transfer):
    transfer('copyto', str(local), dest, '--immutable', '--checksum')
    item = json.loads(transfer('lsjson', dest, '--stat', '--hash'))
    hashes = {k.lower():v.lower() for k,v in item.get('Hashes', {}).items()}
    if (item.get('IsDir') is not False or not item.get('ID') or item.get('Size') != size
            or hashes.get('md5') != md5):
        raise ValueError('remote size, file ID or MD5 did not verify')
    return item['ID']


def media_info(path):
    args = ['ffprobe','-v','error','-count_packets','-select_streams','v:0',
            '-show_entries','stream=codec_name,width,height,nb_read_packets:format=duration',
            '-of','json',str(path)]
    info = json.loads(subprocess.check_output(args, text=True))
    stream = info['streams'][0]
    return (stream['codec_name'], stream['width'], stream['height'],
            int(stream['nb_read_packets']), float(info['format']['duration']))


def encode_mobile(clip, output):
    source, width, height, count, duration = media_info(clip)
    if not math.isfinite(duration) or duration <= 0 or count <= 0:
        raise ValueError('recording duration or sample count unavailable')
    bitrate = max(4_000_000, min(45_000_000, round(width * height * 3)))
    cfg = tomllib.loads((CONFIG.parent/'config.toml').read_text())
    reserve = cfg.get('recording', {}).get('free_stop_gib', 2) * 2**30
    estimated = clip.stat().st_size if source == 'h264' else bitrate * duration / 8
    if shutil.disk_usage(clip.parent).free < reserve + estimated * 1.15:
        raise OSError('insufficient staging space on recording card')
    if source == 'h264':
        args = ['ffmpeg','-nostdin','-v','error','-i',str(clip),'-map','0:v:0',
                '-c:v','copy','-movflags','+faststart','-y',str(output)]
    elif source == 'hevc':
        args = ['gst-launch-1.0','-q','filesrc','location='+str(clip),'!',
                'matroskademux','!','h265parse','!','nvv4l2decoder','!',
                'nvvidconv','!','video/x-raw(memory:NVMM),format=NV12','!',
                'nvv4l2h264enc','bitrate='+str(bitrate),'!','h264parse','!',
                'mp4mux','faststart=true','faststart-file='+str(output)+'.fast','!',
                'filesink','location='+str(output)]
        # faststart muxing may temporarily need two copies of the encoded data.
        if shutil.disk_usage(clip.parent).free < reserve + estimated * 2.3:
            raise OSError('insufficient MP4 muxing space on recording card')
    else:
        raise ValueError('unsupported source codec')
    with subprocess.Popen(args, stdout=subprocess.DEVNULL) as proc:
        try:
            while proc.poll() is None:
                if shutil.disk_usage(clip.parent).free <= reserve:
                    raise OSError('recording reserve reached')
                try:
                    proc.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, args)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            Path(str(output)+'.fast').unlink(missing_ok=True)
    codec, w, h, samples, seconds = media_info(output)
    if (codec != 'h264' or (w,h,samples) != (width,height,count)
            or not math.isfinite(seconds) or abs(seconds-duration) > max(.15,2*duration/count)):
        raise ValueError('MP4 verification failed')


def clear_stage(directory):
    for name in ('.drive-part.mp4', '.drive-part.mp4.fast', '.drive-video.mp4',
                 '.drive-part.jpg', '.drive-preview.jpg'):
        (directory/name).unlink(missing_ok=True)


def chase_only(data):
    return data.get('triggers') == ['tracking'] and data.get('qualified') is not True


def archive(directory, transfer=remote, render=midpoint_jpeg, encode=encode_mobile):
    clip = directory / 'clip.mkv'
    marker = directory / 'recording.json'
    if (not re.fullmatch('[0-9a-f]{32}', directory.name) or directory.is_symlink()
            or not marker.is_file() or marker.is_symlink() or not clip.is_file() or clip.is_symlink()):
        return False
    data = json.loads(marker.read_text())
    if data.get('state') != 'ready' or data.get('id') != directory.name:
        return False
    signature = identity(clip)
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with clip.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            sha.update(chunk)
            md5.update(chunk)
    if (signature[2] <= 0 or data.get('size_bytes') != signature[2]
            or sha.hexdigest() != data.get('sha256') or identity(clip) != signature):
        raise ValueError('local recording does not match finalization metadata')
    # Stable path plus immutable copy makes a retry after power loss safe.
    stem = reserve_name(directory, data['started_at'], transfer)
    day = stem.split('_')[0]
    # Keep verified, deterministic retry inputs if the second upload fails.
    video = directory/'.drive-video.mp4'
    preview = directory/'.drive-preview.jpg'
    if not video.is_file():
        part = directory/'.drive-part.mp4'
        part.unlink(missing_ok=True)
        try:
            encode(clip, part)
            os.replace(part, video)
        finally:
            part.unlink(missing_ok=True)
    if not preview.is_file():
        part = directory/'.drive-part.jpg'
        render(video, part, data.get('rotation_degrees', 0))
        os.replace(part, preview)
    with video.open('rb') as handle:
        video_md5 = hashlib.file_digest(handle, 'md5').hexdigest()
    video_size = video.stat().st_size
    folder = f'{day}/fail' if chase_only(data) else day
    dest = f'shore-backup:{folder}/'
    video_id = verified_copy(video, dest+stem+'.mp4', video_size, video_md5, transfer)
    jpeg = preview.read_bytes()
    preview_md5 = hashlib.md5(jpeg, usedforsecurity=False).hexdigest()
    preview_id = verified_copy(preview, dest+'預覽圖/'+stem+'.jpg', len(jpeg), preview_md5, transfer)
    if identity(clip) != signature:
        raise ValueError('local recording changed during upload')
    save_receipt(directory / 'drive-backup.json', dict(folder_id=drive_folder_id(), file_id=video_id,
                 name=folder+'/'+stem+'.mp4', video_bytes=video_size, video_md5=video_md5,
                 preview_id=preview_id, preview_md5=preview_md5,
                 bytes=signature[2], md5=md5.hexdigest(), sha256=sha.hexdigest()))
    if identity(clip) != signature:
        raise ValueError('local recording changed before release')
    clip.unlink()
    clear_stage(directory)
    return True


def main():
    root = CARD / 'manual-recordings'
    if not CARD.is_mount() or root.resolve() != root or not root.is_dir():
        raise RuntimeError('recording card unavailable')
    if (BASE / 'data/manual-recordings').resolve() != root:
        raise RuntimeError('recording root differs from the mounted card')
    failures = 0
    with (BASE / 'backup/worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Clear only notifications present at the start. A later notification
        # keeps the path unit ready for another pass after this process exits.
        for signal in (root / '.backup-wake').glob('*'):
            if re.fullmatch('[0-9a-f]{32}', signal.name):
                signal.unlink(missing_ok=True)
        # Completed files waiting offline get numbers in recording-start order.
        pending = []
        for directory in root.iterdir():
            if (not directory.is_symlink() and (directory/'drive-backup.json').is_file()
                    and not (directory/'clip.mkv').exists()):
                clear_stage(directory)
            if directory.is_symlink() or not (directory / 'clip.mkv').is_file():
                continue
            try:
                metadata = json.loads((directory / 'recording.json').read_text())
                if metadata.get('state') != 'ready':
                    continue
                started = metadata['started_at']
                pending.append((datetime.fromisoformat(started).timestamp(), directory))
            except (KeyError, ValueError, OSError):
                failures += 1
        for _, directory in sorted(pending):
            try:
                if archive(directory):
                    print('Verified Drive backup and released clip:', directory.name, flush=True)
            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
                failures += 1
                print('Recording retained; retry pending:', directory.name, type(error).__name__, flush=True)
                if isinstance(error, subprocess.SubprocessError):
                    break  # Network or quota fault; let systemd retry.
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
