"""COLMAP Reconstruction을 표현하는 Blender Scene 요소를 생성합니다."""

from __future__ import annotations

import json

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..core.camera_models import pinhole_parameters
from ..core.import_types import DEFAULT_CAMERA_FRUSTUM_SIZE
from ..core.model import LoadedReconstruction
from .axis_display import dataset_display_matrix
from .ownership import (
    COMMON_RECORD_ID_KEY,
    DATASET_METADATA_KEYS,
    DATASET_UUID_KEY,
    LEGACY_METADATA_KEYS,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    SCHEMA_KEY,
    SOURCE_FINGERPRINT_KEY,
    copy_dataset_metadata,
    mark_dataset_owned,
)


MANAGED_METADATA_KEYS = (*LEGACY_METADATA_KEYS, *DATASET_METADATA_KEYS)
PENDING_FILTER_ATTRIBUTE = "colmap_pending_filtered"


def mark_managed(data_block, loaded: LoadedReconstruction, kind: str) -> None:
    """생성 데이터에 공용 소유권과 Legacy Modifier 식별자를 함께 기록합니다."""

    record_id = None
    if kind in {"CAMERA", "CAMERA_DATA"}:
        # Camera 생성 직후 IMAGE_ID를 보강하므로 초기 표시는 빈 record로 둡니다.
        record_id = ""
    mark_dataset_owned(
        data_block,
        dataset_uuid=loaded.source.dataset_uuid,
        role=kind,
        record_id=record_id,
        source_fingerprint=loaded.source.source_fingerprint,
    )
    data_block[OWNER_KEY] = OWNER_TOKEN
    data_block[SCHEMA_KEY] = loaded.source.schema_version
    data_block[DATASET_UUID_KEY] = loaded.source.dataset_uuid
    data_block[RECORD_KIND_KEY] = kind
    data_block[SOURCE_FINGERPRINT_KEY] = loaded.source.source_fingerprint


def copy_managed_metadata(source, target) -> None:
    """commit 대상 identity를 유지하면서 staging 메타데이터를 복사합니다."""
    copy_dataset_metadata(source, target)
    for key in MANAGED_METADATA_KEYS:
        if key in source:
            target[key] = source[key]


def point_scale_socket_identifier(node_group: bpy.types.GeometryNodeTree) -> str:
    """관리 Node Group에 저장된 Point Scale Modifier 입력 식별자를 반환합니다."""
    identifier = node_group.get("colmap_point_scale_socket")
    if not identifier:
        raise RuntimeError("Point 표시 Node Group에 Point Scale 입력이 없습니다.")
    return str(identifier)


def set_modifier_point_scale(
    modifier: bpy.types.NodesModifier,
    node_group: bpy.types.GeometryNodeTree,
    point_scale: float,
) -> None:
    """Blender 5.2 Geometry Nodes Modifier 입력에 Point Scale을 기록합니다."""
    identifier = point_scale_socket_identifier(node_group)
    modifier_input = getattr(modifier.properties.inputs, identifier, None)
    if modifier_input is None:
        raise RuntimeError("Point 표시 Modifier에 Point Scale 입력이 없습니다.")
    modifier_input.value = point_scale


def _signed_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _create_point_material(loaded: LoadedReconstruction) -> bpy.types.Material:
    """COLMAP RGB 속성을 조명과 무관하게 확인할 수 있는 머티리얼을 만듭니다."""
    material = bpy.data.materials.new("COLMAP_Point_Color")
    mark_managed(material, loaded, "POINT_DISPLAY_MATERIAL")
    material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    material.use_nodes = True

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    output = nodes.get("Material Output")
    principled = nodes.get("Principled BSDF")
    color_attribute = nodes.new("ShaderNodeAttribute")
    color_attribute.name = "COLMAP_Color_Attribute"
    color_attribute.label = "COLMAP Color"
    color_attribute.attribute_name = "colmap_color"
    links.new(color_attribute.outputs["Color"], principled.inputs["Base Color"])
    links.new(color_attribute.outputs["Color"], principled.inputs["Emission Color"])
    principled.inputs["Roughness"].default_value = 1.0
    principled.inputs["Emission Strength"].default_value = 0.25
    if output is None:
        raise RuntimeError("Point 표시 머티리얼에 Material Output 노드가 없습니다.")
    return material


