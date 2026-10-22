"""참조 COLMAP Importer 방식으로 단일 Point Mesh와 표시 리소스를 생성합니다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from time import perf_counter
from typing import Any, Iterable

import bpy
from mathutils import Matrix

from .ownership import is_owned, owned_uuid_of, resolve_owned, tag_owned

PENDING_FILTER_ATTRIBUTE = "colmap_pending_filtered"


@dataclass(frozen=True, slots=True)
class PointSceneResources:
    """Point 표시를 구성하는 소유 Blender ID를 묶어 보관합니다."""

    point_object: Any
    mesh: Any
    material: Any
    node_group: Any
    point_count: int


class PointSceneBuildPhase(str, Enum):
    """Point3D 표시 리소스 생성의 사용자 표시 단계를 정의합니다."""

    PREPARE_BUFFERS = "PREPARE_BUFFERS"
    CREATE_MESH = "CREATE_MESH"
    CREATE_COLOR = "CREATE_COLOR"
    CREATE_ERROR = "CREATE_ERROR"
    CREATE_INDEX = "CREATE_INDEX"
    CREATE_IDS = "CREATE_IDS"
    CREATE_PENDING = "CREATE_PENDING"
    CREATE_DISPLAY = "CREATE_DISPLAY"
    DONE = "DONE"


_POINT_PHASE_MESSAGES = {
    PointSceneBuildPhase.PREPARE_BUFFERS: "Preparing Point3D buffers",
    PointSceneBuildPhase.CREATE_MESH: "Creating Point3D mesh",
    PointSceneBuildPhase.CREATE_COLOR: "Writing Point3D colors",
    PointSceneBuildPhase.CREATE_ERROR: "Writing Point3D errors",
    PointSceneBuildPhase.CREATE_INDEX: "Writing Point3D source indexes",
    PointSceneBuildPhase.CREATE_IDS: "Writing Point3D identifiers",
    PointSceneBuildPhase.CREATE_PENDING: "Writing Point3D visibility state",
    PointSceneBuildPhase.CREATE_DISPLAY: "Creating Point3D display",
    PointSceneBuildPhase.DONE: "Point3D display complete",
}


def _field(value: Any, name: str) -> Any:
    """Mapping과 일반 객체에서 같은 이름의 필드를 읽습니다."""
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def _signed_int32(value: int) -> int:
    """부호 없는 32비트 Pattern을 Blender signed INT 값으로 바꿉니다."""
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def point_scale_socket_identifier(node_group: Any) -> str:
    """관리 Node Group의 Point Scale 입력 식별자를 반환합니다."""
    identifier = node_group.get("pp_point_scale_socket")
    if not identifier:
        raise RuntimeError("Point 표시 Node Group에 Point Scale 입력이 없습니다.")
    return str(identifier)


def set_modifier_point_scale(
    modifier: Any,
    node_group: Any,
    point_scale: float,
) -> None:
    """Blender 5.2 Geometry Nodes Modifier에 Point Scale을 기록합니다."""
    identifier = point_scale_socket_identifier(node_group)
    modifier_input = getattr(modifier.properties.inputs, identifier, None)
    if modifier_input is None:
        raise RuntimeError("Point 표시 Modifier에 Point Scale 입력이 없습니다.")
    modifier_input.value = float(point_scale)


def _create_point_material(dataset_uuid: str) -> Any:
    """COLMAP RGB Attribute를 표시하는 관리 Material을 만듭니다."""
    material = bpy.data.materials.new("PP_COLMAP_Point_Color")
    tag_owned(material, "POINT_DISPLAY_MATERIAL", dataset_uuid=dataset_uuid)
    material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = nodes.get("Principled BSDF")
    output = nodes.get("Material Output")
    if principled is None or output is None:
        raise RuntimeError("Point 표시 Material의 기본 Shader Node가 없습니다.")
    color_attribute = nodes.new("ShaderNodeAttribute")
    color_attribute.name = "PP_COLMAP_Color_Attribute"
    color_attribute.label = "COLMAP Color"
    color_attribute.attribute_name = "colmap_color"
    links.new(color_attribute.outputs["Color"], principled.inputs["Base Color"])
    links.new(color_attribute.outputs["Color"], principled.inputs["Emission Color"])
    principled.inputs["Roughness"].default_value = 1.0
    principled.inputs["Emission Strength"].default_value = 0.25
    return material


def _create_point_display_nodes(
    dataset_uuid: str,
    material: Any,
    point_size: float,
) -> Any:
    """Mesh Vertex를 색상과 반지름이 있는 Point Cloud 표시로 변환합니다."""
    node_group = bpy.data.node_groups.new(
        "PP_COLMAP_Point_Display",
        "GeometryNodeTree",
    )
    tag_owned(node_group, "POINT_DISPLAY_NODES", dataset_uuid=dataset_uuid)
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
    point_scale_socket.default_value = float(point_size)
    point_scale_socket.min_value = 0.000001
    point_scale_socket.max_value = 1000000.0
    point_scale_socket.subtype = "DISTANCE"
    node_group["pp_point_scale_socket"] = point_scale_socket.identifier
    node_group.interface.new_socket(
        name="Geometry",
        in_out="OUTPUT",
        socket_type="NodeSocketGeometry",
    )

    nodes = node_group.nodes
    links = node_group.links
    group_input = nodes.new("NodeGroupInput")
    group_input.location = (-360.0, 0.0)
    mesh_to_points = nodes.new("GeometryNodeMeshToPoints")
    mesh_to_points.mode = "VERTICES"
    mesh_to_points.location = (-120.0, 0.0)
    pending_attribute = nodes.new("GeometryNodeInputNamedAttribute")
    pending_attribute.data_type = "BOOLEAN"
    pending_attribute.inputs["Name"].default_value = PENDING_FILTER_ATTRIBUTE
    pending_attribute.location = (-360.0, -180.0)
    visible_points = nodes.new("FunctionNodeBooleanMath")
    visible_points.operation = "NOT"
    visible_points.location = (-120.0, -180.0)
    set_material = nodes.new("GeometryNodeSetMaterial")
    set_material.location = (120.0, 0.0)
    set_material.inputs["Material"].default_value = material
    group_output = nodes.new("NodeGroupOutput")
    group_output.location = (360.0, 0.0)

    links.new(group_input.outputs["Geometry"], mesh_to_points.inputs["Mesh"])
    links.new(group_input.outputs["Point Scale"], mesh_to_points.inputs["Radius"])
    links.new(pending_attribute.outputs["Attribute"], visible_points.inputs[0])
    links.new(visible_points.outputs["Boolean"], mesh_to_points.inputs["Selection"])
    links.new(mesh_to_points.outputs["Points"], set_material.inputs["Geometry"])
    links.new(set_material.outputs["Geometry"], group_output.inputs["Geometry"])
    return node_group


class PointSceneBuildJob:
    """Point3D 버퍼와 Blender 표시 리소스를 시간 예산 안에서 생성합니다."""

    def __init__(
        self,
        points: tuple[Any, ...],
        collection: Any,
        axis_root: Any,
        dataset_uuid: str,
        point_size: float,
    ) -> None:
        """정렬된 Point3D와 출력 소유자를 고정해 초기 Job 상태를 만듭니다."""

        if not isfinite(point_size) or point_size <= 0.0:
            raise ValueError("Point Scale은 0보다 큰 유한값이어야 합니다.")
        self._points = points
        self._collection = collection
        self._axis_root = axis_root
        self._dataset_uuid = dataset_uuid
        self._point_size = float(point_size)
        self._phase = PointSceneBuildPhase.PREPARE_BUFFERS
        self._cursor = 0
        self._fixed_completed = 0
        self._coordinates = []
        self._colors = []
        self._errors = []
        self._indexes = []
        self._id_low = []
        self._id_high = []
        self._mesh = None
        self._material = None
        self._node_group = None
        self._point_object = None
        self._result = None

    @property
    def done(self) -> bool:
        """Point3D 표시 리소스가 모두 완성되었는지 반환합니다."""

        return self._phase is PointSceneBuildPhase.DONE

    @property
    def phase_message(self) -> str:
        """현재 Point3D 세부 단계의 Overlay 문구를 반환합니다."""

        return _POINT_PHASE_MESSAGES[self._phase]

    @property
    def completed_units(self) -> int:
        """완료한 Point 레코드와 고정 생성 단계 수를 반환합니다."""

        return self._cursor + self._fixed_completed

    @property
    def total_units(self) -> int:
        """Point 레코드와 일곱 개 고정 생성 단계의 전체 작업량을 반환합니다."""

        return len(self._points) + 7

    @property
    def result(self) -> PointSceneResources:
        """완료된 Point3D 표시 리소스를 반환합니다."""

        if not self.done or self._result is None:
            raise RuntimeError("완료되지 않은 Point3D 표시 Job의 결과입니다.")
        return self._result

    def _prepare_buffers(self, deadline: float, hard_cap: int) -> None:
        """배열 입력은 일괄 변환하고 기존 레코드 입력은 시간 예산 안에서 변환합니다."""
        from ..core.colmap.shared_reader import PointPayloadView
        if isinstance(self._points, PointPayloadView):
            (self._coordinates, self._colors, self._errors, self._indexes,
             self._id_low, self._id_high) = self._points.blender_buffers()
            self._cursor = len(self._points)
            self._phase = PointSceneBuildPhase.CREATE_MESH
            return

        processed = 0
        while self._cursor < len(self._points) and processed < hard_cap:
            point = self._points[self._cursor]
            point_id = int(_field(point, "point3d_id"))
            red, green, blue = _field(point, "rgb")
            self._coordinates.append(_field(point, "xyz"))
            self._colors.extend(
                (red / 255.0, green / 255.0, blue / 255.0, 1.0)
            )
            self._errors.append(_field(point, "error"))
            self._indexes.append(self._cursor)
            self._id_low.append(_signed_int32(point_id))
            self._id_high.append(_signed_int32(point_id >> 32))
            self._cursor += 1
            processed += 1
            if processed % 256 == 0 and perf_counter() >= deadline:
                return
        if self._cursor == len(self._points):
            self._phase = PointSceneBuildPhase.CREATE_MESH

    def _create_mesh(self) -> None:
        """연속 좌표 배열은 정점에 일괄 기록하고 기존 목록 입력도 지원합니다."""

        self._mesh = bpy.data.meshes.new("PP_COLMAP_Points")
        tag_owned(self._mesh, "POINT_MESH_DATA", dataset_uuid=self._dataset_uuid)
        from ..core.colmap.shared_reader import PointPayloadView
        if isinstance(self._points, PointPayloadView):
            self._mesh.vertices.add(len(self._points))
            self._mesh.vertices.foreach_set("co", self._coordinates)
        else:
            self._mesh.from_pydata(self._coordinates, [], [])
        self._mesh.update()
        self._coordinates = []
        self._fixed_completed += 1
        self._phase = PointSceneBuildPhase.CREATE_COLOR

    def _create_color(self) -> None:
        """Point3D RGB 값을 Mesh Color Attribute에 일괄 기록합니다."""

        attribute = self._mesh.color_attributes.new(
            name="colmap_color",
            type="BYTE_COLOR",
            domain="POINT",
        )
        attribute.data.foreach_set("color", self._colors)
        self._mesh.color_attributes.active = attribute
        self._mesh.color_attributes.active_color = attribute
        self._colors = []
        self._fixed_completed += 1
        self._phase = PointSceneBuildPhase.CREATE_ERROR

    def _create_error(self) -> None:
        """Point3D 재투영 오차를 Float Attribute에 일괄 기록합니다."""

        attribute = self._mesh.attributes.new(
            "colmap_reprojection_error",
            "FLOAT",
            "POINT",
        )
        attribute.data.foreach_set("value", self._errors)
        self._errors = []
        self._fixed_completed += 1
        self._phase = PointSceneBuildPhase.CREATE_INDEX

    def _create_index(self) -> None:
        """원본 Point3D 순서를 Integer Attribute에 일괄 기록합니다."""

        attribute = self._mesh.attributes.new(
            "colmap_source_index",
            "INT",
            "POINT",
        )
        attribute.data.foreach_set("value", self._indexes)
        self._indexes = []
        self._fixed_completed += 1
        self._phase = PointSceneBuildPhase.CREATE_IDS

    def _create_ids(self) -> None:
        """64비트 Point3D ID를 상·하위 Integer Attribute로 기록합니다."""

        low = self._mesh.attributes.new("colmap_point3d_id_low", "INT", "POINT")
        high = self._mesh.attributes.new("colmap_point3d_id_high", "INT", "POINT")
        low.data.foreach_set("value", self._id_low)
        high.data.foreach_set("value", self._id_high)
        self._id_low = []
        self._id_high = []
        self._fixed_completed += 1
        self._phase = PointSceneBuildPhase.CREATE_PENDING

    def _create_pending(self) -> None:
        """Point 필터용 기본 가시성 Boolean Attribute를 생성합니다."""

        self._mesh.attributes.new(PENDING_FILTER_ATTRIBUTE, "BOOLEAN", "POINT")
        self._fixed_completed += 1
        self._phase = PointSceneBuildPhase.CREATE_DISPLAY

    def _create_display(self) -> None:
        """Point Object, Material과 Geometry Nodes 표시를 연결합니다."""

        self._point_object = bpy.data.objects.new("PP_COLMAP_Points", self._mesh)
        tag_owned(
            self._point_object,
            "POINT_MESH",
            dataset_uuid=self._dataset_uuid,
        )
        self._point_object["colmap_point3d_id_encoding"] = "uint64_high_low_int32"
        self._point_object["colmap_point_size"] = self._point_size
        self._point_object.parent = self._axis_root
        self._point_object.matrix_parent_inverse = Matrix.Identity(4)
        self._point_object.matrix_basis = Matrix.Identity(4)
        self._point_object.hide_select = True
        self._point_object.select_set(False)
        self._collection.objects.link(self._point_object)
        self._material = _create_point_material(self._dataset_uuid)
        self._node_group = _create_point_display_nodes(
            self._dataset_uuid,
            self._material,
            self._point_size,
        )
        modifier = self._point_object.modifiers.new("COLMAP Point Display", "NODES")
        modifier.node_group = self._node_group
        set_modifier_point_scale(modifier, self._node_group, self._point_size)
        self._fixed_completed += 1
        self._result = PointSceneResources(
            point_object=self._point_object,
            mesh=self._mesh,
            material=self._material,
            node_group=self._node_group,
            point_count=len(self._points),
        )
        self._phase = PointSceneBuildPhase.DONE

    def advance(
        self,
        *,
        time_budget: float = 0.010,
        hard_cap: int = 16_384,
    ) -> bool:
        """현재 Point3D 단계를 시간·레코드 상한 안에서 한 번 진행합니다."""

        deadline = perf_counter() + max(0.001, float(time_budget))
        try:
            if self._phase is PointSceneBuildPhase.PREPARE_BUFFERS:
                self._prepare_buffers(deadline, max(1, int(hard_cap)))
            elif self._phase is PointSceneBuildPhase.CREATE_MESH:
                self._create_mesh()
            elif self._phase is PointSceneBuildPhase.CREATE_COLOR:
                self._create_color()
            elif self._phase is PointSceneBuildPhase.CREATE_ERROR:
                self._create_error()
            elif self._phase is PointSceneBuildPhase.CREATE_INDEX:
                self._create_index()
            elif self._phase is PointSceneBuildPhase.CREATE_IDS:
                self._create_ids()
            elif self._phase is PointSceneBuildPhase.CREATE_PENDING:
                self._create_pending()
            elif self._phase is PointSceneBuildPhase.CREATE_DISPLAY:
                self._create_display()
            return self.done
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        """생성 중인 Blender 자원과 목록·배열 입력의 작업 버퍼를 모두 해제합니다."""

        remove_point_scene_resources(
            PointSceneResources(
                self._point_object,
                self._mesh,
                self._material,
                self._node_group,
                len(self._points),
            )
        )
        self._coordinates = []
        self._colors = []
        self._errors = []
        self._indexes = []
        self._id_low = []
        self._id_high = []


def create_point_scene_resources(
    points: Iterable[Any],
    collection: Any,
    axis_root: Any,
    dataset_uuid: str,
    point_size: float,
) -> PointSceneResources:
    """검증된 Point3D를 참조 Importer와 같은 단일 Mesh 표시로 생성합니다."""
    ordered = tuple(
        sorted(points, key=lambda item: int(_field(item, "point3d_id")))
    )
    job = PointSceneBuildJob(
        ordered,
        collection,
        axis_root,
        dataset_uuid,
        point_size,
    )
    while not job.advance(time_budget=1.0, hard_cap=1_000_000):
        pass
    return job.result


def remove_point_scene_resources(resources: PointSceneResources | None) -> None:
    """생성 도중 남은 Point Object, Mesh, Material과 Node Group을 제거합니다."""
    if resources is None:
        return
    if resources.point_object is not None:
        try:
            bpy.data.objects.remove(resources.point_object, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass
    for values, data in (
        (bpy.data.meshes, resources.mesh),
        (bpy.data.node_groups, resources.node_group),
        (bpy.data.materials, resources.material),
    ):
        if data is None:
            continue
        try:
            if data.users == 0:
                values.remove(data)
        except (ReferenceError, RuntimeError):
            pass


def _tag_point_display_update(obj: Any) -> None:
    """Point Geometry 재평가와 열린 3D Viewport 다시 그리기를 요청합니다."""

    obj.update_tag(refresh={"DATA"})
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def update_dataset_point_scale(scene: Any, point_scale: float) -> None:
    """현재 Dataset의 Point 표시 반지름을 관리 Modifier에 반영합니다."""
    settings = getattr(scene, "projection_paint", None)
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    if not dataset_uuid:
        return
    for obj in scene.objects:
        if not is_owned(obj, kind="POINT_MESH", dataset_uuid=dataset_uuid):
            continue
        modifier = next(
            (
                item
                for item in obj.modifiers
                if item.type == "NODES"
                and item.node_group is not None
                and is_owned(
                    item.node_group,
                    kind="POINT_DISPLAY_NODES",
                    dataset_uuid=dataset_uuid,
                )
            ),
            None,
        )
        if modifier is not None:
            set_modifier_point_scale(modifier, modifier.node_group, point_scale)
            obj["colmap_point_size"] = float(point_scale)
            _tag_point_display_update(obj)


def hide_dataset_points(scene: Any, view_layer: Any) -> list[dict[str, Any]]:
    """현재 Dataset의 관리 Point 눈 표시를 끄고 복구 기록을 반환합니다."""
    settings = getattr(scene, "projection_paint", None)
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    if not dataset_uuid:
        return []
    points = [
        obj
        for obj in scene.objects
        if is_owned(obj, kind="POINT_MESH", dataset_uuid=dataset_uuid)
    ]
    records = [
        {
            "uuid": owned_uuid_of(obj),
            "dataset_uuid": dataset_uuid,
            "original_hidden": bool(obj.hide_get(view_layer=view_layer)),
            "applied_hidden": True,
        }
        for obj in points
    ]
    changed: list[Any] = []
    try:
        for obj, record in zip(points, records, strict=True):
            if not bool(record["original_hidden"]):
                obj.hide_set(True, view_layer=view_layer)
                changed.append(obj)
    except (ReferenceError, RuntimeError):
        for obj in reversed(changed):
            try:
                obj.hide_set(False, view_layer=view_layer)
            except (ReferenceError, RuntimeError):
                pass
        raise
    return records


def restore_dataset_points(
    scene: Any,
    view_layer: Any,
    records: Iterable[dict[str, Any]],
) -> None:
    """애드온이 적용한 상태가 유지된 관리 Point만 진입 전 표시로 복구합니다."""
    for record in records:
        dataset_uuid = str(record.get("dataset_uuid", "") or "")
        point = resolve_owned(
            scene.objects,
            str(record.get("uuid", "") or ""),
            kind="POINT_MESH",
            dataset_uuid=dataset_uuid,
        )
        if point is None:
            continue
        applied = bool(record.get("applied_hidden", True))
        try:
            current_hidden = bool(point.hide_get(view_layer=view_layer))
        except RuntimeError:
            continue
        if current_hidden != applied:
            continue
        point.hide_set(
            bool(record.get("original_hidden", False)),
            view_layer=view_layer,
        )
