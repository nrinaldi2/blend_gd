"""
read_blend.py

This script is the Blender side of the workflow.
It reads material and node data from a .blend file,
collects extra details the importer needs, and writes
that information into a JSON export for Godot.

In the workflow, this file is the handoff step.
It does not rebuild materials in Godot. It gathers,
organizes, and exports the Blender material data so
post_import.gd can use it during import.

"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from blender_asset_tracer import blendfile
from blender_asset_tracer.blendfile import iterators

SCHEMA_VERSION = "4.3.0"
TEXTURE_EXPORTS_DIRNAME = "textures"
MATERIAL_OUTPUTS_DIRNAME = "Material Outputs"

NON_GLTF_IDNAMES = {
    "ShaderNodeBsdfToon": "Toon BSDF",
    "ShaderNodeShaderToRGB": "Shader to RGB",
    "ShaderNodeValToRGB": "Color Ramp",
}

IN_OUT_MAP = {
    1: "INPUT",
    2: "OUTPUT",
}

SOCKET_SUBTYPE_MAP = {
    0: "NONE",
    1: "UNSIGNED",
    14: "ANGLE",
    15: "FACTOR",
    16: "PERCENTAGE",
}

SOCKET_TYPE_SUBTYPE_FALLBACK = {
    0: "FLOAT",
    1: "VECTOR",
    2: "COLOR",
    3: "SHADER",
}

BLEND_METHOD_MAP = {
    0: "OPAQUE",
    1: "CLIP",
    2: "HASHED",
    3: "BLEND",
    4: "BLEND",
}

TOON_COMPONENT_MAP = {
    0: "DIFFUSE",
    1: "GLOSSY",
}

MIX_BLEND_TYPE_MAP = {
    0: "MIX",
    1: "DARKEN",
    2: "MULTIPLY",
    3: "BURN",
    4: "LIGHTEN",
    5: "SCREEN",
    6: "DODGE",
    7: "ADD",
    8: "OVERLAY",
    9: "SOFT_LIGHT",
    10: "LINEAR_LIGHT",
    11: "DIFFERENCE",
    12: "EXCLUSION",
    13: "SUBTRACT",
    14: "DIVIDE",
    15: "HUE",
    16: "SATURATION",
    17: "COLOR",
    18: "VALUE",
}

COLOR_RAMP_INTERPOLATION_MAP = {
    0: "LINEAR",
    1: "EASE",
    2: "B_SPLINE",
    3: "CARDINAL",
    4: "CONSTANT",
}

COLOR_RAMP_HUE_MAP = {
    0: "NEAR",
    1: "FAR",
    2: "CW",
    3: "CCW",
}

COLOR_RAMP_COLOR_MODE_MAP = {
    0: "RGB",
    1: "HSV",
    2: "HSL",
}

NODE_MUTED_BIT = 8
NODE_DO_OUTPUT_BIT = 64
MA_BL_CULL_BACKFACE = 64

SOCKET_TYPE_NAME_MAP = {
    0: "FLOAT",
    1: "VECTOR",
    2: "COLOR",
    3: "SHADER",
    4: "BOOLEAN",
    5: "INT",
    6: "STRING",
    7: "OBJECT",
    8: "IMAGE",
    9: "GEOMETRY",
    10: "COLLECTION",
    11: "TEXTURE",
    12: "MATERIAL",
    13: "ROTATION",
    14: "MENU",
}


def as_str(value):
    """
    Decode Blender strings that BAT gives back as bytes.

    Many fields in a .blend are stored as null-terminated bytes. Converting
    them early keeps the exported JSON readable and makes downstream mapping to
    Godot material parameters easier to debug.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return value

def add_warning(warnings, message):
    """
    Add a warning once and keep the original order.
    """
    if message not in warnings:
        warnings.append(message)

def _material_outputs_dir(blend_path: Path) -> Path:
    """
    Choose the per-blend output folder for JSON and exported assets.

    Keeping exports under Material Outputs/<blend-stem>/ makes it easy to
    keep each JSON file and its exported textures together.
    """
    base_dir = Path(__file__).resolve().parent
    return base_dir / MATERIAL_OUTPUTS_DIRNAME / blend_path.stem

def _clean_output_filename(name: str, *, default_suffix: str | None = None) -> str:
    """
    Normalize an output filename for use under the export directory.

    Keep the basename so callers cannot accidentally (or intentionally)
    write outside Material Outputs/<blend-stem>/. When a suffix is missing, 
    apply a default (.json) to keep outputs consistent.
    """
    filename = Path(name).name

    out_path = Path(filename)
    if default_suffix and out_path.suffix == "":
        return f"{out_path.name}{default_suffix}"
    return out_path.name

def _resolve_output_json_path(blend_path: Path, out_arg: str | None) -> Path:
    """
    Resolve and create the JSON output path for a .blend export.
    """
    out_dir = _material_outputs_dir(blend_path)
    out_name = (
        _clean_output_filename(out_arg, default_suffix=".json")
        if out_arg
        else f"{blend_path.stem}.json"
    )
    out_path = out_dir / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return out_path

def _first_pointer(block, pointer_names):
    """
    Try a few possible DNA pointer field names and return the first that works.

    Blender's internal structs can shift across versions. This helper makes the
    extractor more tolerant so Blender-to-Godot conversion code keeps working even
    when a pointer path changes name or moves.
    """
    for pointer_name in pointer_names:
        try:
            pointer = block.get_pointer(pointer_name)
            if pointer:
                return pointer
        except Exception:
            pass
    return None

def _iter_listbase(head):
    """
    Safely iterate a Blender ListBase through blender-asset-tracer.

    Node trees, sockets, and links are stored as linked lists in Blender. BAT
    exposes them via a "first" pointer; this wrapper keeps the exporter from
    blowing up on missing/invalid list pointers.
    """
    if not head:
        return iter(())
    try:
        return iterators.listbase(head)
    except Exception:
        return iter(())

def _safe_filename_part(value: str) -> str:
    """
    Convert arbitrary text into a filesystem-safe filename component.

    This is used when the exporter needs a safe and readable filename.
    For this project, stability matters more than preserving exact punctuation.
    """
    cleaned = "".join(char if (char.isalnum() or char in ("-", "_", ".")) else "_" for char in value.strip())
    cleaned = cleaned.strip("._")
    return cleaned or "material"

def id_name(block) -> str:
    """
    Return a datablock name without Blender's 2-character ID prefix.

    Blender stores names as IDCode + Name (for example MA + material name).
    Stripping the prefix produces stable, more easily readable names for JSON output
    and for any later Godot import step.
    """
    try:
        return block.id_name[2:].decode("utf-8", errors="replace")
    except Exception:
        return "<unknown>"

def normalize_value(value):
    """
    Recursively normalize values into JSON-friendly Python primitives.

    BAT values can be a mix of bytes, arrays, and numeric types. Normalizing
    here keeps the export schema clean so downstream Blender-to-Godot conversion
    code doesn't need to know about BAT internals.
    """
    if isinstance(value, bytes):
        return as_str(value)
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value

def enum_or_unknown(code, mapping, enum_name, warnings):
    """
    Map a Blender enum integer to a readable string, recording unknown values.

    Unknown enums are valuable signals for this project: they usually mean the
    Blender version introduced a new option and the Godot conversion layer needs
    an explicit mapping (instead of silently guessing).
    """
    if code in mapping:
        return mapping[code]
    warnings.append(f"Unknown {enum_name} enum value: {code}")
    return f"UNKNOWN_{code}"

