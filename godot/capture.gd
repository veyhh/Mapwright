extends SceneTree

const CAPTURE_SIZE := Vector2i(1024, 1024)
const CAMERA_VIEWS := {
	"top_down": {"direction": Vector3(0.0, 1.0, 0.001), "up": Vector3(0.0, 0.0, -1.0)},
	"iso_ne": {"direction": Vector3(1.0, 1.0, 1.0), "up": Vector3.UP},
	"iso_sw": {"direction": Vector3(-1.0, 1.0, -1.0), "up": Vector3.UP},
}

var _viewport: SubViewport
var _camera: Camera3D


func _initialize() -> void:
	# A real display/rendering driver is required. Keep its 1x1 host window off-screen.
	if DisplayServer.get_name() != "headless":
		DisplayServer.window_set_position(Vector2i(-10000, -10000))
		DisplayServer.window_set_size(Vector2i.ONE)
	call_deferred("_run")


func _run() -> void:
	var version := Engine.get_version_info()
	if int(version.get("major", 0)) != 4:
		_fail("Mapwright capture requires Godot 4.x; detected %s." % version.get("string", "unknown"))
		return

	if DisplayServer.get_name() == "headless":
		_fail(
			"Godot --headless uses the dummy rendering driver and cannot render 3D captures. "
			+ "Run with a real rendering driver; use Xvfb on display-less Linux CI."
		)
		return

	var arguments := OS.get_cmdline_user_args()
	if arguments.size() != 2:
		_fail("Usage: godot --path <project> --script godot/capture.gd -- <scene.tscn> <output_dir>")
		return

	var scene_path := _resolve_scene_path(arguments[0])
	if scene_path.is_empty():
		return
	var output_directory := _resolve_output_directory(arguments[1])
	if output_directory.is_empty():
		return

	var packed_scene := ResourceLoader.load(scene_path, "PackedScene") as PackedScene
	if packed_scene == null:
		_fail("Could not load Godot scene: %s" % scene_path)
		return

	var scene_instance := packed_scene.instantiate()
	if scene_instance == null:
		_fail("Could not instantiate Godot scene: %s" % scene_path)
		return

	_setup_viewport(scene_instance)
	await process_frame
	await process_frame

	var bounds := _calculate_scene_bounds(scene_instance)
	if bounds.size.length_squared() <= 0.000001:
		_fail("Scene has no renderable VisualInstance3D bounds: %s" % scene_path)
		return

	var created_files: Array[String] = []
	for view_name: String in CAMERA_VIEWS:
		var view: Dictionary = CAMERA_VIEWS[view_name]
		_position_camera(bounds, view.direction, view.up)
		_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
		await RenderingServer.frame_post_draw

		var image := _viewport.get_texture().get_image()
		if image == null or image.is_empty():
			_fail("Renderer returned an empty image for view '%s'." % view_name)
			return
		if not _image_has_visual_variation(image):
			_fail(
				"Renderer returned a blank or near-uniform image for view '%s'. " % view_name
				+ "Check GPU/display-driver availability."
			)
			return

		var output_path := output_directory.path_join("%s.png" % view_name)
		var save_error := image.save_png(output_path)
		if save_error != OK:
			_fail("Could not save PNG '%s' (error %d)." % [output_path, save_error])
			return
		created_files.append(output_path)
		print("Captured %s: %s" % [view_name, output_path])

	print("Capture complete: %d non-blank PNG files." % created_files.size())
	quit(0)


func _resolve_scene_path(argument: String) -> String:
	var normalized := argument.replace("\\", "/")
	var localized: String
	if normalized.begins_with("res://"):
		localized = normalized
	elif normalized.is_absolute_path():
		localized = ProjectSettings.localize_path(normalized)
	else:
		localized = "res://" + normalized.trim_prefix("./")

	if not localized.begins_with("res://"):
		_fail("Scene must be inside the Godot project: %s" % argument)
		return ""
	if not localized.to_lower().ends_with(".tscn"):
		_fail("Expected a .tscn scene path: %s" % argument)
		return ""
	if not ResourceLoader.exists(localized, "PackedScene"):
		_fail("Godot scene file not found: %s" % localized)
		return ""
	return localized


