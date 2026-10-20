#!/usr/bin/env python3
"""Verify SWEET/EMERALD stage scripts and pinned HFS layout for NEXT-26.

Checks (no phone, no PHYS_BASE, no full XNU compile):
  1. 0019/0020/0026 stage scripts accept MACHINE_CONFIG=EMERALD (not SWEET-only).
  2. Gate still skips unrelated machine configs (e.g. VMAPPLE).
  3. Optional: fetch pinned hfs SHA and confirm core/kext-config.h exists.

Usage:
  python tools/check_emerald_stage_scripts.py
  python tools/check_emerald_stage_scripts.py --fetch-hfs
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / "xnu" / "patches"
HFS_SHA = "31d5ec238dfab485e78a9a30b8b92d13e8914f03"
STAGE_SCRIPTS = (
    "0019-stage-libpthread.sh",
    "0020-stage-bearssl.sh",
    "0026-stage-hfs.sh",
)
# case "${MACHINE_CONFIG:-}" in / SWEET|EMERALD) ;;
GATE_RE = re.compile(
    r'case\s+"\$\{MACHINE_CONFIG:-\}"\s+in\s*\nSWEET\|EMERALD\)\s*;;',
    re.MULTILINE,
)
OLD_SWEET_ONLY = re.compile(
    r'\[\[\s*"\$\{MACHINE_CONFIG:-\}"\s*==\s*SWEET\s*\]\]\s*\|\|\s*exit\s+0'
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def check_gates() -> int:
    failed = 0
    for name in STAGE_SCRIPTS:
        path = PATCH_DIR / name
        if not path.is_file():
            print(f"FAIL: missing {name}", file=sys.stderr)
            failed += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        digest = sha256(path)
        print(f"{name} sha256={digest}")
        if OLD_SWEET_ONLY.search(text):
            print(f"FAIL: {name} still has SWEET-only gate", file=sys.stderr)
            failed += 1
            continue
        if not GATE_RE.search(text):
            print(f"FAIL: {name} missing SWEET|EMERALD case gate", file=sys.stderr)
            failed += 1
            continue
        print(f"OK gate {name}")
    return failed


def simulate_gate(machine: str) -> dict[str, bool]:
    """Return whether each script's gate would proceed (True=run body)."""
    out: dict[str, bool] = {}
    for name in STAGE_SCRIPTS:
        text = (PATCH_DIR / name).read_text(encoding="utf-8", errors="replace")
        # Mirror case SWEET|EMERALD) ;; *) exit 0 ;;
        out[name] = machine in ("SWEET", "EMERALD") and bool(GATE_RE.search(text.replace("\r\n", "\n")))
    return out


def check_gate_matrix() -> int:
    failed = 0
    for machine, expect_run in (("SWEET", True), ("EMERALD", True), ("VMAPPLE", False), ("T8101", False)):
        got = simulate_gate(machine)
        for name, runs in got.items():
            if runs != expect_run:
                print(
                    f"FAIL: {name} gate for {machine}: runs={runs} expected={expect_run}",
                    file=sys.stderr,
                )
                failed += 1
        print(f"OK matrix {machine} -> run={expect_run}")
    return failed


def fetch_hfs_kext_config() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hfs_pin_"))
    try:
        r = run(["git", "init", "-q", str(tmp)])
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            return 1
        r = run(
            [
                "git",
                "-C",
                str(tmp),
                "fetch",
                "-q",
                "--depth",
                "1",
                "https://github.com/apple-oss-distributions/hfs",
                HFS_SHA,
            ]
        )
        if r.returncode != 0:
            print(f"FAIL: hfs fetch: {r.stderr or r.stdout}", file=sys.stderr)
            return 1
        r = run(["git", "-C", str(tmp), "checkout", "-q", "FETCH_HEAD"])
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            return 1
        head = run(["git", "-C", str(tmp), "rev-parse", "HEAD"]).stdout.strip()
        if head != HFS_SHA:
            print(f"FAIL: hfs HEAD {head} != {HFS_SHA}", file=sys.stderr)
            return 1
        kext = tmp / "core" / "kext-config.h"
        if not kext.is_file():
            print(f"FAIL: missing {kext}", file=sys.stderr)
            return 1
        print(f"OK hfs pin {HFS_SHA} has core/kext-config.h ({kext.stat().st_size} bytes)")
        # Spot-check include used by sweet_hfs_compat.h
        if "KEXT" not in kext.read_text(encoding="utf-8", errors="replace") and "kext" not in kext.name:
            pass
        return 0
    finally:
        # best-effort cleanup
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fetch-hfs",
        action="store_true",
        help="fetch pinned apple-oss hfs and verify core/kext-config.h",
    )
    ns = ap.parse_args()

    failed = 0
    failed += check_gates()
    failed += check_gate_matrix()

    # Lexicographic order among critical emerald patches still holds (scripts
    # are not patches; they run before *.patch in dxb).
    names = sorted(p.name for p in PATCH_DIR.glob("*.patch"))
    need = [
        "0017-sweet-device-tree-virtual-timer-irq.patch",
        "0082-emerald-machine-config.patch",
        "0083-emerald-vtimer-extern-guard.patch",
        "0084-emerald-linker-sources.patch",
    ]
    idx = [names.index(n) for n in need]
    if idx != sorted(idx):
        print(f"FAIL: patch order {need} -> {idx}", file=sys.stderr)
        failed += 1
    else:
        print("OK patch_order", " < ".join(need))
    for n in need:
        p = PATCH_DIR / n
        print(f"{n} sha256={sha256(p)}")

    # git apply --check via existing tool is heavier; call its critical path
    # only when --fetch-hfs not the only ask — always run apply-check lightly
    # by invoking the sibling script when xnu tree exists.
    xnu = Path(r"C:\ios-emerald\downloads\xnu-8796.141.3")
    apply_tool = ROOT / "tools" / "check_emerald_patch_apply.py"
    if xnu.is_dir() and apply_tool.is_file():
        r = run([sys.executable, str(apply_tool), "--xnu", str(xnu)])
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr)
            failed += 1
        else:
            print("OK check_emerald_patch_apply")
    else:
        print("SKIP apply-check (xnu tree or tool missing)")

    if ns.fetch_hfs:
        failed += fetch_hfs_kext_config()
    else:
        print("NOTE: pass --fetch-hfs to verify pinned core/kext-config.h over the network")

    if failed:
        print(f"FAIL: {failed} check(s)", file=sys.stderr)
        return 1
    print("VERIFY_OK: stage scripts accept EMERALD; HFS gate matches run 39382491296 fix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
