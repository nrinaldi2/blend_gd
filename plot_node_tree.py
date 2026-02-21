"""
plot_node_tree.py

Debug visualizer for the JSON produced by read_blend.py.

This script is a development aid for the Blender material extraction project.
It renders the exported node tree so sockets, defaults, and link topology can
be checked before writing the Blender-to-Godot conversion layer,
especially for Blender-only, non-glTF features that should be preserved.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

MATERIAL_OUTPUTS_DIRNAME = "Material Outputs"
DEFAULT_NODE_WIDTH = 140.0
DEFAULT_NODE_HEIGHT = 100.0


def _material_outputs_dir(blend_stem: str) -> Path:
    """
    Return the standard output directory for a given .blend stem.

    Keeping plots next to the JSON snapshot makes it easier to iterate on a
    Blender-to-Godot mapping while always looking at the exact extracted data.

    inputs:
        blend_stem: Filename stem (no extension) of the source .blend.
    returns:
        Path to Material Outputs/<blend-stem>/.
    """
    return Path(__file__).resolve().parent / MATERIAL_OUTPUTS_DIRNAME / blend_stem


def _clean_output_filename(name: str, *, default_suffix: str | None = None) -> str:
    """
    Normalize an output filename for saving plots next to an export snapshot.

    The plotter writes under Material Outputs/<blend-stem>/, so we keep only the
    basename and optionally add a default suffix to keep filenames predictable.

    inputs:
        name: Filename or path (directory portion is ignored).
        default_suffix: Suffix to apply when name has no suffix (".png").
    returns:
        Clean filename (no directory components).
    """
    filename = Path(name).name
    out_path = Path(filename)
    if default_suffix and out_path.suffix == "":
        return f"{out_path.name}{default_suffix}"
    return out_path.name


def _blend_stem_from_export(data: dict, json_path: Path) -> str:
    """
    Best-effort lookup of the source .blend stem for an exported JSON document.

    We use this to save PNGs under the same Material Outputs/<blend-stem>/
    folder layout as read_blend.py.

    inputs:
        data: Parsed JSON dict from read_blend.py.
        json_path: Path to the JSON file (used as fallback).
    returns:
        The .blend file stem (directory-friendly name).
    """
    blend_value = (data.get("blend") or "").strip()
    if blend_value:
        try:
            stem = Path(blend_value).stem
            if stem:
                return stem
        except Exception:
            pass

    # Fallback: derive from JSON filename.
    stem = json_path.stem
    return stem or "Blend"


def _pick_material(data: dict, material_name: str | None) -> dict:
    """
    Select a material entry to plot from an exported JSON document.

    This is mainly used while implementing the Blender-to-Godot converter: being
    able to plot one material at a time makes it easier to confirm that the
    extracted snapshot includes the non-glTF properties that matter for the
    conversion.

    inputs:
        data: Parsed JSON dict from read_blend.py.
        material_name: Optional material name to select.
    returns:
        Material dict that contains a node_tree.
    raises:
        SystemExit: If the material is not found or has no node tree.
    """
    mats = data.get("materials") or []
    if material_name:
        for m in mats:
            if m.get("name") == material_name:
                return m
        raise SystemExit(f"Material not found: {material_name!r}")
    for m in mats:
        if m.get("node_tree"):
            return m
    raise SystemExit("No material with a node tree found in JSON.")


def _node_title(node: dict) -> str:
    """
    Pick a short label for a node rectangle.

    We prefer Blender's visible label/name so it's easy to compare this plot
    against the node editor while validating extraction and conversion logic.

    inputs:
        node: Serialized node dict.
    returns:
        Title string (label, ui_name, or idname).
    """
    label = (node.get("label") or "").strip()
    if label:
        return label
    ui_name = (node.get("ui_name") or "").strip()
    if ui_name:
        return ui_name
    return (node.get("idname") or "<node>").strip()


def _node_geometry(node: dict) -> tuple[float, float, float, float]:
    """
    Read a node's position and size with fallback defaults.

    The exporter includes UI location/size so this plot can roughly match Blender,
    which helps when debugging how a graph should translate into Godot material
    properties.

    inputs:
        node: Serialized node dict.
    returns:
        Tuple (x, y_top, width, height) in data coordinates.
    """
    x, y_top = node.get("loc") or (0.0, 0.0)
    width = float(node.get("width") or DEFAULT_NODE_WIDTH)
    height = float(node.get("height") or DEFAULT_NODE_HEIGHT)
    return float(x), float(y_top), width, height


def _layout_sort_key(node: dict) -> tuple[float, float]:
    """
    Provide a stable top-to-bottom, left-to-right layout sort key.

    A deterministic ordering makes plots (and diffs) more predictable while
    iterating on extraction and a Godot conversion step.

    inputs:
        node: Serialized node dict.
    returns:
        Sort key tuple (-y_top, x).
    """
    x, y_top, _, _ = _node_geometry(node)
    return (-y_top, x)


def _compute_socket_positions(node: dict, side: str) -> dict[int, tuple[float, float]]:
    """
    Compute approximate socket anchor positions for drawing link arrows.

    The export does not include per-socket UI coordinates, so we space sockets
    evenly. This is good enough to visualize link topology while validating the
    extracted snapshot for Blender-to-Godot mapping.

    inputs:
        node: Serialized node dict.
        side: Either `"inputs"` or `"outputs"`.
    returns:
        Mapping of socket ptr -> (x, y) position in data coordinates.
    """
    x, y_top, width, height = _node_geometry(node)

    if side == "inputs":
        sockets = node.get("inputs") or []
        sx = float(x)
    else:
        sockets = node.get("outputs") or []
        sx = float(x) + width

    socket_count = len(sockets)
    if socket_count == 0:
        return {}

    margin = min(30.0, max(12.0, height * 0.12))
    inner = max(1.0, height - 2.0 * margin)
    step = inner / float(socket_count + 1)

    positions: dict[int, tuple[float, float]] = {}
    for index, socket in enumerate(sockets):
        ptr = socket.get("ptr")
        if not ptr:
            continue
        sy = float(y_top) - margin - float(index + 1) * step
        positions[int(ptr)] = (sx, sy)
    return positions


def _socket_detail_entries(node: dict) -> list[tuple[str, dict]]:
    """
    Return the sockets to render under a node, grouped as Input/Output.

    Grouping by side makes it easier to scan identifiers, defaults, and link flags
    when checking what should become Godot material parameters.

    inputs:
        node: Serialized node dict.
    returns:
        List of (kind, socket_dict) tuples in display order.
    """
    entries: list[tuple[str, dict]] = []
    for socket in node.get("inputs") or []:
        entries.append(("Input", socket))
    for socket in node.get("outputs") or []:
        entries.append(("Output", socket))
    return entries


def _escape_mpl_text(text: str) -> str:
    """
    Escape text that would be interpreted by matplotlib mathtext.

    Some identifiers can include $, and we want the debug labels to render
    literally while inspecting extracted socket data for Godot mapping.

    inputs:
        text: Raw text.
    returns:
        Escaped text safe to pass to ax.text.
    """
    return text.replace("$", r"\$")


def _detail_text_block(node: dict, wrap_width: int) -> dict[str, object]:
    """
    Format per-node socket JSON into wrapped text lines for plotting.

    This is the "inspect what we extracted" view: it surfaces socket identifiers,
    defaults, and link metadata so the snapshot can be checked for the data
    needed before translating the material into Godot.

    inputs:
        node: Serialized node dict.
        wrap_width: Approximate character width before wrapping.
    returns:
        Dict with draw_lines (strings), plus line_count and max_chars for layout.
    """
    draw_lines: list[str] = []
    plain_lines: list[str] = []

    payload_width = max(24, int(wrap_width))
    inner_width = max(16, payload_width - 2)

    for kind, payload in _socket_detail_entries(node):
        draw_lines.append(rf"$\bf{{{kind}:}}$ " + _escape_mpl_text("{"))
        plain_lines.append(f"{kind}: " + "{")

        items = list(payload.items()) if isinstance(payload, dict) else []
        for idx, (key, value) in enumerate(items):
            value_json = json.dumps(value, ensure_ascii=False)
            pair_text = f'"{key}": {value_json}'
            if idx < len(items) - 1:
                pair_text += ","
            wrapped = textwrap.wrap(
                pair_text,
                width=inner_width,
                break_long_words=False,
                break_on_hyphens=False,
            ) or [pair_text]
            for line in wrapped:
                draw_lines.append(_escape_mpl_text("  " + line))
                plain_lines.append("  " + line)

        draw_lines.append(_escape_mpl_text("}"))
        plain_lines.append("}")

    return {
        "draw_lines": draw_lines,
        "line_count": len(draw_lines),
        "max_chars": max((len(line) for line in plain_lines), default=0),
    }


def _detail_size_estimate(meta: dict[str, object], line_height: float, char_width: float) -> tuple[float, float]:
    """
    Estimate detail-text bounding size in data coordinates.

    This estimate is used before matplotlib can measure text in pixels, so we can
    get a rough layout and axis extents without rendering.

    inputs:
        meta: Detail metadata dict containing line_count and max_chars.
        line_height: Estimated height of a text line.
        char_width: Estimated width per character.
    returns:
        Tuple (detail_width, detail_height) in data units.
    """
    line_count = int(meta.get("line_count") or 0)
    max_chars = int(meta.get("max_chars") or 0)
    detail_height = (line_count * line_height + 8.0) if line_count > 0 else 0.0
    detail_width = (max_chars * char_width + 8.0) if max_chars > 0 else 0.0
    return detail_width, detail_height


def _node_box(
    x: float,
    y_top: float,
    node_width: float,
    node_height: float,
) -> tuple[float, float, float, float]:
    """
    Compute a node rectangle bounds in data coordinates.

    These bounds feed the collision checks that keep the debug plot readable while
    inspecting extracted materials (especially when a node has a lot of socket
    detail text).

    inputs:
        x: Node left x.
        y_top: Node top y.
        node_width: Rectangle width.
        node_height: Rectangle height.
    returns:
        Bounding box tuple (left, right, bottom, top).
    """
    return (x, x + node_width, y_top - node_height, y_top)


def _detail_box(
    x: float,
    y_top: float,
    node_height: float,
    detail_width: float,
    detail_height: float,
    detail_gap_y: float,
    detail_offset_x: float,
) -> tuple[float, float, float, float] | None:
    """
    Compute the detail text bounds under a node in data coordinates.

    The plot includes optional per-socket metadata (defaults, identifiers, link
    flags). This box lets the layout solver treat that text as real content so it
    does not overlap with other nodes.

    inputs:
        x: Node left x.
        y_top: Node top y.
        node_height: Node rectangle height.
        detail_width: Estimated text width.
        detail_height: Estimated text height.
        detail_gap_y: Vertical gap below the node.
        detail_offset_x: Horizontal offset from node left.
    returns:
        Bounding box tuple or None if there is no detail text.
    """
    if detail_width <= 0.0 or detail_height <= 0.0:
        return None
    left = x + detail_offset_x
    right = left + detail_width
    top = y_top - node_height - detail_gap_y
    bottom = top - detail_height
    return (left, right, bottom, top)


def _merge_boxes(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    """
    Union two axis-aligned bounding boxes.

    We use this to combine a node box and its detail-text box into a single region
    for collision checks and extent calculations.

    inputs:
        box_a: Base box.
        box_b: Optional box to union with.
    returns:
        Union bounds.
    """
    if box_b is None:
        return box_a
    left_a, right_a, bottom_a, top_a = box_a
    left_b, right_b, bottom_b, top_b = box_b
    return (
        min(left_a, left_b),
        max(right_a, right_b),
        min(bottom_a, bottom_b),
        max(top_a, top_b),
    )


def _boxes_overlap(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
    pad: float,
) -> bool:
    """
    Check whether two boxes overlap (with optional padding).

    This is the core predicate used by the collision solver that spaces nodes out
    in the debug plot.

    inputs:
        box_a: First box (left, right, bottom, top).
        box_b: Second box (left, right, bottom, top).
        pad: Extra spacing treated as part of each box.
    returns:
        True if the boxes overlap, else False.
    """
    a_left, a_right, a_bottom, a_top = box_a
    b_left, b_right, b_bottom, b_top = box_b
    if (a_right + pad) < b_left:
        return False
    if (b_right + pad) < a_left:
        return False
    if (a_top + pad) < b_bottom:
        return False
    if (b_top + pad) < a_bottom:
        return False
    return True


def _placement_conflicts(
    candidate_node: tuple[float, float, float, float],
    candidate_detail: tuple[float, float, float, float] | None,
    placed: list[dict[str, tuple[float, float, float, float] | None]],
    node_pad: float,
    detail_pad: float,
) -> bool:
    """
    Determine if a candidate placement collides with any placed boxes.

    The auto-layout uses this to keep nodes and their detail text from overlapping,
    so the extracted socket defaults and identifiers remain readable.

    inputs:
        candidate_node: Node rectangle bounds (display coords).
        candidate_detail: Optional detail-text bounds (display coords).
        placed: List of dicts with node and detail boxes for already-placed nodes.
        node_pad: Padding for node collisions.
        detail_pad: Padding for detail-text collisions.
    returns:
        True if placement conflicts, else False.
    """
    for other in placed:
        other_node = other["node"]
        other_detail = other["detail"]

        if _boxes_overlap(candidate_node, other_node, node_pad):
            return True
        if candidate_detail is not None and other_detail is not None:
            if _boxes_overlap(candidate_detail, other_detail, detail_pad):
                return True
        if candidate_detail is not None:
            if _boxes_overlap(candidate_detail, other_node, node_pad):
                return True
        if other_detail is not None:
            if _boxes_overlap(candidate_node, other_detail, node_pad):
                return True
    return False


def _node_box_display(
    ax,
    x: float,
    y_top: float,
    node_width: float,
    node_height: float,
) -> tuple[float, float, float, float]:
    """
    Compute node rectangle bounds in display (pixel) coordinates.

    We switch to display space when measuring text extents so collision detection
    matches what will actually be rendered on the plot.

    inputs:
        ax: Matplotlib axes.
        x: Node left x (data coords).
        y_top: Node top y (data coords).
        node_width: Rectangle width (data coords).
        node_height: Rectangle height (data coords).
    returns:
        Bounding box tuple (left, right, bottom, top) in pixels.
    """
    p_a = ax.transData.transform((x, y_top))
    p_b = ax.transData.transform((x + node_width, y_top - node_height))
    left = min(p_a[0], p_b[0])
    right = max(p_a[0], p_b[0])
    bottom = min(p_a[1], p_b[1])
    top = max(p_a[1], p_b[1])
    return (left, right, bottom, top)


def _detail_box_display(
    ax,
    x: float,
    y_top: float,
    node_height: float,
    detail_gap_y: float,
    detail_offset_x: float,
    detail_size: tuple[float, float],
) -> tuple[float, float, float, float] | None:
    """
    Compute detail-text bounds in display (pixel) coordinates.

    Text extents are measured in pixels, so we compute collisions in display space
    and then convert back to data coordinates for axis sizing.

    inputs:
        ax: Matplotlib axes.
        x: Node left x (data coords).
        y_top: Node top y (data coords).
        node_height: Node height (data coords).
        detail_gap_y: Gap under node (data coords).
        detail_offset_x: X offset under node (data coords).
        detail_size: (width_px, height_px) measured from a text bbox.
    returns:
        Bounding box tuple in pixels, or None if size is empty.
    """
    width_px, height_px = detail_size
    if width_px <= 0.0 or height_px <= 0.0:
        return None
    anchor = ax.transData.transform((x + detail_offset_x, y_top - node_height - detail_gap_y))
    left = float(anchor[0])
    top = float(anchor[1])
    right = left + float(width_px)
    bottom = top - float(height_px)
    return (left, right, bottom, top)


def _display_box_to_data(
    ax,
    display_box: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Convert a display-space (pixel) box into data coordinates.

    The final collision pass runs in pixel space (matching matplotlib text
    measurement), but the rest of the plot uses data coordinates for layout and
    axis limits.

    inputs:
        ax: Matplotlib axes.
        display_box: (left, right, bottom, top) in pixels.
    returns:
        Bounding box tuple in data coordinates.
    """
    inv = ax.transData.inverted()
    left, right, bottom, top = display_box
    p_lb = inv.transform((left, bottom))
    p_rt = inv.transform((right, top))
    x_min = min(p_lb[0], p_rt[0])
    x_max = max(p_lb[0], p_rt[0])
    y_min = min(p_lb[1], p_rt[1])
    y_max = max(p_lb[1], p_rt[1])
    return (x_min, x_max, y_min, y_max)


