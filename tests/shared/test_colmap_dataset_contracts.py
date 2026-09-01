"""공용 COLMAP Dataset 순수 계약의 불변 조건을 검증합니다."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from shared.colmap_dataset.contracts import (
    DATASET_SCHEMA_VERSION,
    DatasetImportMetadata,
    DatasetSnapshot,
)


class ColmapDatasetContractTests(unittest.TestCase):
    """Blender 없이 공용 Import 입력과 Snapshot 계약을 검증합니다."""

    def _metadata(self, root: Path, **changes) -> DatasetImportMetadata:
        """각 테스트가 필요한 필드만 바꿀 수 있는 유효 메타데이터를 만듭니다."""

        values = {
            "dataset_uuid": "12345678-1234-5678-1234-567812345678",
            "source_fingerprint": "a" * 64,
            "dataset_root": str(root),
            "sparse_root": str(root / "sparse"),
            "model_directory": str(root / "sparse" / "0"),
            "images_directory": str(root / "images"),
            "input_format": "text",
            "layout": "legacy",
            "selected_files_json": "[]",
            "image_path_manifest_json": "[]",
            "camera_count": 1,
            "image_count": 2,
            "point3d_count": 3,
            "source_schema_version": 2,
            "axis_preset": "REALITY_CAPTURE",
            "axis_forward": "Z",
            "axis_up": "-Y",
            "point_size": 0.05,
            "camera_frustum_size": 1.0,
        }
        values.update(changes)
        return DatasetImportMetadata(**values)

    def test_import_metadata_accepts_lossless_source_counts(self) -> None:
        """공용 계약이 Source 경로·형식·수량과 표시값을 손실 없이 보존합니다."""

        with tempfile.TemporaryDirectory() as directory:
            metadata = self._metadata(Path(directory).resolve())
        self.assertEqual("text", metadata.input_format)
        self.assertEqual((1, 2, 3), (
            metadata.camera_count,
            metadata.image_count,
            metadata.point3d_count,
        ))
        self.assertEqual(("Z", "-Y"), (metadata.axis_forward, metadata.axis_up))

    def test_import_metadata_rejects_invalid_axis_and_counts(self) -> None:
        """같은 축과 음수 수량은 Scene commit 전에 거부합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(ValueError):
                self._metadata(root, axis_forward="Z", axis_up="-Z")
            with self.assertRaises(ValueError):
                self._metadata(root, point3d_count=-1)

    def test_snapshot_availability_requires_neutral_schema_and_source(self) -> None:
        """UUID·Source·Schema가 완전한 Snapshot만 활성 Dataset으로 판정합니다."""

        values = {
            "dataset_uuid": "dataset",
            "dataset_revision": 1,
            "display_revision": 1,
            "source_fingerprint": "fingerprint",
            "dataset_root": "C:/dataset",
            "sparse_root": "C:/dataset/sparse",
            "model_directory": "C:/dataset/sparse/0",
            "images_directory": "C:/dataset/images",
            "input_format": "text",
            "layout": "legacy",
            "selected_files_json": "[]",
            "image_path_manifest_json": "[]",
            "camera_count": 1,
            "image_count": 1,
            "point3d_count": 1,
            "schema_version": DATASET_SCHEMA_VERSION,
            "source_schema_version": 2,
            "axis_preset": "REALITY_CAPTURE",
            "axis_forward": "Z",
            "axis_up": "-Y",
            "point_size": 0.05,
            "camera_frustum_size": 1.0,
        }
        self.assertTrue(DatasetSnapshot(**values).available)
        values["schema_version"] = DATASET_SCHEMA_VERSION + 1
        self.assertFalse(DatasetSnapshot(**values).available)


if __name__ == "__main__":
    unittest.main()
