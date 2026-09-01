"""Blender Texture Paint 도구 전환 Edge만 최소한으로 연결합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ownership import PROJECT_STENCIL_DRAW_BRUSH_ROLE, is_owned


ESSENTIALS_TEXTURE_BRUSH_LIBRARY = "brushes/essentials_brushes-mesh_texture.blend"


@dataclass(frozen=True, slots=True)
class PaintToolSpec:
    """지원 Texture Paint 도구의 Brush 종류와 기본 자산을 보관합니다."""

    tool_id: str
    brush_type: str
    asset_name: str

    @property
    def essentials_identifier(self) -> str:
        """Blender Essentials Brush 자산 상대 경로를 반환합니다."""

        return f"{ESSENTIALS_TEXTURE_BRUSH_LIBRARY}/Brush/{self.asset_name}"


@dataclass(frozen=True, slots=True)
class BrushAssetChoice:
    """다시 활성화할 수 있는 호환 사용자 Brush 자산 참조를 보관합니다."""

    library_type: str
    library_identifier: str
    relative_identifier: str


@dataclass(slots=True)
class PaintToolBridgeState:
    """현재 Tool ID 기준선과 Tool별 마지막 호환 Brush만 Runtime에 보관합니다."""

    tool_id: str = ""
    last_compatible_brushes: dict[str, BrushAssetChoice] = field(default_factory=dict)


_TOOL_SPECS = {
    "builtin.brush": PaintToolSpec("builtin.brush", "DRAW", "Paint Hard"),
    "builtin_brush.soften": PaintToolSpec("builtin_brush.soften", "SOFTEN", "Blur"),
    "builtin_brush.smear": PaintToolSpec("builtin_brush.smear", "SMEAR", "Smear"),
    "builtin_brush.clone": PaintToolSpec("builtin_brush.clone", "CLONE", "Clone"),
    "builtin_brush.fill": PaintToolSpec("builtin_brush.fill", "FILL", "Fill"),
    "builtin_brush.mask": PaintToolSpec("builtin_brush.mask", "MASK", "Mask"),
}


def _pointer(value: Any) -> int:
    """삭제된 Blender RNA 객체를 안전한 정수 Pointer로 변환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def active_texture_paint_tool(context: Any):
    """현재 Workspace의 Texture Paint Tool을 변경 없이 조회합니다."""

    workspace = getattr(context, "workspace", None)
    if workspace is None:
        workspace = getattr(getattr(context, "window", None), "workspace", None)
    tools = getattr(workspace, "tools", None)
    if tools is None:
        return None
    try:
        return tools.from_space_view3d_mode("PAINT_TEXTURE", create=False)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None


def active_tool_id(context: Any) -> str:
    """현재 Texture Paint Tool의 실제 ID를 비교 가능한 문자열로 반환합니다."""

    return str(getattr(active_texture_paint_tool(context), "idname", "") or "")


def paint_tool_spec(tool_id: str) -> PaintToolSpec | None:
    """지원 Tool ID에 대응하는 Brush 명세를 반환합니다."""

    return _TOOL_SPECS.get(str(tool_id))


def active_brush_choice(scene: Any) -> BrushAssetChoice | None:
    """현재 활성 Brush의 재활성화 가능한 Asset 참조를 반환합니다."""

    image_paint = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    reference = getattr(image_paint, "brush_asset_reference", None)
    relative = str(getattr(reference, "relative_asset_identifier", "") or "")
    if relative:
        return BrushAssetChoice(
            str(getattr(reference, "asset_library_type", "") or ""),
            str(getattr(reference, "asset_library_identifier", "") or ""),
            relative,
        )
    brush = getattr(image_paint, "brush", None)
    if _pointer(brush) and getattr(brush, "asset_data", None) is not None:
        return BrushAssetChoice("LOCAL", "", f"Brush/{brush.name}")
    return None


def activate_brush_asset(
    context: Any,
    scene: Any,
    choice: BrushAssetChoice,
    *,
    expected_tool_id: str = "",
    expected_brush_type: str = "",
):
    """지정 Brush Asset을 활성화하고 요청한 Tool·Brush 종류를 검증합니다."""

    import bpy

    with context.temp_override(**_viewport_override(context)):
        result = bpy.ops.brush.asset_activate(
            asset_library_type=choice.library_type,
            asset_library_identifier=choice.library_identifier,
            relative_asset_identifier=choice.relative_identifier,
        )
    if result != {"FINISHED"}:
        raise RuntimeError(f"Blender Brush 자산 활성화가 실패했습니다: {result}")
    if expected_tool_id and active_tool_id(context) != expected_tool_id:
        raise RuntimeError("Brush 활성화 중 사용자가 선택한 Blender Tool이 바뀌었습니다.")
    brush = scene.tool_settings.image_paint.brush
    if (
        expected_brush_type
        and str(getattr(brush, "image_brush_type", "") or "") != expected_brush_type
    ):
        raise RuntimeError("활성 Brush 종류가 요청한 Blender Tool과 일치하지 않습니다.")
    return brush


def _brush_is_compatible(scene: Any, spec: PaintToolSpec) -> bool:
    """현재 활성 Brush가 선택 Tool의 Brush 종류와 일치하는지 확인합니다."""

    image_paint = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    brush = getattr(image_paint, "brush", None)
    return bool(
        _pointer(brush)
        and str(getattr(brush, "image_brush_type", "") or "") == spec.brush_type
    )