def _layout_boxes_data(
    x: float,
    y_top: float,
    node_width: float,
    node_height: float,
    detail_width: float,
    detail_height: float,
    detail_gap_y: float,
    detail_offset_x: float,
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float] | None, tuple[float, float, float, float]]:
    """
    Build node/detail/union boxes in data coordinates.

    This keeps the layout code consistent by producing the three box variants the
    collision solver needs.

    inputs:
        x: Node left x.
        y_top: Node top y.
        node_width: Node rectangle width.
        node_height: Node rectangle height.
        detail_width: Estimated detail text width.
        detail_height: Estimated detail text height.
        detail_gap_y: Vertical gap below node.
        detail_offset_x: Horizontal detail offset from node left.
    returns:
        Tuple (node_box, detail_box, union_box).
    """
    node_box = _node_box(x, y_top, node_width, node_height)
    detail_box = _detail_box(
        x,
        y_top,
        node_height,
        detail_width,
        detail_height,
        detail_gap_y,
        detail_offset_x,
    )
    return node_box, detail_box, _merge_boxes(node_box, detail_box)


def _layout_boxes_display(
    ax,
    x: float,
    y_top: float,
    node_width: float,
    node_height: float,
    detail_gap_y: float,
    detail_offset_x: float,
    detail_size: tuple[float, float],
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float] | None, tuple[float, float, float, float]]:
    """
    Build node/detail/union boxes in display (pixel) coordinates.

    Display-space boxes let the layout solver account for actual text measurement,
    which is important when validating extracted socket metadata.

    inputs:
        ax: Matplotlib axes.
        x: Node left x (data coords).
        y_top: Node top y (data coords).
        node_width: Node rectangle width (data coords).
        node_height: Node rectangle height (data coords).
        detail_gap_y: Vertical gap below node (data coords).
        detail_offset_x: Horizontal detail offset (data coords).
        detail_size: Measured detail text size (width_px, height_px).
    returns:
        Tuple (node_box, detail_box, union_box) in display coordinates.
    """
    node_box = _node_box_display(ax, x, y_top, node_width, node_height)
    detail_box = _detail_box_display(
        ax,
        x,
        y_top,
        node_height,
        detail_gap_y,
        detail_offset_x,
        detail_size,
    )
    return node_box, detail_box, _merge_boxes(node_box, detail_box)


