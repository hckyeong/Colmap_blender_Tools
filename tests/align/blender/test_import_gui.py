"""GUI Blender에서 실제 COLMAP 가져오기와 취소의 Runtime 경계를 검증합니다."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy


_POINT_COUNT = 4096
_DISPLAY_POINT_LIMIT = 1024
_STAGE = "PREPARE"
_TEMPORARY_DIRECTORY: TemporaryDirectory[str] | None = None
_FIRST_MODEL_PATH: Path | None = None
_SECOND_MODEL_PATH: Path | None = None
_SCENE_DATA_SNAPSHOT: tuple[frozenset[int], frozenset[int], frozenset[int]] | None = None
_PROGRESS_SAMPLES: list[tuple[str, float, int, int]] = []
_CANCELLED_JOB = None
_CANCELLED_READER = None
_WAIT_COUNT = 0
_POPUP_CALLS: list[tuple[str, str, tuple[str, ...]]] = []
_ORIGINAL_IMPORT_POPUP = None
_ORIGINAL_IMPORT_READ_MAX_RECORDS: int | None = None
_ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS: int | None = None
_ADDON_MODE = "SOURCE"
_STATUS_VIEWS = []


def _script_arguments() -> tuple[str, ...]:
    """Blender의 `--` 뒤에 전달된 GUI 테스트 인수를 반환합니다."""
    if "--" not in sys.argv:
        return ()
    return tuple(sys.argv[sys.argv.index("--") + 1 :])


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _write_text_sparse_model(directory: Path, point_count: int) -> None:
    """유효한 points2D↔points3D 트랙을 가진 실제 text sparse model을 생성합니다."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.txt").write_text(
        "# Camera list\n1 SIMPLE_PINHOLE 640 480 500 320 240\n",
        encoding="utf-8",
    )
    point_ids = [100_000 + index for index in range(point_count)]
    point2d_tokens = " ".join(
        f"{index % 640}.0 {index % 480}.0 {point_id}"
        for index, point_id in enumerate(point_ids)
    )
    (directory / "images.txt").write_text(
        "# Image list\n"
        "1 1 0 0 0 0 0 0 1 import_gui.jpg\n"
        f"{point2d_tokens}\n",
        encoding="utf-8",
    )
    point_lines = "\n".join(
        (
            f"{point_id} {index % 97}.0 {index % 53}.0 {index % 29}.0 "
            f"{index % 256} {(index * 3) % 256} {(index * 7) % 256} 0.1 1 {index}"
        )
        for index, point_id in enumerate(point_ids)
    )
    (directory / "points3D.txt").write_text(
        f"# 3D point list\n{point_lines}\n",
        encoding="utf-8",
    )


def _scene_data_snapshot() -> tuple[frozenset[int], frozenset[int], frozenset[int]]:
    """COLMAP 입력이 Blender 데이터블록을 만들지 않는지 확인할 포인터 집합을 기록합니다."""
    return (
        frozenset(item.as_pointer() for item in bpy.data.objects),
        frozenset(item.as_pointer() for item in bpy.data.collections),
        frozenset(item.as_pointer() for item in bpy.data.cameras),
    )


def _main_window() -> bpy.types.Window:
    """현재 COLMAP Align 세션이 소유한 메인 Window를 반환합니다."""
    from tools.align.operators.workspace import find_main_window

    window = find_main_window(bpy.context.window_manager)
    assert window is not None, "COLMAP Align 메인 Window를 찾지 못했습니다."
    return window


def _operator_override(window: bpy.types.Window) -> dict[str, object]:
    """가져오기 모달 오퍼레이터를 실행할 활성 VIEW_3D WINDOW override를 만듭니다."""
    area = next((item for item in window.screen.areas if item.type == "VIEW_3D"), None)
    assert area is not None, "COLMAP 가져오기 오퍼레이터용 VIEW_3D Area가 없습니다."
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    assert region is not None, "COLMAP 가져오기 오퍼레이터용 WINDOW Region이 없습니다."
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
    }


