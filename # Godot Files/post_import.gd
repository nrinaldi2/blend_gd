@tool
extends EditorScenePostImport


# post_import.gd
#
# This script is the Godot side of the workflow.
# It reads the JSON exported from read_blend.py,
# checks which materials are supported, builds
# shader code from the exported graph data, and
# applies the new materials to the imported mesh.
#
# In the workflow, this file is the reconstruction
# step on the Godot side. It takes the exported
# Blender material data and turns it into working
# Godot materials during import.



const JSON_PATH_OVERRIDE := ""
const DEBUG_LOG_SUMMARY := true
const JSON_CANDIDATE_PATTERNS := [
	"{stem}.json",
	"Material Outputs/{stem}/{stem}.json",
	"{stem}/{stem}.json",
	"json/{stem}.json",
	"materials/{stem}.json"
]

const SUPPORTED_NODE_TYPES := {
	"ShaderNodeOutputMaterial": true,
	"ShaderNodeTexImage": true,
	"ShaderNodeMix": true,
	"ShaderNodeValToRGB": true,
	"ShaderNodeFresnel": true,
	"ShaderNodeBsdfDiffuse": true,
	"ShaderNodeShaderToRGB": true,
	"ShaderNodeRGB": true,
	"ShaderNodeValue": true,
	"ShaderNodeMath": true,
	"ShaderNodeVectorMath": true,
	"ShaderNodeTexCoord": true,
	"ShaderNodeUVMap": true,
	"ShaderNodeMapping": true,
	"ShaderNodeNormalMap": true,
	"ShaderNodeBsdfPrincipled": true
}

# Godot calls this after the scene import finishes.
# It loads the matching JSON export, keeps only the materials this importer can handle,
# builds shader code from that data, and swaps the generated materials onto matching surfaces.
func _post_import(scene: Node) -> Object:
	var source_file = get_source_file()
	var json_path = _find_json_path(source_file)
	if json_path.is_empty():
		_warn("No JSON found for %s." % source_file)
		return scene

	var json_root = _load_json_dictionary(json_path)
	if json_root.is_empty():
		_warn("JSON could not be parsed: %s" % json_path)
		return scene

	var materials = _get_importable_materials(json_root)
	if materials.is_empty():
		_warn("No import-ready graph materials were found in %s." % json_path)
		return scene

	if DEBUG_LOG_SUMMARY:
		print("[BlenderImporter] Source: %s" % source_file)
		print("[BlenderImporter] JSON: %s" % json_path)
		print("[BlenderImporter] Import-ready materials: %d" % materials.size())

	var total_applied = 0
	for material_dict in materials:
		var material_label = _material_label(material_dict)
		var ir_result = _build_material_ir(material_dict, json_path)
		if ir_result.is_empty():
			_warn("Could not build graph IR for %s." % material_label)
			continue

		var compiled = _compile_material_ir(ir_result, material_dict, json_path)
		if compiled.is_empty():
			_warn("Could not compile shader code for %s." % material_label)
			continue

		if DEBUG_LOG_SUMMARY:
			_log_material_debug_summary(material_dict, ir_result, compiled)

		var applied = _apply_compiled_material(scene, material_dict, compiled)
		total_applied += applied
		if applied <= 0:
			_warn("The importer built a shader for %s but found no matching surface." % material_label)
		else:
			print("[BlenderImporter] Applied generated shader for %s to %d surface(s)." % [material_label, applied])

	if total_applied <= 0:
		_warn("The importer did not apply any generated materials.")
	elif DEBUG_LOG_SUMMARY:
		print("[BlenderImporter] Total applied surfaces: %d" % total_applied)

	return scene

# This filters the exported material list down to the ones this importer can actually use.
# The readiness check still reads the exporter's older JSON tag name because that is part of the current data contract.
func _get_importable_materials(json_root: Dictionary) -> Array:
	var result: Array = []
	var materials_variant = json_root.get("materials", [])
	if typeof(materials_variant) != TYPE_ARRAY:
		return result

	for value in materials_variant:
		if not (value is Dictionary):
			continue
		var material_dict: Dictionary = value
		var graph_status: Dictionary = material_dict.get("graph_export_status", {})
		if not bool(graph_status.get("exported", false)):
			continue
		var analysis: Dictionary = material_dict.get("analysis", {})
		var readiness = String(analysis.get("stage1_readiness", ""))
		if readiness != "ready_for_stage2":
			continue
		if typeof(material_dict.get("node_graph", {})) != TYPE_DICTIONARY:
			continue
		result.append(material_dict)
	return result

# This turns one exported material graph into an intermediate data structure the compiler can use.
# It follows the active surface output, gathers the expressions that feed it, and keeps the texture and debug info tied to that graph.
func _build_material_ir(material_dict: Dictionary, json_path: String) -> Dictionary:
	var graph: Dictionary = material_dict.get("node_graph", {})
	if graph.is_empty():
		return {}

	var output_branches: Dictionary = graph.get("output_branches", {})
	var surface_branch: Dictionary = output_branches.get("surface", {})
	if not bool(surface_branch.get("connected", false)):
		_warn("Material '%s' has no connected surface output." % String(material_dict.get("name", "")))
		return {}

	var target: Dictionary = surface_branch.get("target", {})
	var from_node_id = String(target.get("from_node_id", ""))
	var from_socket_id = String(target.get("from_socket_id", ""))
	var from_socket_identifier = String(target.get("from_socket_identifier", ""))
	if from_node_id.is_empty() or from_socket_id.is_empty():
		_warn("Material '%s' has an incomplete surface target." % String(material_dict.get("name", "")))
		return {}

	var ctx = _build_graph_context(graph, json_path)
	var surface_expr = _build_output_ir(ctx, from_node_id, from_socket_identifier, from_socket_id)
	if surface_expr.is_empty():
		_warn("Material '%s' could not build graph data for the surface output." % String(material_dict.get("name", "")))
		return {}

	var surface_type = String(surface_expr.get("result_type", ""))
	if surface_type != "color" and surface_type != "shader":
		surface_expr = _coerce_expr(surface_expr, "color")

	return {
		"kind": "material_output",
		"material_name": String(material_dict.get("name", "")),
		"surface": surface_expr,
		"targeting": material_dict.get("targeting", {}),
		"textures": ctx.get("textures", []),
		"analysis": material_dict.get("analysis", {}),
		"build_notes": ctx.get("notes", []),
		"unsupported_nodes": ctx.get("unsupported_nodes", []),
		"used_nodes": ctx.get("used_nodes", []),
		"surface_target": target
	}

# This builds the lookup tables used while walking the exported node graph.
# It stores nodes, links, socket mappings, cached expressions, and the texture list built during graph traversal.
func _build_graph_context(graph: Dictionary, json_path: String) -> Dictionary:
	var nodes_by_id = {}
	var input_links = {}
	var socket_outputs = {}

	var nodes_variant = graph.get("nodes", [])
	if typeof(nodes_variant) == TYPE_ARRAY:
		for value in nodes_variant:
			if not (value is Dictionary):
				continue
			var node_dict: Dictionary = value
			nodes_by_id[String(node_dict.get("id", ""))] = node_dict
			var outputs_variant = node_dict.get("outputs", [])
			if typeof(outputs_variant) == TYPE_ARRAY:
				for output_socket in outputs_variant:
					if output_socket is Dictionary:
						socket_outputs[String(output_socket.get("id", ""))] = {
							"node_id": String(node_dict.get("id", "")),
							"socket": output_socket
						}

	var links_variant = graph.get("links", [])
	if typeof(links_variant) == TYPE_ARRAY:
		for value in links_variant:
			if not (value is Dictionary):
				continue
			var link_dict: Dictionary = value
			input_links[String(link_dict.get("to_socket_id", ""))] = link_dict

	return {
		"graph": graph,
		"json_path": json_path,
		"nodes_by_id": nodes_by_id,
		"input_links": input_links,
		"socket_outputs": socket_outputs,
		"expr_cache": {},
		"textures": [],
		"next_texture_index": 0,
		"notes": [],
		"unsupported_nodes": [],
		"used_nodes": []
	}