def _auto_space_nodes_display(
    ax,
    nodes: list[dict],
    detail_meta: dict[int, dict[str, object]],
    detail_font_size: float,
    detail_gap_y: float,
    detail_offset_x: float,
) -> dict[int, tuple[float, float, float, float]]:
    """Resolve node collisions using measured text extents in display space.

    This is the "final" reflow step: it uses the matplotlib renderer to measure
    actual detail-text boxes, then shifts nodes until the plot is readable. That
    readability is the point of this tool: it should be easy to see what
    read_blend.py extracted before mapping it into Godot materials.

    inputs:
        ax: Matplotlib axes used for transforms and text measurement.
        nodes: List of node dicts (mutated in-place via loc).
        detail_meta: Per-node detail text metadata (draw_lines/line_count/etc).
        detail_font_size: Font size used for detail text.
        detail_gap_y: Vertical gap between node and detail text (data coords).
        detail_offset_x: Horizontal offset for detail text (data coords).
    returns:
        Mapping of id(node_dict) -> union bounding box in data coordinates.
    """
    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    detail_sizes: dict[int, tuple[float, float]] = {}
    probes: list[tuple[int, object]] = []
    for node in nodes:
        lines = detail_meta.get(id(node), {}).get("draw_lines") or []
        if not lines:
            detail_sizes[id(node)] = (0.0, 0.0)
            continue
        probe_text = ax.text(
            0.0,
            0.0,
            "\n".join(lines),
            ha="left",
            va="top",
            fontsize=detail_font_size,
            linespacing=1.1,
            family="monospace",
            alpha=0.0,
            clip_on=False,
        )
        probes.append((id(node), probe_text))

    if probes:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    for node_id, probe in probes:
        bbox = probe.get_window_extent(renderer=renderer)
        detail_sizes[node_id] = (float(bbox.width), float(bbox.height))
        probe.remove()

    fig.canvas.draw()
    inv = ax.transData.inverted()
    p0 = inv.transform((0.0, 0.0))
    py = inv.transform((0.0, 1.0))
    data_per_px_y = abs(float(py[1] - p0[1]))

    shift_y = max(1e-6, data_per_px_y * 22.0)
    max_tries = 900
    node_overlap_pad = 12.0
    detail_overlap_pad = 10.0

    sorted_nodes = sorted(nodes, key=_layout_sort_key)

    placed: list[dict[str, tuple[float, float, float, float] | None]] = []
    boxes_data: dict[int, tuple[float, float, float, float]] = {}

    for node in sorted_nodes:
        x, y_top, width, height = _node_geometry(node)
        detail_size = detail_sizes.get(id(node), (0.0, 0.0))

        tries = 0
        node_box, detail_box, union_box = _layout_boxes_display(
            ax,
            x,
            y_top,
            width,
            height,
            detail_gap_y,
            detail_offset_x,
            detail_size,
        )
        while _placement_conflicts(
            node_box,
            detail_box,
            placed,
            node_pad=node_overlap_pad,
            detail_pad=detail_overlap_pad,
        ):
            tries += 1
            y_top -= shift_y
            node_box, detail_box, union_box = _layout_boxes_display(
                ax,
                x,
                y_top,
                width,
                height,
                detail_gap_y,
                detail_offset_x,
                detail_size,
            )
            if tries >= max_tries:
                break

        node["loc"] = [x, y_top]
        placed.append({"node": node_box, "detail": detail_box})
        boxes_data[id(node)] = _display_box_to_data(ax, union_box)

    return boxes_data


