"""
read_blend.py

Extract material settings and shader node graphs from a Blender .blend file
using blender-asset-tracer, without launching Blender.

This exporter is meant to capture material details that don't survive the
glTF pipeline (toon shading, Shader-to-RGB, ColorRamp settings, and other
Blender-specific node behavior). The JSON snapshot is a stable intermediate
format that can be translated into Godot-friendly material properties later,
with enough context to explain why a material looks the way it does even when
glTF cannot.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from blender_asset_tracer import blendfile
from blender_asset_tracer.blendfile import iterators

SCHEMA_VERSION = "2.0.0"
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


def as_str(value):
    """
    Decode Blender DNA strings coming from blender-asset-tracer.

    Many fields in a .blend are stored as null-terminated bytes. Converting
    them early keeps the exported JSON readable and makes downstream mapping to
    Godot material parameters easier to debug.

    inputs:
        value: Any value, often null-terminated bytes from BAT.
    returns:
        Decoded str if value is bytes, otherwise value unchanged.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return value


def _material_outputs_dir(blend_path: Path) -> Path:
    """
    Choose the per-blend output folder for JSON and debug graphs.

    Keeping exports under Material Outputs/<blend-stem>/ makes it easy to
    iterate on a Blender-to-Godot conversion step and keep plots next to the
    exact JSON snapshot they came from.

    inputs:
        blend_path: Path to the input .blend file.
    returns:
        Path to the output directory for this .blend export.
    """
    base_dir = Path(__file__).resolve().parent
    return base_dir / MATERIAL_OUTPUTS_DIRNAME / blend_path.stem


def _clean_output_filename(name: str, *, default_suffix: str | None = None) -> str:
    """
    Normalize an output filename for use under the export directory.

    Keep the basename so callers cannot accidentally (or intentionally)
    write outside Material Outputs/<blend-stem>/. When a suffix is missing, 
    apply a default (.json) to keep outputs consistent.

    inputs:
        name: Filename or path (directory portion is ignored).
        default_suffix: Suffix to apply when name has no suffix (".json").
    returns:
        Clean filename (no directory components).
    """
    filename = Path(name).name

    out_path = Path(filename)
    if default_suffix and out_path.suffix == "":
        return f"{out_path.name}{default_suffix}"
    return out_path.name


def _resolve_output_json_path(blend_path: Path, out_arg: str | None) -> Path:
    """
    Resolve and create the JSON output path for a .blend export.

    inputs:
        blend_path: Source .blend path.
        out_arg: Optional filename/path argument from the CLI.
    returns:
        Final writable JSON path inside Material Outputs/<blend-stem>/.
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

    inputs:
        block: BAT block that supports get_pointer.
        pointer_names: Iterable of pointer names/paths accepted by BAT.
    returns:
        First resolved pointer block, or None if all lookups fail.
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

    inputs:
        head: First pointer in a Blender listbase.
    returns:
        Iterator of listbase entries (empty iterator if head is null/invalid).
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

    This is used for naming debug artifacts like per-material node graph PNGs.
    For this project, stability and readability matter more than preserving
    exact punctuation.

    inputs:
        value: Raw text (for example, a material name).
    returns:
        Sanitized text safe to embed in a filename.
    """
    cleaned = "".join(char if (char.isalnum() or char in ("-", "_", ".")) else "_" for char in value.strip())
    cleaned = cleaned.strip("._")
    return cleaned or "material"


def _python_command_candidates() -> list[list[str]]:
    """
    Build a prioritized list of Python commands for running plot_node_tree.py.

    When read_blend.py is run inside Blender's embedded Python, matplotlib may
    not be installed. Trying a few system Python entrypoints improves the chance
    that debug graphs can still be generated to validate extraction before the
    data is mapped into Godot material properties.

    inputs:
        none
    returns:
        List of command prefixes (each prefix is a list of argv items).
    """
    commands: list[list[str]] = [[sys.executable]]
    if shutil.which("py"):
        commands.append(["py", "-3"])
    if shutil.which("python"):
        commands.append(["python"])

    # Remove duplicates while preserving order.
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for cmd in commands:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cmd)
    return unique