def _create_point_display_nodes(
    loaded: LoadedReconstruction,
    material: bpy.types.Material,
    point_size: float,
) -> bpy.types.GeometryNodeTree:
    """공개 반지름과 내부 배율 보정을 분리한 Point Cloud 표시를 생성합니다."""
    node_group = bpy.data.node_groups.new("COLMAP_Point_Display", "GeometryNodeTree")
    mark_managed(node_group, loaded, "POINT_DISPLAY_NODES")
    node_group.interface.new_socket(
        name="Geometry",
        in_out="INPUT",
        socket_type="NodeSocketGeometry",
    )
    point_scale_socket = node_group.interface.new_socket(
        name="Point Scale",
        description="COLMAP Point3D 표시 반지름",
        in_out="INPUT",
        socket_type="NodeSocketFloat",
    )
    point_scale_socket.default_value = point_size
    point_scale_socket.min_value = 0.000001
    point_scale_socket.max_value = 1000000.0
    point_scale_socket.subtype = "DISTANCE"
    node_group["colmap_point_scale_socket"] = point_scale_socket.identifier
    node_group.interface.new_socket(
        name="Geometry",
        in_out="OUTPUT",
        socket_type="NodeSocketGeometry",
    )

    nodes = node_group.nodes
    links = node_group.links
    group_input = nodes.new("NodeGroupInput")
    group_input.name = "COLMAP_Group_Input"
    group_input.location = (-360.0, 0.0)
    mesh_to_points = nodes.new("GeometryNodeMeshToPoints")
    mesh_to_points.name = "COLMAP_Mesh_To_Points"
    mesh_to_points.mode = "VERTICES"
    mesh_to_points.location = (-120.0, 0.0)
    pending_attribute = nodes.new("GeometryNodeInputNamedAttribute")
    pending_attribute.name = "COLMAP_Pending_Filter_Attribute"
    pending_attribute.data_type = "BOOLEAN"
    pending_attribute.inputs["Name"].default_value = PENDING_FILTER_ATTRIBUTE
    pending_attribute.location = (-360.0, -180.0)
    visible_points = nodes.new("FunctionNodeBooleanMath")
    visible_points.name = "COLMAP_Visible_Points"
    visible_points.operation = "NOT"
    visible_points.location = (-120.0, -180.0)
    set_material = nodes.new("GeometryNodeSetMaterial")
    set_material.name = "COLMAP_Set_Point_Material"
    set_material.location = (120.0, 0.0)
    set_material.inputs["Material"].default_value = material
    group_output = nodes.new("NodeGroupOutput")
    group_output.name = "COLMAP_Group_Output"
    group_output.location = (360.0, 0.0)

    links.new(group_input.outputs["Geometry"], mesh_to_points.inputs["Mesh"])
    links.new(group_input.outputs["Point Scale"], mesh_to_points.inputs["Radius"])
    links.new(pending_attribute.outputs["Attribute"], visible_points.inputs[0])
    links.new(visible_points.outputs["Boolean"], mesh_to_points.inputs["Selection"])
    links.new(mesh_to_points.outputs["Points"], set_material.inputs["Geometry"])
    links.new(set_material.outputs["Geometry"], group_output.inputs["Geometry"])
    from .display_scale import ensure_radius_compensation

    ensure_radius_compensation(node_group)
    return node_group


def _create_points(
    loaded: LoadedReconstruction,
    collection: bpy.types.Collection,
    axis_root: bpy.types.Object,
    point_size: float,
) -> None:
    points = sorted(
        loaded.reconstruction.points3d.values(),
        key=lambda item: item.point3d_id,
    )
    coordinates = [point.xyz for point in points]
    colors = [
        component
        for point in points
        for component in (
            point.rgb[0] / 255.0,
            point.rgb[1] / 255.0,
            point.rgb[2] / 255.0,
            1.0,
        )
    ]
    errors = [point.error for point in points]
    point3d_id_low = [_signed_int32(point.point3d_id) for point in points]
    point3d_id_high = [_signed_int32(point.point3d_id >> 32) for point in points]

    mesh = _create_point_mesh(loaded, coordinates)
    _create_point_attributes(
        mesh,
        colors=colors,
        errors=errors,
        point3d_id_low=point3d_id_low,
        point3d_id_high=point3d_id_high,
    )
    _create_point_object_display(
        loaded,
        collection,
        axis_root,
        mesh,
        point_size,
    )