func _build_output_ir(ctx: Dictionary, node_id: String, socket_identifier: String, socket_id: String) -> Dictionary:
	var cache_key = "%s|%s" % [node_id, socket_id]
	var expr_cache: Dictionary = ctx.get("expr_cache", {})
	if expr_cache.has(cache_key):
		return expr_cache[cache_key]

	var nodes_by_id: Dictionary = ctx.get("nodes_by_id", {})
	var node: Dictionary = nodes_by_id.get(node_id, {})
	if node.is_empty():
		_trace_note(ctx, "Missing node for output lookup: %s" % node_id)
		return {}

	_record_used_node(ctx, node, socket_identifier, socket_id)
	_trace_note(ctx, "Build output IR -> %s.%s [%s]" % [_node_debug_label(node), socket_identifier if not socket_identifier.is_empty() else "<default>", socket_id])

	var node_type = String(node.get("idname", ""))
	if not SUPPORTED_NODE_TYPES.has(node_type):
		_record_unsupported_node(ctx, node, socket_identifier, socket_id)
		return {}

	var expr = {}
	if node_type == "ShaderNodeTexImage":
		expr = _build_tex_image_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeMix":
		expr = _build_mix_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeValToRGB":
		expr = _build_color_ramp_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeFresnel":
		expr = _build_fresnel_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeBsdfDiffuse":
		expr = _build_diffuse_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeShaderToRGB":
		expr = _build_shader_to_rgb_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeRGB":
		expr = _build_rgb_ir(node, socket_identifier)
	elif node_type == "ShaderNodeValue":
		expr = _build_value_ir(node, socket_identifier)
	elif node_type == "ShaderNodeMath":
		expr = _build_math_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeVectorMath":
		expr = _build_vector_math_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeTexCoord":
		expr = _build_tex_coord_ir(node, socket_identifier)
	elif node_type == "ShaderNodeUVMap":
		expr = _build_uv_map_ir(node, socket_identifier)
	elif node_type == "ShaderNodeMapping":
		expr = _build_mapping_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeNormalMap":
		expr = _build_normal_map_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeBsdfPrincipled":
		expr = _build_principled_bsdf_ir(ctx, node, socket_identifier)
	elif node_type == "ShaderNodeOutputMaterial":
		expr = _build_output_material_ir(ctx, node)

	if not expr.is_empty():
		expr_cache[cache_key] = expr
		ctx["expr_cache"] = expr_cache

	return expr

func _build_tex_image_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	var image_payload: Dictionary = node.get("image", {})
	var export_payload: Dictionary = image_payload.get("export", {})
	var rel_path = String(export_payload.get("relative_path", ""))
	if rel_path.is_empty():
		_warn("Image Texture node '%s' does not have an exported relative path." % String(node.get("ui_name", "")))
		return {}

	var uniform_name = _register_texture_binding(ctx, node, rel_path)
	var vector_expr = _resolve_linked_texture_vector_expr(ctx, node)

	if socket_identifier == "Alpha":
		return {
			"kind": "texture_alpha",
			"result_type": "float",
			"uniform_name": uniform_name,
			"uv": vector_expr,
			"node_id": String(node.get("id", ""))
		}

	return {
		"kind": "image_texture",
		"result_type": "color",
		"uniform_name": uniform_name,
		"uv": vector_expr,
		"node_id": String(node.get("id", ""))
	}

func _build_mix_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	var result_type = "color"
	var a_identifier = "A_Color"
	var b_identifier = "B_Color"
	if socket_identifier == "Result_Float":
		result_type = "float"
		a_identifier = "A_Float"
		b_identifier = "B_Float"
	elif socket_identifier == "Result_Vector":
		result_type = "vector"
		a_identifier = "A_Vector"
		b_identifier = "B_Vector"
	elif socket_identifier == "Result_Color" or socket_identifier.is_empty():
		result_type = "color"

	var factor_expr = _resolve_input_expr(ctx, node, "Factor_Float", "float")
	if factor_expr.is_empty():
		factor_expr = _resolve_input_expr_any(ctx, node, ["Factor", "Fac"], "float")
	var a_expr = _resolve_input_expr(ctx, node, a_identifier, result_type)
	var b_expr = _resolve_input_expr(ctx, node, b_identifier, result_type)
	if factor_expr.is_empty() or a_expr.is_empty() or b_expr.is_empty():
		return {}

	return {
		"kind": "mix",
		"result_type": result_type,
		"node_id": String(node.get("id", "")),
		"blend_type": String(node.get("properties", {}).get("blend_type", "MIX")),
		"use_clamp": bool(node.get("properties", {}).get("use_clamp", false)),
		"factor": factor_expr,
		"a": a_expr,
		"b": b_expr
	}

func _build_color_ramp_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	var fac_expr = _resolve_input_expr(ctx, node, "Fac", "float")
	if fac_expr.is_empty():
		return {}

	var stops_variant = node.get("color_ramp", [])
	if typeof(stops_variant) != TYPE_ARRAY:
		_warn("Color Ramp node '%s' does not have exported stop data." % String(node.get("ui_name", "")))
		return {}
	var stops_array: Array = stops_variant
	if stops_array.is_empty():
		_warn("Color Ramp node '%s' does not have exported stop data." % String(node.get("ui_name", "")))
		return {}

	var result_type = "color"
	if socket_identifier == "Alpha":
		result_type = "float"

	return {
		"kind": "color_ramp",
		"result_type": result_type,
		"node_id": String(node.get("id", "")),
		"interpolation": String(node.get("properties", {}).get("ramp_settings", {}).get("interpolation", "LINEAR")),
		"stops": stops_array,
		"fac": fac_expr
	}

func _build_rgb_ir(node: Dictionary, socket_identifier: String) -> Dictionary:
	if not socket_identifier.is_empty() and socket_identifier != "Color":
		return {}
	var output_socket = _find_output_socket_by_identifier(node, "Color")
	if output_socket.is_empty():
		return {}
	return {
		"kind": "color_constant",
		"result_type": "color",
		"value": _variant_to_color_array(output_socket.get("default", [1.0, 1.0, 1.0, 1.0])),
		"node_id": String(node.get("id", ""))
	}

func _build_value_ir(node: Dictionary, socket_identifier: String) -> Dictionary:
	if not socket_identifier.is_empty() and socket_identifier != "Value":
		return {}
	var output_socket = _find_output_socket_by_identifier(node, "Value")
	if output_socket.is_empty():
		return {}
	return {
		"kind": "float_constant",
		"result_type": "float",
		"value": float(output_socket.get("default", 0.0)),
		"node_id": String(node.get("id", ""))
	}

func _build_math_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if not socket_identifier.is_empty() and socket_identifier != "Value":
		return {}
	var a_expr = _resolve_input_expr_by_index(ctx, node, 0, "float")
	var b_expr = _resolve_input_expr_by_index(ctx, node, 1, "float")
	var c_expr = _resolve_input_expr_by_index(ctx, node, 2, "float")
	if a_expr.is_empty():
		return {}
	if b_expr.is_empty():
		b_expr = {"kind": "float_constant", "result_type": "float", "value": 0.0}
	return {
		"kind": "math",
		"result_type": "float",
		"node_id": String(node.get("id", "")),
		"operation": String(node.get("properties", {}).get("operation", "ADD")),
		"use_clamp": bool(node.get("properties", {}).get("use_clamp", false)),
		"a": a_expr,
		"b": b_expr,
		"c": c_expr
	}

func _build_vector_math_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	var result_type = "vector"
	if socket_identifier == "Value":
		result_type = "float"
	elif socket_identifier == "Scale":
		result_type = "float"
	elif socket_identifier.is_empty() or socket_identifier == "Vector":
		result_type = "vector"
	else:
		return {}

	var a_expr = _resolve_input_expr_by_index(ctx, node, 0, "vector")
	var b_expr = _resolve_input_expr_by_index(ctx, node, 1, "vector")
	var scale_expr = _resolve_input_expr_any(ctx, node, ["Scale"], "float")
	var c_expr = _resolve_input_expr_by_index(ctx, node, 2, "float")
	if a_expr.is_empty():
		return {}
	if b_expr.is_empty():
		b_expr = {"kind": "vector_constant", "result_type": "vector", "value": [0.0, 0.0, 0.0]}
	if scale_expr.is_empty():
		scale_expr = c_expr
	return {
		"kind": "vector_math",
		"result_type": result_type,
		"node_id": String(node.get("id", "")),
		"operation": String(node.get("properties", {}).get("operation", "ADD")),
		"a": a_expr,
		"b": b_expr,
		"scale": scale_expr,
		"c": c_expr
	}

func _build_tex_coord_ir(node: Dictionary, socket_identifier: String) -> Dictionary:
	var code = ""
	if socket_identifier == "Generated" or socket_identifier == "Object":
		code = "VERTEX"
	elif socket_identifier == "Normal":
		code = "normalize(NORMAL)"
	elif socket_identifier == "UV" or socket_identifier.is_empty():
		code = "vec3(UV, 0.0)"
	elif socket_identifier == "Camera":
		code = "VIEW"
	elif socket_identifier == "Window":
		code = "vec3(SCREEN_UV, 0.0)"
	elif socket_identifier == "Reflection":
		code = "reflect(-normalize(VIEW), normalize(NORMAL))"
	else:
		return {}
	return {
		"kind": "builtin_vector",
		"result_type": "vector",
		"code": code,
		"node_id": String(node.get("id", ""))
	}

func _build_uv_map_ir(node: Dictionary, socket_identifier: String) -> Dictionary:
	if not socket_identifier.is_empty() and socket_identifier != "UV":
		return {}

	var uv_map_name = String(node.get("properties", {}).get("uv_map", ""))

	return {
		"kind": "builtin_vector",
		"result_type": "vector",
		"code": "vec3(UV, 0.0)",
		"node_id": String(node.get("id", "")),
		"uv_map": uv_map_name
	}

