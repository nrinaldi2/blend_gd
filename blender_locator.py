"""
Shared Blender install discovery for the launcher and exporter.
"""

import os
import re
import shutil
from pathlib import Path


def _existing_paths(paths):
    """
    Return unique existing paths, preserving input order.
    """
    existing = []
    seen = set()
    for path in paths:
        if not path:
            continue
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        existing.append(resolved)
    return existing


def _blender_version_key(path: Path) -> tuple:
    """
    Build a sort key so newer Blender installs win when multiple are found.
    """
    version_parts = []
    for part in reversed(path.parts):
        matches = re.findall(r"\d+", part)
        if matches:
            version_parts = [int(match) for match in matches]
            break

    # Prefer 64-bit Program Files over Program Files (x86) when versions tie.
    path_text = os.path.normcase(str(path))
    architecture_rank = 0 if "(x86)" in path_text else 1
    return tuple(version_parts + [architecture_rank])


def _common_blender_search_roots() -> list[Path]:
    """
    Return the standard Windows directories where Blender is commonly installed.
    """
    candidate_roots = []
    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            candidate_roots.append(Path(base) / "Blender Foundation")

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidate_roots.append(Path(local_appdata) / "Programs" / "Blender Foundation")

    return _existing_paths(candidate_roots)


def _iter_blender_exe_candidates(search_root: Path):
    """
    Yield likely blender.exe locations under a known Blender install root.
    """
    direct = search_root / "blender.exe"
    if direct.exists():
        yield direct.resolve()

    try:
        children = sorted(search_root.iterdir(), reverse=True)
    except OSError:
        return

    for child in children:
        if not child.is_dir():
            continue
        candidate = child / "blender.exe"
        if candidate.exists():
            yield candidate.resolve()


def find_common_blender_exe() -> Path | None:
    """
    Locate blender.exe from env, PATH, or common Windows install locations.
    """
    env_blender = os.environ.get("BLENDER_BIN")
    if env_blender:
        env_path = Path(env_blender.strip('"'))
        if env_path.name.lower() == "blender.exe":
            env_candidates = _existing_paths([env_path])
        else:
            env_candidates = _existing_paths(_iter_blender_exe_candidates(env_path))
        if env_candidates:
            return env_candidates[0]

    for executable_name in ("blender.exe", "blender"):
        which_result = shutil.which(executable_name)
        if which_result:
            path_candidates = _existing_paths([Path(which_result).resolve()])
            if path_candidates:
                return path_candidates[0]

    candidates = []
    for search_root in _common_blender_search_roots():
        candidates.extend(_iter_blender_exe_candidates(search_root))

    unique_candidates = _existing_paths(candidates)
    if not unique_candidates:
        return None

    return max(unique_candidates, key=_blender_version_key)


def find_blender_python(blender_dir: Path) -> Path | None:
    """
    Expect user selects folder containing blender.exe OR a parent folder.
    This tries common Blender layouts:
      <dir>\\blender.exe
      <dir>\\<version>\\python\\bin\\python.exe
      <dir>\\python\\bin\\python.exe   (portable builds)
    """
    blender_dir = blender_dir.resolve()

    if (blender_dir / "blender.exe").exists():
        versioned_pythons = []
        for sub in blender_dir.iterdir():
            cand = sub / "python" / "bin" / "python.exe"
            if cand.exists():
                versioned_pythons.append(cand)
        if versioned_pythons:
            return max(versioned_pythons, key=_blender_version_key)
        cand = blender_dir / "python" / "bin" / "python.exe"
        if cand.exists():
            return cand

    candidates = []
    for python_exe in blender_dir.rglob("python.exe"):
        parts = [part.lower() for part in python_exe.parts]
        try:
            idx = parts.index("python")
            if idx + 2 < len(parts) and parts[idx + 1] == "bin" and python_exe.name.lower() == "python.exe":
                rel_depth = len(python_exe.relative_to(blender_dir).parts)
                if rel_depth <= 8:
                    candidates.append(python_exe)
        except ValueError:
            pass

    return max(candidates, key=_blender_version_key) if candidates else None


def find_blender_exe(blender_dir: Path) -> Path | None:
    """
    Resolve blender.exe from the selected Blender folder.
    """
    blender_dir = blender_dir.resolve()

    direct = blender_dir / "blender.exe"
    if direct.exists():
        return direct

    candidates = []
    for blender_exe in blender_dir.rglob("blender.exe"):
        rel_depth = len(blender_exe.relative_to(blender_dir).parts)
        if rel_depth <= 6:
            candidates.append(blender_exe)

    return max(candidates, key=_blender_version_key) if candidates else None