def _create_point_mesh(
    loaded: LoadedReconstruction,
    coordinates,
) -> bpy.types.Mesh:
    """준비된 원본 좌표로 단일 관리 Point Mesh를 생성합니다."""

    mesh = bpy.data.meshes.new("COLMAP_Points")
    try:
        mesh.from_pydata(coordinates, [], [])
        mesh.update()
        mark_managed(mesh, loaded, "POINT_MESH_DATA")
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    return mesh


def _create_point_color_attribute(
    mesh: bpy.types.Mesh,
    color_values,
) -> None:
    """준비된 RGBA 버퍼를 활성 Point Color Attribute로 기록합니다."""

    color_attribute = mesh.color_attributes.new(
        name="colmap_color",
        type="BYTE_COLOR",
        domain="POINT",
    )
    color_attribute.data.foreach_set(
        "color",
        color_values,
    )
    mesh.color_attributes.active = color_attribute
    mesh.color_attributes.active_color = color_attribute


def _create_point_error_attribute(
    mesh: bpy.types.Mesh,
    errors,
) -> None:
    """준비된 재투영 오차를 Point Attribute로 기록합니다."""

    error_attribute = mesh.attributes.new("colmap_reprojection_error", "FLOAT", "POINT")
    error_attribute.data.foreach_set("value", errors)


def _create_point_index_attribute(mesh: bpy.types.Mesh) -> None:
    """오름차순 Point3D 순서와 일치하는 source index를 기록합니다."""

    index_attribute = mesh.attributes.new("colmap_source_index", "INT", "POINT")
    index_attribute.data.foreach_set("value", list(range(len(mesh.vertices))))


def _create_point_id_attributes(
    mesh: bpy.types.Mesh,
    point3d_id_low,
    point3d_id_high,
) -> None:
    """64비트 Point3D ID를 두 개의 signed int32 Attribute로 기록합니다."""

    low_attribute = mesh.attributes.new("colmap_point3d_id_low", "INT", "POINT")
    high_attribute = mesh.attributes.new("colmap_point3d_id_high", "INT", "POINT")
    low_attribute.data.foreach_set("value", point3d_id_low)
    high_attribute.data.foreach_set("value", point3d_id_high)


def _create_point_pending_attribute(mesh: bpy.types.Mesh) -> None:
    """Camera Filter의 지연 삭제 표시용 Boolean Attribute를 추가합니다."""

    mesh.attributes.new(PENDING_FILTER_ATTRIBUTE, "BOOLEAN", "POINT")


def _create_point_attributes(
    mesh: bpy.types.Mesh,
    *,
    colors,
    errors,
    point3d_id_low,
    point3d_id_high,
) -> None:
    """동기 Import 호환 경계에서 Point Attribute를 기존 순서대로 생성합니다."""

    _create_point_color_attribute(mesh, colors)
    _create_point_error_attribute(mesh, errors)
    _create_point_index_attribute(mesh)
    _create_point_id_attributes(mesh, point3d_id_low, point3d_id_high)
    _create_point_pending_attribute(mesh)


def _create_point_object_display(
    loaded: LoadedReconstruction,
    collection: bpy.types.Collection,
    axis_root: bpy.types.Object,
    mesh: bpy.types.Mesh,
    point_size: float,
) -> bpy.types.Object:
    """Point Object와 현재 데이터 배율을 보정한 표시 리소스를 연결합니다."""

    point_object = bpy.data.objects.new("COLMAP_Points", mesh)
    mark_managed(point_object, loaded, "POINT_MESH")
    point_object["colmap_point3d_id_encoding"] = "uint64_high_low_int32"
    point_object["colmap_point_size"] = point_size
    point_object.parent = axis_root
    point_object.matrix_parent_inverse = Matrix.Identity(4)
    point_object.matrix_basis = Matrix.Identity(4)
    point_object.hide_select = True
    point_object.select_set(False)
    collection.objects.link(point_object)
    material = _create_point_material(loaded)
    node_group = _create_point_display_nodes(loaded, material, point_size)
    modifier = point_object.modifiers.new("COLMAP Point Display", "NODES")
    modifier.node_group = node_group
    set_modifier_point_scale(modifier, node_group, point_size)
    from .display_scale import compensate_object

    compensate_object(point_object, axis_root.matrix_world.to_3x3().col[0].length)
    return point_object