def _record_progress() -> None:
    """실행 중 ImportJob의 UI 진행률 경계와 카운터 불변조건을 기록합니다."""
    from tools.align.state import RUNTIME
    from tools.align.ui.import_status import import_status_view

    job = RUNTIME.import_job
    assert job is not None, "진행률 기록 중 ImportJob이 사라졌습니다."
    assert 0.0 <= job.progress <= 1.0, "가져오기 UI 진행률이 0~1 범위를 벗어났습니다."
    assert job.current_count >= 0 and job.total_count >= 0
    if job.total_count:
        assert job.current_count <= job.total_count
    _PROGRESS_SAMPLES.append(
        (job.stage, job.progress, job.current_count, job.total_count)
    )
    _STATUS_VIEWS.append(import_status_view(job))


def _capture_import_popup(
    context,
    *,
    title: str,
    icon: str,
    lines: tuple[str, ...],
) -> None:
    """실제 모달 오퍼레이터가 요청한 팝업 제목·아이콘·본문을 기록합니다."""
    assert context.window is not None
    _POPUP_CALLS.append((title, icon, tuple(lines)))


def _assert_first_import_result() -> None:
    """상위 폴더의 첫 실제 모델이 실제 하위 경로로 원자 등록됐는지 확인합니다."""
    from tools.align.state import RUNTIME

    assert RUNTIME.import_job is None
    assert RUNTIME.import_timer is None
    assert RUNTIME.model_catalog.model_count == 1
    record = RUNTIME.model_catalog.ordered_models()[0]
    assert record.is_anchor is True
    assert record.registration_status == "REGISTERED"
    model = RUNTIME.model_repository.models.get(record.model_id)
    assert model is not None, "첫 Data Layers 항목에 실제 저장소 모델이 없습니다."
    assert model.points.count == _POINT_COUNT
    assert model.points.display_indices.size == _DISPLAY_POINT_LIMIT
    assert model.display_name == "first_workspace"
    assert _FIRST_MODEL_PATH is not None
    assert model.source_path == (_FIRST_MODEL_PATH / "sparse" / "0").resolve()
    transform = RUNTIME.model_repository.display_transforms.get(record.model_id)
    assert transform is not None
    assert transform.preset_id == "MAYA"
    assert (transform.forward_axis, transform.up_axis) == ("-Z", "Y")
    assert tuple(float(value) for value in model.points.positions[0]) == (0.0, 0.0, 0.0)
    assert tuple(float(value) for value in model.images.image_records[1].cam_from_world) == (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    assert _SCENE_DATA_SNAPSHOT is not None
    assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT
    assert len(_POPUP_CALLS) == 1
    title, icon, lines = _POPUP_CALLS[0]
    assert title == "COLMAP Import Complete"
    assert icon == "CHECKMARK"
    assert lines[:3] == (
        "Camera: 1",
        "Image: 1",
        f"Point3D: {_POINT_COUNT:,}",
    )
    assert lines[3] == f"Viewport Points: {_DISPLAY_POINT_LIMIT:,} / {_POINT_COUNT:,}"
    assert lines[4] == "Sampling: Deterministic Random"
    assert lines[5] == "Display Axis: Maya (-Z / Y)"
    assert lines[6].startswith("Elapsed: ") and lines[6].endswith("s")
    assert _STATUS_VIEWS, "가져오기 중 N 패널 진행 정보가 한 번도 생성되지 않았습니다."
    assert any(view.stage_text == "COLMAP 데이터 읽기" for view in _STATUS_VIEWS)
    assert any(
        view.stage_text == "뷰포트 표시 점 선택"
        and view.detail_stage_text == f"표시 상한: 모델당 {_DISPLAY_POINT_LIMIT:,}"
        for view in _STATUS_VIEWS
    )
    assert any(view.detail_stage_text for view in _STATUS_VIEWS)
    assert all(view.axis_text == "Maya (-Z / Y)" for view in _STATUS_VIEWS)
    assert all(view.dataset_text for view in _STATUS_VIEWS)
    status_progress = [view.fraction for view in _STATUS_VIEWS]
    assert all(
        previous <= current
        for previous, current in zip(status_progress, status_progress[1:])
    )


def _assert_first_import_view_fits_model() -> None:
    """가져온 Anchor의 전체 유효 바운드가 A 뷰 안에서 충분히 크게 보이는지 확인합니다."""

    from itertools import product

    from bpy_extras import view3d_utils

    from tools.align.constants import ROLE_REFERENCE
    from tools.align.operators.workspace import _bounds_for_model_ids
    from tools.align.state import RUNTIME
    from tools.align.viewport.registry import resolve_area

    window = _main_window()
    record = RUNTIME.model_catalog.ordered_models()[0]
    bounds = _bounds_for_model_ids([record.model_id])
    assert bounds is not None
    minimum, maximum = bounds
    area = resolve_area(window, ROLE_REFERENCE)
    assert area is not None
    region = next(item for item in area.regions if item.type == "WINDOW")
    region_3d = area.spaces.active.region_3d
    corners = tuple(
        (
            minimum.x if x == 0 else maximum.x,
            minimum.y if y == 0 else maximum.y,
            minimum.z if z == 0 else maximum.z,
        )
        for x, y, z in product((0, 1), repeat=3)
    )
    projected = tuple(
        view3d_utils.location_3d_to_region_2d(region, region_3d, corner)
        for corner in corners
    )
    assert all(point is not None for point in projected), (
        f"가져오기 화면 맞춤 투영 실패: distance={region_3d.view_distance}, "
        f"location={tuple(region_3d.view_location)}, region={region.width}x{region.height}, "
        f"projected={projected}"
    )
    xs = tuple(float(point.x) for point in projected if point is not None)
    ys = tuple(float(point.y) for point in projected if point is not None)
    margin = 2.0
    assert min(xs) >= margin and max(xs) <= float(region.width) - margin
    assert min(ys) >= margin and max(ys) <= float(region.height) - margin
    coverage = max(
        (max(xs) - min(xs)) / max(float(region.width), 1.0),
        (max(ys) - min(ys)) / max(float(region.height), 1.0),
    )
    assert coverage >= 0.35


def _assert_cancel_result() -> None:
    """두 번째 가져오기 취소가 reader·Runtime·Blender 데이터블록을 모두 안전하게 정리했는지 확인합니다."""
    from tools.align.importer import IMPORT_STAGE_CANCELLED
    from tools.align.state import RUNTIME

    assert _CANCELLED_JOB is not None, "취소한 ImportJob 참조를 기록하지 못했습니다."
    assert _CANCELLED_JOB.stage == IMPORT_STAGE_CANCELLED
    assert _CANCELLED_READER is not None, "취소한 증분 reader 참조를 기록하지 못했습니다."
    assert _CANCELLED_READER.progress.is_cancelled is True
    assert _CANCELLED_READER.has_open_file is False
    assert RUNTIME.import_job is None
    assert RUNTIME.import_timer is None
    assert RUNTIME.model_catalog.model_count == 1
    assert len(RUNTIME.model_repository.models) == 1
    assert _SCENE_DATA_SNAPSHOT is not None
    assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT


def _finish(succeeded: bool) -> None:
    """테스트 리소스를 정리하고 결과 마커를 출력한 뒤 Blender를 종료합니다."""
    global _TEMPORARY_DIRECTORY, _ORIGINAL_IMPORT_POPUP
    global _ORIGINAL_IMPORT_READ_MAX_RECORDS, _ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS
    try:
        from tools.align import registration
        from tools.align import importer
        from tools.align.operators import import_model as import_model_module

        if _ORIGINAL_IMPORT_POPUP is not None:
            import_model_module._show_import_popup = _ORIGINAL_IMPORT_POPUP
            _ORIGINAL_IMPORT_POPUP = None
        if _ORIGINAL_IMPORT_READ_MAX_RECORDS is not None:
            importer.IMPORT_READ_MAX_RECORDS = _ORIGINAL_IMPORT_READ_MAX_RECORDS
            _ORIGINAL_IMPORT_READ_MAX_RECORDS = None
        if _ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS is not None:
            importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = _ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS
            _ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS = None
        if _ADDON_MODE == "INSTALLED":
            import addon_utils

            _loaded, enabled = addon_utils.check("colmap_align")
            if enabled:
                addon_utils.disable("colmap_align", default_set=False)
        elif registration.is_registered():
            registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    finally:
        if _TEMPORARY_DIRECTORY is not None:
            _TEMPORARY_DIRECTORY.cleanup()
            _TEMPORARY_DIRECTORY = None
        print(f"IMPORT_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
        bpy.ops.wm.quit_blender()


def _run_gui_test_step() -> float | None:
    """작업공간 진입·실제 가져오기·취소를 Blender 이벤트 루프에서 순서대로 검증합니다."""
    global _STAGE, _SCENE_DATA_SNAPSHOT, _CANCELLED_JOB, _CANCELLED_READER, _WAIT_COUNT
    try:
        from tools.align.importer import (
            IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE,
            IMPORT_STAGE_READ_MODEL,
        )
        from tools.align.state import RUNTIME

        if RUNTIME.workspace_setup_task is not None:
            return 0.05
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "PREPARE":
            assert bpy.ops.colmap_align.import_model.poll() is False
            assert bpy.ops.colmap_align.import_model_job.poll() is False
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "WAIT_WORKSPACE"
            return 0.05

        window = _main_window()
        override = _operator_override(window)

        if _STAGE == "WAIT_WORKSPACE":
            from tools.align.operators.workspace import _process_sidebar_activation
            from tools.align.session import is_session_active_for_window

            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.05
            assert is_session_active_for_window(window) is True
            assert RUNTIME.model_catalog.model_count == 0
            assert _FIRST_MODEL_PATH is not None
            with bpy.context.temp_override(**override):
                assert bpy.ops.colmap_align.import_model.poll() is True
                assert bpy.ops.colmap_align.import_model_job.poll() is True
            _SCENE_DATA_SNAPSHOT = _scene_data_snapshot()
            with bpy.context.temp_override(**override):
                result = bpy.ops.colmap_align.import_model(
                    "EXEC_DEFAULT",
                    directory=str(_FIRST_MODEL_PATH),
                    input_format="AUTO",
                    display_point_limit=_DISPLAY_POINT_LIMIT,
                    axis_preset="MAYA",
                    axis_forward="-Z",
                    axis_up="Y",
                )
            assert "RUNNING_MODAL" in result
            settings = window.scene.colmap_align_settings
            assert Path(settings.import_last_directory).resolve() == _FIRST_MODEL_PATH.resolve()
            assert settings.import_display_point_limit == _DISPLAY_POINT_LIMIT
            assert settings.import_axis_preset == "MAYA"
            _STAGE = "WAIT_FIRST_IMPORT"
            _WAIT_COUNT = 0
            return 0.02

        if _STAGE == "WAIT_FIRST_IMPORT":
            if RUNTIME.import_job is not None:
                _record_progress()
                _WAIT_COUNT += 1
                assert _WAIT_COUNT < 1200, "첫 COLMAP 가져오기가 제한 시간 안에 끝나지 않았습니다."
                return 0.02
            _assert_first_import_result()
            assert any(stage == IMPORT_STAGE_READ_MODEL for stage, *_ in _PROGRESS_SAMPLES)
            assert any(
                stage == IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE
                for stage, *_ in _PROGRESS_SAMPLES
            )
            assert len(_PROGRESS_SAMPLES) >= 2
            first_progress_values = [sample[1] for sample in _PROGRESS_SAMPLES]
            assert all(
                previous <= current
                for previous, current in zip(first_progress_values, first_progress_values[1:])
            )
            _STAGE = "WAIT_FIRST_VIEW_FIT"
            return 0.1

        if _STAGE == "WAIT_FIRST_VIEW_FIT":
            _assert_first_import_view_fits_model()
            _STAGE = "START_CANCEL_IMPORT"
            return 0.02

        if _STAGE == "START_CANCEL_IMPORT":
            assert _SECOND_MODEL_PATH is not None
            with bpy.context.temp_override(**override):
                result = bpy.ops.colmap_align.import_model_job(
                    "INVOKE_DEFAULT",
                    source_path=str(_SECOND_MODEL_PATH),
                )
            assert "RUNNING_MODAL" in result
            _STAGE = "WAIT_CANCEL_READER"
            _WAIT_COUNT = 0
            return 0.02

        if _STAGE == "WAIT_CANCEL_READER":
            job = RUNTIME.import_job
            assert job is not None, "취소 전 ImportJob이 예상보다 일찍 끝났습니다."
            _record_progress()
            _WAIT_COUNT += 1
            assert _WAIT_COUNT < 1200, "취소할 COLMAP reader가 제한 시간 안에 시작되지 않았습니다."
            reader = job.reader
            if (
                job.stage != IMPORT_STAGE_READ_MODEL
                or reader is None
                or not reader.has_open_file
            ):
                return 0.02
            _CANCELLED_JOB = job
            _CANCELLED_READER = reader
            assert _CANCELLED_READER is not None, "READ_MODEL 단계에 증분 reader가 없습니다."
            assert _CANCELLED_READER.has_open_file is True
            with bpy.context.temp_override(**override):
                result = bpy.ops.colmap_align.cancel_import_model()
            assert "FINISHED" in result
            _STAGE = "WAIT_CANCELLED"
            return 0.02

        if _STAGE == "WAIT_CANCELLED":
            if RUNTIME.import_job is not None:
                _record_progress()
                return 0.02
            _assert_cancel_result()
            assert len(_POPUP_CALLS) == 1, "사용자 취소에는 완료·실패 팝업이 없어야 합니다."
            assert _SECOND_MODEL_PATH is not None
            with bpy.context.temp_override(**override):
                result = bpy.ops.colmap_align.import_model_job(
                    "INVOKE_DEFAULT",
                    source_path=str(_SECOND_MODEL_PATH / "missing"),
                )
            assert "RUNNING_MODAL" in result
            _STAGE = "WAIT_FAILED_IMPORT"
            return 0.02

        if _STAGE == "WAIT_FAILED_IMPORT":
            if RUNTIME.import_job is not None:
                return 0.02
            assert len(_POPUP_CALLS) == 2
            title, icon, lines = _POPUP_CALLS[1]
            assert title == "COLMAP Import Failed"
            assert icon == "ERROR"
            assert len(lines) == 1 and "데이터 레이어를 추가하지 않았습니다" in lines[0]
            assert RUNTIME.model_catalog.model_count == 1
            assert len(RUNTIME.model_repository.models) == 1
            _finish(True)
            return None

        raise AssertionError(f"알 수 없는 GUI 가져오기 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def _start() -> None:
    """애드온과 두 임시 text sparse model을 준비한 뒤 GUI 테스트 타이머를 시작합니다."""
    global _TEMPORARY_DIRECTORY, _FIRST_MODEL_PATH, _SECOND_MODEL_PATH
    global _ORIGINAL_IMPORT_POPUP, _ORIGINAL_IMPORT_READ_MAX_RECORDS
    global _ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS, _ADDON_MODE

    arguments = _script_arguments()
    _ADDON_MODE = arguments[0].upper() if arguments else "SOURCE"
    if _ADDON_MODE not in {"SOURCE", "INSTALLED"}:
        raise ValueError(f"지원하지 않는 GUI 테스트 애드온 모드입니다: {_ADDON_MODE}")
    if _ADDON_MODE == "INSTALLED":
        import addon_utils

        addon_path = Path(bpy.utils.user_resource("SCRIPTS", path="addons")).resolve()
        addon_text = str(addon_path)
        if addon_text not in sys.path:
            sys.path.insert(0, addon_text)
        _loaded, enabled = addon_utils.check("colmap_align")
        if not enabled:
            addon_utils.enable("colmap_align", default_set=False, persistent=False)
    else:
        _add_source_path()
        from tools.align import registration

        registration.unregister()
        registration.register()
    from tools.align import importer
    from tools.align.operators import import_model as import_model_module

    _ORIGINAL_IMPORT_POPUP = import_model_module._show_import_popup
    import_model_module._show_import_popup = _capture_import_popup
    # GUI 상태 보기가 읽기와 표시 샘플링 단계를 관측하도록 TIMER 예산을 작게 고정합니다.
    _ORIGINAL_IMPORT_READ_MAX_RECORDS = importer.IMPORT_READ_MAX_RECORDS
    importer.IMPORT_READ_MAX_RECORDS = 128
    _ORIGINAL_DISPLAY_SAMPLE_MAX_RECORDS = importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS
    importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = 128
    _TEMPORARY_DIRECTORY = TemporaryDirectory()
    root = Path(_TEMPORARY_DIRECTORY.name)
    _FIRST_MODEL_PATH = root / "first_workspace"
    _SECOND_MODEL_PATH = root / "second_sparse"
    _write_text_sparse_model(_FIRST_MODEL_PATH / "sparse" / "0", _POINT_COUNT)
    _write_text_sparse_model(_SECOND_MODEL_PATH, _POINT_COUNT)
    bpy.app.timers.register(_run_gui_test_step, first_interval=0.1)


_start()
