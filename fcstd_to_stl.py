#!/usr/bin/env python3
"""
Cross-platform launcher: convert an FCStd file to STL using FreeCADCmd.exe.

Works when executed by:
  * Windows Python (python.exe / py.exe)
  * WSL Linux Python (python3) -- spawns the Windows FreeCADCmd.exe via
    WSL interop, translating paths with wslpath.

Usage:
    python fcstd_to_stl.py [input.FCStd [output.stl]]

Without arguments it converts testdata/motor-bracket.FCStd.

The heavy lifting lives in fc2stl_core.py, which is executed by FreeCAD's
own interpreter as a child process (never imported here).
"""

import os
import platform
import subprocess
import sys

# --- configuration ---------------------------------------------------------
FREECAD_CMD_WIN = r"C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_SCRIPT = os.path.join(BASE_DIR, "fc2stl_core.py")
INPUT_FILE = os.path.join(BASE_DIR, "testdata", "motor-bracket.FCStd")
OUTPUT_FILE = os.path.splitext(INPUT_FILE)[0] + ".stl"
# ---------------------------------------------------------------------------


def is_wsl() -> bool:
    return sys.platform.startswith("linux") and "microsoft" in platform.release().lower()


def to_windows_path(path: str) -> str:
    """Translate a native path into a Windows path for the FreeCADCmd child."""
    if os.name == "nt":
        return os.path.abspath(path)
    if is_wsl():
        return subprocess.check_output(["wslpath", "-w", path], text=True).strip()
    sys.exit(f"Unsupported platform for Windows FreeCAD: {sys.platform}")


def freecad_cmd_path() -> str:
    """Return the path to FreeCADCmd.exe usable from this interpreter."""
    if os.name == "nt":
        return FREECAD_CMD_WIN
    if is_wsl():
        # WSL can execute Windows binaries via the /mnt/<drive>/ mount.
        return subprocess.check_output(["wslpath", "-u", FREECAD_CMD_WIN], text=True).strip()
    sys.exit(f"Unsupported platform for Windows FreeCAD: {sys.platform}")


def main() -> None:
    input_file = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else INPUT_FILE
    output_file = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT_FILE

    if not os.path.isfile(input_file):
        sys.exit(f"Input file not found: {input_file}")

    exe = freecad_cmd_path()
    if not os.path.isfile(exe):
        sys.exit(f"FreeCADCmd.exe not found at: {exe}")

    cmd = [
        exe,
        to_windows_path(CORE_SCRIPT),
        to_windows_path(input_file),
        to_windows_path(output_file),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)

    if proc.returncode != 0:
        sys.exit(f"FreeCADCmd exited with code {proc.returncode}")
    if not os.path.isfile(output_file):
        sys.exit("FreeCADCmd finished but no STL was produced")


if __name__ == "__main__":
    main()
