"""`.blend` Text 프로젝트 상태의 owner 검사와 원자 교체를 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import bpy


def _add_source_path() -> None:
    """애드온 소스 경로를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _snapshot(label: str):
    """Text store가 기록할 최소의 유효한 확정 프로젝트 snapshot을 만듭니다."""

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
                model_id=f"model-{label}",
                display_name=label,
                load_order=1,
                source=ProjectSourceSnapshot(
                    selected_root_absolute=f"C:/dataset/{label}",
                    sparse_path_absolute=f"C:/dataset/{label}/sparse/0",
                    sparse_path_relative="",
                    source_format="text",
                    source_fingerprint="a" * 64,
                    display_point_limit=250_000,
                    axis_preset="BLENDER",
                    axis_forward="Y",
                    axis_up="Z",
                    display_transform_fingerprint="b" * 32,
                ),
                catalog=ProjectCatalogSnapshot("REGISTERED", True, True, None),
                alignment_matrix_float64=identity,
            ),
        ),
        markers=ProjectMarkerSnapshot(1, None, ()),
    )


def _remove_owned_project_texts() -> None:
    """테스트가 만든 애드온 소유 Text만 제거해 다음 검사를 독립시킵니다."""

    from tools.align.constants import PROJECT_TEXT_OWNER_KEY, PROJECT_TEXT_OWNER_VALUE

    for text in tuple(bpy.data.texts):
        if text.get(PROJECT_TEXT_OWNER_KEY) == PROJECT_TEXT_OWNER_VALUE:
            bpy.data.texts.remove(text)


def test_owner_selection_and_atomic_replacement() -> None:
    """ACTIVE owner 하나만 읽고 사용자 Text를 보존한 채 새 상태로 교체하는지 확인합니다."""

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
    user_text = bpy.data.texts.new(".colmap_align_project_user_note")
    user_text.write("사용자 메모는 보존되어야 합니다.")
    first = _snapshot("first")
    first_text = write_active_project_state(first)
    first_text_name = first_text.name
    assert first_text.use_fake_user is True
    assert active_project_text() is first_text
    assert read_active_project_state().project_uuid == first.project_uuid

    second = _snapshot("second")
    second_text = write_active_project_state(second)
    assert active_project_text() is second_text
    assert read_active_project_state().project_uuid == second.project_uuid
    assert bpy.data.texts.get(user_text.name) is user_text
    assert user_text.as_string() == "사용자 메모는 보존되어야 합니다."
    assert bpy.data.texts.get(first_text_name) is None

    conflicting = bpy.data.texts.new(".colmap_align_project_conflict")
    conflicting[PROJECT_TEXT_OWNER_KEY] = PROJECT_TEXT_OWNER_VALUE
    conflicting[PROJECT_TEXT_STATE_KEY] = PROJECT_TEXT_STATE_ACTIVE
    try:
        active_project_text()
    except Exception as error:
        assert "둘 이상" in str(error)
    else:
        raise AssertionError("ACTIVE owner Text 충돌을 거부하지 않았습니다.")
    bpy.data.texts.remove(conflicting)
    bpy.data.texts.remove(user_text)
    _remove_owned_project_texts()


def test_staging_failure_keeps_previous_active_text() -> None:
    """STAGING 검증 실패가 이전 정상 ACTIVE Text를 손상시키지 않는지 확인합니다."""

    from tools.align.project_persistence import store
    from tools.align.project_persistence.store import active_project_text, write_active_project_state

    _remove_owned_project_texts()
    first = _snapshot("stable")
    first_text = write_active_project_state(first)
    original_verify = store._verify_staging_text

    def _raise_staging_failure(_text, _payload, _snapshot_value) -> None:
        """Text 기록 직후 실패 상황을 재현해 원자 교체 rollback을 검증합니다."""

        raise store.ProjectTextStoreError("테스트용 STAGING 검증 실패")

    try:
        store._verify_staging_text = _raise_staging_failure
        try:
            write_active_project_state(_snapshot("replacement"))
        except store.ProjectTextStoreError:
            pass
        else:
            raise AssertionError("STAGING 검증 실패가 호출자에게 전달되지 않았습니다.")
    finally:
        store._verify_staging_text = original_verify

    assert active_project_text() is first_text
    assert first_text.as_string()
    _remove_owned_project_texts()