func _build_mapping_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if not socket_identifier.is_empty() and socket_identifier != "Vector":
		return {}
	var vector_expr = _resolve_input_expr(ctx, node, "Vector", "vector")
	if vector_expr.is_empty():
		vector_expr = _default_uv_expr()
	var location_expr = _resolve_input_expr(ctx, node, "Location", "vector")
	if location_expr.is_empty():
		location_expr = {"kind": "vector_constant", "result_type": "vector", "value": [0.0, 0.0, 0.0]}
	var rotation_expr = _resolve_input_expr(ctx, node, "Rotation", "vector")
	if rotation_expr.is_empty():
		rotation_expr = {"kind": "vector_constant", "result_type": "vector", "value": [0.0, 0.0, 0.0]}
	var scale_expr = _resolve_input_expr(ctx, node, "Scale", "vector")
	if scale_expr.is_empty():
		scale_expr = {"kind": "vector_constant", "result_type": "vector", "value": [1.0, 1.0, 1.0]}
	return {
		"kind": "mapping",
		"result_type": "vector",
		"node_id": String(node.get("id", "")),
		"vector": vector_expr,
		"location": location_expr,
		"rotation": rotation_expr,
		"scale": scale_expr,
		"vector_type": String(node.get("properties", {}).get("vector_type", "POINT"))
	}

func _build_normal_map_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if not socket_identifier.is_empty() and socket_identifier != "Normal":
		return {}
	var color_expr = _resolve_input_expr_any(ctx, node, ["Color"], "color")
	if color_expr.is_empty():
		return {}
	var strength_expr = _resolve_input_expr_any(ctx, node, ["Strength"], "float")
	if strength_expr.is_empty():
		strength_expr = {"kind": "float_constant", "result_type": "float", "value": 1.0}
	return {
		"kind": "normal_map",
		"result_type": "vector",
		"node_id": String(node.get("id", "")),
		"color": color_expr,
		"strength": strength_expr,
		"space": String(node.get("properties", {}).get("space", "TANGENT"))
	}
	
func _resolve_linked_normal_expr(ctx: Dictionary, node: Dictionary, input_identifier: String = "Normal") -> Dictionary:
	var socket = _find_socket_by_identifier(node.get("inputs", []), input_identifier)
	if socket.is_empty():
		return {}
	if not bool(socket.get("is_linked", false)):
		return {}
	return _resolve_input_socket_expr(ctx, node, socket, "vector")
	
func _resolve_linked_texture_vector_expr(ctx: Dictionary, node: Dictionary) -> Dictionary:
	var socket = _find_socket_by_identifier(node.get("inputs", []), "Vector")
	if socket.is_empty():
		return _default_uv_expr()
	if not bool(socket.get("is_linked", false)):
		return _default_uv_expr()

	var expr = _resolve_input_socket_expr(ctx, node, socket, "vector")
	if expr.is_empty():
		return _default_uv_expr()
	return expr

func _build_fresnel_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if socket_identifier != "Fac" and not socket_identifier.is_empty():
		return {}

	var ior_expr = _resolve_input_expr(ctx, node, "IOR", "float")
	if ior_expr.is_empty():
		return {}

	var normal_expr = _resolve_linked_normal_expr(ctx, node, "Normal")
	return {
		"kind": "fresnel",
		"result_type": "float",
		"node_id": String(node.get("id", "")),
		"ior": ior_expr,
		"normal": normal_expr
	}

func _build_diffuse_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if socket_identifier != "BSDF" and not socket_identifier.is_empty():
		return {}

	var color_expr = _resolve_input_expr(ctx, node, "Color", "color")
	if color_expr.is_empty():
		return {}
	var roughness_expr = _resolve_input_expr(ctx, node, "Roughness", "float")
	if roughness_expr.is_empty():
		roughness_expr = {"kind": "float_constant", "result_type": "float", "value": 0.0}
	var normal_expr = _resolve_linked_normal_expr(ctx, node, "Normal")

	return {
		"kind": "diffuse_bsdf",
		"result_type": "shader",
		"node_id": String(node.get("id", "")),
		"color": color_expr,
		"roughness": roughness_expr,
		"normal": normal_expr
	}

func _build_principled_bsdf_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if socket_identifier != "BSDF" and not socket_identifier.is_empty():
		return {}

	var base_color_expr = _resolve_input_expr_any(ctx, node, ["Base Color"], "color")
	if base_color_expr.is_empty():
		base_color_expr = {"kind": "color_constant", "result_type": "color", "value": [0.8, 0.8, 0.8, 1.0]}
	var metallic_expr = _resolve_input_expr_any(ctx, node, ["Metallic"], "float")
	if metallic_expr.is_empty():
		metallic_expr = {"kind": "float_constant", "result_type": "float", "value": 0.0}
	var roughness_expr = _resolve_input_expr_any(ctx, node, ["Roughness"], "float")
	if roughness_expr.is_empty():
		roughness_expr = {"kind": "float_constant", "result_type": "float", "value": 0.5}
	var normal_expr = _resolve_linked_normal_expr(ctx, node, "Normal")
	var alpha_expr = _resolve_input_expr_any(ctx, node, ["Alpha"], "float")
	if alpha_expr.is_empty():
		alpha_expr = {"kind": "float_constant", "result_type": "float", "value": 1.0}
	var emission_color_expr = _resolve_input_expr_any(ctx, node, ["Emission Color", "Emission"], "color")
	if emission_color_expr.is_empty():
		emission_color_expr = {"kind": "color_constant", "result_type": "color", "value": [0.0, 0.0, 0.0, 1.0]}
	var emission_strength_expr = _resolve_input_expr_any(ctx, node, ["Emission Strength"], "float")
	if emission_strength_expr.is_empty():
		emission_strength_expr = {"kind": "float_constant", "result_type": "float", "value": 1.0}
	var specular_expr = _resolve_input_expr_any(ctx, node, ["Specular IOR Level", "Specular"], "float")
	if specular_expr.is_empty():
		specular_expr = {"kind": "float_constant", "result_type": "float", "value": 0.5}

	return {
		"kind": "principled_bsdf",
		"result_type": "shader",
		"node_id": String(node.get("id", "")),
		"base_color": base_color_expr,
		"metallic": metallic_expr,
		"roughness": roughness_expr,
		"normal": normal_expr,
		"alpha": alpha_expr,
		"emission_color": emission_color_expr,
		"emission_strength": emission_strength_expr,
		"specular": specular_expr,
		"distribution": String(node.get("properties", {}).get("distribution", ""))
	}

func _build_shader_to_rgb_ir(ctx: Dictionary, node: Dictionary, socket_identifier: String) -> Dictionary:
	if socket_identifier == "Alpha":
		return {
			"kind": "float_constant",
			"result_type": "float",
			"value": 1.0,
			"node_id": String(node.get("id", ""))
		}

	var shader_expr = _resolve_input_expr(ctx, node, "Shader", "shader")
	if shader_expr.is_empty():
		return {}

	return {
		"kind": "shader_to_rgb",
		"result_type": "color",
		"node_id": String(node.get("id", "")),
		"shader": shader_expr
	}

func _build_output_material_ir(ctx: Dictionary, node: Dictionary) -> Dictionary:
	var surface_expr = _resolve_input_expr(ctx, node, "Surface", "")
	if surface_expr.is_empty():
		return {}
	return {
		"kind": "material_output",
		"result_type": String(surface_expr.get("result_type", "material_output")),
		"node_id": String(node.get("id", "")),
		"surface": surface_expr
	}

func _resolve_input_expr(ctx: Dictionary, node: Dictionary, input_identifier: String, expected_type: String) -> Dictionary:
	var socket = _find_socket_by_identifier(node.get("inputs", []), input_identifier)
	if socket.is_empty():
		_trace_note(ctx, "Input not found: %s.%s" % [_node_debug_label(node), input_identifier])
		return {}
	return _resolve_input_socket_expr(ctx, node, socket, expected_type)

func _resolve_input_expr_any(ctx: Dictionary, node: Dictionary, candidates: Array, expected_type: String) -> Dictionary:
	var socket = _find_socket_by_candidates(node.get("inputs", []), candidates)
	if socket.is_empty():
		return {}
	return _resolve_input_socket_expr(ctx, node, socket, expected_type)

func _resolve_input_expr_by_index(ctx: Dictionary, node: Dictionary, input_index: int, expected_type: String) -> Dictionary:
	var socket = _find_socket_by_index(node.get("inputs", []), input_index)
	if socket.is_empty():
		return {}
	return _resolve_input_socket_expr(ctx, node, socket, expected_type)

func _resolve_input_socket_expr(ctx: Dictionary, node: Dictionary, socket: Dictionary, expected_type: String) -> Dictionary:
	var socket_id = String(socket.get("id", ""))
	var input_links: Dictionary = ctx.get("input_links", {})
	var expr = {}
	if input_links.has(socket_id):
		var link: Dictionary = input_links[socket_id]
		_trace_note(ctx, "Resolve linked input -> %s.%s <= %s.%s" % [
			_node_debug_label(node),
			String(socket.get("identifier", "")),
			String(link.get("from_node_id", "")),
			String(link.get("from_socket_identifier", ""))
		])
		expr = _build_output_ir(
			ctx,
			String(link.get("from_node_id", "")),
			String(link.get("from_socket_identifier", "")),
			String(link.get("from_socket_id", ""))
		)
	else:
		_trace_note(ctx, "Resolve constant input -> %s.%s" % [_node_debug_label(node), String(socket.get("identifier", ""))])
		expr = _constant_expr_for_socket(socket)

	if expr.is_empty():
		return {}
	if expected_type.is_empty():
		return expr
	if String(expr.get("result_type", "")) == expected_type:
		return expr
	return _coerce_expr(expr, expected_type)

