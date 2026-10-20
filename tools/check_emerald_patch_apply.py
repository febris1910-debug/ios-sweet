#!/usr/bin/env python3
"""Reproducible git-apply check for ios-sweet xnu/patches (lexicographic order).

Mirrors blacktop/darwin-xnu-build build.sh:
  for PATCH in "${PATCH_DIR}"/*.patch; do git apply --check ...; git apply ...

Resets a local xnu-8796.141.3 checkout to the known tag commit, then applies
every *.patch in sorted name order. Does not invent PHYS_BASE or touch phones.

Usage:
  python tools/check_emerald_patch_apply.py
  python tools/check_emerald_patch_apply.py --xnu C:\\ios-emerald\\downloads\\xnu-8796.141.3
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XNU = Path(r"C:\ios-emerald\downloads\xnu-8796.141.3")
# Same tip used by tools/_gen_emerald_patch.py (xnu-8796.141.3)
XNU_RESET_SHA = "1b191cb58250d0705d8a51287127505aa4bc0789"
CRITICAL = (
    "0017-sweet-device-tree-virtual-timer-irq.patch",
    "0082-emerald-machine-config.patch",
    "0083-emerald-vtimer-extern-guard.patch",
    "0084-emerald-linker-sources.patch",
)


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xnu", type=Path, default=DEFAULT_XNU)
    ap.add_argument(
        "--no-reset",
        action="store_true",
        help="do not git reset --hard (unsafe; only for debug)",
    )
    ns = ap.parse_args()
    xnu: Path = ns.xnu
    patch_dir = ROOT / "xnu" / "patches"
    if not xnu.is_dir():
        print(f"FAIL: xnu tree missing: {xnu}", file=sys.stderr)
        return 2
    if not (xnu / ".git").exists():
        print(f"FAIL: {xnu} is not a git checkout", file=sys.stderr)
        return 2

    patches = sorted(patch_dir.glob("*.patch"), key=lambda p: p.name)
    names = [p.name for p in patches]
    print(f"patch_count={len(patches)}")
    for n in CRITICAL:
        if n not in names:
            print(f"FAIL: missing critical patch {n}", file=sys.stderr)
            return 1
    idx = [names.index(n) for n in CRITICAL]
    if idx != sorted(idx):
        print(f"FAIL: critical order broken: {CRITICAL} -> {idx}", file=sys.stderr)
        return 1
    print("critical_order_ok:", " < ".join(CRITICAL))
    p83 = patch_dir / "0083-emerald-vtimer-extern-guard.patch"
    print(f"0083_sha256={sha256(p83)}")

    if not ns.no_reset:
        r = run(["git", "reset", "--hard", XNU_RESET_SHA], cwd=xnu)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            return 1
        run(["git", "clean", "-fd"], cwd=xnu)
        print(f"reset_ok={XNU_RESET_SHA[:12]}")

    applied: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for p in patches:
        chk = run(["git", "apply", "--check", "--whitespace=nowarn", str(p)], cwd=xnu)
        if chk.returncode != 0:
            # Match CI: skip if check fails (already applied / malformed)
            skipped.append((p.name, (chk.stderr or chk.stdout or "").strip()[:160]))
            continue
        apy = run(["git", "apply", "--whitespace=nowarn", str(p)], cwd=xnu)
        if apy.returncode != 0:
            failed.append((p.name, (apy.stderr or apy.stdout or "").strip()[:200]))
            break
        applied.append(p.name)

    print(f"applied={len(applied)} skipped={len(skipped)} failed={len(failed)}")
    for n, why in skipped:
        if n in CRITICAL:
            print(f"CRITICAL_SKIP {n}: {why}", file=sys.stderr)
    for n, why in failed:
        print(f"FAIL_APPLY {n}: {why}", file=sys.stderr)

    # 0017 / 0082 / 0083 must have been applied (not skipped) on a clean tree
    for n in CRITICAL:
        if n not in applied:
            print(f"FAIL: critical patch not applied on clean tree: {n}", file=sys.stderr)
            return 1

    protos = xnu / "pexpert" / "pexpert" / "arm" / "protos.h"
    text = protos.read_text(encoding="utf-8", errors="replace")
    if "#if defined(SWEET)\nextern uint32_t sweet_vtimer_intid;" not in text.replace(
        "\r\n", "\n"
    ):
        # allow minor whitespace
        if "#if defined(SWEET)" not in text or "extern uint32_t sweet_vtimer_intid" not in text:
            print("FAIL: protos.h missing defined(SWEET) extern after 0083", file=sys.stderr)
            return 1
        if "ARM64_BOARD_CONFIG_SWEET" in text and "sweet_vtimer_intid" in text:
            # ensure the extern itself is not still under BOARD_CONFIG_SWEET only
            import re

            m = re.search(
                r"#if defined\(([^)]+)\)\s*\nextern uint32_t sweet_vtimer_intid;",
                text.replace("\r\n", "\n"),
            )
            if not m or m.group(1) != "SWEET":
                print(f"FAIL: sweet_vtimer_intid guard is {m.group(1) if m else None}", file=sys.stderr)
                return 1
    emerald_h = xnu / "pexpert" / "pexpert" / "arm64" / "EMERALD.h"
    if not emerald_h.is_file():
        print("FAIL: EMERALD.h missing after 0082", file=sys.stderr)
        return 1
    eh = emerald_h.read_text(encoding="utf-8", errors="replace")
    if "#define SWEET" not in eh or "SWEET_VTIMER_INTID        27" not in eh:
        print("FAIL: EMERALD.h missing SWEET / INTID 27", file=sys.stderr)
        return 1

    master_e = xnu / "config" / "MASTER.arm64.emerald"
    if not master_e.is_file():
        print("FAIL: MASTER.arm64.emerald missing after 0084", file=sys.stderr)
        return 1
    me = master_e.read_text(encoding="utf-8", errors="replace")
    if "nos_arm_asm" not in me or "nos_arm_pmap" not in me:
        print("FAIL: MASTER.arm64.emerald missing nos_arm_asm/nos_arm_pmap", file=sys.stderr)
        return 1
    p84 = patch_dir / "0084-emerald-linker-sources.patch"
    print(f"0084_sha256={sha256(p84)}")

    print("VERIFY_OK: 0017 -> 0082 -> 0083 -> 0084; protos.h defined(SWEET); MASTER.emerald nos_arm_*")
    print("NOTE: this is apply-check only — not a full XNU kernel compile")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
