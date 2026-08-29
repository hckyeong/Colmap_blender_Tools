"""실제 Blender 저장·재열기에서 프로젝트 Text 상태가 유지되는지 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import bpy


def _add_source_path() -> None:
    """애드온 소스 경로를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _snapshot():
    """실제 `.blend` 왕복에 사용할 최소 유효 프로젝트 snapshot을 생성합니다."""

    from tools.align.project_persistence.types import (
        ProjectCatalogSnapshot,
        ProjectMarkerSnapshot,
        ProjectModelSnapshot,
        ProjectSourceSnapshot,
        ProjectStateSnapshot,
    )

    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    return ProjectStateSnapshot(
        schema_version=1,
        writer_addon_version=(0, 1, 0),
        project_uuid=str(uuid4()),
        saved_at_utc="2026-08-17T00:00:00Z",
        models=(
            ProjectModelSnapshot(
                model_id="roundtrip-model",
                display_name="roundtrip",
                load_order=1,
                source=ProjectSourceSnapshot(
                    selected_root_absolute="C:/dataset/roundtrip",
                    sparse_path_absolute="C:/dataset/roundtrip/sparse/0",
                    sparse_path_relative="../dataset/roundtrip/sparse/0",
                    source_format="text",
                    source_fingerprint="c" * 64,
                    display_point_limit=250_000,
                    axis_preset="BLENDER",
                    axis_forward="Y",
                    axis_up="Z",
                    display_transform_fingerprint="d" * 32,
                ),
                catalog=ProjectCatalogSnapshot("REGISTERED", True, True, None),
                alignment_matrix_float64=identity,
            ),
        ),
        markers=ProjectMarkerSnapshot(1, None, ()),
    )


def _remove_owned_project_texts() -> None:
    """테스트가 만든 애드온 소유 Text만 다음 검사를 위해 제거합니다."""

    from tools.align.constants import PROJECT_TEXT_OWNER_KEY, PROJECT_TEXT_OWNER_VALUE

    for text in tuple(bpy.data.texts):
        if text.get(PROJECT_TEXT_OWNER_KEY) == PROJECT_TEXT_OWNER_VALUE:
            bpy.data.texts.remove(text)


def test_text_persists_across_save_save_copy_and_reopen() -> None:
    """ACTIVE Text와 owner IDProperty가 실제 Save·Save Copy·재열기 뒤에도 보존되는지 확인합니다."""

    from tools.align.constants import (
        PROJECT_TEXT_OWNER_KEY,
        PROJECT_TEXT_OWNER_VALUE,
        PROJECT_TEXT_STATE_ACTIVE,
        PROJECT_TEXT_STATE_KEY,
    )
    from tools.align.project_persistence.store import (
        active_project_text,
        read_active_project_state,
        write_active_project_state,
    )

    _remove_owned_project_texts()
    snapshot = _snapshot()
    active = write_active_project_state(snapshot)
    assert active.use_fake_user is True
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        project_file = root / "project_state.blend"
        copied_file = root / "project_state_copy.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(project_file), check_existing=False) == {"FINISHED"}
        assert project_file.is_file()
        assert bpy.ops.wm.save_as_mainfile(
            filepath=str(copied_file),
            check_existing=False,
            copy=True,
        ) == {"FINISHED"}
        assert copied_file.is_file()
        assert bpy.ops.wm.open_mainfile(filepath=str(project_file), load_ui=False) == {"FINISHED"}

    restored_text = active_project_text()
    assert restored_text is not None
    assert restored_text.get(PROJECT_TEXT_OWNER_KEY) == PROJECT_TEXT_OWNER_VALUE
    assert restored_text.get(PROJECT_TEXT_STATE_KEY) == PROJECT_TEXT_STATE_ACTIVE
    restored = read_active_project_state()
    assert restored.project_uuid == snapshot.project_uuid
    assert restored.models[0].model_id == "roundtrip-model"
    assert "points3D" not in restored_text.as_string()
    _remove_owned_project_texts()


def run_all() -> None:
    """Blender 저장 매체의 실제 파일 왕복 회귀를 실행합니다."""

    _add_source_path()
    test_text_persists_across_save_save_copy_and_reopen()
    print("test_project_restore_roundtrip: 통과")


if __name__ == "__main__":
    run_all()