func _find_socket_by_identifier(sockets_variant, wanted_identifier: String) -> Dictionary:
	if typeof(sockets_variant) != TYPE_ARRAY:
		return {}
	for value in sockets_variant:
		if value is Dictionary and String(value.get("identifier", "")) == wanted_identifier:
			return value
	return {}

func _find_socket_by_candidates(sockets_variant, candidates: Array) -> Dictionary:
	if typeof(sockets_variant) != TYPE_ARRAY:
		return {}
	for candidate in candidates:
		var candidate_string = String(candidate)
		for value in sockets_variant:
			if value is Dictionary:
				if String(value.get("identifier", "")) == candidate_string:
					return value
				if String(value.get("name", "")) == candidate_string:
					return value
	return {}

func _find_socket_by_index(sockets_variant, wanted_index: int) -> Dictionary:
	if typeof(sockets_variant) != TYPE_ARRAY:
		return {}
	var sockets: Array = sockets_variant
	if wanted_index < 0 or wanted_index >= sockets.size():
		return {}
	var value = sockets[wanted_index]
	if value is Dictionary:
		return value
	return {}

func _find_output_socket_by_identifier(node: Dictionary, wanted_identifier: String) -> Dictionary:
	return _find_socket_by_identifier(node.get("outputs", []), wanted_identifier)

func _constant_expr_for_socket(socket: Dictionary) -> Dictionary:
	var socket_type = String(socket.get("socket_type", ""))
	var default_value = socket.get("default", null)
	if socket_type == "FLOAT" or socket_type == "INT" or socket_type == "BOOLEAN":
		return {"kind": "float_constant", "result_type": "float", "value": float(default_value if default_value != null else 0.0)}
	elif socket_type == "COLOR":
		return {"kind": "color_constant", "result_type": "color", "value": _variant_to_color_array(default_value)}
	elif socket_type == "VECTOR":
		return {"kind": "vector_constant", "result_type": "vector", "value": _variant_to_vector_array(default_value)}
	return {}

func _default_uv_expr() -> Dictionary:
	return {
		"kind": "builtin_vector",
		"result_type": "vector",
		"code": "vec3(UV, 0.0)"
	}

func _coerce_expr(expr: Dictionary, to_type: String) -> Dictionary:
	return {
		"kind": "coerce",
		"result_type": to_type,
		"from_type": String(expr.get("result_type", "")),
		"expr": expr
	}

func _register_texture_binding(ctx: Dictionary, node: Dictionary, relative_path: String) -> String:
	var textures: Array = ctx.get("textures", [])
	for binding in textures:
		if binding is Dictionary and String(binding.get("relative_path", "")) == relative_path:
			return String(binding.get("uniform_name", ""))

	var properties: Dictionary = node.get("properties", {})
	var image_payload: Dictionary = node.get("image", {})
	var uniform_name = "tex_%d" % int(ctx.get("next_texture_index", 0))
	ctx["next_texture_index"] = int(ctx.get("next_texture_index", 0)) + 1
	textures.append({
		"uniform_name": uniform_name,
		"relative_path": relative_path,
		"node_id": String(node.get("id", "")),
		"image_name": String(image_payload.get("image_name", "")),
		"interpolation": String(properties.get("interpolation", "Linear")),
		"extension": String(properties.get("extension", "REPEAT")),
		"colorspace": String(image_payload.get("colorspace", "sRGB"))
	})
	ctx["textures"] = textures
	return uniform_name

func _build_sampler_uniform_line(binding: Dictionary) -> String:
	var hints: Array[String] = []
	var colorspace = String(binding.get("colorspace", "")).strip_edges().to_lower()
	if colorspace != "non-color" and colorspace != "non color" and colorspace != "raw":
		hints.append("source_color")

	var extension = String(binding.get("extension", "REPEAT")).strip_edges().to_upper()
	if extension == "REPEAT" or extension == "MIRROR":
		hints.append("repeat_enable")
	else:
		hints.append("repeat_disable")

	var interpolation = String(binding.get("interpolation", "Linear")).strip_edges().to_lower()
	if interpolation == "closest" or interpolation == "nearest":
		hints.append("filter_nearest")
	else:
		hints.append("filter_linear")

	return "uniform sampler2D %s : %s;" % [
		String(binding.get("uniform_name", "tex_0")),
		", ".join(hints)
	]

func _record_unsupported_node(ctx: Dictionary, node: Dictionary, socket_identifier: String = "", socket_id: String = "") -> void:
	var unsupported_nodes: Array = ctx.get("unsupported_nodes", [])
	var record = {
		"node_id": String(node.get("id", "")),
		"idname": String(node.get("idname", "")),
		"ui_name": String(node.get("ui_name", "")),
		"socket_identifier": socket_identifier,
		"socket_id": socket_id
	}
	for existing in unsupported_nodes:
		if existing is Dictionary and String(existing.get("node_id", "")) == String(record.get("node_id", "")) and String(existing.get("socket_id", "")) == String(record.get("socket_id", "")):
			return
	unsupported_nodes.append(record)
	ctx["unsupported_nodes"] = unsupported_nodes
	_trace_note(ctx, "Unsupported node encountered -> %s.%s" % [_node_debug_label(node), socket_identifier if not socket_identifier.is_empty() else "<default>"])

# This takes the graph IR and turns it into real Godot shader code.
# It tracks which helper functions are needed, writes uniforms for textures, and collects warnings from the compile pass.
func _compile_material_ir(ir_result: Dictionary, material_dict: Dictionary, json_path: String) -> Dictionary:
	var compiler_ctx = {
		"ramp_functions": {},
		"need_luminance": false,
		"need_fresnel": false,
		"need_mix_helper": false,
		"need_shader_to_rgb": false,
		"need_mapping_helper": false,
		"textures": ir_result.get("textures", []),
		"material_name": String(material_dict.get("name", "")),
		"warnings": []
	}

	var surface_expr: Dictionary = ir_result.get("surface", {})
	if surface_expr.is_empty():
		return {}

	var uniform_lines: Array[String] = []
	uniform_lines.append("uniform vec3 importer_main_light_dir = vec3(0.0, 1.0, 0.0);")
	for binding in compiler_ctx.get("textures", []):
		if binding is Dictionary:
			uniform_lines.append(_build_sampler_uniform_line(binding))

	var fragment_lines = _build_fragment_lines(surface_expr, compiler_ctx)
	if fragment_lines.is_empty():
		return {}

	var helper_blocks: Array[String] = []
	helper_blocks.append("float importer_sat(float x) { return clamp(x, 0.0, 1.0); }")
	if bool(compiler_ctx.get("need_luminance", false)):
		helper_blocks.append("float importer_luminance(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }")
	if bool(compiler_ctx.get("need_fresnel", false)):
		helper_blocks.append("float importer_fresnel_dielectric(float cosi, float ior) { cosi = clamp(cosi, 0.0, 1.0); ior = max(ior, 1.0001); float g2 = ior * ior - 1.0 + cosi * cosi; if (g2 <= 0.0) { return 1.0; } float g = sqrt(g2); float a = (g - cosi) / (g + cosi); float b = (cosi * (g + cosi) - 1.0) / (cosi * (g - cosi) + 1.0); return 0.5 * a * a * (1.0 + b * b); }")
	if bool(compiler_ctx.get("need_shader_to_rgb", false)):
		helper_blocks.append("vec4 importer_shader_to_rgb_diffuse(vec4 diffuse_color, vec3 normal_dir) { vec3 n = normalize(normal_dir); vec3 l = normalize(importer_main_light_dir); float nl = max(dot(n, l), 0.0); return vec4(diffuse_color.rgb * nl, diffuse_color.a); }")
	if bool(compiler_ctx.get("need_mapping_helper", false)):
		helper_blocks.append("vec3 importer_rotate_x(vec3 v, float a) { float c = cos(a); float s = sin(a); return vec3(v.x, c * v.y - s * v.z, s * v.y + c * v.z); }")
		helper_blocks.append("vec3 importer_rotate_y(vec3 v, float a) { float c = cos(a); float s = sin(a); return vec3(c * v.x + s * v.z, v.y, -s * v.x + c * v.z); }")
		helper_blocks.append("vec3 importer_rotate_z(vec3 v, float a) { float c = cos(a); float s = sin(a); return vec3(c * v.x - s * v.y, s * v.x + c * v.y, v.z); }")
		helper_blocks.append("vec3 importer_apply_mapping_point(vec3 v, vec3 location, vec3 rotation, vec3 scale) { vec3 out_v = v * scale; out_v = importer_rotate_x(out_v, rotation.x); out_v = importer_rotate_y(out_v, rotation.y); out_v = importer_rotate_z(out_v, rotation.z); vec3 loc_v = location; loc_v = importer_rotate_x(loc_v, rotation.x); loc_v = importer_rotate_y(loc_v, rotation.y); loc_v = importer_rotate_z(loc_v, rotation.z); return out_v + loc_v; }")
		helper_blocks.append("vec3 importer_apply_mapping_direction(vec3 v, vec3 rotation, vec3 scale) { vec3 out_v = v * scale; out_v = importer_rotate_x(out_v, rotation.x); out_v = importer_rotate_y(out_v, rotation.y); out_v = importer_rotate_z(out_v, rotation.z); return normalize(out_v); }")
	for function_code in compiler_ctx.get("ramp_functions", {}).values():
		helper_blocks.append(function_code)

	var shader_code = "shader_type spatial;\n"
	shader_code += "render_mode __IMPORTER_RENDER_MODE__;\n\n"
	shader_code += "\n".join(uniform_lines)
	shader_code += "\n\n"
	shader_code += "\n\n".join(helper_blocks)
	shader_code += "\n\nvoid fragment() {\n"
	for line in fragment_lines:
		shader_code += "\t%s\n" % line
	shader_code += "}\n"

	return {
		"shader_code": shader_code,
		"textures": _resolve_texture_resources(compiler_ctx.get("textures", []), json_path),
		"ir": ir_result,
		"compiler_warnings": compiler_ctx.get("warnings", [])
	}

