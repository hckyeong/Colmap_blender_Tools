"""Merge & Export 패널의 포함 규칙·입력 잠금·오류 안내를 Blender GUI에서 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import traceback

import bpy


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _assert_panel_contract() -> None:
    """유휴 패널이 경로·사전 검사 없이 Export 버튼 하나만 표시하는지 검사합니다."""

    from tools.align.ui import panels

    source_text = Path(panels.__file__).read_text(encoding="utf-8")
    assert "class COLMAPALIGN_PT_merge_export" in source_text
    assert '"tools.align.merge_export"' in source_text
    assert '"colmap_align.select_merge_export_output"' not in source_text
    assert '"colmap_align.preflight_merge_export"' not in source_text
    assert 'path_column.prop(settings, "merge_export_output_root"' not in source_text
    assert 'text="Export"' in source_text
    assert '"colmap_align.cancel_merge_export"' in source_text
    assert '"colmap_align.open_merge_export_output"' in source_text
    assert '"colmap_align.open_merge_export_report"' in source_text


def _assert_visibility_and_input_lock_contract() -> None:
    """숨긴 A도 Included이며 실행 중 Data Layers 입력만 잠기는지 검증합니다."""

    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        MERGE_EXPORT_PHASE_EXPORT_RUNNING,
    )
    from tools.align.operators.export_model import is_merge_export_input_locked
    from tools.align.operators.model_group import _merge_export_block_reason
    from tools.align.state import RUNTIME

    RUNTIME.model_catalog.reset_demo_catalog()
    assert RUNTIME.model_catalog.set_model_visibility(DEMO_MODEL_1_ID, False) is True
    assert len(RUNTIME.model_catalog.active_models()) == 2
    RUNTIME.merge_export.phase = MERGE_EXPORT_PHASE_EXPORT_RUNNING
    assert is_merge_export_input_locked() is True
    assert _merge_export_block_reason() is not None
    RUNTIME.merge_export.clear()


def _assert_error_contract() -> None:
    """POINT2D offset 범위 오류가 원인·영향·해결 UI 문구로 표시되는지 검증합니다."""

    from tools.align.operators.export_model import format_merge_export_error

    entries = format_merge_export_error("POINT2D_IDX uint32 범위 초과")
    assert len(entries) == 3
    assert entries[0].startswith("원인:")
    assert entries[1].startswith("영향:")
    assert entries[2].startswith("해결:")


def _assert_export_browser_uses_last_import_directory() -> None:
    """Export 파일 탐색기가 마지막 COLMAP 경로에서 시작하는지 검증합니다."""

    from tools.align.operators.export_model import (
        _merge_export_browser_start_directory,
        _normalized_output_root,
        _output_root_under_parent,
    )

    with tempfile.TemporaryDirectory(prefix="colmap_align_merge_export_") as temporary:
        parent = Path(temporary).resolve()
        last_import = parent / "last_colmap"
        last_import.mkdir()
        settings = bpy.context.scene.colmap_align_settings
        settings.import_last_directory = str(last_import)
        assert _merge_export_browser_start_directory(bpy.context) == last_import

        output_root = _output_root_under_parent(str(parent))
        assert output_root == parent / "colmap_merged"
        assert output_root.exists() is False
        assert _normalized_output_root(str(output_root)) == output_root
        output_root.mkdir()
        assert _output_root_under_parent(str(parent)) == output_root
        assert _normalized_output_root(
            str(output_root),
            allow_existing=True,
        ) == output_root
        try:
            _normalized_output_root(str(output_root))
        except ValueError:
            pass
        else:
            raise AssertionError("승인 없는 기존 출력 경로가 허용되었습니다.")


def _assert_export_operator_owns_file_browser_and_start() -> None:
    """Export 오퍼레이터 하나가 위치 선택과 전체 worker 시작을 함께 담당하는지 검사합니다."""

    from tools.align.operators import export_model

    source_text = Path(export_model.__file__).read_text(encoding="utf-8")
    class_source = source_text.split("class COLMAPALIGN_OT_merge_export", 1)[1].split(
        "class COLMAPALIGN_OT_cancel_merge_export", 1
    )[0]
    assert "directory: StringProperty" in class_source
    assert "restore_import_axes: BoolProperty" in class_source
    assert 'self.layout.prop(self, "restore_import_axes"' in class_source
    assert "fileselect_add(self)" in class_source
    assert "_merge_export_browser_start_directory(context)" in class_source
    assert "_output_root_under_parent(selected_parent)" in class_source
    assert "invoke_props_dialog" in class_source
    assert 'confirm_text="덮어쓰기"' in class_source
    assert "_start_merge_export_job(" in class_source
    assert "preflight_only=False" in class_source
    assert "overwrite_existing_output=overwrite_existing_output" in class_source
    assert "restore_import_axes=self.restore_import_axes" in class_source
    assert "require_output_path=False" in class_source


def run_all() -> None:
    """애드온 등록 상태에서 Merge & Export GUI 계약을 검사합니다."""

    _add_source_path()
    from tools.align import registration

    try:
        if not registration.is_registered():
            registration.register()
        assert hasattr(bpy.context.scene.colmap_align_settings, "merge_export_output_root")
        _assert_panel_contract()
        _assert_visibility_and_input_lock_contract()
        _assert_error_contract()
        _assert_export_browser_uses_last_import_directory()
        _assert_export_operator_owns_file_browser_and_start()
    finally:
        if registration.is_registered():
            registration.unregister()


def _run_gui_test() -> None:
    """Blender 이벤트 루프에서 GUI 계약을 검사한 뒤 프로세스를 종료합니다."""

    succeeded = True
    try:
        run_all()
    except Exception:
        traceback.print_exc()
        succeeded = False
    print(f"MERGE_EXPORT_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


if __name__ == "__main__":
    bpy.app.timers.register(_run_gui_test, first_interval=0.1)