def _camera_matrix(qvec: tuple[float, ...], tvec: tuple[float, ...]) -> Matrix:
    rotation_world_to_camera = Quaternion(qvec).normalized().to_matrix()
    rotation_camera_to_world = rotation_world_to_camera.transposed()
    axis_conversion = Matrix(
        ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    )
    rotation = rotation_camera_to_world @ axis_conversion
    center = -(rotation_camera_to_world @ Vector(tvec))
    matrix = rotation.to_4x4()
    matrix.translation = center
    return matrix


def _configure_intrinsics(camera_data, camera_record) -> None:
    camera_data.type = "PERSP"
    camera_data.sensor_fit = "HORIZONTAL"
    camera_data.sensor_width = 36.0
    camera_data["colmap_model"] = camera_record.model_name
    camera_data["colmap_width"] = str(camera_record.width)
    camera_data["colmap_height"] = str(camera_record.height)
    camera_data["colmap_params_json"] = json.dumps(camera_record.params)
    if camera_record.model_name not in {"PINHOLE", "SIMPLE_PINHOLE"}:
        camera_data["colmap_projection_warning"] = (
            "왜곡 파라미터는 보존되지만 Blender 투영의 정확성을 보장하지 않습니다."
        )
        return
    fx, fy, cx, cy = pinhole_parameters(camera_record.model_name, camera_record.params)
    pixel_aspect_y_over_x = fx / fy
    camera_data.lens = 36.0 * fx / camera_record.width
    camera_data.shift_x = 0.5 - cx / camera_record.width
    camera_data.shift_y = (
        (cy - camera_record.height * 0.5)
        / camera_record.width
        * pixel_aspect_y_over_x
    )
    camera_data["colmap_fx"] = fx
    camera_data["colmap_fy"] = fy
    camera_data["colmap_cx"] = cx
    camera_data["colmap_cy"] = cy
    camera_data["colmap_pixel_aspect_y_over_x"] = pixel_aspect_y_over_x
    camera_data["colmap_exact_pinhole"] = True


def _create_cameras(
    loaded: LoadedReconstruction,
    collection: bpy.types.Collection,
    axis_root: bpy.types.Object,
    camera_frustum_size: float,
) -> None:
    for image in sorted(
        loaded.reconstruction.images.values(),
        key=lambda item: item.image_id,
    ):
        _create_camera(
            loaded,
            collection,
            axis_root,
            image,
            camera_frustum_size=camera_frustum_size,
        )