def _remember_compatible_brush(
    state: PaintToolBridgeState,
    scene: Any,
    spec: PaintToolSpec,
) -> None:
    """현재 호환 Brush가 자산이면 Tool별 마지막 선택으로 기록합니다."""

    if not _brush_is_compatible(scene, spec):
        return
    choice = active_brush_choice(scene)
    if choice is not None:
        state.last_compatible_brushes[spec.tool_id] = choice


def _project_stencil_draw_brush_choice(
    scene: Any,
    spec: PaintToolSpec,
) -> BrushAssetChoice | None:
    """Draw Tool 복귀 때 저장된 프로젝트 전용 Stencil Brush 참조를 반환합니다."""

    if spec.brush_type != "DRAW":
        return None
    settings = getattr(scene, "projection_paint", None)
    brush = getattr(settings, "stencil_brush", None)
    if not (
        _pointer(brush)
        and is_owned(brush, kind=PROJECT_STENCIL_DRAW_BRUSH_ROLE)
        and str(getattr(brush, "image_brush_type", "") or "") == "DRAW"
        and getattr(brush, "asset_data", None) is not None
    ):
        return None
    return BrushAssetChoice("LOCAL", "", f"Brush/{brush.name}")


def _viewport_override(context: Any) -> dict[str, Any]:
    """Brush 자산 활성화에 필요한 현재 Window의 3D Viewport 문맥을 반환합니다."""

    window = getattr(context, "window", None)
    if window is None:
        raise RuntimeError("Blender Brush를 활성화할 Window가 없습니다.")
    area = getattr(context, "area", None)
    try:
        current_view = bool(
            area is not None
            and area.type == "VIEW_3D"
            and area in tuple(window.screen.areas)
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        current_view = False
    if not current_view:
        area = next(
            (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
            None,
        )
    region = next(
        (candidate for candidate in getattr(area, "regions", ()) if candidate.type == "WINDOW"),
        None,
    )
    if area is None or region is None:
        raise RuntimeError("Blender Brush를 활성화할 3D Viewport가 없습니다.")
    return {"window": window, "screen": window.screen, "area": area, "region": region}


def initialize_paint_tool_bridge(
    context: Any,
    scene: Any,
    state: PaintToolBridgeState | None = None,
) -> PaintToolBridgeState:
    """현재 Tool을 기준선으로 기록하고 Clone이면 같은 활성 Layer 모드를 확정합니다."""

    bridge = state if state is not None else PaintToolBridgeState()
    bridge.tool_id = active_tool_id(context)
    spec = paint_tool_spec(bridge.tool_id)
    if spec is not None:
        if spec.brush_type == "CLONE":
            scene.tool_settings.image_paint.use_clone_layer = False
        _remember_compatible_brush(bridge, scene, spec)
    return bridge


def handle_supported_tool_edge(
    context: Any,
    scene: Any,
    state: PaintToolBridgeState,
    tool_id: str,
) -> bool:
    """명시 지원 Tool 전환 Edge에서만 Clone 모드와 필요 Brush를 한 번 연결합니다."""

    spec = paint_tool_spec(tool_id)
    if spec is None:
        return False
    image_paint = scene.tool_settings.image_paint
    if spec.brush_type == "CLONE":
        image_paint.use_clone_layer = False
    if _brush_is_compatible(scene, spec):
        _remember_compatible_brush(state, scene, spec)
        return False
    fallback = BrushAssetChoice("ESSENTIALS", "", spec.essentials_identifier)
    requested = (
        state.last_compatible_brushes.get(spec.tool_id),
        _project_stencil_draw_brush_choice(scene, spec),
        fallback,
    )
    choices = tuple(dict.fromkeys(choice for choice in requested if choice is not None))
    errors: list[str] = []

    for choice in choices:
        try:
            activate_brush_asset(
                context,
                scene,
                choice,
                expected_tool_id=spec.tool_id,
                expected_brush_type=spec.brush_type,
            )
            _remember_compatible_brush(state, scene, spec)
            return True
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(str(exc))
    raise RuntimeError(
        f"Blender {spec.asset_name} Brush 자산 활성화에 실패했습니다: "
        + "; ".join(error for error in errors if error)
    )


def detect_paint_tool_edge(
    context: Any,
    scene: Any,
    state: PaintToolBridgeState,
) -> bool:
    """Tool ID가 실제로 바뀐 Tick만 처리하고 동일 ID Tick은 즉시 종료합니다."""

    tool_id = active_tool_id(context)
    if tool_id == state.tool_id:
        return False
    previous_spec = paint_tool_spec(state.tool_id)
    if previous_spec is not None:
        _remember_compatible_brush(state, scene, previous_spec)
    state.tool_id = tool_id
    if paint_tool_spec(tool_id) is None:
        return False
    handle_supported_tool_edge(context, scene, state, tool_id)
    return True


__all__ = [
    "BrushAssetChoice",
    "PaintToolBridgeState",
    "PaintToolSpec",
    "activate_brush_asset",
    "active_brush_choice",
    "active_texture_paint_tool",
    "active_tool_id",
    "detect_paint_tool_edge",
    "handle_supported_tool_edge",
    "initialize_paint_tool_bridge",
    "paint_tool_spec",
]