func _build_fragment_lines(surface_expr: Dictionary, compiler_ctx: Dictionary) -> Array[String]:
	var result: Array[String] = []
	var surface_type = String(surface_expr.get("result_type", ""))
	if surface_type == "shader":
		return _emit_surface_shader_assignments(surface_expr, compiler_ctx)

	var surface_code = _emit_expr(surface_expr, compiler_ctx)
	if surface_code.is_empty():
		return result
	result.append("vec4 imported_surface = %s;" % surface_code)
	result.append("ALBEDO = clamp(imported_surface.rgb, vec3(0.0), vec3(1.0));")
	result.append("ALPHA = clamp(imported_surface.a, 0.0, 1.0);")
	return result

func _emit_surface_shader_assignments(expr: Dictionary, compiler_ctx: Dictionary) -> Array[String]:
	var lines: Array[String] = []
	var kind = String(expr.get("kind", ""))
	if kind == "material_output":
		return _emit_surface_shader_assignments(expr.get("surface", {}), compiler_ctx)
	elif kind == "diffuse_bsdf":
		var color_code = _emit_expr(expr.get("color", {}), compiler_ctx)
		if color_code.is_empty():
			return []
		var roughness_code = _emit_expr(expr.get("roughness", {}), compiler_ctx)
		if roughness_code.is_empty():
			roughness_code = "0.0"
		lines.append_array(_emit_surface_normal_lines(expr.get("normal", {}), compiler_ctx))
		lines.append("vec4 imported_surface = %s;" % color_code)
		lines.append("ALBEDO = clamp(imported_surface.rgb, vec3(0.0), vec3(1.0));")
		lines.append("ROUGHNESS = clamp(%s, 0.0, 1.0);" % roughness_code)
		lines.append("ALPHA = clamp(imported_surface.a, 0.0, 1.0);")
		return lines
	elif kind == "principled_bsdf":
		var base_color_code = _emit_expr(expr.get("base_color", {}), compiler_ctx)
		if base_color_code.is_empty():
			return []
		var metallic_code = _emit_expr(expr.get("metallic", {}), compiler_ctx)
		if metallic_code.is_empty():
			metallic_code = "0.0"
		var roughness_code = _emit_expr(expr.get("roughness", {}), compiler_ctx)
		if roughness_code.is_empty():
			roughness_code = "0.5"
		var alpha_code = _emit_expr(expr.get("alpha", {}), compiler_ctx)
		if alpha_code.is_empty():
			alpha_code = "1.0"
		var specular_code = _emit_expr(expr.get("specular", {}), compiler_ctx)
		if specular_code.is_empty():
			specular_code = "0.5"
		var emission_color_code = _emit_expr(expr.get("emission_color", {}), compiler_ctx)
		if emission_color_code.is_empty():
			emission_color_code = "vec4(0.0, 0.0, 0.0, 1.0)"
		var emission_strength_code = _emit_expr(expr.get("emission_strength", {}), compiler_ctx)
		if emission_strength_code.is_empty():
			emission_strength_code = "1.0"
		lines.append_array(_emit_surface_normal_lines(expr.get("normal", {}), compiler_ctx))
		lines.append("vec4 imported_base = %s;" % base_color_code)
		lines.append("ALBEDO = clamp(imported_base.rgb, vec3(0.0), vec3(1.0));")
		lines.append("ALPHA = clamp(imported_base.a * (%s), 0.0, 1.0);" % alpha_code)
		lines.append("METALLIC = clamp(%s, 0.0, 1.0);" % metallic_code)
		lines.append("ROUGHNESS = clamp(%s, 0.0, 1.0);" % roughness_code)
		lines.append("SPECULAR = clamp(%s, 0.0, 1.0);" % specular_code)
		lines.append("EMISSION = clamp((%s).rgb * (%s), vec3(0.0), vec3(64.0));" % [emission_color_code, emission_strength_code])
		return lines
	return []

func _emit_surface_normal_lines(normal_expr: Dictionary, compiler_ctx: Dictionary) -> Array[String]:
	var lines: Array[String] = []
	if normal_expr.is_empty():
		return lines
	if String(normal_expr.get("kind", "")) == "normal_map":
		var color_code = _emit_expr(normal_expr.get("color", {}), compiler_ctx)
		var strength_code = _emit_expr(normal_expr.get("strength", {}), compiler_ctx)
		if not color_code.is_empty():
			lines.append("NORMAL_MAP = (%s).rgb;" % color_code)
			if strength_code.is_empty():
				strength_code = "1.0"
			lines.append("NORMAL_MAP_DEPTH = clamp(%s, 0.0, 8.0);" % strength_code)
	return lines

# This is the main expression writer for the generated shader.
# It turns each IR node into the GLSL/Godot code string that gets placed into the final fragment shader.
func _emit_expr(expr: Dictionary, compiler_ctx: Dictionary) -> String:
	if expr.is_empty():
		return ""

	var kind = String(expr.get("kind", ""))
	if kind == "float_constant":
		return _glsl_float(expr.get("value", 0.0))
	elif kind == "color_constant":
		return _glsl_color(expr.get("value", [1.0, 1.0, 1.0, 1.0]))
	elif kind == "vector_constant":
		return _glsl_vec3(expr.get("value", [0.0, 0.0, 0.0]))
	elif kind == "builtin_vector":
		return String(expr.get("code", "vec3(UV, 0.0)"))
	elif kind == "image_texture":
		var uv_code = _emit_expr(expr.get("uv", _default_uv_expr()), compiler_ctx)
		if uv_code.is_empty():
			uv_code = "vec3(UV, 0.0)"
		return "texture(%s, (%s).xy)" % [String(expr.get("uniform_name", "tex_0")), uv_code]
	elif kind == "texture_alpha":
		var alpha_uv_code = _emit_expr(expr.get("uv", _default_uv_expr()), compiler_ctx)
		if alpha_uv_code.is_empty():
			alpha_uv_code = "vec3(UV, 0.0)"
		return "texture(%s, (%s).xy).a" % [String(expr.get("uniform_name", "tex_0")), alpha_uv_code]
	elif kind == "coerce":
		return _emit_coerce(expr, compiler_ctx)
	elif kind == "mix":
		return _emit_mix_expr(expr, compiler_ctx)
	elif kind == "color_ramp":
		var function_name = _ensure_ramp_function(expr, compiler_ctx)
		var fac_code = _emit_expr(expr.get("fac", {}), compiler_ctx)
		if function_name.is_empty() or fac_code.is_empty():
			return ""
		var call_code = "%s(%s)" % [function_name, fac_code]
		if String(expr.get("result_type", "")) == "float":
			compiler_ctx["need_luminance"] = true
			return "importer_luminance((%s).rgb)" % call_code
		return call_code
	elif kind == "fresnel":
		compiler_ctx["need_fresnel"] = true
		var ior_code = _emit_expr(expr.get("ior", {}), compiler_ctx)
		if ior_code.is_empty():
			return ""
		var normal_code = "NORMAL"
		var fresnel_normal_expr: Dictionary = expr.get("normal", {})
		if not fresnel_normal_expr.is_empty():
			var emitted_normal = _emit_expr(fresnel_normal_expr, compiler_ctx)
			if not emitted_normal.is_empty():
				normal_code = emitted_normal
		return "importer_fresnel_dielectric(clamp(abs(dot(normalize(%s), normalize(VIEW))), 0.0, 1.0), %s)" % [normal_code, ior_code]
	elif kind == "diffuse_bsdf":
		return _emit_expr(expr.get("color", {}), compiler_ctx)
	elif kind == "principled_bsdf":
		return _emit_expr(expr.get("base_color", {}), compiler_ctx)
	elif kind == "shader_to_rgb":
		compiler_ctx["need_shader_to_rgb"] = true
		var shader_expr: Dictionary = expr.get("shader", {})
		var shader_kind = String(shader_expr.get("kind", ""))
		if shader_kind != "diffuse_bsdf" and shader_kind != "principled_bsdf":
			var warnings: Array = compiler_ctx.get("warnings", [])
			warnings.append("Shader to RGB is currently supported only when fed by Diffuse BSDF or first-pass Principled BSDF.")
			compiler_ctx["warnings"] = warnings
			_warn("Stage 3 currently supports Shader to RGB only when fed by Diffuse BSDF or first-pass Principled BSDF.")
			return ""
		var base_expr = shader_expr.get("color", {})
		if shader_kind == "principled_bsdf":
			base_expr = shader_expr.get("base_color", {})
		var diffuse_code = _emit_expr(base_expr, compiler_ctx)
		if diffuse_code.is_empty():
			return ""
		var normal_code = "NORMAL"
		var shader_normal_expr: Dictionary = shader_expr.get("normal", {})
		if not shader_normal_expr.is_empty():
			var shader_normal_code = _emit_expr(shader_normal_expr, compiler_ctx)
			if not shader_normal_code.is_empty():
				normal_code = shader_normal_code
		return "importer_shader_to_rgb_diffuse(%s, %s)" % [diffuse_code, normal_code]
	elif kind == "math":
		return _emit_math_expr(expr, compiler_ctx)
	elif kind == "vector_math":
		return _emit_vector_math_expr(expr, compiler_ctx)
	elif kind == "mapping":
		compiler_ctx["need_mapping_helper"] = true
		var vector_code = _emit_expr(expr.get("vector", {}), compiler_ctx)
		var location_code = _emit_expr(expr.get("location", {}), compiler_ctx)
		var rotation_code = _emit_expr(expr.get("rotation", {}), compiler_ctx)
		var scale_code = _emit_expr(expr.get("scale", {}), compiler_ctx)
		if vector_code.is_empty() or location_code.is_empty() or rotation_code.is_empty() or scale_code.is_empty():
			return ""
		var vector_type = String(expr.get("vector_type", "POINT"))
		if vector_type == "VECTOR" or vector_type == "NORMAL":
			return "importer_apply_mapping_direction((%s), (%s), (%s))" % [vector_code, rotation_code, scale_code]
		return "importer_apply_mapping_point((%s), (%s), (%s), (%s))" % [vector_code, location_code, rotation_code, scale_code]
	elif kind == "normal_map":
		var color_code = _emit_expr(expr.get("color", {}), compiler_ctx)
		var strength_code = _emit_expr(expr.get("strength", {}), compiler_ctx)
		if color_code.is_empty():
			return ""
		if strength_code.is_empty():
			strength_code = "1.0"
		return "normalize(mix(vec3(0.0, 0.0, 1.0), ((%s).rgb * 2.0 - 1.0), clamp(%s, 0.0, 1.0)))" % [color_code, strength_code]
	elif kind == "material_output":
		return _emit_expr(expr.get("surface", {}), compiler_ctx)

	return ""

