"""Blender Background에서 공용 Dataset Property와 Revision transaction을 검증합니다."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.colmap_dataset import register as register_shared_dataset
from shared.colmap_dataset import unregister as unregister_shared_dataset
from shared.colmap_dataset.blender.ownership import (
    OWNER_KEY,
    RECORD_ID_KEY,
    ROLE_KEY,
    SCHEMA_KEY,
    SOURCE_FINGERPRINT_KEY,
    UUID_KEY,
)
from shared.colmap_dataset.blender.registry import (
    begin_edit_commit,
    begin_import_commit,
    snapshot,
)
from shared.colmap_dataset.contracts import DatasetImportMetadata
from tools import modifier as blender_colmap


def assert_true(condition, message: str) -> None:
    """조건이 거짓이면 Background 검사 실패를 명확한 문구로 보고합니다."""

    if not condition:
        raise AssertionError(message)


def _metadata(root: Path, dataset_uuid: str, *, point_count: int) -> DatasetImportMetadata:
    """Revision 회귀에 사용할 유효한 공용 Import 메타데이터를 만듭니다."""

    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    return DatasetImportMetadata(
        dataset_uuid=dataset_uuid,
        source_fingerprint=("a" if dataset_uuid == "dataset-a" else "b") * 64,
        dataset_root=str(root.resolve()),
        sparse_root=str((root / "sparse").resolve()),
        model_directory=str(model.resolve()),
        images_directory=str(images.resolve()),
        input_format="text",
        layout="legacy",
        selected_files_json="[]",
        image_path_manifest_json="[]",
        camera_count=1,
        image_count=1,
        point3d_count=point_count,
        source_schema_version=2,
        axis_preset="REALITY_CAPTURE",
        axis_forward="Z",
        axis_up="-Y",
        point_size=0.05,
        camera_frustum_size=1.0,
    )


register_shared_dataset()
blender_colmap.register()
try:
    assert_true(
        (
            OWNER_KEY,
            UUID_KEY,
            SCHEMA_KEY,
            ROLE_KEY,
            RECORD_ID_KEY,
            SOURCE_FINGERPRINT_KEY,
        )
        == (
            "colmap_dataset_owner",
            "colmap_dataset_uuid",
            "colmap_dataset_schema",
            "colmap_dataset_role",
            "colmap_dataset_record_id",
            "colmap_dataset_source_fingerprint",
        ),
        "공용 Blender ID 소유 키가 중립 계약과 다릅니다.",
    )
    scene = bpy.context.scene
    modifier_settings = scene.colmap_import
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        metadata_a = _metadata(root / "a", "dataset-a", point_count=3)
        modifier_settings.dataset_uuid = metadata_a.dataset_uuid

        first = begin_import_commit(scene, metadata_a)
        first.apply()
        first.complete()
        first_snapshot = snapshot(scene)
        assert_true(
            first_snapshot.dataset_revision == 1
            and first_snapshot.display_revision == 1,
            "첫 공용 Import의 Dataset·Display Revision이 초기화되지 않았습니다.",
        )

        second = begin_import_commit(scene, metadata_a)
        second.apply()
        second.complete()
        assert_true(
            snapshot(scene).dataset_revision == 2,
            "같은 Source Import가 공용 Dataset Revision을 증가시키지 않았습니다.",
        )

        edit = begin_edit_commit(
            scene,
            dataset_uuid="dataset-a",
            camera_count=1,
            image_count=1,
            point3d_count=1,
        )
        before_edit = snapshot(scene)
        edit.apply()
        edit.rollback()
        assert_true(
            snapshot(scene) == before_edit,
            "실패한 공용 편집 transaction이 이전 Revision을 복원하지 못했습니다.",
        )

        committed_edit = begin_edit_commit(
            scene,
            dataset_uuid="dataset-a",
            camera_count=1,
            image_count=1,
            point3d_count=1,
        )
        committed_edit.apply()
        committed_edit.complete()
        assert_true(
            snapshot(scene).dataset_revision == 3
            and snapshot(scene).point3d_count == 1,
            "확정 편집이 공용 Dataset Revision과 수량을 갱신하지 못했습니다.",
        )

        display_revision = snapshot(scene).display_revision
        scene.colmap_dataset.point_size = 0.125
        assert_true(
            snapshot(scene).display_revision == display_revision + 1
            and abs(modifier_settings.point_size - 0.125) < 1.0e-8,
            "공용 표시 변경이 Revision과 Modifier UI에 반영되지 않았습니다.",
        )

        metadata_b = _metadata(root / "b", "dataset-b", point_count=5)
        modifier_settings.dataset_uuid = metadata_b.dataset_uuid
        replacement = begin_import_commit(scene, metadata_b)
        replacement.apply()
        replacement.complete()
        replacement_snapshot = snapshot(scene)
        assert_true(
            replacement_snapshot.dataset_uuid == "dataset-b"
            and replacement_snapshot.dataset_revision == 1
            and replacement_snapshot.point3d_count == 5,
            "다른 Source가 새 UUID와 초기 Revision으로 교체되지 않았습니다.",
        )
finally:
    blender_colmap.unregister()
    unregister_shared_dataset()

assert_true(
    not hasattr(bpy.types.Scene, "colmap_dataset"),
    "공용 Dataset Scene Property가 해제 뒤 남았습니다.",
)
print("COLMAP_SHARED_DATASET_REGISTRY_BACKGROUND_OK")
