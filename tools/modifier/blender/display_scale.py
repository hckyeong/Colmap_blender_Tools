"""데이터 배율에서 점 반지름과 카메라 표시 크기를 분리합니다."""

from math import hypot, isclose, isfinite
from struct import pack, unpack

from .managed_scene import is_managed, iter_point_display_modifiers

RADIUS_NODE = "COLMAP_Display_Radius_Compensation"


def ensure_radius_compensation(node_group):
    """공개 입력을 보존하면서 관리 반지름 경로에 내부 역배율 노드를 연결합니다."""
    group_input = node_group.nodes.get("COLMAP_Group_Input")
    mesh_to_points = node_group.nodes.get("COLMAP_Mesh_To_Points")
    if (group_input is None or group_input.bl_idname != "NodeGroupInput"
            or mesh_to_points is None or mesh_to_points.bl_idname != "GeometryNodeMeshToPoints"
            or group_input.outputs.get("Point Scale") is None):
        raise ValueError("관리 점 표시 노드의 입력 또는 반지름 경로가 손상되었습니다.")
    source = group_input.outputs["Point Scale"]
    radius = mesh_to_points.inputs["Radius"]
    node = node_group.nodes.get(RADIUS_NODE)
    if node is None:
        if len(radius.links) != 1 or radius.links[0].from_socket != source:
            raise ValueError("구형 관리 점 표시의 반지름 연결이 변경되어 이전할 수 없습니다.")
        node = node_group.nodes.new("ShaderNodeMath")
        node.name = RADIUS_NODE
        node.label = "표시 반지름 역배율"
        node.operation = "MULTIPLY"
        node.inputs[1].default_value = 1.0
        node.location = (-120.0, -360.0)
        node_group.links.new(source, node.inputs[0])
        node_group.links.new(node.outputs[0], radius)
    elif (node.bl_idname != "ShaderNodeMath" or node.operation != "MULTIPLY"
          or node.use_clamp or len(node.inputs[0].links) != 1
          or node.inputs[0].links[0].from_socket != source
          or node.inputs[1].is_linked or len(radius.links) != 1
          or radius.links[0].from_socket != node.outputs[0]):
        raise ValueError("관리 점 표시의 역배율 노드 구조가 변경되었습니다.")
    return node


def inverse_display_scale(scale):
    """유한한 역배율이 Blender 단정도 실수에서 유효하게 표현되는지 검사합니다."""
    if not isfinite(scale) or scale <= 0:
        raise ValueError("표시 보정 배율은 유한한 양수여야 합니다.")
    inverse = 1.0 / scale
    try:
        stored = unpack("f", pack("f", inverse))[0]
    except (OverflowError, ValueError) as exc:
        raise ValueError("표시 역배율이 Blender 실수 표현 범위를 벗어났습니다.") from exc
    if not isfinite(stored) or stored <= 0 or not isclose(stored, inverse, rel_tol=1e-6):
        raise ValueError("표시 역배율이 Blender 실수 표현 범위를 벗어났습니다.")
    return stored


def compensate_object(obj, scale):
    """부모의 현재 절대 배율에 맞춰 관리 객체 하나의 표시 크기만 보정합니다."""
    inverse = inverse_display_scale(scale)
    if is_managed(obj, kind="POINT_MESH"):
        for modifier in iter_point_display_modifiers(obj):
            socket = ensure_radius_compensation(modifier.node_group).inputs[1]
            socket.default_value = inverse
            if not isclose(socket.default_value, inverse, rel_tol=1e-6):
                raise ValueError("표시 역배율이 노드 입력의 표현 범위를 벗어났습니다.")
        obj.update_tag(refresh={"DATA"})
    elif is_managed(obj, kind="CAMERA"):
        obj.scale = (inverse,) * 3
        obj.update_tag(refresh={"OBJECT"})


class DisplayScaleTargets:
    """드래그 시작 때 수집한 표시 참조만 사용해 배율 변화에 대응합니다."""

    def __init__(self, scene, roots):
        """현재 데이터셋과 표시 루트에 속한 점 및 카메라를 한 번 수집합니다."""
        dataset_uuid = scene.colmap_import.dataset_uuid
        self.objects = tuple(obj for obj in scene.objects
                             if obj.parent in roots and is_managed(obj, dataset_uuid=dataset_uuid)
                             and (is_managed(obj, kind="POINT_MESH") or is_managed(obj, kind="CAMERA")))
        self.scale = None

    def apply(self, matrix):
        """이동·회전에는 다시 쓰지 않고 절대 균일 배율이 바뀔 때만 보정합니다."""
        scale = hypot(*(matrix[i][0] for i in range(3)))
        inverse_display_scale(scale)
        if self.scale is not None and isclose(scale, self.scale, rel_tol=1.0e-7):
            return
        for obj in self.objects:
            compensate_object(obj, scale)
        self.scale = scale
