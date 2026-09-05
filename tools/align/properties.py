"""Scene 표시 설정과 N 패널 목록 UI 미러 속성을 정의합니다."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)

from .display_axes import (
    AXIS_ITEMS,
    AXIS_PRESET_ITEMS,
    CUSTOM_AXIS_PRESET,
    DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS,
    DEFAULT_UP_AXIS,
    axes_for_preset,
    ensure_distinct_axis_pair,
)
from .viewport.display_sampling import DEFAULT_DISPLAY_POINT_LIMIT


_EXTERNAL_RIGID_BACKENDS = frozenset(
    {"OPEN3D_GICP", "SMALL_GICP", "OPEN3D_ROBUST_POINT_TO_PLANE"}
)
_CC_MAX_CPU_THREADS = 1024
from .distribution_profile import (
    default_icp_backend,
    icp_backend_enum_items,
    includes_optional_backends,
)


def _request_viewport_redraw(_self, _context) -> None:
    """표시 설정 변경 뒤 모든 A/P 뷰포트를 다시 그립니다."""
    from .viewport.renderer import request_redraw

    request_redraw()


def _on_import_axis_preset_changed(self, _context) -> None:
    """고정 축 프리셋을 선택하면 해당 Forward·Up 축을 함께 동기화합니다."""
    fixed_axes = axes_for_preset(self.import_axis_preset)
    if fixed_axes is not None:
        self.import_axis_forward, self.import_axis_up = fixed_axes


def _on_import_axis_forward_changed(self, _context) -> None:
    """Custom Forward 축 변경 시 Up 축과 같은 기준 축이 되지 않게 보정합니다."""
    if self.import_axis_preset != CUSTOM_AXIS_PRESET:
        return
    forward, up = ensure_distinct_axis_pair(
        self.import_axis_forward,
        self.import_axis_up,
        "FORWARD",
    )
    if up != self.import_axis_up:
        self.import_axis_up = up
    if forward != self.import_axis_forward:
        self.import_axis_forward = forward


def _on_import_axis_up_changed(self, _context) -> None:
    """Custom Up 축 변경 시 Forward 축과 같은 기준 축이 되지 않게 보정합니다."""
    if self.import_axis_preset != CUSTOM_AXIS_PRESET:
        return
    forward, up = ensure_distinct_axis_pair(
        self.import_axis_forward,
        self.import_axis_up,
        "UP",
    )
    if forward != self.import_axis_forward:
        self.import_axis_forward = forward
    if up != self.import_axis_up:
        self.import_axis_up = up


def _on_obj_axis_preset_changed(self, _context) -> None:
    """OBJ 축 프리셋을 선택하면 해당 Forward·Up 축을 함께 동기화합니다."""
    fixed_axes = axes_for_preset(self.obj_axis_preset)
    if fixed_axes is not None:
        self.obj_axis_forward, self.obj_axis_up = fixed_axes


def _on_obj_axis_forward_changed(self, _context) -> None:
    """OBJ Custom Forward 축 변경 시 Up 축과 같은 기준 축이 되지 않게 보정합니다."""
    if self.obj_axis_preset != CUSTOM_AXIS_PRESET:
        return
    forward, up = ensure_distinct_axis_pair(
        self.obj_axis_forward,
        self.obj_axis_up,
        "FORWARD",
    )
    if up != self.obj_axis_up:
        self.obj_axis_up = up
    if forward != self.obj_axis_forward:
        self.obj_axis_forward = forward


def _on_obj_axis_up_changed(self, _context) -> None:
    """OBJ Custom Up 축 변경 시 Forward 축과 같은 기준 축이 되지 않게 보정합니다."""
    if self.obj_axis_preset != CUSTOM_AXIS_PRESET:
        return
    forward, up = ensure_distinct_axis_pair(
        self.obj_axis_forward,
        self.obj_axis_up,
        "UP",
    )
    if forward != self.obj_axis_forward:
        self.obj_axis_forward = forward
    if up != self.obj_axis_up:
        self.obj_axis_up = up


def _on_active_pair_index_changed(self, _context) -> None:
    """UIList 인덱스 변경을 안정 pair_id 선택 상태로 변환합니다."""
    from .state import RUNTIME
    from .viewport.renderer import request_redraw

    if RUNTIME.ui_list_syncing:
        return
    pair_id = None
    if 0 <= self.active_index < len(self.items):
        pair_id = self.items[self.active_index].pair_id
    RUNTIME.marker_selection.set_active_pair_id(pair_id)
    request_redraw()


def _on_icp_solver_backend_changed(self, _context) -> None:
    """외부 강체 ICP backend에서는 지원하지 않는 Scale Refine을 해제합니다."""

    if str(self.icp_solver_backend) in _EXTERNAL_RIGID_BACKENDS:
        self.icp_scale_refine_enabled = False


class COLMAPALIGN_AP_preferences(bpy.types.AddonPreferences):
    """개발 프로필의 명시적 실행 경로와 CC 환경 검사를 제공합니다."""

    bl_idname = __package__.split(".tools.", 1)[0]

    cc_cpu_thread_limit: IntProperty(
        name="CPU 스레드",
        description="CC 정렬 작업의 최대 CPU 스레드 수입니다. 0은 자동, 1은 단일 스레드, 2 이상은 최대값입니다.",
        default=0,
        min=0,
        max=_CC_MAX_CPU_THREADS,
    )

    if includes_optional_backends():
        open3d_python_path: StringProperty(
            name="정렬 Python 실행 파일",
            description="개발 환경에서 CC 계산 조정기를 실행할 CPython 3.12 x64 경로입니다. 비워 두면 전용 런타임을 사용합니다.",
            subtype="FILE_PATH",
        )

    def draw(self, _context) -> None:
        """배포 프로필에 맞는 CC 전용 실행 경로와 실제 엔진 검사를 표시합니다."""

        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        if includes_optional_backends():
            layout.prop(self, "open3d_python_path")
            layout.label(text="경로를 비워 두면 애드온의 전용 정렬 환경을 사용합니다.")
        else:
            layout.label(text="CC 정렬 실행 환경이 애드온에 포함되어 있습니다.", icon="PACKAGE")
        layout.prop(self, "cc_cpu_thread_limit")
        layout.label(text=cc_cpu_thread_summary(_context))
        layout.operator(
            "colmap_align.validate_cccore_environment",
            text="CC 정렬 환경 검사",
            icon="CHECKMARK",
        )


def addon_preferences(context: Any | None = None) -> Any | None:
    """현재 Blender 설정에서 COLMAP Align 애드온 환경설정을 반환합니다."""

    current_context = context or getattr(bpy, "context", None)
    preferences = getattr(current_context, "preferences", None)
    addons = getattr(preferences, "addons", None)
    if addons is None:
        return None
    addon = addons.get(COLMAPALIGN_AP_preferences.bl_idname)
    return getattr(addon, "preferences", None) if addon is not None else None


def cc_cpu_thread_counts(
    configured_limit: int,
    detected_thread_count: int | None = None,
) -> tuple[int, int, int]:
    """감지 수와 자동 상한 및 현재 설정을 해석한 작업 상한을 반환합니다."""
    if isinstance(configured_limit, bool) or not isinstance(configured_limit, int):
        raise ValueError("CC CPU 스레드 설정은 정수여야 합니다.")
    if not 0 <= configured_limit <= _CC_MAX_CPU_THREADS:
        raise ValueError(f"CC CPU 스레드 설정은 0 이상 {_CC_MAX_CPU_THREADS} 이하여야 합니다.")
    detected_value = os.cpu_count() if detected_thread_count is None else detected_thread_count
    if isinstance(detected_value, bool) or not isinstance(detected_value, int) or detected_value < 1:
        detected_value = 1
    detected = max(1, detected_value)
    automatic = min(_CC_MAX_CPU_THREADS, max(1, detected - 1))
    selected = automatic if configured_limit == 0 else max(1, min(configured_limit, detected, _CC_MAX_CPU_THREADS))
    return detected, automatic, selected


def configured_cc_cpu_thread_limit(context: Any | None = None) -> int:
    """현재 Add-on Preferences 값을 감지 논리 프로세서 수로 제한해 반환합니다."""
    preferences = addon_preferences(context)
    configured = getattr(preferences, "cc_cpu_thread_limit", 0) if preferences is not None else 0
    return cc_cpu_thread_counts(int(configured))[2]


def cc_cpu_thread_summary(context: Any | None = None) -> str:
    """환경설정에 표시할 감지 수와 다음 작업의 고정 상한 설명을 반환합니다."""
    preferences = addon_preferences(context)
    configured = int(getattr(preferences, "cc_cpu_thread_limit", 0)) if preferences is not None else 0
    detected, automatic, selected = cc_cpu_thread_counts(configured)
    mode = "자동" if configured == 0 else "수동"
    return f"감지 {detected}개 · {mode} {selected}개 · 다음 작업부터 적용"


def bundled_small_gicp_python_path() -> str:
    """설치된 애드온 내부 Small GICP CPython 실행 파일의 상대 경로를 반환합니다."""

    return str(Path(__file__).resolve().parent / "runtime" / "python" / "python.exe")


def bundled_cccore_python_path() -> str:
    """설치 패키지 또는 명시된 실험 소스의 전용 Python 실행 파일만 찾습니다."""
    align_root = Path(__file__).resolve().parent
    bundled = align_root / "runtime" / "python" / "python.exe"
    if bundled.is_file():
        return str(bundled)
    source_root = align_root.parent.parent
    if (source_root / "test_env" / "manage.py").is_file():
        development = source_root.parent / "runtime" / "python" / "python.exe"
        if development.is_file():
            return str(development)
    return ""


def configured_icp_python_path(context: Any | None = None) -> str:
    """개발 프로필의 명시적 경로를 우선하고 그 외에는 CC 전용 런타임을 반환합니다."""
    if includes_optional_backends():
        preferences = addon_preferences(context)
        configured = str(getattr(preferences, "open3d_python_path", "")).strip()
        if configured:
            return configured
    return bundled_cccore_python_path()


class COLMAPALIGN_PG_settings(bpy.types.PropertyGroup):
    """점군 표시와 레이어 변환 기즈모 설정을 Scene 단위로 저장합니다."""

    project_state_revision: IntProperty(
        name="Project State Revision",
        description="확정 COLMAP Align 상태 변경을 Blender 파일 변경으로 알리는 내부 revision입니다.",
        default=0,
        min=0,
        options={"HIDDEN"},
    )

    import_dataset_root: StringProperty(
        name="Dataset Root",
        description="표준 sparse/0 또는 단일 sparse model을 탐색할 COLMAP Dataset Root입니다",
        subtype="DIR_PATH",
        options={"HIDDEN"},
    )
    import_last_directory: StringProperty(
        name="Last COLMAP Directory",
        description="다음 COLMAP 파일 탐색기를 열 때 사용할 마지막 선택 경로입니다.",
        subtype="DIR_PATH",
        options={"HIDDEN"},
    )
    import_companion_obj: BoolProperty(
        name="OBJ 함께 불러오기",
        description="다음 COLMAP 가져오기에서 루트 OBJ를 함께 연결할지 결정합니다",
        default=True,
    )
    import_input_format: EnumProperty(
        name="Input Format",
        description="COLMAP 입력 형식을 자동 감지하거나 명시적으로 선택합니다",
        items=(
            ("AUTO", "Auto", "완전한 BIN이 있으면 우선하고 없으면 TXT를 사용합니다"),
            ("BINARY", "Binary", "cameras.bin, images.bin, points3D.bin을 사용합니다"),
            ("TEXT", "Text", "cameras.txt, images.txt, points3D.txt를 사용합니다"),
        ),
        default="AUTO",
    )
    import_axis_preset: EnumProperty(
        name="Display Axis",
        description="COLMAP 원본 데이터에는 손대지 않고 표시할 좌표 축만 선택합니다.",
        items=AXIS_PRESET_ITEMS,
        default=DEFAULT_AXIS_PRESET,
        update=_on_import_axis_preset_changed,
    )
    import_axis_forward: EnumProperty(
        name="Forward",
        description="Custom Display Axis의 원본 Forward 축입니다.",
        items=AXIS_ITEMS,
        default=DEFAULT_FORWARD_AXIS,
        update=_on_import_axis_forward_changed,
    )
    import_axis_up: EnumProperty(
        name="Up",
        description="Custom Display Axis의 원본 Up 축입니다.",
        items=AXIS_ITEMS,
        default=DEFAULT_UP_AXIS,
        update=_on_import_axis_up_changed,
    )
    import_display_point_limit: IntProperty(
        name="Last Viewport Point Limit",
        description="다음 COLMAP 가져오기에 사용할 마지막 확정 표시 점 상한입니다.",
        default=DEFAULT_DISPLAY_POINT_LIMIT,
        min=1,
        max=2_147_483_647,
        options={"HIDDEN"},
    )
    obj_last_file: StringProperty(
        name="Last OBJ File",
        description="다음 OBJ 파일 탐색기의 마지막 선택 파일입니다.",
        subtype="FILE_PATH",
        options={"HIDDEN"},
    )
    obj_axis_preset: EnumProperty(
        name="OBJ Display Axis",
        description="OBJ 원본에 적용할 마지막 Display Axis preset입니다.",
        items=AXIS_PRESET_ITEMS,
        default=DEFAULT_AXIS_PRESET,
        options={"HIDDEN"},
        update=_on_obj_axis_preset_changed,
    )
    obj_axis_forward: EnumProperty(
        name="OBJ Forward",
        description="OBJ Custom Display Axis의 마지막 Forward 축입니다.",
        items=AXIS_ITEMS,
        default=DEFAULT_FORWARD_AXIS,
        options={"HIDDEN"},
        update=_on_obj_axis_forward_changed,
    )
    obj_axis_up: EnumProperty(
        name="OBJ Up",
        description="OBJ Custom Display Axis의 마지막 Up 축입니다.",
        items=AXIS_ITEMS,
        default=DEFAULT_UP_AXIS,
        options={"HIDDEN"},
        update=_on_obj_axis_up_changed,
    )
    merge_export_output_root: StringProperty(
        name="통합 출력 폴더",
        description="Export 위치 아래의 colmap_merged 경로이며, 기존 폴더는 사용자 확인 뒤 안전하게 교체합니다.",
        subtype="DIR_PATH",
        options={"HIDDEN"},
    )

    point_size: FloatProperty(
        name="포인트 크기",
        description="COLMAP 희소점의 화면상 포인트 크기입니다",
        default=2.0,
        min=1.0,
        max=12.0,
        update=_request_viewport_redraw,
    )
    show_transform_gizmo: BoolProperty(
        name="기즈모 표시",
        description="선택한 COLMAP 데이터 레이어의 이동·회전·스케일 기즈모를 표시합니다",
        default=True,
        update=_request_viewport_redraw,
    )
    use_colmap_rgb: BoolProperty(
        name="COLMAP RGB",
        description="점군은 원본 RGB를, UV와 diffuse 이미지가 있는 OBJ는 원본 텍스처를 표시합니다",
        default=True,
        update=_request_viewport_redraw,
    )
    marker_use_uniform_scale: BoolProperty(
        name="Marker Refine Scale",
        description="3점 이상 마커로 초기 정렬의 균일 스케일을 계산합니다. 자동 CC 정렬의 초기값 대비 최대 ±10% 배율 보정과는 별도입니다",
        default=True,
    )
    icp_preset: EnumProperty(
        name="ICP 품질",
        description="ICP 계산의 해상도와 반복 예산을 선택합니다.",
        items=(
            ("FAST", "빠름", "빠른 검토용 ICP 해상도와 반복 예산을 사용합니다."),
            ("BALANCED", "균형", "일반적인 정렬에 권장하는 균형 ICP 설정입니다."),
            ("PRECISE", "정밀", "더 많은 점과 반복으로 정밀 검사를 수행합니다."),
        ),
        default="BALANCED",
    )
    icp_solver_backend: EnumProperty(
        name="ICP Backend",
        description="마커 미리보기 또는 현재 P 위치에서 시작할 정렬 엔진의 내부 식별자입니다.",
        items=icp_backend_enum_items(),
        default=default_icp_backend(),
        update=_on_icp_solver_backend_changed,
    )
    icp_small_gicp_operation: EnumProperty(
        name="정렬 방식",
        description="Small GICP 강체 정렬 또는 이동·회전·균일 스케일을 함께 보정하는 Refine을 선택합니다.",
        items=(
            (
                "SMALL_GICP",
                "Small GICP",
                "현재 스케일을 유지하고 Small GICP 강체 정렬을 두 번 실행합니다.",
            ),
            (
                "SCALE_REFINE",
                "Refine",
                "Small GICP 이동·회전 정렬 전후에 균일 스케일을 최대 ±5% 보정합니다.",
            ),
        ),
        default="SMALL_GICP",
    )
    icp_scale_refine_enabled: BoolProperty(
        name="Scale Refine",
        description="고정 스케일 ICP 수렴 뒤 마커 스케일 기준 ±5% 안에서만 추가 보정합니다.",
        default=False,
    )


class COLMAPALIGN_PG_marker_pair_item(bpy.types.PropertyGroup):
    """UIList 표시 전용 확정 마커 쌍 요약을 보관합니다."""

    pair_id: IntProperty(options={"HIDDEN"})
    summary: StringProperty(options={"HIDDEN"})


class COLMAPALIGN_PG_marker_pair_list(bpy.types.PropertyGroup):
    """런타임 확정 목록을 보여 주기만 하는 WindowManager UIList 미러입니다."""

    items: CollectionProperty(type=COLMAPALIGN_PG_marker_pair_item)
    active_index: IntProperty(default=-1, update=_on_active_pair_index_changed)


class COLMAPALIGN_PG_marker_align_log_item(bpy.types.PropertyGroup):
    """Marker Align 상태 로그의 한 줄을 보관합니다."""

    message: StringProperty(options={"HIDDEN"})


class COLMAPALIGN_PG_marker_align_log_list(bpy.types.PropertyGroup):
    """현재 Marker Align 단계 메시지를 스크롤 가능한 UIList로 보관합니다."""

    items: CollectionProperty(type=COLMAPALIGN_PG_marker_align_log_item)
    active_index: IntProperty(default=-1)


def marker_pair_list(window_manager: Any | None = None) -> Any | None:
    """지정 또는 현재 WindowManager의 마커 UIList 미러를 반환합니다."""
    manager = window_manager or getattr(bpy.context, "window_manager", None)
    return getattr(manager, "colmap_align_marker_pairs", None) if manager is not None else None


def marker_align_log_list(window_manager: Any | None = None) -> Any | None:
    """지정 또는 현재 WindowManager의 Marker Align 로그 UIList를 반환합니다."""
    manager = window_manager or getattr(bpy.context, "window_manager", None)
    return getattr(manager, "colmap_align_marker_align_log", None) if manager is not None else None


def sync_marker_pair_list(window_manager: Any | None = None) -> None:
    """확정 pair_id 목록을 UIList 미러에 복사하고 활성 인덱스를 맞춥니다."""
    from .state import RUNTIME

    pair_list = marker_pair_list(window_manager)
    if pair_list is None:
        return
    RUNTIME.ui_list_syncing = True
    try:
        pair_list.items.clear()
        active_index = -1
        for index, pair in enumerate(RUNTIME.marker_selection.completed_pairs):
            item = pair_list.items.add()
            item.pair_id = pair.pair_id
            item.summary = f"A{pair.pair_id} ↔ P{pair.pair_id}"
            if pair.pair_id == RUNTIME.marker_selection.active_pair_id:
                active_index = index
        pair_list.active_index = active_index
    finally:
        RUNTIME.ui_list_syncing = False


def sync_marker_align_log(
    entries: tuple[str, ...],
    window_manager: Any | None = None,
) -> None:
    """변경된 Marker Align 로그 줄만 UIList에 복사하고 행 선택을 해제합니다."""
    log_list = marker_align_log_list(window_manager)
    if log_list is None:
        return
    normalized_entries = tuple(str(message).strip() for message in entries if str(message).strip())
    current_entries = tuple(str(item.message) for item in log_list.items)
    if current_entries == normalized_entries:
        return
    log_list.items.clear()
    for message in normalized_entries:
        item = log_list.items.add()
        item.message = message
    log_list.active_index = -1


CLASSES = (
    COLMAPALIGN_PG_settings,
    COLMAPALIGN_PG_marker_pair_item,
    COLMAPALIGN_PG_marker_pair_list,
    COLMAPALIGN_PG_marker_align_log_item,
    COLMAPALIGN_PG_marker_align_log_list,
)
