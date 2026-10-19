"""Compare the reviewed Shore-Spotter source contract without contacting hardware."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT / "docs/shore-spotter-contract.json"
CONTRACT_FILES = (
    "AGENTS.md", "CHANGELOG.md", "include/firmware_version.h",
    "docs/interface.md", "docs/motion-control.md", "docs/uart-servo.md",
    "docs/hardware.md", "include/protocol.h", "include/servo_profile.h",
    "include/servo_motion.h", "include/uart_target.h", "include/uart_set_parser.h",
    "include/command_freshness.h", "include/uart_servo_mode.h",
    "src/uart_servo_mode.cpp", "src/main.cpp", "include/tracking_policy.h",
    "include/control_cadence.h",
    "include/servo_controller.h", "docs/servo-controller-youmi-2026-10-10.md",
)


def upstream_path() -> Path:
    for name in ("Shore-Spotter", "shore-spotter"):
        path = PROJECT.parent / name
        if path.is_dir():
            return path
    raise ValueError("Shore-Spotter sibling directory is unavailable")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, timeout=10)
    if result.returncode:
        raise ValueError("Cannot inspect Shore-Spotter Git state")
    return result.stdout.strip()


def snapshot(root: Path) -> dict:
    files = {}
    for name in CONTRACT_FILES:
        path = root / name
        if not path.is_file():
            raise ValueError(f"Missing upstream interface file: {name}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    version = re.search(r'^#define\s+SHORE_SPOTTER_VERSION\s+"([0-9]+(?:\.[0-9]+)+(?:-[0-9A-Za-z.-]+)?)"',
                        (root / "include/firmware_version.h").read_text(), re.MULTILINE)
    if not version:
        raise ValueError("Cannot read SHORE_SPOTTER_VERSION")
    return {"schema": 1, "firmware_version": version.group(1),
            "git_head": git(root, "rev-parse", "HEAD"), "files": files}


def changes(reviewed: dict, current: dict) -> list[str]:
    changed = []
    for key in ("schema", "firmware_version", "git_head"):
        if reviewed.get(key) != current.get(key):
            changed.append(key)
    before, after = reviewed.get("files", {}), current["files"]
    changed.extend(name for name in sorted(before.keys() | after.keys())
                   if before.get(name) != after.get(name))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--record-reviewed", action="store_true",
                        help="record only after reviewing upstream changes and validating compatibility")
    args = parser.parse_args()
    try:
        root = args.upstream or upstream_path()
        current = snapshot(root)
        status = git(root, "status", "--short", "--untracked-files=normal")
        print(f"Shore-Spotter source version: {current['firmware_version']}")
        print(f"Git HEAD: {current['git_head']}; uncommitted paths: {len(status.splitlines())}")
        print("This checks source files, not the firmware installed on the board.")
        if args.record_reviewed:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.manifest.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
            temporary.replace(args.manifest)
            print("Recorded the explicitly reviewed source baseline.")
            return 0
        if not args.manifest.is_file():
            raise ValueError("No reviewed baseline; review the upstream contract before recording one")
        changed = changes(json.loads(args.manifest.read_text()), current)
        if changed:
            print("REVIEW REQUIRED: " + ", ".join(changed))
            print("Read the changed source/docs, adapt and validate this project before recording a new baseline.")
            return 1
        print("PASS: version, Git HEAD and reviewed interface contents match.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"CHECK INCOMPLETE: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