def blender_version_string(blend_file) -> str:
    """
    Build a Blender version string from the .blend header.

    Recording the Blender version alongside extracted materials makes it easier
    to interpret enum meanings and struct changes during later translation of the
    snapshot into Godot material properties.
    """
    major = int(getattr(blend_file.header, "version", 0) // 100)
    minor = int(getattr(blend_file.header, "version", 0) % 100)
    patch = int(getattr(blend_file, "file_subversion", 0))
    return f"{major}.{minor}.{patch}"

def ptr_to_ref(blend_file, ptr):
    """
    Resolve an old pointer address to a stable {code, name} reference.

    Pointers in .blend files are only meaningful within the file. Converting a
    pointer to an ID code + datablock name makes the JSON easier to inspect and
    supports linking nodes to images and other assets for a later Godot import.
    """
    block = blend_file.block_from_addr.get(ptr) if ptr else None
    if not block:
        return None
    return {"code": as_str(block.id_name[:2]), "name": id_name(block)}

def decode_socket_default(sock):
    """
    Decode a socket's default_value struct into usable values.

    Defaults matter when a socket is not linked: those values are the starting
    point for a Godot material parameter when recreating a Blender graph. Blender
    stores defaults behind the default_value pointer, and the exact fields vary
    by socket type. Reading this out in the graph returns user set values in the 
    node network each respective node.
    """
    decoded = {}
    subtype_code = None
    try:
        default_ptr = sock.get_pointer(b"default_value")
    except Exception:
        default_ptr = None
    if not default_ptr:
        return decoded, subtype_code

    try:
        decoded["default"] = normalize_value(default_ptr.get(b"value"))
    except Exception:
        pass

    key_pairs = (
        (b"min", "min"),
        (b"max", "max"),
        (b"soft_min", "soft_min"),
        (b"soft_max", "soft_max"),
        (b"softmin", "soft_min"),
        (b"softmax", "soft_max"),
    )
    for dna_key, json_key in key_pairs:
        if json_key in decoded:
            continue
        try:
            decoded[json_key] = normalize_value(default_ptr.get(dna_key))
        except Exception:
            pass

    try:
        subtype_code = int(default_ptr.get(b"subtype"))
    except Exception:
        subtype_code = None

    return decoded, subtype_code

def socket_subtype_name(sock_type, subtype_code):
    """
    Pick a readable subtype name for a socket.

    Subtypes (angle, factor, percentage, etc.) help interpret numeric defaults
    when translating Blender sockets into Godot material parameters.
    """
    if subtype_code in SOCKET_SUBTYPE_MAP:
        return SOCKET_SUBTYPE_MAP[subtype_code]
    return SOCKET_TYPE_SUBTYPE_FALLBACK.get(sock_type, "UNKNOWN")

def extract_socket(sock, linked_socket_ptrs, link_counts):
    """
    Serialize a bNodeSocket into the target JSON schema.

    This includes default values and link metadata, which downstream conversion
    needs to decide whether a Godot parameter should use the socket default or be
    driven by an upstream node connection.
    """
    ptr = getattr(sock, "addr_old", None)
    sock_type = sock.get(b"type", None)

    default_data, subtype_code = decode_socket_default(sock)
    in_out_raw = sock.get(b"in_out", None)
    direction = IN_OUT_MAP.get(in_out_raw, in_out_raw)
    info = {
        "ptr": ptr,
        "name": as_str(sock.get(b"name", as_str=True)),
        "identifier": as_str(sock.get(b"identifier", as_str=True)),
        "socket_type": SOCKET_TYPE_NAME_MAP.get(sock_type, f"UNKNOWN_{sock_type}" if sock_type is not None else None),
        "socket_type_code": sock_type,
        "subtype": socket_subtype_name(sock_type, subtype_code),
        "direction": direction,
        "is_linked": bool(ptr in linked_socket_ptrs) if ptr else False,
        "link_count": int(link_counts.get(ptr, 0)) if ptr else 0,
    }

    for key in ("default", "min", "max", "soft_min", "soft_max"):
        if key in default_data:
            info[key] = default_data[key]

    return info

def _color_ramp_point_from_elem(elem):
    """
    Turn one Color Ramp stop into JSON-friendly data.
    """
    return {
        "pos": float(elem.get(b"pos", 0.0)),
        "color": [
            float(elem.get(b"r", 0.0)),
            float(elem.get(b"g", 0.0)),
            float(elem.get(b"b", 0.0)),
            float(elem.get(b"a", 1.0)),
        ],
    }

def extract_color_ramp(node, warnings):
    """
    Pull Color Ramp settings and stop data from a ramp node.

    This exporter prefers blender-asset-tracer for speed, but BAT may not expose
    ColorBand.data cleanly in every Blender version. To keep that failure visible
    to the later Godot conversion step, the exporter records debug metadata even
    when stop extraction fails.
    """
    try:
        ramp = node.get_pointer(b"storage")
    except Exception:
        ramp = None
    if not ramp or ramp.dna_type_name != "ColorBand":
        return None

    node_name = as_str(node.get(b"name", as_str=True)) or "<unnamed Color Ramp>"
    settings = {
        "interpolation": enum_or_unknown(
            ramp.get(b"ipotype", 0),
            COLOR_RAMP_INTERPOLATION_MAP,
            "ColorRamp.interpolation",
            warnings,
        ),
        "color_mode": enum_or_unknown(
            ramp.get(b"color_mode", 0),
            COLOR_RAMP_COLOR_MODE_MAP,
            "ColorRamp.color_mode",
            warnings,
        ),
        "hue_interpolation": enum_or_unknown(
            ramp.get(b"ipotype_hue", 0),
            COLOR_RAMP_HUE_MAP,
            "ColorRamp.hue_interpolation",
            warnings,
        ),
    }

    try:
        total = int(ramp.get(b"tot", 0))
    except Exception as exc:
        add_warning(warnings, f"ColorRamp '{node_name}': could not read stop count ({exc})")
        total = 0

    points = []
    extraction_ok = False
    source = None
    errors = []

    try:
        elements = ramp.get(b"data", [])
        candidate_points = [_color_ramp_point_from_elem(elem) for elem in list(elements)[:total]]
        if len(candidate_points) == total:
            points = candidate_points
            extraction_ok = True
            source = "bat_bulk"
        elif total > 0:
            errors.append(f"bulk read returned {len(candidate_points)} point(s) for expected {total}")
    except Exception as exc:
        errors.append(f"bulk read failed: {exc}")

    if not extraction_ok and total > 0:
        candidate_points = []
        indexed_ok = True
        for idx in range(total):
            try:
                elem = ramp.get((b"data", idx))
                candidate_points.append(_color_ramp_point_from_elem(elem))
            except Exception as exc:
                indexed_ok = False
                errors.append(f"indexed read failed at stop {idx}: {exc}")
                break
        if indexed_ok and len(candidate_points) == total:
            points = candidate_points
            extraction_ok = True
            source = "bat_indexed"

    if total > 0 and not extraction_ok:
        add_warning(
            warnings,
            f"ColorRamp '{node_name}': expected {total} stop(s) but BAT could not export ColorBand.data",
        )
        for error in errors:
            add_warning(warnings, f"ColorRamp '{node_name}': {error}")

    return {
        "settings": settings,
        "points": points,
        "debug": {
            "expected_stop_count": total,
            "exported_stop_count": len(points),
            "points_extracted": extraction_ok,
            "source": source or "unresolved",
            "errors": errors,
        },
    }

def _infer_image_export_name(linked_image: dict, fallback_name: str) -> str:
    """
    Choose a stable PNG filename for an exported Blender image.

    Exporting to PNG gives the Godot post-import step a concrete resource file
    to load from JSON, instead of depending on whatever texture reference the
    intermediate glTF import happened to preserve.
    """
    original = str((linked_image or {}).get("filepath") or fallback_name or "image").strip()
    candidate = Path(original).name or str(fallback_name or "image")
    stem = Path(candidate).stem or Path(candidate).name or "image"
    return f"{_safe_filename_part(stem)}.png"

def _collect_image_export_requests(export_payload, textures_dirname: str = TEXTURE_EXPORTS_DIRNAME):
    """
    Scan serialized materials for Image Texture nodes that need real file exports.

    BAT can tell us which Blender image datablock a node points at, but packed
    or unresolved images still need a bpy pass to become concrete PNG files that
    Godot can import and load by path.
    """
    requests = []
    used_names = {}

    for material in export_payload.get("materials", []):
        node_graph = material.get("node_graph") or {}
        node_tree_name = material.get("node_tree_name") or node_graph.get("name")
        for node in node_graph.get("nodes", []):
            if node.get("idname") != "ShaderNodeTexImage":
                continue

            linked_image = node.get("linked_image") or {}
            linked_id = node.get("linked_id") or {}
            image_name = str(linked_id.get("name") or node.get("ui_name") or "image")

            base_filename = _infer_image_export_name(linked_image, image_name)
            stem = Path(base_filename).stem
            suffix = Path(base_filename).suffix or ".png"
            index = used_names.get(base_filename, 0)
            used_names[base_filename] = index + 1
            final_filename = f"{stem}_{index}{suffix}" if index else f"{stem}{suffix}"
            relative_path = str(Path(textures_dirname) / final_filename).replace("\\", "/")

            export_info = linked_image.setdefault("export", {})
            export_info["relative_path"] = relative_path
            export_info["source"] = "bpy_enrichment"
            export_info["exported"] = False

            requests.append(
                {
                    "material_name": material.get("name"),
                    "node_tree_name": node_tree_name,
                    "node_ui_name": node.get("ui_name"),
                    "image_name": image_name,
                    "relative_path": relative_path,
                }
            )

    return requests

def _export_linked_images_via_bpy(blend_path: Path, out_dir: Path, requests, warnings):
    """
    Export Blender Image Texture node images to concrete PNG files via bpy.
    """
    if not requests:
        return {}

    blender_bin = _find_blender_executable()
    if not blender_bin:
        add_warning(warnings, "Image export enrichment: Blender executable not found; set BLENDER_BIN to enable bpy image export")
        return {}

    helper_code = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        import bpy

        def _write_image_as_png(image, target_path: Path):
            target_path.parent.mkdir(parents=True, exist_ok=True)

            original_filepath_raw = getattr(image, "filepath_raw", "")
            original_file_format = getattr(image, "file_format", "PNG")

            try:
                image.filepath_raw = str(target_path)
                image.file_format = "PNG"
                image.save()
            except Exception:
                try:
                    image.save_render(filepath=str(target_path))
                except Exception as exc:
                    raise exc
            finally:
                try:
                    image.filepath_raw = original_filepath_raw
                except Exception:
                    pass
                try:
                    image.file_format = original_file_format
                except Exception:
                    pass

        def main():
            args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
            manifest_path = Path(args[0])
            out_dir = Path(args[1])
            result_path = Path(args[2])

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            results = []

            for request in manifest.get("requests", []):
                material_name = request.get("material_name")
                node_tree_name = request.get("node_tree_name")
                node_ui_name = request.get("node_ui_name")
                relative_path = request.get("relative_path")

                result = {
                    "material_name": material_name,
                    "node_tree_name": node_tree_name,
                    "node_ui_name": node_ui_name,
                    "relative_path": relative_path,
                    "exported": False,
                }

                try:
                    material = bpy.data.materials.get(material_name)
                    if material is None:
                        raise RuntimeError(f"material not found: {material_name}")
                    if material.node_tree is None:
                        raise RuntimeError(f"material has no node tree: {material_name}")

                    node = material.node_tree.nodes.get(node_ui_name)
                    if node is None:
                        raise RuntimeError(f"node not found: {node_ui_name}")
                    if getattr(node, "bl_idname", "") != "ShaderNodeTexImage":
                        raise RuntimeError(f"node is not ShaderNodeTexImage: {node_ui_name}")

                    image = getattr(node, "image", None)
                    if image is None:
                        raise RuntimeError(f"image is missing on node: {node_ui_name}")

                    target_path = out_dir / relative_path
                    _write_image_as_png(image, target_path)
                    if not target_path.exists():
                        raise RuntimeError(f"image export did not create file: {target_path}")

                    result["exported"] = True
                    result["absolute_path"] = str(target_path)
                    result["width"] = int(image.size[0]) if len(image.size) >= 1 else 0
                    result["height"] = int(image.size[1]) if len(image.size) >= 2 else 0
                    result["colorspace"] = str(getattr(image.colorspace_settings, "name", ""))
                except Exception as exc:
                    result["error"] = str(exc)

                results.append(result)

            result_path.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")

        if __name__ == "__main__":
            main()
        """
    )

    with tempfile.TemporaryDirectory(prefix="blend_image_export_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        helper_path = tmp_path / "export_linked_images_bpy.py"
        manifest_path = tmp_path / "image_export_requests.json"
        result_path = tmp_path / "image_export_results.json"
        helper_path.write_text(helper_code, encoding="utf-8")
        manifest_path.write_text(json.dumps({"requests": requests}, indent=2), encoding="utf-8")

        cmd = [
            blender_bin,
            "--background",
            str(blend_path),
            "--python",
            str(helper_path),
            "--",
            str(manifest_path),
            str(out_dir),
            str(result_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            error_text = (completed.stderr or completed.stdout or "unknown Blender error").strip()
            add_warning(warnings, f"Image export enrichment via Blender failed: {error_text.splitlines()[-1]}")
            return {}

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            add_warning(warnings, f"Image export enrichment: could not read bpy JSON output ({exc})")
            return {}

    results = {}
    for entry in payload.get("results", []):
        key = (
            entry.get("material_name"),
            entry.get("node_tree_name"),
            entry.get("node_ui_name"),
        )
        results[key] = entry
    return results

def enrich_linked_images_with_bpy(export_payload, blend_path: Path, out_dir: Path, warnings):
    """
    Replace abstract linked-image metadata with real exported PNG paths.
    """
    requests = _collect_image_export_requests(export_payload)
    if not requests:
        return

    results = _export_linked_images_via_bpy(blend_path, out_dir, requests, warnings)
    if not results:
        return

    exported_count = 0
    for material in export_payload.get("materials", []):
        node_graph = material.get("node_graph") or {}
        node_tree_name = material.get("node_tree_name") or node_graph.get("name")
        for node in node_graph.get("nodes", []):
            if node.get("idname") != "ShaderNodeTexImage":
                continue

            key = (material.get("name"), node_tree_name, node.get("ui_name"))
            result = results.get(key)
            if not result:
                continue

            linked_image = node.setdefault("linked_image", {})
            export_info = linked_image.setdefault("export", {})
            export_info["relative_path"] = result.get("relative_path") or export_info.get("relative_path")
            export_info["source"] = "bpy_enrichment"
            export_info["exported"] = bool(result.get("exported"))

            if result.get("exported"):
                if result.get("width") is not None:
                    export_info["width"] = int(result.get("width"))
                if result.get("height") is not None:
                    export_info["height"] = int(result.get("height"))
                if result.get("colorspace"):
                    export_info["colorspace"] = result.get("colorspace")
                exported_count += 1
            else:
                error_message = result.get("error") or "unknown bpy image export error"
                export_info["error"] = error_message
                add_warning(
                    warnings,
                    f"Image export enrichment: failed to export image for material '{material.get('name')}', node '{node.get('ui_name')}' ({error_message})",
                )

def _find_blender_executable():
    """
    Try to locate a Blender executable for optional bpy enrichment extraction.
    """
    candidates = []
    env_value = os.environ.get("BLENDER_BIN")
    if env_value:
        candidates.append(env_value)

    for command in ("blender", "blender.exe"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(resolved)

    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            return candidate
    return None

def _extract_color_ramps_via_bpy(blend_path: Path, warnings):
    """
    Use a small bpy helper to pull Color Ramp stop data when BAT misses it.

    BAT remains the primary extractor for speed and broad schema coverage. This
    enrichment only runs when BAT reported ColorBand settings but could not expose
    the stop array.
    """
    blender_bin = _find_blender_executable()
    if not blender_bin:
        add_warning(warnings, "ColorRamp enrichment: Blender executable not found; set BLENDER_BIN to enable bpy extraction")
        return {}

    helper_code = textwrap.dedent("""
        import json
        import sys
        from pathlib import Path

        import bpy

        def main():
            args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
            out_path = Path(args[0])

            payload = {"ramps": []}
            for material in bpy.data.materials:
                node_tree = material.node_tree
                if not node_tree:
                    continue
                for node in node_tree.nodes:
                    if getattr(node, "bl_idname", "") != "ShaderNodeValToRGB":
                        continue
                    ramp = node.color_ramp
                    payload["ramps"].append({
                        "material_name": material.name,
                        "node_tree_name": node_tree.name,
                        "node_ui_name": node.name,
                        "ramp_settings": {
                            "interpolation": str(getattr(ramp, "interpolation", "LINEAR")),
                            "color_mode": str(getattr(ramp, "color_mode", "RGB")),
                            "hue_interpolation": str(getattr(ramp, "hue_interpolation", "NEAR")),
                        },
                        "color_ramp": [
                            {
                                "pos": float(element.position),
                                "color": [float(channel) for channel in element.color],
                            }
                            for element in ramp.elements
                        ],
                    })

            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        if __name__ == "__main__":
            main()
    """)

    with tempfile.TemporaryDirectory(prefix="blend_ramp_enrichment_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        helper_path = tmp_path / "extract_color_ramps_bpy.py"
        result_path = tmp_path / "color_ramps.json"
        helper_path.write_text(helper_code, encoding="utf-8")

        cmd = [
            blender_bin,
            "--background",
            str(blend_path),
            "--python",
            str(helper_path),
            "--",
            str(result_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            error_text = (completed.stderr or completed.stdout or "unknown Blender error").strip()
            add_warning(warnings, f"ColorRamp enrichment via Blender failed: {error_text.splitlines()[-1]}")
            return {}

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            add_warning(warnings, f"ColorRamp enrichment: could not read bpy JSON output ({exc})")
            return {}

    ramps = {}
    for entry in payload.get("ramps", []):
        key = (
            entry.get("material_name"),
            entry.get("node_tree_name"),
            entry.get("node_ui_name"),
        )
        ramps[key] = entry
    return ramps

def enrich_color_ramps_with_bpy(export_payload, blend_path: Path, warnings):
    """
    Fill in missing Color Ramp stops with a small bpy pass when Blender is available.
    """
    missing_keys = []
    for material in export_payload.get("materials", []):
        node_graph = material.get("node_graph") or {}
        node_tree_name = material.get("node_tree_name") or node_graph.get("name")
        for node in node_graph.get("nodes", []):
            if node.get("idname") != "ShaderNodeValToRGB":
                continue
            debug = node.get("color_ramp_debug") or {}
            if debug.get("points_extracted"):
                continue
            if int(debug.get("expected_stop_count", 0) or 0) <= 0:
                continue
            missing_keys.append((material.get("name"), node_tree_name, node.get("ui_name")))

    if not missing_keys:
        return

    bpy_ramps = _extract_color_ramps_via_bpy(blend_path, warnings)
    if not bpy_ramps:
        return

    patched = 0
    for material in export_payload.get("materials", []):
        node_graph = material.get("node_graph") or {}
        node_tree_name = material.get("node_tree_name") or node_graph.get("name")
        for node in node_graph.get("nodes", []):
            if node.get("idname") != "ShaderNodeValToRGB":
                continue

            key = (material.get("name"), node_tree_name, node.get("ui_name"))
            bpy_entry = bpy_ramps.get(key)
            if not bpy_entry:
                continue

            points = bpy_entry.get("color_ramp") or []
            if points:
                node["color_ramp"] = points
                debug = node.setdefault("color_ramp_debug", {})
                debug["expected_stop_count"] = len(points)
                debug["exported_stop_count"] = len(points)
                debug["points_extracted"] = True
                debug["source"] = "bpy_enrichment"
                if "errors" in debug and debug["errors"]:
                    debug["bat_errors"] = debug.pop("errors")
                else:
                    debug.pop("errors", None)

                properties = node.setdefault("properties", {})
                bpy_settings = bpy_entry.get("ramp_settings") or {}
                if bpy_settings and not properties.get("ramp_settings"):
                    properties["ramp_settings"] = bpy_settings
                patched += 1

def extract_linked_image(node, blend_file):
    """
    Extract image metadata for an Image Texture node.

    When converting materials, the image filepath, packed status, and colorspace
    affect how textures should be imported and interpreted in Godot.
    """
    try:
        image_ptr = node.get(b"id", 0)
    except Exception:
        image_ptr = 0
    image_block = blend_file.block_from_addr.get(image_ptr) if image_ptr else None
    if not image_block or as_str(image_block.id_name[:2]) != "IM":
        return None

    filepath = None
    for key in (b"filepath", b"name"):
        try:
            value = as_str(image_block.get(key, as_str=True))
            if value:
                filepath = value
                break
        except Exception:
            pass
    if not filepath:
        filepath = id_name(image_block)

    try:
        colorspace = as_str(image_block.get((b"colorspace_settings", b"name"), as_str=True))
    except Exception:
        colorspace = None

    is_packed = False
    try:
        is_packed = bool(image_block.get(b"packedfile", 0))
    except Exception:
        pass
    if not is_packed:
        try:
            packed_head = image_block.get_pointer((b"packedfiles", b"first"))
            is_packed = bool(packed_head)
        except Exception:
            pass

    return {
        "filepath": filepath,
        "is_packed": is_packed,
        "colorspace": colorspace,
    }

def extract_node_properties(node, idname, warnings, ramp_payload=None):
    """
    Extract node-type-specific properties.

    Sockets and links describe most nodes, but some Blender nodes carry extra
    settings that matter for non-glTF behavior (toon component, mix blend type,
    clamp flags, ramp interpolation modes, texture sampling hints). Exporting
    these makes a Blender-to-Godot conversion step more faithful.
    """
    properties = {}

    if idname == "ShaderNodeBsdfToon":
        component_code = int(node.get(b"custom1", 0))
        properties["component"] = enum_or_unknown(
            component_code,
            TOON_COMPONENT_MAP,
            "ToonBSDF.component",
            warnings,
        )

    if idname == "ShaderNodeMixRGB":
        blend_code = int(node.get(b"custom1", 0))
        properties["blend_type"] = enum_or_unknown(
            blend_code,
            MIX_BLEND_TYPE_MAP,
            "MixRGB.blend_type",
            warnings,
        )
        properties["use_clamp"] = bool(node.get(b"custom2", 0))
        properties.setdefault("translation_hint", "evaluate_mix_rgb")

    if idname == "ShaderNodeMix":
        try:
            storage = node.get_pointer(b"storage")
        except Exception:
            storage = None
        if storage and storage.dna_type_name == "NodeShaderMix":
            blend_code = int(storage.get(b"blend_type", 0))
            properties["blend_type"] = enum_or_unknown(
                blend_code,
                MIX_BLEND_TYPE_MAP,
                "Mix.blend_type",
                warnings,
            )
            properties["use_clamp"] = bool(storage.get(b"clamp_result", 0) or storage.get(b"clamp_factor", 0))
            try:
                data_type = as_str(storage.get(b"data_type", as_str=True))
                if data_type:
                    properties["data_type"] = data_type
            except Exception:
                pass
            try:
                factor_mode = as_str(storage.get(b"factor_mode", as_str=True))
                if factor_mode:
                    properties["factor_mode"] = factor_mode
            except Exception:
                pass
        properties.setdefault("translation_hint", "evaluate_mix")

    if idname == "ShaderNodeTexImage":
        properties.setdefault("translation_hint", "sample_texture")

    if idname == "ShaderNodeRGB":
        properties.setdefault("translation_hint", "use_rgb_constant")

    if idname == "ShaderNodeValue":
        properties.setdefault("translation_hint", "use_value_constant")

    if idname == "ShaderNodeMath":
        properties.setdefault("translation_hint", "evaluate_math")

    if idname == "ShaderNodeVectorMath":
        properties.setdefault("translation_hint", "evaluate_vector_math")

    if idname == "ShaderNodeTexCoord":
        properties.setdefault("translation_hint", "use_texture_coordinates")

    if idname == "ShaderNodeUVMap":
        properties.setdefault("translation_hint", "use_uv_map")

    if idname == "ShaderNodeMapping":
        properties.setdefault("translation_hint", "apply_mapping")

    if idname == "ShaderNodeNormalMap":
        properties.setdefault("translation_hint", "decode_normal_map")

    if idname == "ShaderNodeValToRGB" and ramp_payload and ramp_payload.get("settings"):
        properties["ramp_settings"] = ramp_payload["settings"]
        properties.setdefault("translation_hint", "evaluate_color_ramp")

    if idname == "ShaderNodeFresnel":
        properties.setdefault("translation_hint", "generate_fresnel_expression")

    if idname == "ShaderNodeBsdfPrincipled":
        properties.setdefault("translation_hint", "first_pass_principled_bsdf")

    if idname == "ShaderNodeShaderToRGB":
        properties["requires_shader_generation"] = True
        properties["approximation_required"] = True
        properties.setdefault("translation_hint", "shader_to_rgb_approximation")

    return properties

def extract_node(node, blend_file, linked_socket_ptrs, link_counts, warnings):
    """
    Serialize a node including sockets, linked assets, and special node data.

    The exported node contains enough information to:
    - visualize the graph (debugging extraction and conversion)
    - detect Blender-only nodes that glTF will drop
    - carry the extra flags/enums needed when mapping into Godot materials
    """
    idname = as_str(node.get(b"idname", as_str=True))
    node_flag = int(node.get(b"flag", 0))
    node_data = {
        "ptr": getattr(node, "addr_old", None),
        "idname": idname,
        "ui_name": as_str(node.get(b"name", as_str=True)),
        "label": as_str(node.get(b"label", as_str=True)),
        "type": node.get(b"type", None),
        "loc": [float(node.get(b"locx", 0.0)), float(node.get(b"locy", 0.0))],
        "width": node.get(b"width", None),
        "height": node.get(b"height", None),
        "mute": bool(node_flag & NODE_MUTED_BIT),
        "inputs": [],
        "outputs": [],
    }

    try:
        linked_id_ptr = node.get(b"id", 0)
    except Exception:
        linked_id_ptr = 0
    if linked_id_ptr:
        linked_ref = ptr_to_ref(blend_file, linked_id_ptr)
        if linked_ref:
            node_data["linked_id"] = linked_ref
            if linked_ref.get("code") == "IM":
                linked_image = extract_linked_image(node, blend_file)
                if linked_image:
                    node_data["linked_image"] = linked_image

    try:
        inputs_head = node.get_pointer((b"inputs", b"first"))
        if inputs_head:
            node_data["inputs"] = [
                extract_socket(socket, linked_socket_ptrs=linked_socket_ptrs, link_counts=link_counts)
                for socket in iterators.listbase(inputs_head)
            ]
    except Exception:
        pass

    try:
        outputs_head = node.get_pointer((b"outputs", b"first"))
        if outputs_head:
            node_data["outputs"] = [
                extract_socket(socket, linked_socket_ptrs=linked_socket_ptrs, link_counts=link_counts)
                for socket in iterators.listbase(outputs_head)
            ]
    except Exception:
        pass

    ramp_payload = extract_color_ramp(node, warnings) if idname == "ShaderNodeValToRGB" else None

    properties = extract_node_properties(node, idname=idname, warnings=warnings, ramp_payload=ramp_payload)
    if properties:
        node_data["properties"] = properties

    if ramp_payload is not None:
        node_data["color_ramp"] = ramp_payload.get("points", [])
        node_data["color_ramp_debug"] = ramp_payload.get("debug", {})

    return node_data, node_flag

def extract_link(link, blend_file):
    """
    Serialize a bNodeLink into JSON-friendly dict form.

    Links connect output sockets to input sockets. Capturing both ends lets
    downstream code trace which branch drives the active output when mapping to
    Godot properties.
    """
    def node_info(ptr):
        block = blend_file.block_from_addr.get(ptr) if ptr else None
        if not block:
            return None
        return {
            "ptr": ptr,
            "ui_name": as_str(block.get(b"name", as_str=True)),
            "idname": as_str(block.get(b"idname", as_str=True)),
            "type": block.get(b"type", None),
        }

    def socket_info(ptr):
        block = blend_file.block_from_addr.get(ptr) if ptr else None
        if not block:
            return None
        in_out_raw = block.get(b"in_out", None)
        return {
            "ptr": ptr,
            "name": as_str(block.get(b"name", as_str=True)),
            "identifier": as_str(block.get(b"identifier", as_str=True)),
            "in_out": IN_OUT_MAP.get(in_out_raw, in_out_raw),
            "type": block.get(b"type", None),
        }

    from_node_ptr = link.get(b"fromnode", 0)
    to_node_ptr = link.get(b"tonode", 0)
    from_socket_ptr = link.get(b"fromsock", 0)
    to_socket_ptr = link.get(b"tosock", 0)

    return {
        "ptr": getattr(link, "addr_old", None),
        "from_node": node_info(from_node_ptr),
        "from_socket": socket_info(from_socket_ptr),
        "to_node": node_info(to_node_ptr),
        "to_socket": socket_info(to_socket_ptr),
        "flag": link.get(b"flag", None),
    }

def extract_material_settings(material_block, warnings):
    """
    Extract per-material render settings used by downstream exporters.

    These are the settings that typically need explicit mapping when moving a
    Blender material into an engine like Godot (alpha handling, culling, etc.),
    especially when glTF behavior does not match Blender exactly.
    """
    blend_method_code = None
    alpha_threshold = None
    blend_flag = 0

    try:
        blend_method_code = int(material_block.get(b"blend_method"))
    except Exception:
        pass
    try:
        alpha_threshold = float(material_block.get(b"alpha_threshold"))
    except Exception:
        pass
    try:
        blend_flag = int(material_block.get(b"blend_flag"))
    except Exception:
        pass

    blend_method = (
        enum_or_unknown(blend_method_code, BLEND_METHOD_MAP, "Material.blend_method", warnings)
        if blend_method_code is not None
        else None
    )

    return {
        "blend_method": blend_method,
        "alpha_threshold": alpha_threshold,
        "use_backface_culling": bool(blend_flag & MA_BL_CULL_BACKFACE),
    }

def extract_active_output(node_entries, output_node_flags):
    """
    Determine the active Material Output node and its socket identifiers.

    Materials can contain multiple output nodes; Blender marks one as the "active"
    output. Picking the right one matters when deciding what should drive the
    Godot material's surface/volume/displacement equivalents.
    """
    output_nodes = [node for node in node_entries if node.get("idname") == "ShaderNodeOutputMaterial"]
    if not output_nodes:
        return None, {}

    active_node = None
    for node in output_nodes:
        if output_node_flags.get(node.get("ptr"), 0) & NODE_DO_OUTPUT_BIT:
            active_node = node
            break
    if not active_node:
        active_node = output_nodes[0]

    active_info = {"node_ptr": active_node.get("ptr")}
    input_sockets = active_node.get("inputs", [])
    socket_by_name = {}
    for socket in input_sockets:
        socket_name = (socket.get("name") or "").strip().lower()
        socket_identifier = socket.get("identifier")
        if socket_name:
            socket_by_name[socket_name] = socket_identifier
        if socket_identifier:
            socket_by_name[str(socket_identifier).strip().lower()] = socket_identifier

    for label in ("surface", "volume", "displacement"):
        identifier = socket_by_name.get(label)
        if identifier:
            active_info[f"{label}_socket_identifier"] = identifier

    return active_info, socket_by_name

def extract_output_targets(active_ptr, links):
    """
    Build mapping of output socket target -> upstream connection.

    This gives downstream conversion code a quick way to answer "what feeds the
    active output Surface/Volume/Displacement sockets?" without repeatedly walking
    the full link list.
    """
    if not active_ptr:
        return {}

    targets = {}
    for link in links:
        to_node = link.get("to_node") or {}
        if to_node.get("ptr") != active_ptr:
            continue
        to_socket = link.get("to_socket") or {}
        from_node = link.get("from_node") or {}
        from_socket = link.get("from_socket") or {}
        target_key = (to_socket.get("identifier") or to_socket.get("name") or "").strip().lower()
        if not target_key:
            continue
        targets[target_key] = {
            "from_node_ptr": from_node.get("ptr"),
            "from_socket_identifier": from_socket.get("identifier"),
        }
    return targets

def _build_socket_id(node_id, direction, socket_index):
    """
    Build a stable per-node socket id for the exported graph schema.
    """
    prefix = "in" if direction == "INPUT" else "out"
    return f"{node_id}:{prefix}:{socket_index:02d}"

def _find_socket_by_identifier(node_entry, direction_key, identifier):
    """
    Find an exported socket by Blender identifier/name within one node entry.
    """
    if not identifier:
        return None
    wanted = str(identifier).strip().lower()
    for socket in node_entry.get(direction_key, []):
        socket_identifier = str(socket.get("identifier") or "").strip().lower()
        socket_name = str(socket.get("name") or "").strip().lower()
        if wanted == socket_identifier or wanted == socket_name:
            return socket
    return None

def _collect_material_context_via_bpy(blend_path: Path, warnings):
    # Collect material usage, targeting, and extra node data through bpy.
    blender_bin = _find_blender_executable()
    if not blender_bin:
        add_warning(warnings, "Material context enrichment: Blender executable not found; set BLENDER_BIN to enable bpy enrichment")
        return {}

    helper_code = textwrap.dedent("""
        import json
        import sys
        from pathlib import Path

        import bpy

        def _material_settings(material):
            settings = {}
            for attr in ("blend_method", "shadow_method", "surface_render_method"):
                if hasattr(material, attr):
                    value = getattr(material, attr)
                    if value not in (None, ""):
                        settings[attr] = str(value)
            if hasattr(material, "alpha_threshold"):
                settings["alpha_threshold"] = float(getattr(material, "alpha_threshold", 0.0))
            if hasattr(material, "use_backface_culling"):
                settings["use_backface_culling"] = bool(getattr(material, "use_backface_culling", False))
            if hasattr(material, "use_backface_culling_shadow"):
                settings["use_backface_culling_shadow"] = bool(getattr(material, "use_backface_culling_shadow", False))
            return settings

        def _mesh_uv_context(obj):
            data = getattr(obj, "data", None)
            uv_layers = getattr(data, "uv_layers", None)
            if uv_layers is None:
                return {}

            layers = []
            active_name = ""
            active_render_name = ""
            for layer in uv_layers:
                entry = {
                    "name": str(getattr(layer, "name", "")),
                    "active": bool(getattr(layer, "active", False)),
                    "active_render": bool(getattr(layer, "active_render", False)),
                }
                layers.append(entry)
                if entry["active"] and not active_name:
                    active_name = entry["name"]
                if entry["active_render"] and not active_render_name:
                    active_render_name = entry["name"]

            return {
                "uv_layers": layers,
                "active_uv_map": active_name,
                "active_render_uv_map": active_render_name,
            }

        def _node_properties(node):
            props = {}
            node_type = getattr(node, "bl_idname", "")

            if node_type == "ShaderNodeTexImage":
                props["interpolation"] = str(getattr(node, "interpolation", ""))
                props["extension"] = str(getattr(node, "extension", ""))
                props["projection"] = str(getattr(node, "projection", ""))
                props["projection_blend"] = float(getattr(node, "projection_blend", 0.0))
                image = getattr(node, "image", None)
                if image is not None:
                    props["image_source"] = str(getattr(image, "source", ""))
            elif node_type == "ShaderNodeMix":
                for attr in ("data_type", "factor_mode", "blend_type"):
                    if hasattr(node, attr):
                        props[attr] = str(getattr(node, attr))
                props["clamp_factor"] = bool(getattr(node, "clamp_factor", False))
                props["clamp_result"] = bool(getattr(node, "clamp_result", False))
            elif node_type == "ShaderNodeMixRGB":
                props["blend_type"] = str(getattr(node, "blend_type", ""))
                props["use_clamp"] = bool(getattr(node, "use_clamp", False))
            elif node_type == "ShaderNodeMath":
                props["operation"] = str(getattr(node, "operation", ""))
                props["use_clamp"] = bool(getattr(node, "use_clamp", False))
            elif node_type == "ShaderNodeVectorMath":
                props["operation"] = str(getattr(node, "operation", ""))
            elif node_type == "ShaderNodeMapping":
                props["vector_type"] = str(getattr(node, "vector_type", ""))
            elif node_type == "ShaderNodeUVMap":
                props["uv_map"] = str(getattr(node, "uv_map", ""))
                props["from_instancer"] = bool(getattr(node, "from_instancer", False))
            elif node_type == "ShaderNodeTexCoord":
                props["from_instancer"] = bool(getattr(node, "from_instancer", False))
                obj = getattr(node, "object", None)
                if obj is not None:
                    props["object_name"] = str(getattr(obj, "name", ""))
            elif node_type == "ShaderNodeBsdfPrincipled":
                props["distribution"] = str(getattr(node, "distribution", ""))
                if hasattr(node, "subsurface_method"):
                    props["subsurface_method"] = str(getattr(node, "subsurface_method", ""))
            elif node_type == "ShaderNodeShaderToRGB":
                props["requires_shader_generation"] = True
                props["approximation_required"] = True
            elif node_type == "ShaderNodeNormalMap":
                props["space"] = str(getattr(node, "space", ""))
                props["uv_map"] = str(getattr(node, "uv_map", ""))

            return {k: v for k, v in props.items() if v not in (None, "")}

        def main():
            args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
            out_path = Path(args[0])
            payload = {"materials": []}

            for material in bpy.data.materials:
                node_tree = getattr(material, "node_tree", None)
                users = []
                for obj in bpy.data.objects:
                    slots = getattr(obj, "material_slots", None) or []
                    for index, slot in enumerate(slots):
                        if getattr(slot, "material", None) != material:
                            continue
                        user_entry = {
                            "object_name": obj.name,
                            "object_type": obj.type,
                            "material_slot_index": index,
                            "material_slot_name": getattr(slot, "name", ""),
                        }
                        user_entry.update(_mesh_uv_context(obj))
                        users.append(user_entry)

                node_props = []
                if node_tree is not None:
                    for node in node_tree.nodes:
                        props = _node_properties(node)
                        if props:
                            node_props.append({
                                "node_ui_name": node.name,
                                "node_type": getattr(node, "bl_idname", ""),
                                "properties": props,
                            })

                payload["materials"].append({
                    "material_name": material.name,
                    "use_nodes": bool(getattr(material, "use_nodes", False)),
                    "node_tree_name": getattr(node_tree, "name", None),
                    "users": users,
                    "node_properties": node_props,
                    "material_settings": _material_settings(material),
                })

            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        if __name__ == "__main__":
            main()
    """)

    with tempfile.TemporaryDirectory(prefix="blend_material_context_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        helper_path = tmp_path / "collect_material_context_bpy.py"
        result_path = tmp_path / "material_context.json"
        helper_path.write_text(helper_code, encoding="utf-8")

        cmd = [
            blender_bin,
            "--background",
            str(blend_path),
            "--python",
            str(helper_path),
            "--",
            str(result_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            error_text = (completed.stderr or completed.stdout or "unknown Blender error").strip()
            add_warning(warnings, f"Material context enrichment via Blender failed: {error_text.splitlines()[-1]}")
            return {}

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            add_warning(warnings, f"Material context enrichment: could not read bpy JSON output ({exc})")
            return {}

    return {entry.get("material_name"): entry for entry in payload.get("materials", []) if entry.get("material_name")}

def enrich_material_context_with_bpy(export_payload, blend_path: Path, warnings):
    # Merge bpy targeting data and per-node properties into the export payload.
    context_by_material = _collect_material_context_via_bpy(blend_path, warnings)
    if not context_by_material:
        return

    for material in export_payload.get("materials", []):
        context = context_by_material.get(material.get("name")) or {}
        users = context.get("users") or []
        material_settings = context.get("material_settings") or {}
        if material_settings:
            merged_settings = dict(material.get("settings") or {})
            merged_settings.update(material_settings)
            material["settings"] = merged_settings

        material["targeting"] = {
            "material_name": material.get("name"),
            "preferred_match": "material_slot_name_then_object_material",
            "object_material_users": users,
            "material_slot_indices": sorted({int(user.get("material_slot_index")) for user in users if user.get("material_slot_index") is not None}),
            "object_names": sorted({str(user.get("object_name")) for user in users if user.get("object_name")}),
        }

        if not material.get("has_nodes"):
            use_nodes = context.get("use_nodes")
            node_tree_name = context.get("node_tree_name")
            reason = "material_is_non_node_based"
            if use_nodes and node_tree_name:
                reason = "bpy_reports_node_tree_present_but_bat_did_not_export_graph"
            material["graph_export_status"] = {
                "exported": False,
                "reason": reason,
                "use_nodes": bool(use_nodes),
                "node_tree_name": node_tree_name,
            }
        else:
            material["graph_export_status"] = {
                "exported": True,
                "reason": "ok",
                "use_nodes": bool(context.get("use_nodes", True)),
                "node_tree_name": context.get("node_tree_name") or material.get("node_tree_name"),
            }

        node_graph = material.get("node_graph") or {}
        if not node_graph:
            continue
        patches = {entry.get("node_ui_name"): entry for entry in context.get("node_properties", []) if entry.get("node_ui_name")}
        for node in node_graph.get("nodes", []):
            patch = patches.get(node.get("ui_name"))
            if not patch:
                continue
            props = node.setdefault("properties", {})
            props.update({k: v for k, v in (patch.get("properties") or {}).items() if v not in (None, "")})

def _graph_features_from_node_graph(node_graph):
    """
    Summarize high-level translator signals from exported node types.
    """
    feature_map = {
        "ShaderNodeTexImage": "image_texture",
        "ShaderNodeValToRGB": "color_ramp",
        "ShaderNodeFresnel": "fresnel",
        "ShaderNodeMix": "mix",
        "ShaderNodeMixRGB": "mix_rgb",
        "ShaderNodeMath": "math",
        "ShaderNodeVectorMath": "vector_math",
        "ShaderNodeBsdfPrincipled": "principled_bsdf",
        "ShaderNodeBsdfDiffuse": "diffuse_bsdf",
        "ShaderNodeBsdfToon": "toon_bsdf",
        "ShaderNodeShaderToRGB": "shader_to_rgb",
        "ShaderNodeRGB": "rgb_constant",
        "ShaderNodeValue": "value_constant",
        "ShaderNodeMapping": "mapping",
        "ShaderNodeUVMap": "uv_map",
        "ShaderNodeTexCoord": "texture_coordinates",
        "ShaderNodeNormalMap": "normal_map",
        "ShaderNodeOutputMaterial": "material_output",
    }
    features = set()
    for node in node_graph.get("nodes", []):
        feature = feature_map.get(node.get("idname"))
        if feature:
            features.add(feature)
    return sorted(features)

def _normalize_source_label(source: str | None) -> str | None:
    # Normalize source labels before they go into the cleaned schema.
    mapping = {
        "bpy_fallback": "bpy_enrichment",
        "bpy_image_export": "bpy_enrichment",
    }
    return mapping.get(source, source)

def _compact_value(value):
    # Remove None and empty strings without stripping out False or 0.
    if isinstance(value, dict):
        compacted = {}
        for key, item in value.items():
            compact_item = _compact_value(item)
            if compact_item is None:
                continue
            if compact_item == {} or compact_item == []:
                continue
            compacted[key] = compact_item
        return compacted
    if isinstance(value, list):
        compacted = []
        for item in value:
            compact_item = _compact_value(item)
            if compact_item is None:
                continue
            if compact_item == {} or compact_item == []:
                continue
            compacted.append(compact_item)
        return compacted
    if value == "":
        return None
    return value

def _normalized_image_binding_from_node(node):
    # Build the cleaned image-binding block from the node's linked image data.
    linked_image = node.get("linked_image") or {}
    export_info = linked_image.get("export") or {}
    linked_id = node.get("linked_id") or {}
    if not (linked_image or export_info or linked_id):
        return None

    image_payload = {
        "kind": "image_texture",
        "image_name": linked_id.get("name") or node.get("ui_name"),
        "filepath": linked_image.get("filepath"),
        "colorspace": linked_image.get("colorspace") or export_info.get("colorspace"),
        "is_packed": linked_image.get("is_packed"),
        "export": {
            "relative_path": export_info.get("relative_path"),
            "exported": export_info.get("exported"),
            "source": _normalize_source_label(export_info.get("source")),
            "width": export_info.get("width"),
            "height": export_info.get("height"),
        },
    }
    return _compact_value(image_payload)

def _translator_route_for_material(material):
    # Summarize the likely importer path for one material.
    node_graph = material.get("node_graph") or {}
    analysis = material.get("analysis") or {}
    output_branches = node_graph.get("output_branches") or {}
    surface_connected = bool((output_branches.get("surface") or {}).get("connected"))

    if not node_graph:
        return "native_material_only"
    if analysis.get("requires_shader_generation"):
        return "generated_shader_required"
    if surface_connected:
        return "standard_material_candidate"
    return "graph_present_but_incomplete"

def _stage1_readiness_for_material(material):
    # Classify how ready a material is for the current graph import path.
    status = material.get("graph_export_status") or {}
    if not status.get("exported"):
        if status.get("reason") == "material_is_non_node_based":
            return "non_node_material"
        return "export_gap"

    node_graph = material.get("node_graph") or {}
    output_branches = node_graph.get("output_branches") or {}
    if not (output_branches.get("surface") or {}).get("connected"):
        return "graph_missing_surface_output"

    unresolved_ramps = []
    unresolved_images = []
    for node in node_graph.get("nodes", []):
        ramp_status = node.get("color_ramp_status") or {}
        if node.get("idname") == "ShaderNodeValToRGB" and not ramp_status.get("points_extracted", True):
            unresolved_ramps.append(node.get("id"))
        image_binding = node.get("image") or _normalized_image_binding_from_node(node)
        if node.get("idname") == "ShaderNodeTexImage" and image_binding:
            export_info = image_binding.get("export") or {}
            if export_info and not export_info.get("relative_path"):
                unresolved_images.append(node.get("id"))

    if unresolved_ramps or unresolved_images:
        return "graph_ready_with_asset_gaps"
    return "ready_for_stage2"

def _collect_material_assets(material_entry):
    """
    Collect exported image assets referenced by a material graph.
    """
    node_graph = material_entry.get("node_graph")
    if not node_graph:
        return None

    images = []
    seen = set()
    for node in node_graph.get("nodes", []):
        image_binding = node.get("image") or _normalized_image_binding_from_node(node)
        if not image_binding:
            continue

        export_info = image_binding.get("export") or {}
        dedupe_key = (node.get("id"), image_binding.get("image_name"), export_info.get("relative_path"))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        images.append(_compact_value({
            "source_node_id": node.get("id"),
            "source_node_ui_name": node.get("ui_name"),
            "image_name": image_binding.get("image_name"),
            "filepath": image_binding.get("filepath"),
            "relative_path": export_info.get("relative_path"),
            "exported": export_info.get("exported"),
            "export_source": export_info.get("source"),
            "colorspace": image_binding.get("colorspace"),
            "width": export_info.get("width"),
            "height": export_info.get("height"),
            "is_packed": image_binding.get("is_packed"),
        }))

    if not images:
        return None
    return {"images": images}

def _reachable_active_subgraph(node_graph):
    # Find the nodes and links that actually feed the active material output.
    active_node_id = node_graph.get("active_output_node_id")
    links = node_graph.get("links") or []
    if not active_node_id:
        return {"reachable_node_ids": [], "reachable_link_ids": [], "topo_order_node_ids": []}

    incoming_by_target = {}
    for link in links:
        incoming_by_target.setdefault(link.get("to_node_id"), []).append(link)

    reachable_nodes = set()
    reachable_links = set()
    stack = [active_node_id]
    while stack:
        current = stack.pop()
        if current in reachable_nodes:
            continue
        reachable_nodes.add(current)
        for link in incoming_by_target.get(current, []):
            if link.get("id"):
                reachable_links.add(link.get("id"))
            source = link.get("from_node_id")
            if source and source not in reachable_nodes:
                stack.append(source)

    indegree = {node_id: 0 for node_id in reachable_nodes}
    downstream = {node_id: [] for node_id in reachable_nodes}
    for link in links:
        src = link.get("from_node_id")
        dst = link.get("to_node_id")
        if src in reachable_nodes and dst in reachable_nodes:
            downstream.setdefault(src, []).append(dst)
            indegree[dst] = indegree.get(dst, 0) + 1

    ordered = []
    queue = sorted([node_id for node_id, degree in indegree.items() if degree == 0])
    while queue:
        node_id = queue.pop(0)
        ordered.append(node_id)
        for child in sorted(downstream.get(node_id, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
                queue.sort()

    if len(ordered) != len(reachable_nodes):
        ordered = [node.get("id") for node in node_graph.get("nodes", []) if node.get("id") in reachable_nodes]

    return {
        "reachable_node_ids": ordered,
        "reachable_link_ids": [link.get("id") for link in links if link.get("id") in reachable_links],
        "topo_order_node_ids": ordered,
    }

def _clean_node_for_main_schema(node):
    # Return the main-schema version of one node without debug-only fields.
    cleaned = {
        "id": node.get("id"),
        "idname": node.get("idname"),
        "ui_name": node.get("ui_name"),
        "label": node.get("label"),
        "muted": bool(node.get("muted", False)),
        "location": node.get("loc"),
        "size": {"width": node.get("width"), "height": node.get("height")},
        "inputs": [],
        "outputs": [],
    }

    if node.get("properties"):
        cleaned["properties"] = node.get("properties")

    image_binding = _normalized_image_binding_from_node(node)
    if image_binding:
        cleaned["image"] = image_binding

    if node.get("color_ramp") is not None:
        cleaned["color_ramp"] = node.get("color_ramp")
        if node.get("color_ramp_debug"):
            debug = node.get("color_ramp_debug") or {}
            cleaned["color_ramp_status"] = {
                "points_extracted": bool(debug.get("points_extracted")),
                "expected_stop_count": int(debug.get("expected_stop_count", 0) or 0),
                "exported_stop_count": int(debug.get("exported_stop_count", 0) or 0),
                "source": _normalize_source_label(debug.get("source")),
                "resolution": "complete" if debug.get("points_extracted") else "incomplete",
                "enriched": _normalize_source_label(debug.get("source")) == "bpy_enrichment",
            }

    for direction_key in ("inputs", "outputs"):
        for socket in node.get(direction_key, []):
            cleaned_socket = {
                "id": socket.get("id"),
                "node_id": socket.get("node_id"),
                "order_index": socket.get("order_index"),
                "name": socket.get("name"),
                "identifier": socket.get("identifier"),
                "socket_type": socket.get("socket_type"),
                "subtype": socket.get("subtype"),
                "direction": socket.get("direction"),
                "is_linked": bool(socket.get("is_linked")),
                "link_count": int(socket.get("link_count", 0) or 0),
            }
            for key in ("default", "min", "max", "soft_min", "soft_max"):
                if key in socket:
                    cleaned_socket[key] = socket.get(key)
            cleaned[direction_key].append(cleaned_socket)

    return _compact_value(cleaned)

def _clean_link_for_main_schema(link):
    # Return the main-schema version of one link without nested BAT pointer data.
    cleaned = {
        "id": link.get("id"),
        "from_node_id": link.get("from_node_id"),
        "to_node_id": link.get("to_node_id"),
        "from_socket_id": link.get("from_socket_id"),
        "to_socket_id": link.get("to_socket_id"),
        "from_socket_identifier": link.get("from_socket_identifier"),
        "to_socket_identifier": link.get("to_socket_identifier"),
        "from_socket_name": link.get("from_socket_name"),
        "to_socket_name": link.get("to_socket_name"),
    }
    return cleaned

def _build_export_report(export_payload, warnings):
    # Build the final report for warnings, enrichments, coverage, and graph gaps.
    cleaned_warnings = list(warnings)
    enrichments = []
    graph_export_gaps = []
    non_node_materials = []

    summary = {
        "total_materials": 0,
        "node_graph_materials": 0,
        "ready_for_stage2": 0,
        "generated_shader_required": 0,
        "standard_material_candidates": 0,
        "non_node_materials": 0,
        "graph_export_gaps": 0,
    }

    for material in export_payload.get("materials", []):
        summary["total_materials"] += 1
        status = material.get("graph_export_status") or {}
        route = (material.get("analysis") or {}).get("translator_route")
        readiness = (material.get("analysis") or {}).get("stage1_readiness")

        if status.get("exported"):
            summary["node_graph_materials"] += 1
        elif status.get("reason") == "material_is_non_node_based":
            non_node_materials.append({
                "material_name": material.get("name"),
                "reason": status.get("reason"),
                "use_nodes": status.get("use_nodes"),
                "translator_route": route,
            })
            summary["non_node_materials"] += 1
        else:
            graph_export_gaps.append({
                "material_name": material.get("name"),
                "reason": status.get("reason"),
                "use_nodes": status.get("use_nodes"),
                "translator_route": route,
            })
            summary["graph_export_gaps"] += 1

        if readiness == "ready_for_stage2":
            summary["ready_for_stage2"] += 1
        if route == "generated_shader_required":
            summary["generated_shader_required"] += 1
        if route == "standard_material_candidate":
            summary["standard_material_candidates"] += 1

        for node in (material.get("node_graph") or {}).get("nodes", []):
            ramp_status = node.get("color_ramp_status") or {}
            if ramp_status.get("points_extracted") and ramp_status.get("source") == "bpy_enrichment":
                enrichments.append({
                    "kind": "color_ramp_enrichment",
                    "material_name": material.get("name"),
                    "node_id": node.get("id"),
                    "node_ui_name": node.get("ui_name"),
                    "source": "bpy_enrichment",
                })
                prefix = f"ColorRamp '{node.get('ui_name')}':"
                cleaned_warnings = [warning for warning in cleaned_warnings if not warning.startswith(prefix)]

        for image in (material.get("assets") or {}).get("images", []):
            if image.get("exported") and image.get("export_source") == "bpy_enrichment":
                enrichments.append({
                    "kind": "image_export_enrichment",
                    "material_name": material.get("name"),
                    "node_id": image.get("source_node_id"),
                    "relative_path": image.get("relative_path"),
                    "source": "bpy_enrichment",
                })

    export_payload["warnings"] = cleaned_warnings
    export_payload["export_report"] = {
        "warnings": cleaned_warnings,
        "enrichments": enrichments,
        "recoveries": enrichments,
        "non_node_materials": non_node_materials,
        "graph_export_gaps": graph_export_gaps,
        "skipped_materials": graph_export_gaps,
        "material_summary": summary,
    }

def finalize_material_graph_schema(export_payload, warnings):
    """
    Upgrade serialized materials to the graph-first stage-1 schema.

    This assigns stable node/socket/link ids, derives material-level analysis,
    builds active-subgraph metadata, and emits a clean main schema by default.
    """
    for material in export_payload.get("materials", []):
        node_graph = material.get("node_graph")
        if not node_graph:
            if "graph_export_status" not in material:
                material["graph_export_status"] = {"exported": False, "reason": "material_is_non_node_based"}
            continue

        nodes = node_graph.get("nodes", [])
        ptr_to_node_id = {}
        ptr_to_socket = {}

        for node_index, node in enumerate(nodes, start=1):
            node_id = f"node_{node_index:04d}"
            node["id"] = node_id
            if "mute" in node and "muted" not in node:
                node["muted"] = bool(node.pop("mute"))
            ptr = node.get("ptr")
            if ptr:
                ptr_to_node_id[ptr] = node_id

            for direction_key in ("inputs", "outputs"):
                for socket_index, socket in enumerate(node.get(direction_key, [])):
                    direction = socket.get("direction") or ("INPUT" if direction_key == "inputs" else "OUTPUT")
                    socket_id = _build_socket_id(node_id, direction, socket_index)
                    socket["id"] = socket_id
                    socket["node_id"] = node_id
                    socket["order_index"] = socket_index
                    socket["direction"] = direction
                    socket_ptr = socket.get("ptr")
                    if socket_ptr:
                        ptr_to_socket[socket_ptr] = {
                            "node_id": node_id,
                            "socket_id": socket_id,
                            "identifier": socket.get("identifier"),
                            "name": socket.get("name"),
                        }

        normalized_links = []
        for link_index, link in enumerate(node_graph.get("links", []), start=1):
            from_node = link.get("from_node") or {}
            to_node = link.get("to_node") or {}
            from_socket = link.get("from_socket") or {}
            to_socket = link.get("to_socket") or {}

            from_socket_ref = ptr_to_socket.get(from_socket.get("ptr"), {})
            to_socket_ref = ptr_to_socket.get(to_socket.get("ptr"), {})

            link["id"] = f"link_{link_index:04d}"
            link["from_node_id"] = ptr_to_node_id.get(from_node.get("ptr"))
            link["to_node_id"] = ptr_to_node_id.get(to_node.get("ptr"))
            link["from_socket_id"] = from_socket_ref.get("socket_id")
            link["to_socket_id"] = to_socket_ref.get("socket_id")
            link["from_socket_identifier"] = from_socket_ref.get("identifier") or from_socket.get("identifier")
            link["to_socket_identifier"] = to_socket_ref.get("identifier") or to_socket.get("identifier")
            link["from_socket_name"] = from_socket_ref.get("name") or from_socket.get("name")
            link["to_socket_name"] = to_socket_ref.get("name") or to_socket.get("name")
            normalized_links.append(link)
        if normalized_links:
            node_graph["links"] = normalized_links

        active_output = node_graph.pop("active_output", None)
        active_node_id = None
        if active_output:
            active_node_id = ptr_to_node_id.get(active_output.get("node_ptr"))
            if active_node_id:
                node_graph["active_output_node_id"] = active_node_id

            active_node = next((node for node in nodes if node.get("id") == active_node_id), None)
            if active_node:
                entry_sockets = {}
                for label in ("surface", "volume", "displacement"):
                    identifier = active_output.get(f"{label}_socket_identifier")
                    socket = _find_socket_by_identifier(active_node, "inputs", identifier)
                    if not socket:
                        continue
                    entry_sockets[label] = {
                        "node_id": active_node_id,
                        "socket_id": socket.get("id"),
                        "socket_identifier": socket.get("identifier"),
                        "socket_name": socket.get("name"),
                    }
                if entry_sockets:
                    node_graph["entry_sockets"] = entry_sockets
                    if entry_sockets.get("surface"):
                        node_graph["entry_socket"] = entry_sockets["surface"]

        output_targets = {}
        for branch in ("surface", "volume", "displacement"):
            target = None
            for link in node_graph.get("links", []):
                if link.get("to_node_id") != active_node_id:
                    continue
                candidate_key = str(link.get("to_socket_identifier") or link.get("to_socket_name") or "").strip().lower()
                if candidate_key == branch:
                    target = {
                        "from_node_id": link.get("from_node_id"),
                        "from_socket_id": link.get("from_socket_id"),
                        "from_socket_identifier": link.get("from_socket_identifier"),
                    }
                    output_targets[branch] = target
                    break
        if output_targets:
            node_graph["output_targets"] = output_targets

        output_branches = {}
        for branch in ("surface", "volume", "displacement"):
            output_branches[branch] = {
                "entry_socket": (node_graph.get("entry_sockets") or {}).get(branch),
                "target": (node_graph.get("output_targets") or {}).get(branch),
                "connected": bool((node_graph.get("output_targets") or {}).get(branch)),
            }
        node_graph["output_branches"] = output_branches

        for non_gltf_node in node_graph.get("non_gltf_nodes", []):
            node_id = ptr_to_node_id.get(non_gltf_node.get("ptr"))
            if node_id:
                non_gltf_node["node_id"] = node_id

        active_subgraph = _reachable_active_subgraph(node_graph)
        node_graph["active_subgraph"] = active_subgraph

        node_types = sorted({node.get("idname") for node in nodes if node.get("idname")})
        material.setdefault("analysis", {})
        material["analysis"].update({
            "translation_mode": "auto",
            "features": _graph_features_from_node_graph(node_graph),
            "node_types": node_types,
            "non_gltf_node_types": sorted({
                node.get("idname")
                for node in node_graph.get("non_gltf_nodes", [])
                if node.get("idname")
            }),
            "active_subgraph_node_count": len(active_subgraph.get("reachable_node_ids", [])),
            "requires_shader_generation": any(
                bool((node.get("properties") or {}).get("requires_shader_generation"))
                for node in nodes
            ),
        })
        material["analysis"]["translator_route"] = _translator_route_for_material(material)

        assets = _collect_material_assets(material)
        if assets:
            material["assets"] = assets

        node_graph["nodes"] = [_clean_node_for_main_schema(node) for node in nodes]
        node_graph["links"] = [_clean_link_for_main_schema(link) for link in node_graph.get("links", [])]

        if node_graph.get("non_gltf_nodes"):
            cleaned_non_gltf = []
            for node in node_graph.get("non_gltf_nodes", []):
                entry = {
                    "node_id": node.get("node_id"),
                    "idname": node.get("idname"),
                    "ui_name": node.get("ui_name"),
                    "kind": node.get("kind"),
                }
                cleaned_non_gltf.append(entry)
            node_graph["non_gltf_nodes"] = cleaned_non_gltf

        material["graph_export_status"] = material.get("graph_export_status") or {
            "exported": True,
            "reason": "ok",
            "use_nodes": True,
            "node_tree_name": material.get("node_tree_name"),
        }
        material.setdefault("analysis", {})
        material["analysis"]["stage1_readiness"] = _stage1_readiness_for_material(material)

    for material in export_payload.get("materials", []):
        if not material.get("node_graph"):
            material.setdefault("analysis", {})
            material["analysis"]["translation_mode"] = "native_material_only"
            material["analysis"]["translator_route"] = _translator_route_for_material(material)
            material["analysis"]["stage1_readiness"] = _stage1_readiness_for_material(material)

    _build_export_report(export_payload, warnings)

def _collect_links(node_tree, blend_file):
    """
    Collect serialized links and link metadata for a node tree.

    In addition to exporting the link list, we compute which sockets are linked
    and how many connections touch each socket. That metadata helps when
    distinguishing "use default value" vs "value comes from a link" for Godot
    parameter mapping.
    """
    linked_socket_ptrs = set()
    link_counts = {}
    links = []

    links_head = _first_pointer(node_tree, ((b"links", b"first"),))
    for link in _iter_listbase(links_head):
        try:
            from_socket_ptr = link.get(b"fromsock", 0)
            to_socket_ptr = link.get(b"tosock", 0)
            if from_socket_ptr:
                linked_socket_ptrs.add(from_socket_ptr)
                link_counts[from_socket_ptr] = link_counts.get(from_socket_ptr, 0) + 1
            if to_socket_ptr:
                linked_socket_ptrs.add(to_socket_ptr)
                link_counts[to_socket_ptr] = link_counts.get(to_socket_ptr, 0) + 1
        except Exception:
            pass
        links.append(extract_link(link, blend_file))

    return linked_socket_ptrs, link_counts, links

def _collect_nodes(node_tree, blend_file, linked_socket_ptrs, link_counts, warnings):
    """
    Collect serialized nodes and node-tree summary groups.

    Besides exporting each node, we also summarize known non-glTF node usage.
    Those summaries are useful signals for a Blender-to-Godot conversion
    layer: they highlight which materials likely need special-case mapping.
    """
    all_nodes = []
    non_gltf_nodes = []
    output_node_flags = {}

    nodes_head = _first_pointer(node_tree, ((b"nodes", b"first"),))
    for raw_node in _iter_listbase(nodes_head):
        node_info, node_flag = extract_node(
            raw_node,
            blend_file,
            linked_socket_ptrs=linked_socket_ptrs,
            link_counts=link_counts,
            warnings=warnings,
        )
        all_nodes.append(node_info)

        if node_info.get("idname") == "ShaderNodeOutputMaterial" and node_info.get("ptr"):
            output_node_flags[node_info["ptr"]] = node_flag

        node_kind = NON_GLTF_IDNAMES.get(node_info.get("idname"))
        if node_kind:
            non_gltf_nodes.append(
                {
                    "ptr": node_info.get("ptr"),
                    "idname": node_info.get("idname"),
                    "ui_name": node_info.get("ui_name"),
                    "kind": node_kind,
                }
            )

    return all_nodes, non_gltf_nodes, output_node_flags

def _build_node_graph_entry(node_tree, blend_file, warnings):
    """
    Build a serialized node-tree payload for one material.

    This is the core "shader graph snapshot": nodes, links, the active output,
    and extra summaries that help detect Blender-only features that glTF tends to
    drop but a Godot conversion step should preserve.
    """
    node_graph_entry = {
        "name": id_name(node_tree),
        "id_code": as_str(node_tree.id_name[:2]),
    }

    linked_socket_ptrs, link_counts, links = _collect_links(node_tree, blend_file)
    if links:
        node_graph_entry["links"] = links

    all_nodes, non_gltf_nodes, output_node_flags = _collect_nodes(
        node_tree,
        blend_file,
        linked_socket_ptrs,
        link_counts,
        warnings,
    )
    if all_nodes:
        node_graph_entry["nodes"] = all_nodes
    if non_gltf_nodes:
        node_graph_entry["non_gltf_nodes"] = non_gltf_nodes

    active_output, _ = extract_active_output(all_nodes, output_node_flags)
    if active_output:
        node_graph_entry["active_output"] = active_output
        output_targets = extract_output_targets(active_output.get("node_ptr"), links)
        if output_targets:
            node_graph_entry["output_targets"] = output_targets

    return node_graph_entry

def _build_material_entry(material_block, blend_file, warnings):
    """
    Build the serialized material payload for one Blender material block.

    Materials are exported with basic render settings plus an optional node tree
    snapshot. Downstream conversion can skip materials without nodes or map them
    to simpler Godot materials.
    """
    node_tree = _first_pointer(material_block, (b"nodetree", b"node_tree"))
    material_name = id_name(material_block)
    material_entry = {
        "name": material_name,
        "has_nodes": node_tree is not None,
        "node_tree_name": id_name(node_tree) if node_tree else None,
        "settings": extract_material_settings(material_block, warnings),
        "graph_export_status": {
            "exported": bool(node_tree),
            "reason": "ok" if node_tree else "material_is_non_node_based",
        },
    }
    if node_tree:
        material_entry["node_graph"] = _build_node_graph_entry(node_tree, blend_file, warnings)

    return material_entry

def _build_export_payload(blend_file, blend_path: Path, warnings):
    """
    Build the full export payload for one .blend file.

    The root JSON payload is designed to be a stable intermediate format for a
    later Blender-to-Godot conversion step: it carries versioning, warnings, and
    a per-material snapshot of settings and shader graphs.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "export_profile": {
            "graph_model": "graph_first_clean_schema",
            "schema_focus": "semantic_translation_ready_with_enrichment",
            "translation_target": "semantic_node_translation",
                "color_ramp_primary": "blender_asset_tracer",
            "color_ramp_enrichment": "bpy_if_available",
            "image_texture_enrichment": "bpy_png_export_if_available",
            "material_context_enrichment": "bpy_if_available",
        },
        "blender_version": blender_version_string(blend_file),
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "blend": str(blend_path),
        "warnings": warnings,
        "materials": [
            _build_material_entry(material_block, blend_file, warnings)
            for material_block in blend_file.find_blocks_from_code(b"MA")
        ],
    }

def main():
    """
    CLI entrypoint: parse a .blend and write JSON + graph exports.

    The JSON captures material settings and node graphs with extra metadata for
    Blender-only, non-glTF features. In stage 1 the exporter no longer embeds
    Godot shader presets; it writes a graph-first schema that a later translator
    can compile into Godot-native materials or generated shaders.
    """
    blend_path = (
        Path(sys.argv[1])
        if len(sys.argv) >= 2
        else Path(input("Path to .blend: ").strip('"'))
    )
    out_arg = sys.argv[2] if len(sys.argv) >= 3 else None
    out_path = _resolve_output_json_path(blend_path, out_arg)

    blend_file = blendfile.open_cached(blend_path)
    warnings = []
    export_payload = _build_export_payload(blend_file, blend_path, warnings)
    enrich_color_ramps_with_bpy(export_payload, blend_path, warnings)
    enrich_linked_images_with_bpy(export_payload, blend_path, out_path.parent, warnings)
    enrich_material_context_with_bpy(export_payload, blend_path, warnings)
    finalize_material_graph_schema(export_payload, warnings)

    out_path.write_text(json.dumps(export_payload, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