def render_graph_exports(json_path: Path, materials: list[dict], blend_stem: str) -> None:
    """
    Run plot_node_tree.py to generate graph images for materials with nodes.

    These PNGs are a practical debugging aid: they help confirm that sockets,
    defaults, and links were extracted correctly before implementing the logic
    that turns Blender-only (non-glTF) behavior into Godot-friendly equivalents.

    inputs:
        json_path: Path to the JSON file written by this script.
        materials: List of serialized material dicts from the export payload.
        blend_stem: Stem of the input .blend file, used in graph filenames.
    returns:
        None. Writes graph image files and prints status messages.
    """
    plot_script = Path(__file__).with_name("plot_node_tree.py")
    if not plot_script.exists():
        print(f"Skipped graph export: missing {plot_script.name}")
        return

    materials_with_nodes = [material for material in materials if material.get("node_tree")]
    if not materials_with_nodes:
        return

    multiple_graphs = len(materials_with_nodes) > 1
    used_filenames: dict[str, int] = {}
    python_commands = _python_command_candidates()

    for material in materials_with_nodes:
        material_name = (material.get("name") or "").strip() or "Material"
        if multiple_graphs:
            base_name = f"{blend_stem}_{_safe_filename_part(material_name)}_graph"
        else:
            base_name = f"{blend_stem}_graph"

        suffix_index = used_filenames.get(base_name, 0)
        used_filenames[base_name] = suffix_index + 1
        unique_name = f"{base_name}_{suffix_index}.png" if suffix_index else f"{base_name}.png"

        command_tail = [
            str(plot_script),
            str(json_path),
            "--material",
            material_name,
            "--out",
            unique_name,
        ]

        last_error = ""
        for python_cmd in python_commands:
            cmd = [*python_cmd, *command_tail]
            completed = subprocess.run(cmd, capture_output=True, text=True)
            if completed.returncode == 0:
                if completed.stdout.strip():
                    print(completed.stdout.strip())
                break
            error_text = (completed.stderr or completed.stdout or "").strip()
            if error_text:
                last_error = error_text
        else:
            print(f"Skipped graph export for material '{material_name}':")
            if last_error:
                print(last_error.splitlines()[-1])
            else:
                print("plot_node_tree.py returned an unknown error.")


def id_name(block) -> str:
    """
    Return a datablock name without Blender's 2-character ID prefix.

    Blender stores names as IDCode + Name (for example MA + material name).
    Stripping the prefix produces stable, more easily readable names for JSON output
    and for any later Godot import step.

    inputs:
        block: BAT BlendFileBlock with an id_name field.
    returns:
        More easily readable datablock name.
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

    inputs:
        value: Any nested combination of bytes/lists/numbers/bools.
    returns:
        Normalized value suitable for json.dumps.
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

    inputs:
        code: Enum integer value.
        mapping: Dict mapping integers to names.
        enum_name: Label used in warning messages.
        warnings: List to append warnings to.
    returns:
        Mapped name string (or UNKNOWN_<code> if unmapped).
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

    inputs:
        blend_file: Opened BAT BlendFile.
    returns:
        Version string like 4.2.66.
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

    inputs:
        blend_file: Opened BAT BlendFile.
        ptr: Old address value from a pointer field.
    returns:
        Dict with {code, name} or None if not resolvable.
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
    by socket type.

    inputs:
        sock: bNodeSocket block.
    returns:
        Tuple (decoded, subtype_code) where decoded may include default,
        min, max, soft_min, soft_max.
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

    inputs:
        sock_type: Blender socket type integer.
        subtype_code: Subtype enum from the default_value struct (or None).
    returns:
        Subtype name string, falling back to type-based guesses.
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

    inputs:
        sock: bNodeSocket block.
        linked_socket_ptrs: Set of socket ptrs that participate in links.
        link_counts: Map of socket ptr -> number of links touching it.
    returns:
        Socket dict including identifiers, defaults, and link metadata.
    """
    ptr = getattr(sock, "addr_old", None)
    sock_type = sock.get(b"type", None)

    default_data, subtype_code = decode_socket_default(sock)
    in_out_raw = sock.get(b"in_out", None)
    info = {
        "ptr": ptr,
        "name": as_str(sock.get(b"name", as_str=True)),
        "identifier": as_str(sock.get(b"identifier", as_str=True)),
        "type": sock_type,
        "subtype": socket_subtype_name(sock_type, subtype_code),
        "in_out": IN_OUT_MAP.get(in_out_raw, in_out_raw),
        "is_linked": bool(ptr in linked_socket_ptrs) if ptr else False,
        "link_count": int(link_counts.get(ptr, 0)) if ptr else 0,
    }

    for key in ("default", "min", "max", "soft_min", "soft_max"):
        if key in default_data:
            info[key] = default_data[key]

    return info