def _create_camera(
    loaded: LoadedReconstruction,
    collection: bpy.types.Collection,
    axis_root: bpy.types.Object,
    image,
    *,
    camera_frustum_size: float = DEFAULT_CAMERA_FRUSTUM_SIZE,
) -> bpy.types.Object:
    """Image를 관리 카메라로 생성하고 데이터 배율과 표시 크기를 분리합니다."""

    camera_record = loaded.reconstruction.cameras[image.camera_id]
    camera_data = bpy.data.cameras.new(f"IMAGE_{image.image_id}_Camera")
    camera_object = None
    try:
        mark_managed(camera_data, loaded, "CAMERA_DATA")
        camera_data[COMMON_RECORD_ID_KEY] = str(image.image_id)
        _configure_intrinsics(camera_data, camera_record)
        camera_data.display_size = camera_frustum_size
        camera_object = bpy.data.objects.new(f"IMAGE_{image.image_id}", camera_data)
        mark_managed(camera_object, loaded, "CAMERA")
        camera_object[COMMON_RECORD_ID_KEY] = str(image.image_id)
        camera_object["colmap_image_id_text"] = str(image.image_id)
        camera_object["colmap_camera_id_text"] = str(image.camera_id)
        camera_object["colmap_image_name"] = image.name
        camera_object["colmap_qvec"] = list(image.qvec)
        camera_object["colmap_tvec"] = list(image.tvec)
        camera_object.parent = axis_root
        camera_object.matrix_parent_inverse = Matrix.Identity(4)
        camera_object.matrix_basis = _camera_matrix(image.qvec, image.tvec)
        from .display_scale import compensate_object

        compensate_object(camera_object, axis_root.matrix_world.to_3x3().col[0].length)
        camera_object.hide_select = True
        camera_object.select_set(False)
        collection.objects.link(camera_object)
    except Exception:
        if camera_object is not None:
            bpy.data.objects.remove(camera_object, do_unlink=True)
        if camera_data.name in bpy.data.cameras and camera_data.users == 0:
            bpy.data.cameras.remove(camera_data)
        raise
    return camera_object


def _create_axis_root(
    loaded: LoadedReconstruction,
    collection: bpy.types.Collection,
    forward_axis: str,
    up_axis: str,
) -> bpy.types.Object:
    """하위 표시를 배치하되 화면과 편집 선택에서 제외되는 내부 변환 루트를 만듭니다."""

    axis_root = bpy.data.objects.new("COLMAP_Axis_Root", None)
    mark_managed(axis_root, loaded, "AXIS_ROOT")
    axis_root.empty_display_type = "ARROWS"
    axis_root.empty_display_size = 1.0
    axis_root.hide_render = True
    axis_root.hide_select = True
    axis_root.hide_viewport = True
    axis_root["colmap_display_forward_axis"] = forward_axis
    axis_root["colmap_display_up_axis"] = up_axis
    axis_root.matrix_world = dataset_display_matrix(
        loaded.source.dataset_uuid, forward_axis, up_axis
    )
    collection.objects.link(axis_root)
    return axis_root


def create_staging_collection_tree(
    loaded: LoadedReconstruction,
) -> tuple[
    bpy.types.Collection,
    bpy.types.Collection,
    bpy.types.Collection,
    bpy.types.Collection,
]:
    """기존 이름과 소유권을 유지한 staging Collection 트리를 생성합니다."""

    created: list[bpy.types.Collection] = []
    try:
        root = bpy.data.collections.new(
            f"COLMAP_{loaded.source.dataset_uuid[:8]}_STAGING"
        )
        created.append(root)
        points_collection = bpy.data.collections.new("POINTS")
        created.append(points_collection)
        cameras_collection = bpy.data.collections.new("CAMERAS")
        created.append(cameras_collection)
        guides_collection = bpy.data.collections.new("GUIDES")
        created.append(guides_collection)
        mark_managed(root, loaded, "ROOT")
        mark_managed(points_collection, loaded, "POINTS_COLLECTION")
        mark_managed(cameras_collection, loaded, "CAMERAS_COLLECTION")
        mark_managed(guides_collection, loaded, "GUIDES_COLLECTION")
        root.children.link(points_collection)
        root.children.link(cameras_collection)
        root.children.link(guides_collection)
    except Exception:
        for collection in reversed(created):
            if collection.name in bpy.data.collections:
                bpy.data.collections.remove(collection)
        raise
    return root, points_collection, cameras_collection, guides_collection


def populate_staging_collection_tree(
    loaded: LoadedReconstruction,
    points_collection: bpy.types.Collection,
    cameras_collection: bpy.types.Collection,
    guides_collection: bpy.types.Collection,
    *,
    point_size: float,
    camera_frustum_size: float,
    forward_axis: str,
    up_axis: str,
) -> None:
    """staging Collection에 축, Point와 Camera 요소를 생성합니다."""
    axis_root = _create_axis_root(
        loaded,
        guides_collection,
        forward_axis,
        up_axis,
    )
    _create_points(loaded, points_collection, axis_root, point_size)
    _create_cameras(
        loaded,
        cameras_collection,
        axis_root,
        camera_frustum_size,
    )