def _auto_space_nodes(
    nodes: list[dict],
    detail_meta: dict[int, dict[str, object]],
    detail_gap_y: float,
    detail_offset_x: float,
    line_height: float,
    char_width: float,
) -> dict[int, tuple[float, float, float, float]]:
    """
    Rough collision solver using estimated text widths/heights in data space.

    We run this before creating the matplotlib figure so we can compute rough
    extents for the canvas and axis limits. The layout is later refined by
    _auto_space_nodes_display using measured text sizes.

    inputs:
        nodes: List of node dicts (mutated in-place via loc).
        detail_meta: Per-node detail text metadata.
        detail_gap_y: Vertical gap between node and detail text.
        detail_offset_x: Horizontal offset for detail text.
        line_height: Estimated height of a text line (data coords).
        char_width: Estimated width per character (data coords).
    returns:
        Mapping of id(node_dict) -> union bounding box in data coordinates.
    """
    node_overlap_pad = 30.0
    detail_overlap_pad = 42.0
    shift_y = 52.0
    max_tries = 1100

    sorted_nodes = sorted(nodes, key=_layout_sort_key)

    placed_boxes: list[dict[str, tuple[float, float, float, float] | None]] = []
    node_boxes: dict[int, tuple[float, float, float, float]] = {}

    for node in sorted_nodes:
        x, y_top, node_width, node_height = _node_geometry(node)

        meta = detail_meta.get(id(node), {})
        detail_width, detail_height = _detail_size_estimate(meta, line_height, char_width)

        tries = 0
        node_box, detail_box, box = _layout_boxes_data(
            x,
            y_top,
            node_width,
            node_height,
            detail_width,
            detail_height,
            detail_gap_y,
            detail_offset_x,
        )
        while _placement_conflicts(
            node_box,
            detail_box,
            placed_boxes,
            node_pad=node_overlap_pad,
            detail_pad=detail_overlap_pad,
        ):
            y_top -= shift_y
            tries += 1
            node_box, detail_box, box = _layout_boxes_data(
                x,
                y_top,
                node_width,
                node_height,
                detail_width,
                detail_height,
                detail_gap_y,
                detail_offset_x,
            )
            if tries >= max_tries:
                break

        node["loc"] = [x, y_top]
        placed_boxes.append({"node": node_box, "detail": detail_box, "union": box})
        node_boxes[id(node)] = box

    return node_boxes