def extract_color_ramp(node, warnings):
    """
    Extract ColorRamp settings and points from a ramp node.

    Color ramps are a common source of non-glTF behavior. Capturing interpolation
    and stop colors gives a later Godot conversion step enough information to
    recreate the effect (for example via a gradient texture or curve sampling).

    inputs:
        node: bNode block (expected ShaderNodeValToRGB).
        warnings: Warning sink for unknown enum values.
    returns:
        (settings, points) or (None, None) if no ColorBand storage exists.
    """
    try:
        ramp = node.get_pointer(b"storage")
    except Exception:
        ramp = None
    if not ramp or ramp.dna_type_name != "ColorBand":
        return None, None

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

    points = []
    try:
        total = int(ramp.get(b"tot", 0))
        elements = ramp.get(b"data", [])
        for color_band_data in elements[:total]:
            points.append(
                {
                    "pos": float(color_band_data.get(b"pos", 0.0)),
                    "color": [
                        float(color_band_data.get(b"r", 0.0)),
                        float(color_band_data.get(b"g", 0.0)),
                        float(color_band_data.get(b"b", 0.0)),
                        float(color_band_data.get(b"a", 1.0)),
                    ],
                }
            )
    except Exception:
        pass

    return settings, points


def extract_linked_image(node, blend_file):
    """
    Extract image metadata for an Image Texture node.

    When converting materials, the image filepath, packed status, and colorspace
    affect how textures should be imported and interpreted in Godot.

    inputs:
        node: bNode block that may reference an Image ID via id.
        blend_file: Opened BAT BlendFile.
    returns:
        Dict with filepath, is_packed, colorspace, or None if not applicable.
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


def extract_node_properties(node, idname, warnings):
    """
    Extract node-type-specific properties required by the schema.

    Sockets and links describe most nodes, but some Blender nodes carry extra
    settings that matter for non-glTF behavior (toon component, mix blend type,
    clamp flags, ramp interpolation modes). Exporting these makes a Blender-to-Godot
    conversion step more faithful.

    inputs:
        node: bNode block.
        idname: Node type string (such as ShaderNodeMix).
        warnings: Warning sink for unknown enum values.
    returns:
        Properties dict (empty if none apply).
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

    if idname == "ShaderNodeValToRGB":
        ramp_settings, _ = extract_color_ramp(node, warnings)
        if ramp_settings:
            properties["ramp_settings"] = ramp_settings

    return properties


def extract_node(node, blend_file, linked_socket_ptrs, link_counts, warnings):
    """
    Serialize a node including sockets, linked assets, and special node data.

    The exported node contains enough information to:
    - visualize the graph (debugging extraction and conversion)
    - detect Blender-only nodes that glTF will drop
    - carry the extra flags/enums needed when mapping into Godot materials

    inputs:
        node: bNode block.
        blend_file: Opened BAT BlendFile.
        linked_socket_ptrs: Set of linked socket ptrs.
        link_counts: Map of socket ptr -> number of links touching it.
        warnings: Warning sink for unknown enum values.
    returns:
        (node_dict, node_flag) where node_flag is the raw bNode.flag bits.
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

    properties = extract_node_properties(node, idname=idname, warnings=warnings)
    if properties:
        node_data["properties"] = properties

    if idname == "ShaderNodeValToRGB":
        _, color_ramp_points = extract_color_ramp(node, warnings)
        if color_ramp_points:
            node_data["color_ramp"] = color_ramp_points

    return node_data, node_flag


def extract_link(link, blend_file):
    """
    Serialize a bNodeLink into JSON-friendly dict form.

    Links connect output sockets to input sockets. Capturing both ends (node and
    socket identifiers) supports plotting the graph and lets downstream code trace
    which branch drives the active output when mapping to Godot properties.

    inputs:
        link: bNodeLink block.
        blend_file: Opened BAT BlendFile.
    returns:
        Link dict with from/to node and socket references.
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

    inputs:
        material_block: Material datablock (MA).
        warnings: Warning sink for unknown enum values.
    returns:
        Settings dict (blend_method, alpha_threshold, use_backface_culling).
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

    inputs:
        node_entries: List of serialized node dicts.
        output_node_flags: Map of output node ptr -> raw flag bits.
    returns:
        (active_output, socket_lookup) where active_output may be None.
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

    inputs:
        active_ptr: Ptr of the active output node.
        links: List of serialized link dicts.
    returns:
        Dict keyed by output socket identifier/name (lowercase) to
        {from_node_ptr, from_socket_identifier}.
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