func _emit_coerce(expr: Dictionary, compiler_ctx: Dictionary) -> String:
	var inner: Dictionary = expr.get("expr", {})
	var inner_code = _emit_expr(inner, compiler_ctx)
	if inner_code.is_empty():
		return ""

	var from_type = String(expr.get("from_type", ""))
	var to_type = String(expr.get("result_type", ""))
	if from_type == to_type:
		return inner_code
	if from_type == "color" and to_type == "float":
		compiler_ctx["need_luminance"] = true
		return "importer_luminance((%s).rgb)" % inner_code
	if from_type == "float" and to_type == "color":
		return "vec4(vec3(%s), 1.0)" % inner_code
	if from_type == "vector" and to_type == "color":
		return "vec4(%s, 1.0)" % inner_code
	if from_type == "float" and to_type == "vector":
		return "vec3(%s)" % inner_code
	if from_type == "color" and to_type == "vector":
		return "(%s).rgb" % inner_code
	if from_type == "vector" and to_type == "float":
		return "(%s).x" % inner_code
	return inner_code

func _emit_math_expr(expr: Dictionary, compiler_ctx: Dictionary) -> String:
	var a_code = _emit_expr(expr.get("a", {}), compiler_ctx)
	var b_code = _emit_expr(expr.get("b", {}), compiler_ctx)
	var c_code = _emit_expr(expr.get("c", {}), compiler_ctx)
	if a_code.is_empty():
		return ""
	if b_code.is_empty():
		b_code = "0.0"
	if c_code.is_empty():
		c_code = "0.001"
	var operation = String(expr.get("operation", "ADD"))
	var code = ""
	if operation == "ADD":
		code = "((%s) + (%s))" % [a_code, b_code]
	elif operation == "SUBTRACT":
		code = "((%s) - (%s))" % [a_code, b_code]
	elif operation == "MULTIPLY":
		code = "((%s) * (%s))" % [a_code, b_code]
	elif operation == "DIVIDE":
		code = "((%s) / max(abs(%s), 0.000001))" % [a_code, b_code]
	elif operation == "POWER":
		code = "pow(max(%s, 0.0), %s)" % [a_code, b_code]
	elif operation == "MINIMUM":
		code = "min((%s), (%s))" % [a_code, b_code]
	elif operation == "MAXIMUM":
		code = "max((%s), (%s))" % [a_code, b_code]
	elif operation == "LESS_THAN":
		code = "((%s) < (%s) ? 1.0 : 0.0)" % [a_code, b_code]
	elif operation == "GREATER_THAN":
		code = "((%s) > (%s) ? 1.0 : 0.0)" % [a_code, b_code]
	elif operation == "COMPARE":
		code = "(abs((%s) - (%s)) <= max(%s, 0.000001) ? 1.0 : 0.0)" % [a_code, b_code, c_code]
	elif operation == "MULTIPLY_ADD":
		code = "(((%s) * (%s)) + (%s))" % [a_code, b_code, c_code]
	elif operation == "MODULO":
		code = "mod((%s), max(abs(%s), 0.000001))" % [a_code, b_code]
	elif operation == "ABSOLUTE":
		code = "abs(%s)" % a_code
	elif operation == "FLOOR":
		code = "floor(%s)" % a_code
	elif operation == "CEIL":
		code = "ceil(%s)" % a_code
	elif operation == "ROUND":
		code = "floor((%s) + 0.5)" % a_code
	elif operation == "FRACTION":
		code = "fract(%s)" % a_code
	elif operation == "SINE":
		code = "sin(%s)" % a_code
	elif operation == "COSINE":
		code = "cos(%s)" % a_code
	elif operation == "TANGENT":
		code = "tan(%s)" % a_code
	elif operation == "ARCSINE":
		code = "asin(clamp(%s, -1.0, 1.0))" % a_code
	elif operation == "ARCCOSINE":
		code = "acos(clamp(%s, -1.0, 1.0))" % a_code
	elif operation == "ARCTANGENT":
		code = "atan(%s)" % a_code
	elif operation == "SIGN":
		code = "sign(%s)" % a_code
	elif operation == "SQRT":
		code = "sqrt(max(%s, 0.0))" % a_code
	elif operation == "INVERSE_SQRT":
		code = "inversesqrt(max(%s, 0.000001))" % a_code
	else:
		var warnings: Array = compiler_ctx.get("warnings", [])
		warnings.append("Unsupported Math operation '%s'; falling back to first input." % operation)
		compiler_ctx["warnings"] = warnings
		code = a_code

	if bool(expr.get("use_clamp", false)):
		code = "clamp(%s, 0.0, 1.0)" % code
	return code

func _emit_vector_math_expr(expr: Dictionary, compiler_ctx: Dictionary) -> String:
	var a_code = _emit_expr(expr.get("a", {}), compiler_ctx)
	if a_code.is_empty():
		return ""
	var b_code = _emit_expr(expr.get("b", {}), compiler_ctx)
	var scale_code = _emit_expr(expr.get("scale", {}), compiler_ctx)
	if scale_code.is_empty():
		scale_code = "1.0"
	if b_code.is_empty():
		b_code = "vec3(0.0, 0.0, 0.0)"
	var operation = String(expr.get("operation", "ADD"))
	var result_type = String(expr.get("result_type", "vector"))
	var code = ""

	if result_type == "float":
		if operation == "DOT_PRODUCT":
			code = "dot((%s), (%s))" % [a_code, b_code]
		elif operation == "DISTANCE":
			code = "distance((%s), (%s))" % [a_code, b_code]
		elif operation == "LENGTH":
			code = "length(%s)" % a_code
		elif operation == "SCALE":
			code = "%s" % scale_code
		else:
			code = "length(%s)" % a_code
		return code

	if operation == "ADD":
		code = "((%s) + (%s))" % [a_code, b_code]
	elif operation == "SUBTRACT":
		code = "((%s) - (%s))" % [a_code, b_code]
	elif operation == "MULTIPLY":
		code = "((%s) * (%s))" % [a_code, b_code]
	elif operation == "DIVIDE":
		code = "((%s) / max(abs(%s), vec3(0.000001)))" % [a_code, b_code]
	elif operation == "SCALE":
		code = "((%s) * (%s))" % [a_code, scale_code]
	elif operation == "CROSS_PRODUCT":
		code = "cross((%s), (%s))" % [a_code, b_code]
	elif operation == "PROJECT":
		code = "((dot((%s), (%s)) / max(dot((%s), (%s)), 0.000001)) * (%s))" % [a_code, b_code, b_code, b_code, b_code]
	elif operation == "REFLECT":
		code = "reflect((%s), normalize(%s))" % [a_code, b_code]
	elif operation == "NORMALIZE":
		code = "normalize(%s)" % a_code
	elif operation == "FLOOR":
		code = "floor(%s)" % a_code
	elif operation == "CEIL":
		code = "ceil(%s)" % a_code
	elif operation == "FRACTION":
		code = "fract(%s)" % a_code
	elif operation == "ABSOLUTE":
		code = "abs(%s)" % a_code
	elif operation == "MINIMUM":
		code = "min((%s), (%s))" % [a_code, b_code]
	elif operation == "MAXIMUM":
		code = "max((%s), (%s))" % [a_code, b_code]
	elif operation == "MODULO":
		code = "mod((%s), max(abs(%s), vec3(0.000001)))" % [a_code, b_code]
	else:
		var warnings: Array = compiler_ctx.get("warnings", [])
		warnings.append("Unsupported Vector Math operation '%s'; falling back to first vector input." % operation)
		compiler_ctx["warnings"] = warnings
		code = a_code
	return code