func _resolve_output_directory(argument: String) -> String:
	var normalized := argument.replace("\\", "/")
	var absolute_path: String
	if normalized.begins_with("res://") or normalized.begins_with("user://"):
		absolute_path = ProjectSettings.globalize_path(normalized)
	elif normalized.is_absolute_path():
		absolute_path = normalized
	else:
		absolute_path = ProjectSettings.globalize_path("res://" + normalized.trim_prefix("./"))

	var make_error := DirAccess.make_dir_recursive_absolute(absolute_path)
	if make_error != OK:
		_fail("Could not create capture output directory '%s' (error %d)." % [absolute_path, make_error])
		return ""
	return absolute_path


func _setup_viewport(scene_instance: Node) -> void:
	_viewport = SubViewport.new()
	_viewport.name = "MapwrightCaptureViewport"
	_viewport.size = CAPTURE_SIZE
	_viewport.world_3d = World3D.new()
	_viewport.render_target_clear_mode = SubViewport.CLEAR_MODE_ALWAYS
	_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
	_viewport.msaa_3d = Viewport.MSAA_4X
	root.add_child(_viewport)
	_viewport.add_child(scene_instance)

	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color(0.36, 0.42, 0.50)
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.ambient_light_color = Color(0.82, 0.87, 1.0)
	environment.ambient_light_energy = 0.65
	_viewport.world_3d.environment = environment

	var key_light := DirectionalLight3D.new()
	key_light.name = "MapwrightKeyLight"
	key_light.rotation_degrees = Vector3(-55.0, -35.0, 0.0)
	key_light.light_color = Color(1.0, 0.93, 0.82)
	key_light.light_energy = 1.35
	key_light.shadow_enabled = true
	_viewport.add_child(key_light)

	_camera = Camera3D.new()
	_camera.name = "MapwrightCaptureCamera"
	_camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	_camera.current = true
	_viewport.add_child(_camera)


func _calculate_scene_bounds(scene_root: Node) -> AABB:
	var points: Array[Vector3] = []
	_collect_visual_bounds(scene_root, points)
	if points.is_empty():
		return AABB()

	var minimum := points[0]
	var maximum := points[0]
	for point in points:
		minimum = minimum.min(point)
		maximum = maximum.max(point)
	return AABB(minimum, maximum - minimum)


func _collect_visual_bounds(node: Node, points: Array[Vector3]) -> void:
	if node is VisualInstance3D and node.visible:
		var visual := node as VisualInstance3D
		var local_bounds := visual.get_aabb().abs()
		if local_bounds.size.length_squared() > 0.000001:
			for x in [0.0, 1.0]:
				for y in [0.0, 1.0]:
					for z in [0.0, 1.0]:
						var local_point := local_bounds.position + local_bounds.size * Vector3(x, y, z)
						points.append(visual.global_transform * local_point)

	for child in node.get_children():
		_collect_visual_bounds(child, points)


func _position_camera(bounds: AABB, direction: Vector3, up: Vector3) -> void:
	var center := bounds.get_center()
	var radius := maxf(bounds.size.length() * 0.5, 1.0)
	_camera.size = radius * 2.35
	_camera.near = maxf(radius * 0.01, 0.05)
	_camera.far = radius * 8.0
	_camera.global_position = center + direction.normalized() * radius * 3.0
	_camera.look_at(center, up)


func _image_has_visual_variation(source: Image) -> bool:
	var image := source.duplicate()
	image.resize(64, 64, Image.INTERPOLATE_BILINEAR)
	var minimum_luminance := 1.0
	var maximum_luminance := 0.0
	for y in range(image.get_height()):
		for x in range(image.get_width()):
			var color: Color = image.get_pixel(x, y)
			var luminance: float = color.get_luminance()
			minimum_luminance = minf(minimum_luminance, luminance)
			maximum_luminance = maxf(maximum_luminance, luminance)
	return maximum_luminance > 0.02 and maximum_luminance - minimum_luminance > 0.02


func _fail(message: String) -> void:
	push_error("Mapwright capture: %s" % message)
	quit(1)
