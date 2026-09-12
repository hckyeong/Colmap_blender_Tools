"""확정 변환의 동기·증분 Painter 전달과 미확정 상태 차단을 검증합니다."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.modifier.blender.projection_snapshot_provider import (
    ProjectionSnapshotProviderHandle, build_projection_snapshot,
)
from tools.modifier.core.model import CameraRecord, ImageRecord, Point3DRecord, Reconstruction


class TransformSnapshotTests(unittest.TestCase):
    """출처가 고정된 값 복사에 데이터셋 변환이 정확히 한 번 적용되는지 확인합니다."""

    def setUp(self):
        """외부 파일 접근 없이 알려진 카메라와 변환을 가진 Domain을 준비합니다."""
        root = Path.cwd()
        self.state = SimpleNamespace(
            dataset_uuid="22222222-2222-4222-8222-222222222222",
            edit_revision=1, history_hash="b" * 64, point_id_order=(9,),
            dataset_transform=((0., -2., 0., 4.), (2., 0., 0., 5.), (0., 0., 2., 6.), (0., 0., 0., 1.)),
            current_reconstruction=Reconstruction(
                cameras={1: CameraRecord(1, 1, "PINHOLE", 800, 600, (500., 500., 400., 300.))},
                images={2: ImageRecord(2, (1., 0., 0., 0.), (0., 0., 0.), 1, "a.png", ())},
                points3d={9: Point3DRecord(9, (1., 2., 3.), (1, 2, 3), .25, ())},
            ),
            source=SimpleNamespace(source_fingerprint="a" * 64, model_directory=root, images_directory=root),
            image_path_manifest=SimpleNamespace(entries={2: SimpleNamespace(resolved_image_path=root / "a.png")}),
        )
        self.scene = SimpleNamespace(colmap_import=SimpleNamespace(
            dataset_uuid=self.state.dataset_uuid, axis_preset="BLENDER", axis_forward="Y", axis_up="Z",
            point_size=2., camera_frustum_size=1.,
        ))
        self.domain = SimpleNamespace(
            dataset_state=self.state, transform_preview_active=False, require_editable=lambda: None,
            identity=SimpleNamespace(domain_uuid="11111111-1111-4111-8111-111111111111", domain_generation=1),
        )

    def test_sync_and_incremental_transfer_apply_same_transform_once(self):
        """두 전달 경로의 값과 지문이 같고 기준 데이터는 보존됩니다."""
        prefix = "tools.modifier.blender.projection_snapshot_provider."
        with patch(prefix + "ensure_runtime", return_value=self.state), patch(
            prefix + "domain_for_scene", return_value=self.domain
        ), patch(prefix + "runtime_restore_token", return_value=object()):
            sync = build_projection_snapshot(self.scene)
            handle = ProjectionSnapshotProviderHandle(self.scene)
            while not handle.done:
                handle.advance(1.)
        self.assertEqual(handle.result, sync)
        self.assertEqual(sync.points3d[0].xyz, (0., 7., 12.))
        self.assertEqual(sync.images[0].tvec, (-5., 4., -6.))
        self.assertEqual(sync.cameras[0].params, (500., 500., 400., 300.))
        self.assertEqual(self.state.current_reconstruction.points3d[9].xyz, (1., 2., 3.))
        self.assertEqual(self.state.current_reconstruction.images[2].tvec, (0., 0., 0.))

    def test_preview_rejects_both_transfer_entry_points(self):
        """확정 전 조작 중에는 이전 확정 값의 새 전달도 시작하지 않습니다."""
        self.domain.transform_preview_active = True
        prefix = "tools.modifier.blender.projection_snapshot_provider."
        with patch(prefix + "ensure_runtime", return_value=self.state), patch(
            prefix + "domain_for_scene", return_value=self.domain
        ), patch(prefix + "runtime_restore_token", return_value=object()):
            for entry in (build_projection_snapshot, ProjectionSnapshotProviderHandle):
                with self.subTest(entry=entry.__name__), self.assertRaises(ValueError):
                    entry(self.scene)

    def test_revision_change_during_transfer_rejects_new_snapshot(self):
        """증분 전달 중 Domain이 바뀌면 부분 결과를 확정하지 않습니다."""
        prefix = "tools.modifier.blender.projection_snapshot_provider."
        with patch(prefix + "domain_for_scene", return_value=self.domain), patch(
            prefix + "runtime_restore_token", return_value=object()
        ):
            handle = ProjectionSnapshotProviderHandle(self.scene)
            handle.advance(1.)
            self.domain.identity = SimpleNamespace(domain_uuid="11111111-1111-4111-8111-111111111111", domain_generation=2)
            with self.assertRaises(ValueError):
                handle.advance(1.)
            self.assertIsNone(handle.result)


if __name__ == "__main__":
    unittest.main()