func _emit_mix_expr(expr: Dictionary, compiler_ctx: Dictionary) -> String:
	var factor_code = _emit_expr(expr.get("factor", {}), compiler_ctx)
	var a_code = _emit_expr(expr.get("a", {}), compiler_ctx)
	var b_code = _emit_expr(expr.get("b", {}), compiler_ctx)
	if factor_code.is_empty() or a_code.is_empty() or b_code.is_empty():
		return ""
	var blend_type = String(expr.get("blend_type", "MIX"))
	var f = "clamp(%s, 0.0, 1.0)" % factor_code
	var blended = "mix((%s), (%s), %s)" % [a_code, b_code, f]
	if blend_type == "MIX":
		blended = "mix((%s), (%s), %s)" % [a_code, b_code, f]
	elif blend_type == "MULTIPLY":
		blended = "mix((%s), (%s) * (%s), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "ADD":
		blended = "mix((%s), (%s) + (%s), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "SUBTRACT":
		blended = "mix((%s), (%s) - (%s), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "DIVIDE":
		blended = "mix((%s), (%s) / max(abs(%s), 0.000001), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "SCREEN":
		blended = "mix((%s), 1.0 - (1.0 - (%s)) * (1.0 - (%s)), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "DARKEN":
		blended = "mix((%s), min((%s), (%s)), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "LIGHTEN":
		blended = "mix((%s), max((%s), (%s)), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "DIFFERENCE":
		blended = "mix((%s), abs((%s) - (%s)), %s)" % [a_code, a_code, b_code, f]
	elif blend_type == "EXCLUSION":
		blended = "mix((%s), (%s) + (%s) - 2.0 * (%s) * (%s), %s)" % [a_code, a_code, b_code, a_code, b_code, f]
	elif blend_type == "OVERLAY":
		blended = "mix((%s), mix(2.0 * (%s) * (%s), 1.0 - 2.0 * (1.0 - (%s)) * (1.0 - (%s)), step(0.5, (%s))), %s)" % [a_code, a_code, b_code, a_code, b_code, a_code, f]
	elif blend_type == "SOFT_LIGHT":
		blended = "mix((%s), (1.0 - 2.0 * (%s)) * (%s) * (%s) + 2.0 * (%s) * (%s), %s)" % [a_code, b_code, a_code, a_code, b_code, a_code, f]
	else:
		var warnings: Array = compiler_ctx.get("warnings", [])
		warnings.append("Unsupported Mix blend type '%s'; falling back to standard mix." % blend_type)
		compiler_ctx["warnings"] = warnings
		blended = "mix((%s), (%s), %s)" % [a_code, b_code, f]
	if bool(expr.get("use_clamp", false)):
		blended = "clamp(%s, 0.0, 1.0)" % blended
	return blended

# Color ramps need their own generated helper function because the stop count and colors change per material.
# This builds that function once, stores it in the compiler context, and returns the function name to call later.
func _ensure_ramp_function(expr: Dictionary, compiler_ctx: Dictionary) -> String:
	var node_id = String(expr.get("node_id", "ramp"))
	var function_name = "importer_ramp_%s" % node_id.replace("node_", "")
	var ramp_functions: Dictionary = compiler_ctx.get("ramp_functions", {})
	if ramp_functions.has(function_name):
		return function_name

	var stops_variant = expr.get("stops", [])
	if typeof(stops_variant) != TYPE_ARRAY:
		return ""
	var stops: Array = stops_variant
	if stops.is_empty():
		return ""

	var interpolation = String(expr.get("interpolation", "CONSTANT"))
	var lines: Array[String] = []
	lines.append("vec4 %s(float t) {" % function_name)
	lines.append("	t = clamp(t, 0.0, 1.0);")

	if interpolation == "CONSTANT":
		for i in range(stops.size()):
			var stop: Dictionary = stops[i]
			if i == 0:
				continue
			var pos = _glsl_float(stop.get("pos", 1.0))
			var prev_stop: Dictionary = stops[i - 1]
			lines.append("	if (t < %s) { return %s; }" % [pos, _glsl_color(prev_stop.get("color", [1.0, 1.0, 1.0, 1.0]))])
		lines.append("	return %s;" % _glsl_color(stops[stops.size() - 1].get("color", [1.0, 1.0, 1.0, 1.0])))
	else:
		if stops.size() == 1:
			lines.append("	return %s;" % _glsl_color(stops[0].get("color", [1.0, 1.0, 1.0, 1.0])))
		else:
			for i in range(stops.size() - 1):
				var a: Dictionary = stops[i]
				var b: Dictionary = stops[i + 1]
				var a_pos = _glsl_float(a.get("pos", 0.0))
				var b_pos = _glsl_float(b.get("pos", 1.0))
				var a_color = _glsl_color(a.get("color", [1.0, 1.0, 1.0, 1.0]))
				var b_color = _glsl_color(b.get("color", [1.0, 1.0, 1.0, 1.0]))
				if i == 0:
					lines.append("	if (t <= %s) { return %s; }" % [a_pos, a_color])
				lines.append("	if (t <= %s) { float x = smoothstep(%s, %s, t); return mix(%s, %s, x); }" % [b_pos, a_pos, b_pos, a_color, b_color])
			lines.append("	return %s;" % _glsl_color(stops[stops.size() - 1].get("color", [1.0, 1.0, 1.0, 1.0])))

	lines.append("}")
	ramp_functions[function_name] = "\n".join(lines)
	compiler_ctx["ramp_functions"] = ramp_functions
	return function_name

func _resolve_texture_resources(texture_bindings: Array, json_path: String) -> Array:
	var resolved: Array = []
	var json_dir = json_path.get_base_dir()
	for binding in texture_bindings:
		if not (binding is Dictionary):
			continue
		var binding_dict: Dictionary = binding
		var rel_path = String(binding_dict.get("relative_path", ""))
		var resource_path = json_dir.path_join(rel_path)
		resolved.append({
			"uniform_name": String(binding_dict.get("uniform_name", "tex_0")),
			"resource_path": resource_path,
			"relative_path": rel_path
		})
	return resolved

func _build_render_mode_string(surface_expr: Dictionary, imported_material, material_dict: Dictionary) -> String:
	var tokens: Array[String] = []
	if String(surface_expr.get("result_type", "")) != "shader":
		tokens.append("unshaded")

	var cull_mode := _derive_cull_render_mode(imported_material, material_dict)
	if cull_mode == "cull_disabled" and _surface_uses_tangent_normal_map(surface_expr):
		cull_mode = "cull_back"

	tokens.append(cull_mode)
	tokens.append("depth_draw_opaque")
	return ", ".join(tokens)

func _derive_cull_render_mode(imported_material, material_dict: Dictionary) -> String:
	if imported_material != null and imported_material is BaseMaterial3D:
		var base_material: BaseMaterial3D = imported_material
		match base_material.cull_mode:
			BaseMaterial3D.CULL_DISABLED:
				return "cull_disabled"
			BaseMaterial3D.CULL_FRONT:
				return "cull_front"
			_:
				return "cull_back"

	var settings: Dictionary = material_dict.get("settings", {})
	if settings.has("use_backface_culling"):
		return "cull_back" if bool(settings.get("use_backface_culling", false)) else "cull_disabled"

	return "cull_disabled"

func _surface_uses_tangent_normal_map(surface_expr: Dictionary) -> bool:
	if surface_expr.is_empty():
		return false
	var normal_expr: Dictionary = surface_expr.get("normal", {})
	return String(normal_expr.get("kind", "")) == "normal_map"