def _collect_links(node_tree, blend_file):
    """
    Collect serialized links and link metadata for a node tree.

    In addition to exporting the link list, we compute which sockets are linked
    and how many connections touch each socket. That metadata helps when
    distinguishing "use default value" vs "value comes from a link" for Godot
    parameter mapping.

    inputs:
        node_tree: bNodeTree block.
        blend_file: Opened BAT BlendFile.
    returns:
        Tuple (linked_socket_ptrs, link_counts, links).
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

    inputs:
        node_tree: bNodeTree block.
        blend_file: Opened BAT BlendFile.
        linked_socket_ptrs: Set of linked socket ptrs.
        link_counts: Socket ptr -> link count mapping.
        warnings: Warning sink for unknown enum values.
    returns:
        Tuple (all_nodes, non_gltf_nodes, output_node_flags).
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


def _build_node_tree_entry(node_tree, blend_file, warnings):
    """
    Build a serialized node-tree payload for one material.

    This is the core "shader graph snapshot": nodes, links, the active output,
    and extra summaries that help detect Blender-only features that glTF tends to
    drop but a Godot conversion step should preserve.

    inputs:
        node_tree: bNodeTree block from a material.
        blend_file: Opened BAT BlendFile.
        warnings: Warning sink for unknown enum values.
    returns:
        Node-tree dict matching the existing JSON schema.
    """
    node_tree_entry = {
        "name": id_name(node_tree),
        "id_code": as_str(node_tree.id_name[:2]),
    }

    linked_socket_ptrs, link_counts, links = _collect_links(node_tree, blend_file)
    if links:
        node_tree_entry["links"] = links

    all_nodes, non_gltf_nodes, output_node_flags = _collect_nodes(
        node_tree,
        blend_file,
        linked_socket_ptrs,
        link_counts,
        warnings,
    )
    if all_nodes:
        node_tree_entry["nodes"] = all_nodes
    if non_gltf_nodes:
        node_tree_entry["non_gltf_nodes"] = non_gltf_nodes

    active_output, _ = extract_active_output(all_nodes, output_node_flags)
    if active_output:
        node_tree_entry["active_output"] = active_output
        output_targets = extract_output_targets(active_output.get("node_ptr"), links)
        if output_targets:
            node_tree_entry["output_targets"] = output_targets

    return node_tree_entry


def _build_material_entry(material_block, blend_file, warnings):
    """
    Build the serialized material payload for one Blender material block.

    Materials are exported with basic render settings plus an optional node tree
    snapshot. Downstream conversion can skip materials without nodes or map them
    to simpler Godot materials.

    inputs:
        material_block: Material datablock (MA).
        blend_file: Opened BAT BlendFile.
        warnings: Warning sink for unknown enum values.
    returns:
        Material dict matching the existing JSON schema.
    """
    node_tree = _first_pointer(material_block, (b"nodetree", b"node_tree"))
    material_entry = {
        "name": id_name(material_block),
        "has_nodes": node_tree is not None,
        "node_tree_name": id_name(node_tree) if node_tree else None,
        "settings": extract_material_settings(material_block, warnings),
    }
    if node_tree:
        material_entry["node_tree"] = _build_node_tree_entry(node_tree, blend_file, warnings)
    return material_entry


def _build_export_payload(blend_file, blend_path: Path, warnings):
    """
    Build the complete export payload for one .blend file.

    The root JSON payload is designed to be a stable intermediate format for a
    later Blender-to-Godot conversion step: it carries versioning, warnings, and
    a per-material snapshot of settings and shader graphs.

    inputs:
        blend_file: Opened BAT BlendFile.
        blend_path: Source .blend path.
        warnings: Warning sink list shared across extractors.
    returns:
        Root JSON payload dict.
    """
    return {
        "schema_version": SCHEMA_VERSION,
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
    Blender-only, non-glTF features. The optional graph images help verify
    extraction before translating the data into Godot material properties.

    inputs:
        Uses sys.argv or an interactive prompt for the input path.
        Optionally accepts an output filename/path as the second CLI argument.
    returns:
        None. Writes outputs under Material Outputs/<blend-stem>/ and prints
        generated file paths/status.
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

    out_path.write_text(json.dumps(export_payload, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    render_graph_exports(out_path, export_payload["materials"], blend_path.stem)


if __name__ == "__main__":
    main()
