"""`.blend` 프로젝트 상태의 순수 schema v1 codec을 검증합니다."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4


def _add_source_path() -> None:
    """애드온 소스 경로를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _source_snapshot(label: str):
    """codec 검증에 필요한 최소의 검증 완료 source snapshot을 만듭니다."""

    from tools.align.project_persistence.types import ProjectSourceSnapshot

    return ProjectSourceSnapshot(
        selected_root_absolute=f"C:/dataset/{label}",
        sparse_path_absolute=f"C:/dataset/{label}/sparse/0",
        sparse_path_relative=f"../dataset/{label}/sparse/0",
        source_format="text",
        source_fingerprint="a" * 64,
        display_point_limit=250_000,
        axis_preset="BLENDER",
        axis_forward="Y",
        axis_up="Z",
        display_transform_fingerprint="e" * 32,
    )


def _snapshot():
    """A/P·Anchor·완료 marker를 포함한 정상 schema v1 snapshot을 생성합니다."""

    from tools.align.project_persistence.types import (
        ProjectCatalogSnapshot,
        ProjectMarkerPairSnapshot,
        ProjectMarkerPointSnapshot,
        ProjectMarkerSnapshot,
        ProjectModelSnapshot,
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
                model_id="model-a",
                display_name="anchor",
                load_order=1,
                source=_source_snapshot("anchor"),
                catalog=ProjectCatalogSnapshot("REGISTERED", True, True, None),
                alignment_matrix_float64=identity,
            ),
            ProjectModelSnapshot(
                model_id="model-p",
                display_name="moving",
                load_order=2,
                source=_source_snapshot("moving"),
                catalog=ProjectCatalogSnapshot("PENDING", False, True, None),
                alignment_matrix_float64=identity,
            ),
        ),
        markers=ProjectMarkerSnapshot(
            next_pair_id=2,
            active_pair_id=1,
            completed_pairs=(
                ProjectMarkerPairSnapshot(
                    pair_id=1,
                    reference=ProjectMarkerPointSnapshot("REFERENCE", "model-a", 1),
                    moving=ProjectMarkerPointSnapshot("MOVING", "model-p", 1),
                ),
            ),
        ),
    )


def test_schema_roundtrip_and_canonical_fingerprint() -> None:
    """같은 schema 값은 key 순서와 관계없이 동일 fingerprint로 복원되는지 확인합니다."""

    from tools.align.project_persistence import (
        decode_project_state,
        encode_project_state,
        payload_fingerprint_for_snapshot,
    )

    payload = encode_project_state(_snapshot())
    decoded = decode_project_state(payload)
    reordered = json.dumps(json.loads(payload), ensure_ascii=False, sort_keys=False, indent=2)
    decoded_reordered = decode_project_state(reordered)
    assert decoded.models[0].model_id == "model-a"
    assert decoded.markers.completed_pairs[0].moving.model_id == "model-p"
    assert decoded.payload_fingerprint == decoded_reordered.payload_fingerprint
    assert decoded.payload_fingerprint == payload_fingerprint_for_snapshot(decoded)


def test_corruption_and_structure_are_rejected() -> None:
    """변조 fingerprint와 중복 model ID가 조용히 수용되지 않는지 확인합니다."""

    from tools.align.project_persistence import (
        ProjectPersistenceCodecError,
        decode_project_state,
        encode_project_state,
    )

    payload_mapping = json.loads(encode_project_state(_snapshot()))
    payload_mapping["payload_fingerprint"] = "0" * 64
    try:
        decode_project_state(json.dumps(payload_mapping))
    except ProjectPersistenceCodecError:
        pass
    else:
        raise AssertionError("변조된 payload fingerprint를 거부하지 않았습니다.")

    snapshot = _snapshot()
    duplicated = snapshot.models + (snapshot.models[0],)
    try:
        encode_project_state(snapshot.__class__(
            schema_version=snapshot.schema_version,
            writer_addon_version=snapshot.writer_addon_version,
            project_uuid=snapshot.project_uuid,
            saved_at_utc=snapshot.saved_at_utc,
            models=duplicated,
            markers=snapshot.markers,
        ))
    except ProjectPersistenceCodecError:
        pass
    else:
        raise AssertionError("중복 model ID를 거부하지 않았습니다.")


def run_all() -> None:
    """순수 codec 회귀를 Blender 실행 환경에서 순서대로 실행합니다."""

    _add_source_path()
    test_schema_roundtrip_and_canonical_fingerprint()
    test_corruption_and_structure_are_rejected()
    print("test_project_persistence_codec: 통과")


if __name__ == "__main__":
    run_all()
