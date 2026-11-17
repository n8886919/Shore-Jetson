#!/usr/bin/env python3
"""Reviewed cleanup for the dedicated Nano; default is plan-only, no autoremove."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

REMOVE = """
libcudnn8-dev libcublas-dev libnvinfer-dev libnvinfer-plugin-dev
libnvonnxparsers-dev libnvparsers-dev cuda-driver-dev-10-2
cuda-nvrtc-dev-10-2 cuda-cudart-dev-10-2
nvidia-l4t-jetson-multimedia-api nvidia-l4t-graphics-demos
nvidia-l4t-kernel-headers libnvvpi1 libc6-dbg manpages-dev
python3-dev python3.6-dev libpython3-dev libpython3.6-dev
python3-pandas python3-pandas-lib python3-scipy python3-matplotlib
docker runc libnvidia-container-tools libnvidia-container0 libnvidia-container1
nvidia-container-runtime nvidia-container-toolkit ubuntu-fan
printer-driver-brlaser printer-driver-c2esp printer-driver-min12xxw
printer-driver-pnm2ppa printer-driver-ptouch printer-driver-sag-gdi
system-config-printer system-config-printer-udev
gnome-software gnome-software-common example-content remmina-common
rhythmbox-data shotwell-common totem-common deja-dup modemmanager
spice-vdagent nfs-common rpcbind
""".split()
# Explicitly reviewed reverse dependencies; never permit an arbitrary cascade.
ALLOWED = set(REMOVE + ['ubuntu-desktop', 'ubuntu-software', 'python3-vpi1'])
KEEP = """
cuda-cudart-10-2 cuda-nvrtc-10-2 libcufft-10-2 libcublas10 libcudnn8
libnvinfer8 libnvinfer-plugin8 libnvonnxparsers8 python3-libnvinfer
python3-gi gir1.2-gst-plugins-base-1.0 network-manager openssh-server
gdm3 chromium-browser gnome-control-center
""".split()


def run(args, **kwargs):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, **kwargs)


def installed(name):
    result = run(['dpkg-query', '-W', '-f=${db:Status-Abbrev}', name])
    return result.returncode == 0 and result.stdout.startswith('ii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--user', default=os.environ.get('SUDO_USER'),
                        help='Owner of the cleanup report (defaults to the sudo caller)')
    args = parser.parse_args()
    home = None
    if args.apply:
        if not args.user:
            parser.error('--apply requires --user or a sudo caller')
        account = pwd.getpwnam(args.user)
        if account.pw_uid == 0:
            parser.error('Choose a non-root report owner')
        home = Path(account.pw_dir)
    assert b'p3448' in Path('/proc/device-tree/compatible').read_bytes(), 'Not Nano'
    assert 'REVISION: 7.6' in Path('/etc/nv_tegra_release').read_text(), 'Unexpected L4T'
    if args.apply:
        assert os.geteuid() == 0, 'Run --apply using sudo'
    assert run(['findmnt', '-rn', '-t', 'nfs,nfs4,cifs']).stdout.strip() == '', 'Remote filesystem in use'
    packages = [p for p in REMOVE if installed(p)]
    plan = run(['apt-get', '-s', 'purge'] + packages, check=True) if packages else None
    proposed = [] if plan is None else [line.split()[1].split(':')[0]
        for line in plan.stdout.splitlines() if line.startswith(('Purg ', 'Remv '))]
    unexpected = sorted(set(proposed) - ALLOWED)
    assert not unexpected, 'Unreviewed removal: ' + repr(unexpected)
    print('REVIEWED_PACKAGES', json.dumps(proposed), flush=True)
    if not args.apply:
        print('PLAN_ONLY: no changes made')
        return

    os.umask(0o077)
    report = home / '.local/share/shore-tracker/maintenance'
    report.mkdir(parents=True, exist_ok=True)
    before = shutil.disk_usage('/')
    plan_path = report/'root-cleanup-plan.txt'
    if plan or not plan_path.exists():
        plan_path.write_text(plan.stdout if plan else 'Already removed\n')
    run(['apt-mark', 'manual'] + [p for p in KEEP if installed(p)], check=True)
    if packages:
        env = dict(os.environ, DEBIAN_FRONTEND= 'noninteractive')
        result = run(['apt-get', '-y', '-o', 'APT::Get::AutomaticRemove=false', 'purge'] + packages, env=env)
        (report/'root-cleanup-apt.log').write_text(result.stdout)
        assert result.returncode == 0, 'Package cleanup failed; inspect root-cleanup-apt.log'
    run(['apt-get', 'clean'], check=True)
    for service in ('docker.service', 'containerd.service'):
        assert run(['systemctl', 'is-active', '--quiet', service]).returncode != 0, 'Container service still active'
    paths = ['/var/lib/docker', '/var/lib/containerd', '/opt/containerd',
             '/root/.cache/pip', '/root/.cache/torch', '/root/.cache/huggingface',
             '/opt/openssl-3.1.4/include', '/opt/openssl-3.1.4/share']
    removed = []
    mounts = run(['findmnt', '-rn', '-o', 'TARGET'], check=True).stdout.splitlines()
    for name in paths:
        assert not any(m == name or m.startswith(name + '/') for m in mounts), 'Mounted cleanup path'
        path = Path(name)
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(str(path))
        elif path.is_file():
            path.unlink()
        else:
            continue
        removed.append(name)
    audit = {}
    for name in ('/root', '/opt', '/srv', '/usr/local/src', '/var/lib'):
        audit[name] = run(['du', '-xhd1', name]).stdout
    audit['root_entries'] = sorted(p.name for p in Path('/root').iterdir())
    audit['package_check'] = run(['dpkg', '--audit']).stdout
    # Save evidence before final checks so a failed check remains diagnosable.
    after = shutil.disk_usage('/')
    result = dict(status='verification_pending', removed_packages_this_run=proposed,
                  removed_paths_this_run=removed, freed_bytes=after.free-before.free,
                  free_bytes=after.free, audit=audit)
    output = report/'root-cleanup-result.json'
    output.write_text(json.dumps(result, indent=2) + '\n')
    uid, gid = home.stat().st_uid, home.stat().st_gid
    for p in report.glob('root-cleanup-*'):
        os.chown(str(p), uid, gid)
    assert not audit['package_check'].strip(), 'dpkg reports incomplete packages'
    for binary in ('/usr/sbin/sshd', '/usr/local/sbin/sshd'):
        assert 'not found' not in run(['ldd', binary], check=True).stdout, 'SSH library missing'
    run(['/usr/sbin/sshd', '-t'], check=True)
    assert run(['systemctl', 'is-active', '--quiet', 'ssh']).returncode == 0, \
        'SSH service not active; inspect systemctl status ssh and journalctl -u ssh'
    assert run(['systemctl', 'is-active', '--quiet', 'NetworkManager']).returncode == 0, \
        'NetworkManager not active'
    remaining = [p for p in REMOVE if installed(p)]
    assert not remaining, 'Packages still installed: ' + repr(remaining)
    result['status'] = 'complete'
    output.write_text(json.dumps(result, indent=2) + '\n')
    print('NANO_DEEP_CLEANUP_DONE', 'freed_bytes=', result['freed_bytes'], flush=True)
    print('Post-cleanup GPU and live camera checks must run separately.', flush=True)


if __name__ == '__main__':
    main()
