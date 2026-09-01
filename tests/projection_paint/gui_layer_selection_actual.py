"""설치된 Extension의 실제 Layer 버튼을 event_simulate로 검증합니다.

실행 전 별도 GUI Blender에서 Extension 0.3.23을 활성화하고 다음 두 환경 변수를
해당 창의 실제 Layers 패널 팝업 좌표로 지정합니다. Blender Python RNA는
UILayout.operator 버튼의 사각형을 공개하지 않으므로 좌표를 추측하지 않습니다.

PP_LAYER_SELECTION_PANEL_OPEN_AT='[x, y]'
PP_LAYER_SELECTION_BUTTON_COORDS='{"0": [x, y], "1": [x, y], "2": [x, y], "3": [x, y]}'

예: blender --factory-startup --enable-event-simulate --python tests/gui_layer_selection_actual.py
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
import time
import tomllib

import bpy


ROOT = Path(__file__).resolve().parents[2] / "tools" / "projection_paint"
EXPECTED_VERSION = "0.3.23"
_STATE: dict[str, object] = {
    "actions": [],
    "checks": [],
    "finished": False,
}


class _PreconditionError(RuntimeError):
    """추측 없이 실제 UI 입력을 보내기 위한 사전 조건 오류입니다."""


def _emit(payload: dict) -> None:
    """자동 판독 가능한 단일 JSON 결과를 표준 출력으로 보냅니다."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _finish(success: bool, *, code: str = "", error: str = "") -> None:
    """검증 결과를 한 번 기록하고 저장 없이 별도 Blender를 종료합니다."""
    if bool(_STATE["finished"]):
        return
    _STATE["finished"] = True
    _emit(
        {
            "kind": "projection_paint.layer_selection_actual",
            "success": bool(success),
            "code": str(code),
            "error": str(error),
            "version": EXPECTED_VERSION,
            "checks": list(_STATE["checks"]),
        }
    )
    bpy.app.timers.register(_quit_without_save, first_interval=0.1)


def _quit_without_save() -> None:
    """테스트 Scene을 저장하지 않고 별도 GUI Blender만 종료합니다."""
    bpy.ops.wm.quit_blender()


def _record_check(name: str) -> None:
    """성공한 상태 검증 이름을 구조화된 결과에 추가합니다."""
    checks = _STATE["checks"]
    assert isinstance(checks, list)
    checks.append(str(name))


def _parse_point(value: object, name: str) -> tuple[int, int]:
    """외부에서 계측한 창 좌표만 정수 점으로 검증합니다."""
    if not isinstance(value, list) or len(value) != 2:
        raise _PreconditionError(f"{name} 좌표는 [x, y] 배열이어야 합니다.")
    if not all(isinstance(component, int) for component in value):
        raise _PreconditionError(f"{name} 좌표는 정수여야 합니다.")
    return int(value[0]), int(value[1])


def _load_coordinates(window) -> tuple[tuple[int, int], dict[str, tuple[int, int]]]:
    """추측 없는 실제 팝업·버튼 좌표 입력을 모두 확인합니다."""
    raw_open_at = os.environ.get("PP_LAYER_SELECTION_PANEL_OPEN_AT", "")
    raw_buttons = os.environ.get("PP_LAYER_SELECTION_BUTTON_COORDS", "")
    if not raw_open_at or not raw_buttons:
        raise _PreconditionError(
            "BUTTON_COORDINATES_REQUIRED: UILayout.operator의 공개 좌표 API가 없어 "
            "PP_LAYER_SELECTION_PANEL_OPEN_AT 및 PP_LAYER_SELECTION_BUTTON_COORDS가 필요합니다."
        )
    try:
        open_at = _parse_point(json.loads(raw_open_at), "패널 시작")
        values = json.loads(raw_buttons)
    except json.JSONDecodeError as exc:
        raise _PreconditionError(f"좌표 JSON 형식이 올바르지 않습니다: {exc}") from exc
    if not isinstance(values, dict):
        raise _PreconditionError("버튼 좌표는 Layer index 문자열을 키로 하는 JSON 객체여야 합니다.")
    coordinates = {
        str(index): _parse_point(values.get(str(index)), f"Layer {index}")
        for index in range(4)
    }
    for name, point in (("패널 시작", open_at), *coordinates.items()):
        x, y = point
        if not 0 <= x < int(window.width) or not 0 <= y < int(window.height):
            raise _PreconditionError(f"{name} 좌표가 현재 Blender 창 범위를 벗어납니다.")
    return open_at, coordinates


