"""실제 Blender File Browser에서 단일 Export 버튼의 시작 경로와 취소 경계를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import traceback

import bpy


_STAGE = "PREPARE"
_WAIT_COUNT = 0
_MAIN_WINDOW = None
_TEMPORARY_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_LAST_IMPORT_DIRECTORY: Path | None = None
_ORIGINAL_START_MERGE_EXPORT_JOB = None
_START_CALLS: list[tuple[bool, bool, str, bool]] = []


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _main_window():
    """현재 테스트의 Blender 메인 창을 반환합니다."""

    global _MAIN_WINDOW
    if _MAIN_WINDOW is None:
        windows = tuple(bpy.context.window_manager.windows)
        assert len(windows) == 1
        _MAIN_WINDOW = windows[0]
    return _MAIN_WINDOW


def _view3d_override(window) -> dict[str, object]:
    """COLMAP Align 뷰에서 Export 오퍼레이터를 호출할 컨텍스트를 만듭니다."""

    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "WINDOW")
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
        "scene": window.scene,
    }


def _file_browser_context():
    """현재 열린 단일 File Browser의 Window와 Area를 반환합니다."""

    matches = tuple(
        (window, area)
        for window in bpy.context.window_manager.windows
        for area in window.screen.areas
        if area.type == "FILE_BROWSER"
    )
    return matches[0] if len(matches) == 1 else None


def _finish(succeeded: bool) -> None:
    """등록과 임시 경로를 정리하고 테스트 결과를 출력합니다."""

    global _TEMPORARY_DIRECTORY, _ORIGINAL_START_MERGE_EXPORT_JOB
    try:
        from tools.align import registration
        from tools.align.operators import export_model

        if _ORIGINAL_START_MERGE_EXPORT_JOB is not None:
            export_model._start_merge_export_job = _ORIGINAL_START_MERGE_EXPORT_JOB
            _ORIGINAL_START_MERGE_EXPORT_JOB = None

        if registration.is_registered():
            registration.unregister()
    finally:
        if _TEMPORARY_DIRECTORY is not None:
            _TEMPORARY_DIRECTORY.cleanup()
            _TEMPORARY_DIRECTORY = None
        print(
            "MERGE_EXPORT_FILE_BROWSER_GUI_TEST_RESULT="
            f"{'PASS' if succeeded else 'FAIL'}"
        )
        bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """작업공간 진입부터 Export 파일 탐색기 취소까지 단계별로 검증합니다."""

    global _STAGE, _WAIT_COUNT, _TEMPORARY_DIRECTORY, _LAST_IMPORT_DIRECTORY
    global _ORIGINAL_START_MERGE_EXPORT_JOB
    try:
        from tools.align.operators.workspace import _process_sidebar_activation
        from tools.align.session import is_session_active_for_window
        from tools.align.state import RUNTIME

        if RUNTIME.workspace_setup_task is not None:
            return 0.05
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "PREPARE":
            _TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory(
                prefix="colmap_align_export_browser_"
            )
            _LAST_IMPORT_DIRECTORY = (
                Path(_TEMPORARY_DIRECTORY.name).resolve() / "last_colmap"
            )
            _LAST_IMPORT_DIRECTORY.mkdir()
            settings = bpy.context.scene.colmap_align_settings
            settings.import_last_directory = str(_LAST_IMPORT_DIRECTORY)
            settings.merge_export_output_root = ""
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "WAIT_WORKSPACE"
            return 0.05

        window = _main_window()
        if _STAGE == "WAIT_WORKSPACE":
            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.05
            assert is_session_active_for_window(window) is True
            RUNTIME.model_catalog.reset_demo_catalog()
            with bpy.context.temp_override(**_view3d_override(window)):
                assert bpy.ops.colmap_align.merge_export.poll() is True
                result = bpy.ops.colmap_align.merge_export("INVOKE_DEFAULT")
            assert "RUNNING_MODAL" in result
            _STAGE = "WAIT_FILE_BROWSER"
            _WAIT_COUNT = 0
            return 0.05

        if _STAGE == "WAIT_FILE_BROWSER":
            _WAIT_COUNT += 1
            assert _WAIT_COUNT < 200, "Export File Browser가 제한 시간 안에 열리지 않았습니다."
            file_context = _file_browser_context()
            if file_context is None:
                return 0.05
            file_window, area = file_context
            active_operator = area.spaces.active.active_operator
            assert active_operator is not None
            assert active_operator.bl_rna.identifier == "COLMAP_ALIGN_OT_merge_export"
            assert active_operator.output_format == "TEXT"
            assert active_operator.restore_import_axes is True
            assert _LAST_IMPORT_DIRECTORY is not None
            assert Path(active_operator.directory).resolve() == _LAST_IMPORT_DIRECTORY
            assert window.scene.colmap_align_settings.merge_export_output_root == ""
            assert RUNTIME.merge_export.job is None
            file_window.event_simulate(type="ESC", value="PRESS")
            file_window.event_simulate(type="ESC", value="RELEASE")
            _STAGE = "WAIT_CANCEL"
            _WAIT_COUNT = 0
            return 0.05

        if _STAGE == "WAIT_CANCEL":
            _WAIT_COUNT += 1
            assert _WAIT_COUNT < 200, "Export File Browser가 제한 시간 안에 닫히지 않았습니다."
            if _file_browser_context() is not None:
                return 0.05
            assert RUNTIME.merge_export.job is None
            assert RUNTIME.merge_export.backend is None
            assert window.scene.colmap_align_settings.merge_export_output_root == ""
            assert _TEMPORARY_DIRECTORY is not None
            export_parent = Path(_TEMPORARY_DIRECTORY.name).resolve() / "destination"
            export_parent.mkdir()
            existing_output = export_parent / "colmap_merged"
            existing_output.mkdir()
            sentinel = existing_output / "old-result.txt"
            sentinel.write_text("기존 결과", encoding="utf-8")

            from tools.align.operators import export_model

            _ORIGINAL_START_MERGE_EXPORT_JOB = export_model._start_merge_export_job

            def record_start(
                _context,
                *,
                preflight_only: bool,
                output_format: str = "TEXT",
                restore_import_axes: bool = True,
                overwrite_existing_output: bool = False,
            ) -> None:
                """실제 worker 대신 확인 대화상자의 형식·승인 인자만 기록합니다."""

                _START_CALLS.append(
                    (
                        preflight_only,
                        overwrite_existing_output,
                        output_format,
                        restore_import_axes,
                    )
                )

            export_model._start_merge_export_job = record_start
            with bpy.context.temp_override(**_view3d_override(window)):
                result = bpy.ops.colmap_align.merge_export(
                    "EXEC_DEFAULT",
                    directory=str(export_parent),
                    output_format="BINARY",
                    restore_import_axes=False,
                )
            assert "RUNNING_MODAL" in result
            assert _START_CALLS == []
            assert sentinel.read_text(encoding="utf-8") == "기존 결과"
            _STAGE = "CONFIRM_OVERWRITE"
            return 0.1

        if _STAGE == "CONFIRM_OVERWRITE":
            window.event_simulate(type="RET", value="PRESS")
            window.event_simulate(type="RET", value="RELEASE")
            _STAGE = "WAIT_OVERWRITE_START"
            _WAIT_COUNT = 0
            return 0.05

        if _STAGE == "WAIT_OVERWRITE_START":
            _WAIT_COUNT += 1
            assert _WAIT_COUNT < 200, "덮어쓰기 확인이 제한 시간 안에 처리되지 않았습니다."
            if not _START_CALLS:
                return 0.05
            assert _START_CALLS == [(False, True, "BINARY", False)]
            assert _TEMPORARY_DIRECTORY is not None
            export_parent = Path(_TEMPORARY_DIRECTORY.name).resolve() / "destination"
            existing_output = export_parent / "colmap_merged"
            assert (existing_output / "old-result.txt").read_text(encoding="utf-8") == "기존 결과"
            assert Path(window.scene.colmap_align_settings.merge_export_output_root) == existing_output
            _finish(True)
            return None

        raise AssertionError(f"알 수 없는 Export File Browser 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def _start() -> None:
    """소스 애드온을 등록하고 실제 File Browser 검증을 시작합니다."""

    _add_source_path()
    from tools.align import registration

    if registration.is_registered():
        registration.unregister()
    registration.register()
    bpy.app.timers.register(_run_step, first_interval=0.1)


_start()