# This creates the ShaderMaterial object Godot will actually use on the mesh.
# It loads the generated shader code, sets the texture uniforms, and keeps the material name readable in the editor.
func _instantiate_shader_material(compiled: Dictionary, material_dict: Dictionary, render_mode: String) -> ShaderMaterial:
	var shader_code = String(compiled.get("shader_code", ""))
	if shader_code.is_empty():
		return null
	shader_code = shader_code.replace("__IMPORTER_RENDER_MODE__", render_mode)

	var shader = Shader.new()
	shader.code = shader_code

	var shader_material = ShaderMaterial.new()
	shader_material.shader = shader
	shader_material.resource_name = "%s_ImportedShader" % String(material_dict.get("name", "GraphMaterial"))

	for texture_binding in compiled.get("textures", []):
		if not (texture_binding is Dictionary):
			continue
		var binding_dict: Dictionary = texture_binding
		var resource_path = String(binding_dict.get("resource_path", ""))
		var texture = load(resource_path)
		if texture == null:
			_warn("Could not load exported texture at %s" % resource_path)
			continue
		shader_material.set_shader_parameter(String(binding_dict.get("uniform_name", "tex_0")), texture)

	return shader_material

# This walks the imported scene, finds the surfaces that belong to the exported material,
# and applies the generated ShaderMaterial to each matching surface.
func _apply_compiled_material(scene: Node, material_dict: Dictionary, compiled: Dictionary) -> int:
	if String(compiled.get("shader_code", "")).is_empty():
		return 0

	var applied = 0
	var shader_materials_by_render_mode := {}
	var ir_root: Dictionary = compiled.get("ir", {})
	var surface_expr: Dictionary = ir_root.get("surface", {})

	for mesh_instance in _collect_mesh_instances(scene):
		var mesh = mesh_instance.mesh
		if mesh == null:
			continue
		var surface_count = mesh.get_surface_count()
		for surface_index in range(surface_count):
			var imported_material = mesh_instance.get_surface_override_material(surface_index)
			if imported_material == null:
				imported_material = mesh.surface_get_material(surface_index)
			if not _surface_matches(mesh_instance, surface_index, imported_material, material_dict):
				continue

			var render_mode = _build_render_mode_string(surface_expr, imported_material, material_dict)
			var shader_material: ShaderMaterial = shader_materials_by_render_mode.get(render_mode)
			if shader_material == null:
				shader_material = _instantiate_shader_material(compiled, material_dict, render_mode)
				if shader_material == null:
					continue
				shader_materials_by_render_mode[render_mode] = shader_material

			mesh_instance.set_surface_override_material(surface_index, shader_material)
			applied += 1
	return applied

func _surface_matches(mesh_instance: MeshInstance3D, surface_index: int, imported_material, material_dict: Dictionary) -> bool:
	var targeting: Dictionary = material_dict.get("targeting", {})
	var material_name = String(material_dict.get("name", ""))
	var object_names_variant = targeting.get("object_names", [])
	var slot_indices_variant = targeting.get("material_slot_indices", [])

	var object_match = true
	if typeof(object_names_variant) == TYPE_ARRAY:
		var object_names: Array = object_names_variant
		if not object_names.is_empty():
			object_match = object_names.has(String(mesh_instance.name))

	var slot_match = true
	if typeof(slot_indices_variant) == TYPE_ARRAY:
		var slot_indices: Array = slot_indices_variant
		if not slot_indices.is_empty():
			slot_match = slot_indices.has(surface_index)

	if object_match and slot_match:
		return true

	if imported_material != null:
		var imported_name = String(imported_material.resource_name)
		if imported_name == material_name:
			return true

	return false

func _collect_mesh_instances(root: Node) -> Array:
	var result: Array = []
	if root is MeshInstance3D:
		result.append(root)
	for child in root.get_children():
		if child is Node:
			result.append_array(_collect_mesh_instances(child))
	return result

func _find_json_path(source_file: String) -> String:
	var source_dir = source_file.get_base_dir()
	var stem = source_file.get_file().get_basename()
	var candidates: Array[String] = []

	if not JSON_PATH_OVERRIDE.is_empty():
		candidates.append(source_dir.path_join(JSON_PATH_OVERRIDE))

	for pattern in JSON_CANDIDATE_PATTERNS:
		candidates.append(source_dir.path_join(pattern.replace("{stem}", stem)))

	for candidate in candidates:
		if FileAccess.file_exists(candidate):
			return candidate
	return ""

func _load_json_dictionary(path: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return {}
	var text = FileAccess.get_file_as_string(path)
	var json = JSON.new()
	var parse_error = json.parse(text)
	if parse_error != OK:
		_warn("JSON parse error in %s at line %d: %s" % [path, json.get_error_line(), json.get_error_message()])
		return {}
	if typeof(json.data) != TYPE_DICTIONARY:
		_warn("Expected a Dictionary at the root of %s." % path)
		return {}
	return json.data

func _variant_to_color_array(value) -> Array:
	if typeof(value) == TYPE_ARRAY:
		var arr: Array = value
		if arr.size() >= 4:
			return [float(arr[0]), float(arr[1]), float(arr[2]), float(arr[3])]
		if arr.size() == 3:
			return [float(arr[0]), float(arr[1]), float(arr[2]), 1.0]
	return [1.0, 1.0, 1.0, 1.0]

func _variant_to_vector_array(value) -> Array:
	if typeof(value) == TYPE_ARRAY:
		var arr: Array = value
		if arr.size() >= 3:
			return [float(arr[0]), float(arr[1]), float(arr[2])]
	return [0.0, 0.0, 0.0]

func _glsl_float(value) -> String:
	var v = float(value)
	if is_nan(v) or is_inf(v):
		v = 0.0
	var text = String.num(v, 6)
	if not text.contains("."):
		text += ".0"
	return text

func _glsl_color(value) -> String:
	var arr = _variant_to_color_array(value)
	return "vec4(%s, %s, %s, %s)" % [
		_glsl_float(arr[0]),
		_glsl_float(arr[1]),
		_glsl_float(arr[2]),
		_glsl_float(arr[3])
	]

func _glsl_vec3(value) -> String:
	var arr = _variant_to_vector_array(value)
	return "vec3(%s, %s, %s)" % [
		_glsl_float(arr[0]),
		_glsl_float(arr[1]),
		_glsl_float(arr[2])
	]

func _material_label(material_dict: Dictionary) -> String:
	var material_name = String(material_dict.get("name", "")).strip_edges()
	if material_name.is_empty():
		return "<unnamed material>"
	return "'%s'" % material_name

func _node_debug_label(node: Dictionary) -> String:
	var ui_name = String(node.get("ui_name", "")).strip_edges()
	var idname = String(node.get("idname", "")).strip_edges()
	var node_id = String(node.get("id", "")).strip_edges()
	if ui_name.is_empty():
		ui_name = "<unnamed node>"
	return "%s [%s|%s]" % [ui_name, idname, node_id]

func _trace_note(ctx: Dictionary, message: String) -> void:
	var notes: Array = ctx.get("notes", [])
	notes.append(message)
	ctx["notes"] = notes

func _record_used_node(ctx: Dictionary, node: Dictionary, socket_identifier: String, socket_id: String) -> void:
	var used_nodes: Array = ctx.get("used_nodes", [])
	var record = {
		"node_id": String(node.get("id", "")),
		"idname": String(node.get("idname", "")),
		"ui_name": String(node.get("ui_name", "")),
		"socket_identifier": socket_identifier,
		"socket_id": socket_id
	}
	for existing in used_nodes:
		if existing is Dictionary and String(existing.get("node_id", "")) == String(record.get("node_id", "")) and String(existing.get("socket_id", "")) == String(record.get("socket_id", "")):
			return
	used_nodes.append(record)
	ctx["used_nodes"] = used_nodes

func _format_node_records(records: Array) -> String:
	if records.is_empty():
		return "<none>"
	var parts: Array[String] = []
	for value in records:
		if value is Dictionary:
			var record: Dictionary = value
			parts.append("%s [%s|%s]" % [
				String(record.get("ui_name", "<unnamed node>")),
				String(record.get("idname", "")),
				String(record.get("node_id", ""))
			])
	return ", ".join(parts)

func _log_material_debug_summary(material_dict: Dictionary, ir_result: Dictionary, compiled: Dictionary) -> void:
	var material_label = _material_label(material_dict)
	var textures: Array = compiled.get("textures", [])
	var used_nodes: Array = ir_result.get("used_nodes", [])
	var unsupported_nodes: Array = ir_result.get("unsupported_nodes", [])
	print("[BlenderImporter] Material %s summary:" % material_label)
	print("[BlenderImporter]   Used nodes: %s" % _format_node_records(used_nodes))
	if unsupported_nodes.is_empty():
		print("[BlenderImporter]   Unsupported nodes: <none>")
	else:
		print("[BlenderImporter]   Unsupported nodes: %s" % _format_node_records(unsupported_nodes))
	print("[BlenderImporter]   Texture bindings: %d" % textures.size())
	for binding in textures:
		if binding is Dictionary:
			var binding_dict: Dictionary = binding
			print("[BlenderImporter]     %s -> %s" % [String(binding_dict.get("uniform_name", "tex_0")), String(binding_dict.get("resource_path", ""))])
	var compiler_warnings: Array = compiled.get("compiler_warnings", [])
	if compiler_warnings.is_empty():
		print("[BlenderImporter]   Compiler warnings: <none>")
	else:
		for warning in compiler_warnings:
			print("[BlenderImporter]   Compiler warning: %s" % String(warning))

func _warn(message: String) -> void:
	push_warning("[BlenderImporter] %s" % message)