def _build_detail_meta(nodes: list[dict], hide_socket_details: bool, wrap_width: int) -> dict[int, dict[str, object]]:
    """
    Build per-node detail-text metadata used for layout/drawing.

    The detail blocks are where extracted socket defaults, identifiers, and link
    flags can be verified before translating the material graph into Godot
    properties.

    inputs:
        nodes: List of serialized node dicts.
        hide_socket_details: Whether detail text should be suppressed.
        wrap_width: Approximate character width before line wrapping.
    returns:
        Mapping id(node_dict) -> detail metadata dict.
    """
    if hide_socket_details:
        return {id(node): {"draw_lines": [], "line_count": 0, "max_chars": 0} for node in nodes}
    return {id(node): _detail_text_block(node, wrap_width) for node in nodes}


def _boxes_extents(node_boxes: dict[int, tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    """
    Compute min/max layout extents from union node boxes.

    We use this to size the figure and axis limits so saved PNGs include every
    node and detail block.

    inputs:
        node_boxes: Mapping of node id -> (left, right, bottom, top).
    returns:
        Tuple (min_x, max_x, min_y, max_y).
    """
    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y = float("-inf")
    for left, right, bottom, top in node_boxes.values():
        min_x = min(min_x, left)
        max_x = max(max_x, right)
        min_y = min(min_y, bottom)
        max_y = max(max_y, top)
    return min_x, max_x, min_y, max_y


def _fit_canvas_layout(
    ax,
    fig,
    nodes: list[dict],
    base_locs: dict[int, list[float]],
    detail_meta: dict[int, dict[str, object]],
    detail_font_size: float,
    detail_gap_y: float,
    detail_offset_x: float,
) -> dict[int, tuple[float, float, float, float]]:
    """
    Reflow node layout and grow canvas until all content fits in axis limits.

    This is a convenience wrapper for saving readable PNGs. It tries a few
    passes, growing the canvas when any content falls outside the current axis
    limits, so manual figure-size tweaking is usually unnecessary while debugging
    a Blender-to-Godot material mapping.

    inputs:
        ax: Matplotlib axes used for layout transforms.
        fig: Matplotlib figure that may be resized.
        nodes: Node dicts that will have loc updated in-place.
        base_locs: Original node locations keyed by id(node_dict).
        detail_meta: Per-node detail text metadata.
        detail_font_size: Font size used for detail text.
        detail_gap_y: Vertical gap below each node.
        detail_offset_x: Horizontal detail offset from each node.
    returns:
        Mapping of node id -> union box in data coordinates after final layout.
    """
    node_boxes = {}
    max_canvas_attempts = 6
    canvas_growth = 1.35

    for _ in range(max_canvas_attempts):
        for node in nodes:
            node["loc"] = list(base_locs[id(node)])

        node_boxes = _auto_space_nodes_display(
            ax,
            nodes=nodes,
            detail_meta=detail_meta,
            detail_font_size=detail_font_size,
            detail_gap_y=detail_gap_y,
            detail_offset_x=detail_offset_x,
        )

        xlim = ax.get_xlim()
        ylim = ax.get_ylim()
        outside = sum(
            1
            for left, right, bottom, top in node_boxes.values()
            if left < xlim[0] or right > xlim[1] or bottom < ylim[0] or top > ylim[1]
        )
        if outside == 0:
            break

        width_in, height_in = fig.get_size_inches()
        fig.set_size_inches(width_in * canvas_growth, height_in * canvas_growth, forward=True)

    return node_boxes


def _capture_original_locations(nodes: list[dict]) -> dict[int, list[float]]:
    """
    Capture current node locations for later layout resets.

    The canvas-fitting step runs multiple layout passes; keeping the original
    locations makes those passes deterministic.

    inputs:
        nodes: List of serialized node dicts.
    returns:
        Mapping id(node_dict) -> [x, y_top] location list.
    """
    return {
        id(node): [
            float((node.get("loc") or (0.0, 0.0))[0]),
            float((node.get("loc") or (0.0, 0.0))[1]),
        ]
        for node in nodes
    }


def _socket_positions_map(nodes: list[dict]) -> dict[int, tuple[float, float]]:
    """
    Build a map of all socket anchor coordinates for all nodes.

    These anchors make link arrows land near the right sockets, which makes it
    easier to trace the extracted shading flow while planning a Godot mapping.

    inputs:
        nodes: List of serialized node dicts.
    returns:
        Mapping socket ptr -> (x, y) in data coordinates.
    """
    socket_pos: dict[int, tuple[float, float]] = {}
    for node in nodes:
        socket_pos.update(_compute_socket_positions(node, "inputs"))
        socket_pos.update(_compute_socket_positions(node, "outputs"))
    return socket_pos


def _draw_links(
    ax,
    links: list[dict],
    nodes_by_ptr: dict,
    socket_pos: dict[int, tuple[float, float]],
    patches,
    label_links: bool,
) -> None:
    """
    Draw node-link arrows (and optional labels) onto an axes.

    This makes it easier to follow the extracted shading flow when comparing to
    Blender and when planning how it should become Godot material parameters.

    inputs:
        ax: Matplotlib axes to draw on.
        links: Serialized link dicts.
        nodes_by_ptr: Mapping node ptr -> node dict.
        socket_pos: Mapping socket ptr -> (x, y).
        patches: matplotlib.patches module-like object.
        label_links: Whether to render link labels at arrow midpoints.
    returns:
        None. Mutates the axes by drawing link artists.
    """
    for link in links:
        from_node_ptr = (link.get("from_node") or {}).get("ptr")
        to_node_ptr = (link.get("to_node") or {}).get("ptr")
        from_sock_ptr = (link.get("from_socket") or {}).get("ptr")
        to_sock_ptr = (link.get("to_socket") or {}).get("ptr")

        from_node = nodes_by_ptr.get(from_node_ptr)
        to_node = nodes_by_ptr.get(to_node_ptr)
        if not from_node or not to_node:
            continue

        fx, fy_top, fw, fh = _node_geometry(from_node)
        tx, ty_top, _, th = _node_geometry(to_node)
        start = socket_pos.get(from_sock_ptr) or (fx + fw, fy_top - fh * 0.5)
        end = socket_pos.get(to_sock_ptr) or (tx, ty_top - th * 0.5)

        arrow = patches.FancyArrowPatch(
            start,
            end,
            arrowstyle="->",
            mutation_scale=8,
            linewidth=1.0,
            color="#1f77b4",
            alpha=0.75,
            zorder=1,
        )
        ax.add_patch(arrow)

        if not label_links:
            continue
        from_socket_name = ((link.get("from_socket") or {}).get("name") or "").strip()
        to_socket_name = ((link.get("to_socket") or {}).get("name") or "").strip()
        label = f"{from_socket_name} -> {to_socket_name}".strip(" ->")
        if not label:
            continue
        ax.text(
            (start[0] + end[0]) * 0.5,
            (start[1] + end[1]) * 0.5,
            label,
            fontsize=6,
            color="#1f77b4",
            zorder=2,
        )


def _draw_node(
    ax,
    node: dict,
    detail_meta: dict[int, dict[str, object]],
    detail_font_size: float,
    detail_gap_y: float,
    detail_offset_x: float,
    draw_sockets: bool,
    hide_socket_details: bool,
    socket_pos: dict[int, tuple[float, float]],
    patches,
) -> None:
    """
    Draw a single node rectangle, title, socket dots, and detail text.

    The detail text is intentionally verbose: it exposes identifiers and defaults
    that often become Godot uniforms/parameters, which helps validate conversion
    logic for Blender-only (non-glTF) nodes.

    inputs:
        ax: Matplotlib axes to draw on.
        node: Serialized node dict.
        detail_meta: Per-node detail text metadata map.
        detail_font_size: Font size used for detail text.
        detail_gap_y: Vertical gap below node for detail text.
        detail_offset_x: Horizontal offset for detail text anchor.
        draw_sockets: Whether to draw socket markers.
        hide_socket_details: Whether to suppress detail text.
        socket_pos: Mapping socket ptr -> (x, y).
        patches: matplotlib.patches module-like object.
    returns:
        None. Mutates the axes by drawing node artists.
    """
    x, y_top, width, height = _node_geometry(node)

    rect = patches.Rectangle(
        (x, y_top - height),
        width,
        height,
        linewidth=1.0,
        edgecolor="#222222",
        facecolor="#f0f0f0",
        alpha=0.95,
        zorder=3,
    )
    ax.add_patch(rect)

    title = _node_title(node)
    if len(title) > 38:
        title = title[:35] + "..."
    ax.text(
        x + width * 0.5,
        y_top - height * 0.5,
        title,
        ha="center",
        va="center",
        fontsize=7,
        color="#111111",
        zorder=4,
    )

    if draw_sockets:
        for side in ("inputs", "outputs"):
            for socket in node.get(side) or []:
                ptr = socket.get("ptr")
                if not ptr:
                    continue
                pos = socket_pos.get(int(ptr))
                if not pos:
                    continue
                color = "#2ca02c" if socket.get("is_linked") else "#777777"
                ax.plot([pos[0]], [pos[1]], marker="o", markersize=2.5, color=color, zorder=5)

    if hide_socket_details:
        return

    draw_lines = (detail_meta.get(id(node), {}) or {}).get("draw_lines") or []
    if not draw_lines:
        return
    ax.text(
        x + detail_offset_x,
        y_top - height - detail_gap_y,
        "\n".join(draw_lines),
        ha="left",
        va="top",
        fontsize=detail_font_size,
        linespacing=1.1,
        family="monospace",
        color="#111111",
        zorder=4,
    )


def main() -> None:
    """
    CLI entrypoint: render a node graph from exported JSON.

    Use this while iterating on extraction and conversion:
    - confirm nodes/links match Blender's graph
    - inspect socket defaults and identifiers
    - generate a PNG next to the JSON snapshot for quick review

    inputs:
        Reads a JSON path and optional flags from argparse.
    returns:
        None. Saves an image (when --out is provided) or shows a window.
    """
    parser = argparse.ArgumentParser(
        description="Plot node layout + links from read_blend.py JSON.",
    )
    parser.add_argument(
        "json_path",
        type=Path,
        help="Path to the JSON exported by read_blend.py",
    )
    parser.add_argument(
        "--material",
        default=None,
        help="Material name to plot (defaults to first with a node tree)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output image filename (graph.png). Saved under "
            "Material Outputs/<blend-stem>/. If omitted, shows a window."
        ),
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=160,
        help="DPI for saved output (default: 160)",
    )
    parser.add_argument(
        "--draw-sockets",
        action="store_true",
        help="Draw small markers for sockets (green=linked, gray=unlinked)",
    )
    parser.add_argument(
        "--label-links",
        action="store_true",
        help="Label links with socket names (can get cluttered)",
    )
    parser.add_argument(
        "--hide-socket-details",
        action="store_true",
        help="Hide per-node Input/Output JSON lines",
    )
    parser.add_argument(
        "--socket-wrap-width",
        type=int,
        default=72,
        help="Approximate characters before Input/Output JSON wraps",
    )
    args = parser.parse_args()

    data = json.loads(args.json_path.read_text(encoding="utf-8"))
    material = _pick_material(data, args.material)
    node_tree = material.get("node_tree") or {}
    nodes = node_tree.get("nodes") or []
    links = node_tree.get("links") or []
    original_locations = _capture_original_locations(nodes)

    if not nodes:
        raise SystemExit("Selected material has no exported nodes to plot.")

    try:
        import matplotlib.pyplot as plt
        from matplotlib import patches
    except ModuleNotFoundError:
        import sys

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit("matplotlib is missing for this Python runtime.")
        try:
            install = input("matplotlib is missing. Install now? [y/N]: ").strip().lower()
        except EOFError:
            install = ""
        if install in {"y", "yes"}:
            import subprocess

            subprocess.run([sys.executable, "-m", "pip", "install", "matplotlib"], check=True)
            import matplotlib.pyplot as plt
            from matplotlib import patches
        else:
            raise SystemExit("matplotlib is required for plotting.")

    detail_font_size = 4.5
    detail_line_height = 11.5
    detail_char_width = 9.2
    detail_gap_y = 8.0
    detail_offset_x = 2.0

    detail_meta = _build_detail_meta(
        nodes,
        hide_socket_details=args.hide_socket_details,
        wrap_width=args.socket_wrap_width,
    )

    node_boxes = _auto_space_nodes(
        nodes,
        detail_meta=detail_meta,
        detail_gap_y=detail_gap_y,
        detail_offset_x=detail_offset_x,
        line_height=detail_line_height,
        char_width=detail_char_width,
    )

    min_x, max_x, min_y, max_y = _boxes_extents(node_boxes)

    fig, ax = plt.subplots(figsize=(12, 8))
    ax.set_title(f"\"{material.get('name', '<material>')}\" {node_tree.get('name', '<node_tree>')}")
    rough_pad = 180.0
    ax.set_xlim(min_x - rough_pad, max_x + rough_pad)
    ax.set_ylim(min_y - rough_pad, max_y + rough_pad)

    _fit_canvas_layout(
        ax,
        fig,
        nodes=nodes,
        base_locs=original_locations,
        detail_meta=detail_meta,
        detail_font_size=detail_font_size,
        detail_gap_y=detail_gap_y,
        detail_offset_x=detail_offset_x,
    )

    nodes_by_ptr = {node.get("ptr"): node for node in nodes if node.get("ptr")}
    socket_pos = _socket_positions_map(nodes)
    _draw_links(ax, links, nodes_by_ptr, socket_pos, patches, args.label_links)
    for node in nodes:
        _draw_node(
            ax,
            node,
            detail_meta=detail_meta,
            detail_font_size=detail_font_size,
            detail_gap_y=detail_gap_y,
            detail_offset_x=detail_offset_x,
            draw_sockets=args.draw_sockets,
            hide_socket_details=args.hide_socket_details,
            socket_pos=socket_pos,
            patches=patches,
        )

    ax.axis("off")

    fig.tight_layout()

    if args.out:
        blend_stem = _blend_stem_from_export(data, args.json_path)
        out_dir = _material_outputs_dir(blend_stem)
        out_name = _clean_output_filename(str(args.out), default_suffix=".png")
        out_path = out_dir / out_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=args.dpi)
        print(f"Wrote {out_path}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
