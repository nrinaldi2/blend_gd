"""
launcher.py

Windows GUI launcher for read_blend.py. It finds Blender's embedded Python,
checks or installs blender-asset-tracer, prompts for the input .blend path,
and runs the reader script to generate the JSON export.

JSON exports are written automatically under `Material Outputs/<blend-stem>/`
next to this launcher/read_blend.py script.
"""

import os
import subprocess
from pathlib import Path

MATERIAL_OUTPUTS_DIRNAME = "Material Outputs"

# --- GUI helpers (Windows) ---
def _tk():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root, filedialog, messagebox

def ask_blender_dir():
    root, filedialog, _ = _tk()
    d = filedialog.askdirectory(title="Select your Blender install folder (the one containing blender.exe)")
    root.destroy()
    return Path(d) if d else None

def ask_blend_file():
    root, filedialog, _ = _tk()
    f = filedialog.askopenfilename(
        title="SELECT A .blend FILE",
        filetypes=[("Blender files", "*.blend"), ("All files", "*.*")]
    )
    root.destroy()
    return Path(f) if f else None

def ask_yes_no(title, msg):
    root, _, messagebox = _tk()
    ans = messagebox.askyesno(title, msg)
    root.destroy()
    return ans

def show_info(title, msg):
    root, _, messagebox = _tk()
    messagebox.showinfo(title, msg)
    root.destroy()

def show_error(title, msg):
    root, _, messagebox = _tk()
    messagebox.showerror(title, msg)
    root.destroy()


# --- Blender python discovery ---
def find_blender_python(blender_dir: Path) -> Path | None:
    """
    Expect user selects folder containing blender.exe OR a parent folder.
    This tries common Blender layouts:
      <dir>\blender.exe
      <dir>\<version>\python\bin\python.exe
      <dir>\python\bin\python.exe   (portable builds)
    """
    blender_dir = blender_dir.resolve()

    # If they picked the exact folder with blender.exe
    if (blender_dir / "blender.exe").exists():
        # Try: <dir>\<ver>\python\bin\python.exe
        for sub in blender_dir.iterdir():
            cand = sub / "python" / "bin" / "python.exe"
            if cand.exists():
                return cand
        # Try: <dir>\python\bin\python.exe
        cand = blender_dir / "python" / "bin" / "python.exe"
        if cand.exists():
            return cand

    # Search around for python.exe 
    for p in blender_dir.rglob("python.exe"):
        parts = [x.lower() for x in p.parts]
        try:
            i = parts.index("python")
            if i + 2 < len(parts) and parts[i + 1] == "bin" and p.name.lower() == "python.exe":
                # Depth guard
                rel_depth = len(p.relative_to(blender_dir).parts)
                if rel_depth <= 8:
                    return p
        except ValueError:
            pass

    return None


# --- BAT detection / installation ---
def run_python(python_exe: Path, args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [str(python_exe), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)

def has_bat(python_exe: Path, extra_pythonpath: Path | None = None) -> bool:
    env = os.environ.copy()
    if extra_pythonpath:
        env["PYTHONPATH"] = str(extra_pythonpath) + os.pathsep + env.get("PYTHONPATH", "")
    cp = run_python(python_exe, ["-c", "import blender_asset_tracer; print('OK')"], env=env)
    return cp.returncode == 0

def install_bat(python_exe: Path, target_dir: Path | None = None) -> tuple[bool, str]:
    """
    Attempts:
      - ensurepip (if needed)
      - pip install blender-asset-tracer
    If target_dir is provided, uses --target <dir> (no admin rights required).
    Returns (success, combined_output).
    """
    outputs = []

    # Ensure pip exists
    cp = run_python(python_exe, ["-m", "pip", "--version"])
    if cp.returncode != 0:
        cp2 = run_python(python_exe, ["-m", "ensurepip", "--upgrade"])
        outputs.append(cp2.stdout + cp2.stderr)
        # try pip again
        cp = run_python(python_exe, ["-m", "pip", "--version"])
        outputs.append(cp.stdout + cp.stderr)
        if cp.returncode != 0:
            return False, "\n".join(outputs)

    # Upgrade pip (optional but helps)
    cp3 = run_python(python_exe, ["-m", "pip", "install", "--upgrade", "pip"])
    outputs.append(cp3.stdout + cp3.stderr)

    install_cmd = ["-m", "pip", "install", "--upgrade", "blender-asset-tracer"]
    if target_dir:
        target_dir.mkdir(parents=True, exist_ok=True)
        install_cmd += ["--target", str(target_dir)]

    cp4 = run_python(python_exe, install_cmd)
    outputs.append(cp4.stdout + cp4.stderr)

    return cp4.returncode == 0, "\n".join(outputs)


def main():
    blender_dir = ask_blender_dir()
    if not blender_dir:
        return

    blender_py = find_blender_python(blender_dir)
    if not blender_py or not blender_py.exists():
        show_error("Blender Python not found",
                   "Couldn't locate Blender's embedded python.exe.\n\n"
                   "Make sure you selected the folder that contains blender.exe (or a portable Blender folder).")
        return

    # Check if BAT is available (normal import)
    extra_path = None
    if not has_bat(blender_py):
        # Ask to install
        if not ask_yes_no("BAT not found",
                          "blender_asset_tracer (BAT) is not available in Blender's Python.\n\n"
                          "Do you want to install it now?"):
            show_info("Cancelled", "BAT was not installed.")
            return

        # Try normal install first (may fail under Program Files)
        ok, log = install_bat(blender_py, target_dir=None)
        if not ok:
            # 3) Fallback: install to user-writable target directory
            fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BlenderPyPackages" / "BAT"
            ok2, log2 = install_bat(blender_py, target_dir=fallback)
            if not ok2:
                show_error("Install failed",
                           "Tried installing BAT but it failed.\n\n"
                           "Normal install output:\n" + log[-1500:] + "\n\n"
                           "Fallback --target install output:\n" + log2[-1500:])
                return
            extra_path = fallback

        # Verify after install
        if not has_bat(blender_py, extra_pythonpath=extra_path):
            show_error("Install issue",
                       "BAT install seemed to complete, but import still fails.\n"
                       "This usually means Python can't see the install location.")
            return

    # Choose the .blend file; output JSON is determined automatically.
    blend_path = ask_blend_file()
    if not blend_path:
        return

    script_dir = Path(__file__).resolve().parent
    out_dir = script_dir / MATERIAL_OUTPUTS_DIRNAME / blend_path.stem
    out_name = f"{blend_path.stem}.json"
    out_json = out_dir / out_name

    # Run the reader script using Blender's python
    reader_script = Path(__file__).with_name("read_blend.py")
    if not reader_script.exists():
        show_error("Missing script", f"Couldn't find {reader_script.name} next to the launcher script.")
        return

    env = os.environ.copy()
    if extra_path:
        env["PYTHONPATH"] = str(extra_path) + os.pathsep + env.get("PYTHONPATH", "")

    cp = subprocess.run(
        [str(blender_py), str(reader_script), str(blend_path), out_json.name],
        capture_output=True, text=True, env=env
    )

    if cp.returncode != 0:
        show_error("Reader failed", (cp.stderr or cp.stdout or "Unknown error")[-2000:])
        return

    show_info("Done", f"Saved JSON:\n{out_json}\n\nOutput:\n{(cp.stdout or '').strip()[:1200]}")

if __name__ == "__main__":
    main()
