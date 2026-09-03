"""Modifier→Projection Painting 값 Snapshot 계약을 검증합니다."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from shared.colmap_transfer.contracts import (
    TRANSFER_SCHEMA_VERSION,
    CameraValue,
    ColmapTransferSnapshot,
    ImageValue,
    Point3DValue,
    SnapshotContentFingerprintBuilder,
    TransferDisplayDefaults,
    snapshot_content_fingerprint,
    validate_snapshot,
)
from shared.colmap_transfer.registry import (
    build_snapshot,
    provider_available,
    register_provider,
    unregister_provider,
    validate_open_provider_snapshot,
    validate_provider_snapshot,
)


DOMAIN_UUID = "11111111-1111-4111-8111-111111111111"
DATASET_UUID = "22222222-2222-4222-8222-222222222222"
SOURCE_FINGERPRINT = "a" * 64
HISTORY_HASH = "b" * 64


def _snapshot(root: Path, revision: int = 0) -> ColmapTransferSnapshot:
    """테스트용 최소 PINHOLE Snapshot을 생성합니다."""

    cameras = (
        CameraValue(7, 1, "PINHOLE", 1920, 1080, (1000.0, 1001.0, 960.0, 540.0)),
    )
    images = (
        ImageValue(11, 7, "frame.png", (1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 2.0), str(root / "frame.png")),
    )
    points = (Point3DValue(99, (1.0, 2.0, 3.0), (10, 20, 30), 0.25),)
    fingerprint = snapshot_content_fingerprint(
        source_domain_uuid=DOMAIN_UUID,
        source_domain_generation=revision,
        source_dataset_uuid=DATASET_UUID,
        source_revision=revision,
        source_fingerprint=SOURCE_FINGERPRINT,
        source_history_hash=HISTORY_HASH,
        cameras=cameras,
        images=images,
        points3d=points,
    )
    return ColmapTransferSnapshot(
        schema_version=TRANSFER_SCHEMA_VERSION,
        source_domain_uuid=DOMAIN_UUID,
        source_domain_generation=revision,
        source_dataset_uuid=DATASET_UUID,
        source_revision=revision,
        source_fingerprint=SOURCE_FINGERPRINT,
        source_history_hash=HISTORY_HASH,
        content_fingerprint=fingerprint,
        model_directory=str(root),
        images_directory=str(root),
        cameras=cameras,
        images=images,
        points3d=points,
        display_defaults=TransferDisplayDefaults(
            "REALITY_CAPTURE", "Z", "-Y", 0.05, 1.0
        ),
    )


class ColmapTransferContractTests(unittest.TestCase):
    """전송값 불변성과 제공자 Revision 검증을 확인합니다."""

    def tearDown(self) -> None:
        """테스트 제공자를 다음 테스트 전에 해제합니다."""

        unregister_provider("modifier")

    def test_snapshot_is_immutable_and_contains_no_runtime_fields(self) -> None:
        """전송 모델이 frozen이고 Blender ID·Runtime 필드를 노출하지 않는지 확인합니다."""

        with TemporaryDirectory() as directory:
            snapshot = validate_snapshot(_snapshot(Path(directory).resolve()))
        with self.assertRaises(FrozenInstanceError):
            snapshot.source_revision = 2
        field_names = {field.name for field in fields(snapshot)}
        self.assertFalse(
            field_names & {"runtime", "scene", "object", "camera_object", "blender_id"}
        )
        self.assertEqual(snapshot.image_paths[11], snapshot.images[0].image_path)
        with self.assertRaises(TypeError):
            snapshot.image_paths[12] = "forbidden"

    def test_provider_registration_and_revision_change_detection(self) -> None:
        """등록·해제와 생성 뒤 Revision 변경 감지를 검증합니다."""

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = SimpleNamespace(revision=3)

            def provider(_context):
                """현재 테스트 Revision의 Snapshot을 반환합니다."""

                return _snapshot(root, state.revision)

            def validator(_context, snapshot):
                """생성 시 Revision과 현재 테스트 Revision을 비교합니다."""

                return snapshot.source_revision == state.revision

            register_provider("modifier", provider, validator)
            self.assertTrue(provider_available("modifier"))
            snapshot = build_snapshot(object())
            self.assertTrue(validate_provider_snapshot(object(), snapshot))
            state.revision += 1
            self.assertFalse(validate_provider_snapshot(object(), snapshot))
            unregister_provider("modifier")
            self.assertFalse(provider_available("modifier"))

    def test_non_contiguous_ids_and_simple_pinhole_are_preserved(self) -> None:
        """비연속 ID와 SIMPLE_PINHOLE intrinsics를 값 그대로 보존합니다."""

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cameras = (
                CameraValue(2, 0, "SIMPLE_PINHOLE", 800, 600, (500.0, 400.0, 300.0)),
                CameraValue(19, 1, "PINHOLE", 1200, 900, (700.0, 701.0, 600.0, 450.0)),
            )
            images = (
                ImageValue(4, 19, "b.png", (1.0, 0.0, 0.0, 0.0), (3.0, 2.0, 1.0), str(root / "b.png")),
            )
            fingerprint = snapshot_content_fingerprint(
                source_domain_uuid=DOMAIN_UUID,
                source_domain_generation=8,
                source_dataset_uuid=DATASET_UUID,
                source_revision=8,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_history_hash=HISTORY_HASH,
                cameras=cameras,
                images=images,
                points3d=(),
            )
            snapshot = ColmapTransferSnapshot(
                schema_version=TRANSFER_SCHEMA_VERSION,
                source_domain_uuid=DOMAIN_UUID,
                source_domain_generation=8,
                source_dataset_uuid=DATASET_UUID,
                source_revision=8,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_history_hash=HISTORY_HASH,
                content_fingerprint=fingerprint,
                model_directory=str(root),
                images_directory=str(root),
                cameras=cameras,
                images=images,
                points3d=(),
                display_defaults=TransferDisplayDefaults("BLENDER", "Y", "Z", 0.1, 2.0),
            )
        self.assertEqual(tuple(item.camera_id for item in snapshot.cameras), (2, 19))
        self.assertEqual(snapshot.images[0].camera_id, 19)
        self.assertEqual(snapshot.images[0].tvec, (3.0, 2.0, 1.0))

    def test_incremental_fingerprint_matches_legacy_json_bytes(self) -> None:
        """증분 Builder가 기존 단일 JSON 직렬화와 같은 SHA-256을 만드는지 확인합니다."""

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            snapshot = _snapshot(root, 4)
        payload = {
            "source_domain_uuid": snapshot.source_domain_uuid,
            "source_domain_generation": snapshot.source_domain_generation,
            "source_dataset_uuid": snapshot.source_dataset_uuid,
            "source_revision": snapshot.source_revision,
            "source_fingerprint": snapshot.source_fingerprint,
            "source_history_hash": snapshot.source_history_hash,
            "cameras": [
                tuple(getattr(item, field.name) for field in fields(item))
                for item in snapshot.cameras
            ],
            "images": [
                tuple(getattr(item, field.name) for field in fields(item))
                for item in snapshot.images
            ],
            "points3d": [
                tuple(getattr(item, field.name) for field in fields(item))
                for item in snapshot.points3d
            ],
        }
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        builder = SnapshotContentFingerprintBuilder(
            source_domain_uuid=snapshot.source_domain_uuid,
            source_domain_generation=snapshot.source_domain_generation,
            source_dataset_uuid=snapshot.source_dataset_uuid,
            source_revision=snapshot.source_revision,
            source_fingerprint=snapshot.source_fingerprint,
            source_history_hash=snapshot.source_history_hash,
        )
        for item in snapshot.cameras:
            builder.add_camera(item)
        builder.finish_cameras()
        for item in snapshot.images:
            builder.add_image(item)
        builder.finish_images()
        for item in snapshot.points3d:
            builder.add_point3d(item)
        self.assertEqual(expected, builder.finish())
        self.assertEqual(expected, snapshot.content_fingerprint)

    def test_open_handle_validation_uses_exact_immutable_result(self) -> None:
        """증분 Handle 검증이 결과 객체 동일성과 현재 Provider token을 함께 확인합니다."""

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            snapshot = _snapshot(root, 2)
            other = _snapshot(root, 3)
        handle = SimpleNamespace(done=True, failed=False, result=snapshot)

        def provider(_context):
            """동기 호환 등록에 동일 Snapshot을 반환합니다."""

            return snapshot

        def validator(_context, candidate):
            """Revision 2인 현재 Snapshot만 유효한 것으로 판정합니다."""

            return candidate.source_revision == 2

        register_provider("modifier", provider, validator)
        self.assertTrue(validate_open_provider_snapshot(object(), handle, snapshot))
        self.assertFalse(validate_open_provider_snapshot(object(), handle, other))
        handle.failed = True
        self.assertFalse(validate_open_provider_snapshot(object(), handle, snapshot))


if __name__ == "__main__":
    unittest.main()
