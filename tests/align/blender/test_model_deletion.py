"""선택한 COLMAP 모델의 원자적 런타임 삭제와 정리 범위를 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import sys
import tempfile
from pathlib import Path


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _pair(pair_id: int, reference_model_id: str, moving_model_id: str):
    """모델 소유권을 가진 최소 확정 마커 쌍을 만듭니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import MarkerPair, SelectedPoint

    return MarkerPair(
        pair_id=pair_id,
        reference=SelectedPoint(
            ROLE_REFERENCE,
            pair_id,
            (float(pair_id), 0.0, 0.0),
            model_id=reference_model_id,
        ),
        moving=SelectedPoint(
            ROLE_MOVING,
            pair_id + 100,
            (float(pair_id), 1.0, 0.0),
            model_id=moving_model_id,
        ),
    )


def _select_model(model_id: str) -> None:
    """테스트 대상 데이터 레이어를 잠금 없는 상태에서 선택합니다."""
    from tools.align.state import RUNTIME

    assert RUNTIME.model_catalog.select_model(
        model_id,
        has_marker_draft=False,
        completed_pair_count=0,
    )


def test_catalog_removal_preserves_anchor_and_load_order() -> None:
    """Anchor 삭제와 중간 삭제 뒤 선택·Anchor·가져오기 순서를 검증합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        MODEL_STATUS_REGISTERED,
    )
    from tools.align.state import ModelCatalogState

    catalog = ModelCatalogState()
    catalog.reset_demo_catalog()
    assert catalog.select_model(
        DEMO_MODEL_3_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.remove_model(DEMO_MODEL_3_ID) is not None
    assert catalog.selected_model_id is None
    assert catalog.selected_model_ids == ()
    added = catalog.add_imported_model("new_model", "New Model", select_after_import=True)
    assert added.load_order == 5

    catalog.reset_demo_catalog()
    assert catalog.select_model(
        DEMO_MODEL_1_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.remove_model(DEMO_MODEL_1_ID) is not None
    assert catalog.models[DEMO_MODEL_2_ID].is_anchor is True
    assert catalog.selected_model_id is None
    assert catalog.selected_model_ids == ()
    assert catalog.remove_model(DEMO_MODEL_2_ID) is not None
    assert catalog.models[DEMO_MODEL_3_ID].is_anchor is True
    assert catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED
    assert catalog.remove_model(DEMO_MODEL_3_ID) is not None
    assert catalog.models[DEMO_MODEL_4_ID].is_anchor is True
    assert catalog.remove_model(DEMO_MODEL_4_ID) is not None
    assert catalog.model_count == 0
    assert catalog.selected_model_id is None


def test_runtime_deletion_cleans_model_markers_and_gpu_without_touching_source() -> None:
    """모델 관련 런타임 참조만 정리되고 원본 폴더와 Scene은 유지되는지 확인합니다."""
    import bpy

    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )
    from tools.align.model_repository import canonical_source_path
    from tools.align.operators.demo_data import install_demo_fixture
    from tools.align.operators.model_group import delete_model_from_runtime
    from tools.align.state import RUNTIME

    install_demo_fixture(point_count=12)
    _select_model(DEMO_MODEL_3_ID)
    selection = RUNTIME.marker_selection
    selection.clear()
    selection.completed_pairs = [
        _pair(1, DEMO_MODEL_1_ID, DEMO_MODEL_3_ID),
        _pair(2, DEMO_MODEL_2_ID, DEMO_MODEL_4_ID),
        _pair(3, DEMO_MODEL_3_ID, DEMO_MODEL_4_ID),
    ]
    selection.active_pair_id = 1
    selection.next_pair_id = 4
    RUNTIME.undo_journal.record_committed("이전", "이후", "테스트 이력", "TEST")

    target_gpu_key = (DEMO_MODEL_3_ID, 1, 1, "target")
    other_gpu_key = (DEMO_MODEL_4_ID, 1, 1, "other")
    RUNTIME.gpu_batches[target_gpu_key] = object()
    other_gpu_batch = object()
    RUNTIME.gpu_batches[other_gpu_key] = other_gpu_batch
    scene_snapshot = (
        tuple(bpy.data.objects),
        tuple(bpy.data.cameras),
        tuple(bpy.data.collections),
    )

    with tempfile.TemporaryDirectory(prefix="colmap_align_delete_") as temporary_directory:
        source_directory = Path(temporary_directory).resolve()
        source_marker = source_directory / "points3D.txt"
        source_marker.write_text("원본 보존 검사\n", encoding="utf-8")
        repository = RUNTIME.model_repository
        original_model = repository.models[DEMO_MODEL_3_ID]
        display_transform = repository.display_transform_for_model_id(DEMO_MODEL_3_ID)
        repository.remove_model(DEMO_MODEL_3_ID)
        repository.add_model(
            replace(
                original_model,
                source_path=Path(canonical_source_path(source_directory)),
            ),
            display_transform,
        )

        result = delete_model_from_runtime(DEMO_MODEL_3_ID)
        assert source_marker.read_text(encoding="utf-8") == "원본 보존 검사\n"

    assert result.model_id == DEMO_MODEL_3_ID
    assert result.point_count == 12
    assert result.removed_marker_pair_count == 2
    assert DEMO_MODEL_3_ID not in RUNTIME.model_catalog.models
    assert RUNTIME.model_catalog.selected_model_id is None
    assert RUNTIME.model_catalog.selected_model_ids == ()
    assert DEMO_MODEL_3_ID not in RUNTIME.model_repository.models
    assert DEMO_MODEL_3_ID not in RUNTIME.model_repository.display_transforms
    assert DEMO_MODEL_3_ID not in RUNTIME.point_clouds
    assert target_gpu_key not in RUNTIME.gpu_batches
    assert RUNTIME.gpu_batches[other_gpu_key] is other_gpu_batch
    assert [pair.pair_id for pair in selection.completed_pairs] == [2]
    assert selection.active_pair_id == 2
    assert selection.next_pair_id == 4
    assert RUNTIME.undo_journal.undo_count == 0
    assert RUNTIME.undo_journal.redo_count == 0
    assert scene_snapshot == (
        tuple(bpy.data.objects),
        tuple(bpy.data.cameras),
        tuple(bpy.data.collections),
    )


def test_runtime_deletion_rolls_back_every_store_on_failure() -> None:
    """삭제 도중 오류가 나면 카탈로그·저장소·마커·GPU 상태가 전부 복원되는지 확인합니다."""
    import numpy as np

    from tools.align.alignment import create_sim3_transform
    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_3_ID
    from tools.align.operators.demo_data import install_demo_fixture
    from tools.align.operators.model_group import delete_model_from_runtime
    from tools.align.state import RUNTIME

    install_demo_fixture(point_count=8)
    _select_model(DEMO_MODEL_3_ID)
    RUNTIME.marker_selection.clear()
    RUNTIME.marker_selection.completed_pairs = [
        _pair(1, DEMO_MODEL_1_ID, DEMO_MODEL_3_ID)
    ]
    RUNTIME.marker_selection.active_pair_id = 1
    cache_key = (DEMO_MODEL_3_ID, 1, 1, "rollback")
    cache_value = object()
    RUNTIME.gpu_batches[cache_key] = cache_value

    catalog_snapshot = RUNTIME.model_catalog.snapshot()
    marker_snapshot = RUNTIME.marker_selection.snapshot()
    model = RUNTIME.model_repository.models[DEMO_MODEL_3_ID]
    transform = RUNTIME.model_repository.display_transform_for_model_id(DEMO_MODEL_3_ID)
    alignment = create_sim3_transform(
        np.identity(3, dtype=np.float64),
        1.25,
        np.asarray((2.0, -3.0, 4.0), dtype=np.float64),
    )
    RUNTIME.model_repository.set_alignment_transform(DEMO_MODEL_3_ID, alignment)
    point_cloud = RUNTIME.point_clouds[DEMO_MODEL_3_ID]
    original_remove_pairs = RUNTIME.marker_selection.remove_pairs_for_model

    def _fail_pair_cleanup(_model_id: str) -> int:
        """원자 삭제 롤백을 검사하려고 마커 정리 단계에서 실패합니다."""
        raise RuntimeError("의도한 마커 정리 실패")

    RUNTIME.marker_selection.remove_pairs_for_model = _fail_pair_cleanup
    try:
        try:
            delete_model_from_runtime(DEMO_MODEL_3_ID)
        except RuntimeError as error:
            assert str(error) == "의도한 마커 정리 실패"
        else:
            raise AssertionError("삭제 단계 오류가 호출자에게 전달되지 않았습니다.")
    finally:
        RUNTIME.marker_selection.remove_pairs_for_model = original_remove_pairs

    assert RUNTIME.model_catalog.snapshot() == catalog_snapshot
    assert RUNTIME.marker_selection.snapshot() == marker_snapshot
    assert RUNTIME.model_repository.models[DEMO_MODEL_3_ID] is model
    assert RUNTIME.model_repository.display_transform_for_model_id(DEMO_MODEL_3_ID) == transform
    assert RUNTIME.model_repository.alignment_transform_for_model_id(DEMO_MODEL_3_ID) == alignment
    assert RUNTIME.point_clouds[DEMO_MODEL_3_ID] is point_cloud
    assert RUNTIME.gpu_batches[cache_key] is cache_value


def run_all() -> None:
    """모델 삭제의 상태·리소스·원자성 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_catalog_removal_preserves_anchor_and_load_order()
    test_runtime_deletion_cleans_model_markers_and_gpu_without_touching_source()
    test_runtime_deletion_rolls_back_every_store_on_failure()
    print("test_model_deletion: 통과")


if __name__ == "__main__":
    run_all()