def test_owner_metadata_mismatch_and_active_only_removal() -> None:
    """Text owner와 JSON UUID 불일치를 거부하고 ACTIVE Text만 제거하는지 확인합니다."""

    from tools.align.constants import (
        PROJECT_TEXT_OWNER_KEY,
        PROJECT_TEXT_OWNER_VALUE,
        PROJECT_TEXT_STATE_KEY,
        PROJECT_TEXT_STATE_RETIRED,
        PROJECT_TEXT_UUID_KEY,
    )
    from tools.align.project_persistence.store import (
        ProjectTextStoreError,
        active_project_text,
        read_active_project_state,
        remove_active_project_state,
        write_active_project_state,
    )

    _remove_owned_project_texts()
    snapshot = _snapshot("metadata")
    active = write_active_project_state(snapshot)
    active[PROJECT_TEXT_UUID_KEY] = str(uuid4())
    try:
        read_active_project_state()
    except ProjectTextStoreError:
        pass
    else:
        raise AssertionError("Text owner UUID와 JSON UUID 불일치를 거부하지 않았습니다.")
    active[PROJECT_TEXT_UUID_KEY] = snapshot.project_uuid
    retired = bpy.data.texts.new(".colmap_align_project_retired")
    retired[PROJECT_TEXT_OWNER_KEY] = PROJECT_TEXT_OWNER_VALUE
    retired[PROJECT_TEXT_STATE_KEY] = PROJECT_TEXT_STATE_RETIRED
    assert remove_active_project_state() is True
    assert active_project_text() is None
    assert retired.name in bpy.data.texts
    bpy.data.texts.remove(retired)


def test_empty_runtime_does_not_remove_saved_project_text() -> None:
    """복원 전 빈 Runtime의 등록 해제가 기존 ACTIVE Text를 삭제하지 않는지 확인합니다."""

    from tools.align.project_persistence.runtime import sync_project_state_if_dirty
    from tools.align.project_persistence.store import active_project_text, write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    snapshot = _snapshot("preserve-empty-runtime")
    active = write_active_project_state(snapshot)
    runtime = RuntimeState()
    assert sync_project_state_if_dirty(runtime=runtime) is False
    assert active_project_text() is active
    _remove_owned_project_texts()


def test_fixture_without_source_metadata_stays_nonpersistent() -> None:
    """기존 Demo fixture는 저장 실패 없이 비영구 Runtime으로 기존 동작을 유지하는지 확인합니다."""

    from tools.align.operators.demo_data import install_demo_fixture
    from tools.align.project_persistence.runtime import (
        notify_project_state_changed,
        sync_project_state_if_dirty,
    )
    from tools.align.project_persistence.store import active_project_text
    from tools.align.state import RUNTIME

    _remove_owned_project_texts()
    RUNTIME.clear_transient()
    try:
        install_demo_fixture()
        notify_project_state_changed("fixture 변경")
        assert sync_project_state_if_dirty() is False
        assert active_project_text() is None
        assert "source metadata가 없는 내부 fixture" in RUNTIME.project_restore.last_message
    finally:
        RUNTIME.clear_transient()
        _remove_owned_project_texts()


def run_all() -> None:
    """Text owner와 원자 교체 회귀를 Blender에서 순서대로 실행합니다."""

    _add_source_path()
    test_owner_selection_and_atomic_replacement()
    test_staging_failure_keeps_previous_active_text()
    test_owner_metadata_mismatch_and_active_only_removal()
    test_empty_runtime_does_not_remove_saved_project_text()
    test_fixture_without_source_metadata_stays_nonpersistent()
    print("test_project_persistence_store: 통과")


if __name__ == "__main__":
    run_all()
