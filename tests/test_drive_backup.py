import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import pytest

spec = importlib.util.spec_from_file_location('backup', Path(__file__).parents[1]/'deploy/backup-recordings.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture(autouse=True)
def local_drive_config(tmp_path, monkeypatch):
    conf = tmp_path/'backup-drive.conf'
    conf.write_text('[shore-backup]\nroot_folder_id = test-folder\n')
    monkeypatch.setattr(backup, 'CONFIG', conf)
    backup.drive_folder_id.cache_clear()
    yield
    backup.drive_folder_id.cache_clear()


def test_missing_folder_refuses_drive_root():
    backup.CONFIG.write_text('[shore-backup]\n')
    with pytest.raises(ValueError, match='root_folder_id'):
        backup.remote('lsf', 'shore-backup:')


def clip(tmp_path, label='a', start='2026-11-19T17:30:00+00:00'):
    d = tmp_path/(label*32)
    d.mkdir()
    raw = b'synthetic shore recording'
    (d/'clip.mkv').write_bytes(raw)
    (d/'recording.json').write_text(json.dumps(dict(id=d.name,state='ready',size_bytes=len(raw),
        started_at=start,sha256=hashlib.sha256(raw).hexdigest())))
    return d


def image(video, dest, rotation):
    dest.write_bytes(b'sample jpeg')


def encode(source, dest):
    dest.write_bytes(b'converted sample mp4')


class Drive:
    def __init__(self, fault='ok'):
        self.fault = fault
        self.files = {}

    def __call__(self, op, *args):
        if op == 'mkdir':
            return ''
        if op == 'lsf':
            return '261120_006.mp4\n'
        if op == 'copyto':
            assert '--immutable' in args
            if self.fault == 'offline':
                raise subprocess.CalledProcessError(1,['rclone'])
            self.files[args[1]] = Path(args[0]).read_bytes()
            return ''
        assert op == 'lsjson'
        data = self.files[args[0]]
        md5 = hashlib.md5(data).hexdigest()
        if self.fault=='preview' and args[0].endswith('.jpg'):
            md5 = 'bad'
        return json.dumps(dict(ID='' if self.fault=='missing-id' else 'verified-file',IsDir=False,
                          Size=2 if self.fault=='size' else len(data),
                          Hashes={'md5':'bad' if self.fault=='hash' else md5}))


@pytest.mark.parametrize('fault',['offline','size','hash','missing-id','preview','ok'])
def test_backup_requires_both_verified_objects(tmp_path,fault):
    d=clip(tmp_path)
    drive=Drive(fault)
    if fault!='ok':
        with pytest.raises((ValueError,subprocess.CalledProcessError)):
            backup.archive(d,drive,image,encode)
        assert (d/'clip.mkv').is_file()
    drive.fault='ok'
    assert backup.archive(d,drive,image,encode)
    assert not (d/'clip.mkv').exists()
    assert not (d/'.drive-video.mp4').exists()
    assert set(drive.files)=={'shore-backup:261120/261120_007.mp4',
                             'shore-backup:261120/預覽圖/261120_007.jpg'}
    assert json.loads((d/'drive-backup.json').read_text())['name']=='261120/261120_007.mp4'
    assert not backup.archive(d,drive,image,encode)


def test_dates_retries_and_serial_after_cleanup(tmp_path):
    d=clip(tmp_path)
    second=clip(tmp_path,'b')
    drive=Drive()
    assert backup.reserve_name(d,'2026-11-19T17:00:00Z',drive)=='261120_007'
    assert backup.reserve_name(second,'2026-11-20T02:00:00Z',drive)=='261120_008'
    assert backup.reserve_name(second,'2026-11-23T10:10:00Z',drive)=='261120_008'
    third=clip(tmp_path,'c')
    assert backup.reserve_name(third,'2026-11-20T17:00:00Z',drive)=='261121_001'


def test_incomplete_symlink_and_changed_original(tmp_path):
    d=clip(tmp_path)
    original=d/'clip.mkv'
    original.rename(d/'clip.partial.mkv')
    assert not backup.archive(d)
    original.symlink_to(d/'clip.partial.mkv')
    assert not backup.archive(d)
    original.unlink()
    (d/'clip.partial.mkv').rename(original)
    def change(source, dest):
        encode(source,dest)
        source.write_bytes(b'replaced')
    with pytest.raises(ValueError,match='changed'):
        backup.archive(d, Drive(), image, change)
    assert original.read_bytes()==b'replaced'



@pytest.mark.parametrize(('triggers', 'qualified', 'folder'), [
    (['tracking'], False, '261120/fail'), (['tracking'], True, '261120'),
    (['manual'], False, '261120'), (['continuous'], False, '261120'),
    (['tracking', 'manual'], False, '261120'), (['continuous', 'tracking'], False, '261120'),
])
def test_backup_destination_survives_retry(tmp_path, triggers, qualified, folder):
    directory = clip(tmp_path)
    marker = directory/'recording.json'
    data = json.loads(marker.read_text())
    data.update(triggers=triggers, qualified=qualified)
    marker.write_text(json.dumps(data))
    drive = Drive('preview')
    with pytest.raises(ValueError):
        backup.archive(directory, drive, image, encode)
    assert (directory/'clip.mkv').is_file()
    drive.fault = 'ok'
    assert backup.archive(directory, drive, image, encode)
    assert not (directory/'clip.mkv').exists()
    assert set(drive.files) == {f'shore-backup:{folder}/261120_007.mp4',
                                f'shore-backup:{folder}/預覽圖/261120_007.jpg'}
    assert json.loads((directory/'drive-backup.json').read_text())['name'] == f'{folder}/261120_007.mp4'


def test_daily_sequence_counts_both_remote_folders_after_card_change(tmp_path):
    def transfer(op, *args):
        if op == 'mkdir':
            return ''
        assert op == 'lsf' and '--recursive' in args
        return '261120_001.mp4\nfail/261120_009.mp4\nother/261120_050.mp4\n'
    assert backup.reserve_name(clip(tmp_path), '2026-11-20T00:00:00Z', transfer) == '261120_010'


def test_worker_archives_finalized_clip_while_next_recording_is_open(tmp_path, monkeypatch):
    card = tmp_path/'card'
    root = card/'manual-recordings'
    root.mkdir(parents=True)
    base = tmp_path/'runtime'
    (base/'backup').mkdir(parents=True)
    (base/'data').mkdir()
    (base/'data/manual-recordings').symlink_to(root)
    monkeypatch.setattr(backup, 'CARD', card)
    monkeypatch.setattr(backup, 'BASE', base)
    monkeypatch.setattr(Path, 'is_mount', lambda p: p == card)
    finished = clip(root)
    marker = finished/'recording.json'
    marker.write_text(json.dumps(dict(json.loads(marker.read_text()), triggers=['tracking'], qualified=False)))
    pending = clip(root, 'b')
    partial = pending/'clip.partial.mkv'
    (pending/'clip.mkv').rename(partial)
    (pending/'recording.json').write_text(json.dumps({'id': pending.name, 'state': 'recording'}))
    wake = root/'.backup-wake'
    wake.mkdir()
    (wake/finished.name).touch()
    archive, drive = backup.archive, Drive()
    monkeypatch.setattr(backup, 'archive', lambda d: archive(d, drive, image, encode))
    assert backup.main() == 0
    assert json.loads((finished/'drive-backup.json').read_text())['name'].startswith('261120/fail/')
    assert not (finished/'clip.mkv').exists()
    assert partial.read_bytes() == b'synthetic shore recording'
    assert not (pending/'drive-backup.json').exists()