def _installed_panel_module():
    """작업 트리 아닌 설치본 0.3.23의 실제 Layers 패널 모듈만 반환합니다."""
    panel_type = getattr(bpy.types, "PP_PT_layers", None)
    if panel_type is None:
        raise _PreconditionError(
            "INSTALLED_EXTENSION_NOT_ENABLED: 별도 GUI Blender에서 설치된 Extension 0.3.23을 활성화해야 합니다."
        )
    module = importlib.import_module(panel_type.__module__)
    module_path = Path(inspect.getfile(module)).resolve()
    try:
        module_path.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise _PreconditionError("WORKSPACE_SOURCE_LOADED: 설치본이 아니라 작업 트리 Extension이 활성화되었습니다.")
    manifest = next(
        (
            candidate
            for parent in module_path.parents
            if (candidate := parent / "blender_manifest.toml").is_file()
        ),
        None,
    )
    if manifest is None:
        raise _PreconditionError("INSTALLED_MANIFEST_MISSING: 설치본 manifest를 찾지 못했습니다.")
    version = str(tomllib.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    if version != EXPECTED_VERSION:
        raise _PreconditionError(f"INSTALLED_VERSION_MISMATCH: 설치본은 {version}입니다.")
    return module


def _make_test_scene(panel_module):
    """저장되지 않는 별도 Scene에 Image가 있는 일반 Layer 네 개를 만듭니다."""
    package = panel_module.__name__.rsplit(".blender.panels", 1)[0]
    layer_shader = importlib.import_module(f"{package}.blender.layer_shader")
    material_setup = importlib.import_module(f"{package}.blender.material_setup")
    paint_state = importlib.import_module(f"{package}.blender.paint_state")
    scene = bpy.data.scenes.new("PP_GUI_LayerSelectionActual")
    bpy.context.window.scene = scene
    mesh = bpy.data.meshes.new("PP_GUI_LayerSelectionMesh")
    mesh.from_pydata(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("PP_GUI_LayerSelectionTarget", mesh)
    scene.collection.objects.link(target)
    material = bpy.data.materials.new("PP_GUI_LayerSelectionMaterial")
    material.use_nodes = True
    target.data.materials.append(material)
    setup = material_setup.prepare_active_material(
        target,
        64,
        32,
        use_base_resolution=False,
    )
    paint_state.commit_paint_setup(scene, setup)
    layer_shader.reconcile_layer_graph(
        scene,
        material=setup.material,
        overlay=setup.overlay,
    )
    setup.commit()
    settings = scene.projection_paint
    for index in range(1, 4):
        layer_shader.add_layer_transaction(
            bpy.context,
            f"GUI Layer {index}",
            f"gui-layer-selection-{index}",
        )
    if len(settings.layers) != 4 or any(layer.image is None for layer in settings.layers):
        raise RuntimeError("GUI 회귀 테스트용 일반 Paint Layer 네 개를 준비하지 못했습니다.")
    for index, layer in enumerate(settings.layers):
        layer.name = f"GUI Layer {index}"
        layer.ui_selected = False
    layer_shader.activate_layer_paint_image(
        bpy.context,
        str(settings.layers[3].layer_uuid),
    )
    _STATE["settings"] = settings
    _STATE["layers"] = tuple(settings.layers)
    _STATE["panel_module"] = panel_module
    _record_check("독립 Scene과 Paint Image 네 개 준비")


def _assert_panel_contract() -> None:
    """선택 배경과 별개인 고정폭 실제 활성 기호 열을 확인합니다."""
    panel_module = _STATE["panel_module"]
    assert panel_module is not None
    source = inspect.getsource(panel_module.PP_PT_layers.draw)
    if "_layer_button_text" in source or "✓" in source:
        raise AssertionError("Layer 이름 버튼에 활성 체크 문자열이 남아 있습니다.")
    if "text=str(layer.name)" not in source:
        raise AssertionError("Layer 이름 버튼이 저장된 이름을 그대로 표시하지 않습니다.")
    if 'parent.label(text="", icon="CHECKMARK")' in source:
        raise AssertionError("별도 CHECKMARK UI 열이 남아 있습니다.")
    if '"projection_paint.layer_select"' not in source:
        raise AssertionError("실제 PP_PT_layers에 layer_select UILayout.operator가 없습니다.")
    marker_source = inspect.getsource(panel_module._draw_active_tree_marker)
    if '"RADIOBUT_ON"' not in marker_source or '"BLANK1"' not in marker_source:
        raise AssertionError("실제 활성·비활성 행의 고정폭 기호가 없습니다.")
    if "ui_units_x = 1.0" not in marker_source:
        raise AssertionError("실제 활성 기호 열의 고정폭이 보장되지 않습니다.")
    _record_check("선택과 별개인 고정폭 실제 활성 기호")


def _selection() -> tuple[int, ...]:
    """현재 선택된 Layer index만 불변 순서로 반환합니다."""
    layers = _STATE["layers"]
    assert isinstance(layers, tuple)
    return tuple(index for index, layer in enumerate(layers) if bool(layer.ui_selected))


def _assert_selection(expected: tuple[int, ...], name: str) -> None:
    """실제 UI 입력 뒤 ui_selected 선택 행렬을 검증합니다."""
    actual = _selection()
    if actual != expected:
        raise AssertionError(f"{name}: 선택 결과 {actual} != 기대값 {expected}")
    _record_check(name)


def _assert_active(index: int, name: str) -> None:
    """실제 활성 Layer index와 Paint Image Pointer를 함께 검증합니다."""
    settings = _STATE["settings"]
    layers = _STATE["layers"]
    assert settings is not None and isinstance(layers, tuple)
    if int(settings.active_layer_index) != index:
        raise AssertionError(f"{name}: active_layer_index가 {index}가 아닙니다.")
    if settings.active_paint_image.as_pointer() != layers[index].image.as_pointer():
        raise AssertionError(f"{name}: active_paint_image가 선택 Layer Image와 다릅니다.")
    _record_check(name)


def _queue(action, delay: float = 0.07) -> None:
    """UI Event Loop가 각 입력을 처리할 시간을 둔 검증 동작을 추가합니다."""
    actions = _STATE["actions"]
    assert isinstance(actions, list)
    actions.append((float(delay), action))


def _double_click_input_delay() -> float:
    """팝업 재개방 뒤에도 사용자 더블클릭 시간 안에 넣을 짧은 입력 간격을 반환합니다."""
    interval = int(bpy.context.preferences.inputs.mouse_double_click_time) / 1000.0
    if interval < 0.1:
        raise _PreconditionError(
            "DOUBLE_CLICK_INTERVAL_TOO_SHORT: 100ms 미만 환경에서는 팝업 재개방 뒤 실제 더블클릭을 신뢰성 있게 검증할 수 없습니다."
        )
    return min(0.02, interval / 12.0)


def _queue_click(
    index: int,
    *,
    ctrl: bool = False,
    shift: bool = False,
    double_click_part: str = "",
) -> None:
    """계측된 동일 버튼 좌표로 실제 LEFTMOUSE PRESS·RELEASE를 추가합니다."""
    window = bpy.context.window
    coordinates = _STATE["coordinates"]
    assert isinstance(coordinates, dict)
    x, y = coordinates[str(index)]
    input_delay = _double_click_input_delay() if double_click_part else 0.04
    release_delay = input_delay if double_click_part else 0.12

    def emit(event_type: str, value: str) -> None:
        """Window.event_simulate에 실제 modifier와 같은 좌표를 전달합니다."""
        window.event_simulate(
            type=event_type,
            value=value,
            x=x,
            y=y,
            ctrl=ctrl,
            shift=shift,
        )

    def release() -> None:
        """두 번째 더블클릭 입력 전 실제 주입 시간을 사용자 설정과 대조합니다."""
        emit("LEFTMOUSE", "RELEASE")
        if double_click_part == "FIRST":
            _STATE["double_click_first_release_at"] = time.monotonic()
        elif double_click_part == "SECOND":
            first_release_at = float(_STATE.get("double_click_first_release_at", 0.0))
            elapsed = time.monotonic() - first_release_at
            allowed = int(bpy.context.preferences.inputs.mouse_double_click_time) / 1000.0
            if first_release_at <= 0.0 or elapsed > allowed:
                raise AssertionError("팝업 재개방 뒤 더블클릭 입력 시간이 허용 시간을 넘었습니다.")

    _queue(_open_real_panel, input_delay)
    _queue(lambda: emit("MOUSEMOVE", "NOTHING"), input_delay)
    _queue(lambda: emit("LEFTMOUSE", "PRESS"), input_delay)
    _queue(release, release_delay)


def _open_real_panel() -> None:
    """실제 PP_PT_layers UILayout.operator 버튼을 팝업으로 표시합니다."""
    window = bpy.context.window
    open_at = _STATE["open_at"]
    assert isinstance(open_at, tuple)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=open_at[0], y=open_at[1])
    area = next(candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D")
    region = next(candidate for candidate in area.regions if candidate.type == "WINDOW")
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.wm.call_panel(
            "INVOKE_DEFAULT",
            name="PP_PT_layers",
            keep_open=True,
        )
    if result not in ({"INTERFACE"}, {"FINISHED"}):
        raise RuntimeError(f"실제 PP_PT_layers 팝업을 열지 못했습니다: {result}")
    if not bool(_STATE.get("panel_opened", False)):
        _STATE["panel_opened"] = True
        _record_check("실제 PP_PT_layers UILayout.operator 팝업")


def _prepare_actions() -> None:
    """첫 클릭 선택·수정키 선택·두 번째 클릭 활성화의 실제 검증 순서를 구성합니다."""
    _queue_click(0)
    _queue(lambda: _assert_selection((0,), "일반 클릭은 단일 선택"))
    _queue(lambda: _assert_active(3, "첫 일반 클릭은 기존 활성 Paint 유지"))
    _queue_click(1)
    _queue(lambda: _assert_selection((1,), "다른 Layer 일반 클릭은 기존 선택 해제"))
    _queue(lambda: _assert_active(3, "다른 Layer 첫 클릭도 기존 활성 Paint 유지"))
    _queue_click(0, ctrl=True)
    _queue(lambda: _assert_selection((0, 1), "Ctrl 클릭은 후보 추가"))
    _queue_click(0, ctrl=True)
    _queue(lambda: _assert_selection((1,), "Ctrl 클릭은 후보 토글 해제"))
    _queue_click(3)
    _queue_click(1, shift=True)
    _queue(lambda: _assert_selection((1, 2, 3), "Shift 클릭은 앵커 범위 교체"))
    _queue_click(0)
    _queue_click(3, ctrl=True)
    _queue_click(1, ctrl=True, shift=True)
    _queue(lambda: _assert_selection((0, 1, 2, 3), "Ctrl+Shift 클릭은 앵커 범위 추가"))
    _queue(lambda: _assert_active(3, "수정키 연속 클릭은 활성화하지 않음"))
    _queue_click(1, double_click_part="FIRST")
    _queue(lambda: _assert_selection((1,), "더블클릭 첫 입력은 즉시 단일 선택"))
    _queue(lambda: _assert_active(3, "더블클릭 첫 입력은 활성 Paint 유지"))
    _queue_click(1, double_click_part="SECOND")
    _queue(lambda: _assert_selection((1,), "두 번째 클릭도 단일 선택 유지"))
    _queue(lambda: _assert_active(1, "허용 시간 안의 같은 두 번째 클릭은 활성화"))


def _advance() -> float | None:
    """한 번에 하나의 실제 UI 입력 또는 상태 검증만 실행합니다."""
    try:
        actions = _STATE["actions"]
        assert isinstance(actions, list)
        if not actions:
            _finish(True)
            return None
        delay, action = actions.pop(0)
        action()
        return float(delay)
    except Exception as exc:
        _finish(False, code="ASSERTION_OR_UI_ERROR", error=str(exc))
        return None


def _start() -> float | None:
    """설치본·event_simulate·추적기·좌표를 확인한 뒤 실제 패널 검증을 시작합니다."""
    try:
        if not bool(getattr(bpy.app, "use_event_simulate", False)):
            raise _PreconditionError("EVENT_SIMULATE_DISABLED: --enable-event-simulate로 실행해야 합니다.")
        if not hasattr(bpy.context.window, "event_simulate"):
            raise _PreconditionError("EVENT_SIMULATE_UNAVAILABLE: Window.event_simulate API가 없습니다.")
        panel_module = _installed_panel_module()
        package = panel_module.__name__.rsplit(".blender.panels", 1)[0]
        layers_module = importlib.import_module(f"{package}.blender.layers")
        layers_module.layer_ui_event_tracker_heartbeat()
        window_key = int(bpy.context.window.as_pointer())
        if window_key not in layers_module._layer_ui_tracked_windows:
            _STATE["retry_count"] = int(_STATE.get("retry_count", 0)) + 1
            if int(_STATE["retry_count"]) <= 10:
                return 0.1
            raise _PreconditionError("POINTER_TRACKER_UNAVAILABLE: 실제 UI modifier 추적기가 시작되지 않았습니다.")
        open_at, coordinates = _load_coordinates(bpy.context.window)
        _STATE["open_at"] = open_at
        _STATE["coordinates"] = coordinates
        _make_test_scene(panel_module)
        _assert_panel_contract()
        _prepare_actions()
        bpy.app.timers.register(_advance, first_interval=0.35)
    except _PreconditionError as exc:
        code, _, message = str(exc).partition(": ")
        _finish(False, code=code, error=message or code)
    except Exception as exc:
        _finish(False, code="SETUP_ERROR", error=str(exc))
    return None


bpy.app.timers.register(_start, first_interval=0.5)
