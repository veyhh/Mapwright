extends Node3D

@export_enum("Box", "Cylinder", "Sphere", "Capsule") var shape := 0
@export var dimensions := Vector3.ONE
@export var color := Color(0.7, 0.7, 0.7, 1.0)


func _ready() -> void:
	var mesh: PrimitiveMesh
	match shape:
		1:
			var cylinder := CylinderMesh.new()
			cylinder.height = dimensions.y
			cylinder.top_radius = dimensions.x * 0.5
			cylinder.bottom_radius = dimensions.z * 0.5
			mesh = cylinder
		2:
			var sphere := SphereMesh.new()
			sphere.radius = dimensions.x * 0.5
			sphere.height = dimensions.y
			mesh = sphere
		3:
			var capsule := CapsuleMesh.new()
			capsule.radius = dimensions.x * 0.5
			capsule.height = maxf(dimensions.y, dimensions.x)
			mesh = capsule
		_:
			var box := BoxMesh.new()
			box.size = dimensions
			mesh = box

	var material := StandardMaterial3D.new()
	material.albedo_color = color
	material.roughness = 0.82
	mesh.material = material

	var mesh_instance := MeshInstance3D.new()
	mesh_instance.mesh = mesh
	mesh_instance.position.y = dimensions.y * 0.5
	add_child(mesh_instance)
