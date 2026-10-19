"""Source drift must be noticed even before the upstream version is bumped."""
import json
import subprocess

from tools import check_shore_spotter as check


def upstream(tmp_path):
    root = tmp_path / "Shore-Spotter"
    root.mkdir()
    for name in check.CONTRACT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('// initial contract\n')
    (root / "include/firmware_version.h").write_text('#define SHORE_SPOTTER_VERSION "0.1"\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    return root


def test_same_version_and_head_cannot_hide_uncommitted_protocol_edit(tmp_path):
    root = upstream(tmp_path)
    before = check.snapshot(root)
    path = root / 'include/uart_set_parser.h'
    path.write_text('// changed protocol without a version bump\n')
    after = check.snapshot(root)
    assert before['firmware_version'] == after['firmware_version'] == '0.1'
    assert before['git_head'] == after['git_head']
    assert check.changes(before, after) == ['include/uart_set_parser.h']


def test_check_fails_on_drift_without_silently_updating_baseline(tmp_path, monkeypatch):
    root = upstream(tmp_path)
    manifest = tmp_path / 'reviewed.json'
    manifest.write_text(json.dumps(check.snapshot(root)))
    original = manifest.read_bytes()
    (root / 'include/firmware_version.h').write_text('#define SHORE_SPOTTER_VERSION "0.2"\n')
    monkeypatch.setattr('sys.argv', ['check', '--upstream', str(root), '--manifest', str(manifest)])
    assert check.main() == 1
    assert manifest.read_bytes() == original


def test_missing_upstream_fails_as_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.argv', ['check', '--upstream', str(tmp_path / 'absent')])
    assert check.main() == 2


def test_dev_version_and_direct_controller_changes_are_tracked(tmp_path):
    root = upstream(tmp_path)
    (root / 'include/firmware_version.h').write_text('#define SHORE_SPOTTER_VERSION "0.6-dev"\n')
    before = check.snapshot(root)
    assert before['firmware_version'] == '0.6-dev'
    (root / 'include/servo_controller.h').write_text('// changed M0 framing\n')
    assert check.changes(before, check.snapshot(root)) == ['include/servo_controller.h']


def test_retired_dependency_requires_review_before_recording(tmp_path, monkeypatch, capsys):
    root = upstream(tmp_path)
    reviewed = check.snapshot(root)
    reviewed['files']['lib/ruckig/UPSTREAM.md'] = 'previously-reviewed-hash'
    manifest = tmp_path / 'reviewed.json'
    manifest.write_text(json.dumps(reviewed))
    original = manifest.read_bytes()
    args = ['check', '--upstream', str(root), '--manifest', str(manifest)]
    monkeypatch.setattr('sys.argv', args)
    assert check.main() == 1
    assert 'lib/ruckig/UPSTREAM.md' in capsys.readouterr().out
    assert manifest.read_bytes() == original

    monkeypatch.setattr('sys.argv', args + ['--record-reviewed'])
    assert check.main() == 0
    assert 'lib/ruckig/UPSTREAM.md' not in json.loads(manifest.read_text())['files']
    monkeypatch.setattr('sys.argv', args)
    assert check.main() == 0


def test_missing_required_control_source_still_fails_closed(tmp_path, monkeypatch):
    root = upstream(tmp_path)
    manifest = tmp_path / 'reviewed.json'
    manifest.write_text(json.dumps(check.snapshot(root)))
    original = manifest.read_bytes()
    (root / 'include/servo_motion.h').unlink()
    monkeypatch.setattr('sys.argv', ['check', '--upstream', str(root),
                                   '--manifest', str(manifest), '--record-reviewed'])
    assert check.main() == 2
    assert manifest.read_bytes() == original
