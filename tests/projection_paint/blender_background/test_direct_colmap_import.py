"""실제 Blender에서 파일 기반 Painter 입력의 확정과 복구를 검증합니다."""
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import sys
import unittest

import bpy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "projection_paint" / "core"))
from support import write_layout
from tools import projection_paint as addon
from tools.projection_paint.core.colmap.model import CameraRecord, ImagePoseRecord, Point3DRecord
from tools.projection_paint.blender.core_adapter import build_camera_import_request, CameraImportRequestJob
from tools.projection_paint.blender.camera_import import CameraImportJob, PP_OT_import_colmap, PP_OT_select_colmap_directory
from tools.projection_paint.blender.camera_scene import scene_manifest, clear_camera_runtime
from tools.projection_paint.blender.snapshot_update import snapshot_is_ready
from shared.blender_runtime.scene_marker_session import acquire_scene_marker_session, release_scene_marker_session


class DirectFileImportTests(unittest.TestCase):
    """공급자 없이 TXT와 BIN을 읽고 독립 스냅샷을 생성합니다."""

    @classmethod
    def setUpClass(cls):
        """Painter를 독립 테스트 프로세스에 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls):
        """등록된 Painter와 세션을 해제합니다."""
        addon.unregister()

    def setUp(self):
        """각 테스트에 임시 원본과 독립 장면을 만듭니다."""
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scene = bpy.data.scenes.new("직접 파일 입력 검증")
        self.context = SimpleNamespace(scene=self.scene)
        acquire_scene_marker_session(self.scene, "projection_paint")
        self.settings = self.scene.projection_paint
        self.settings.camera_axis_preset = "BLENDER"

    def tearDown(self):
        """현재 테스트의 장면과 임시 파일을 정리합니다."""
        clear_camera_runtime()
        release_scene_marker_session(self.scene, "projection_paint")
        bpy.data.scenes.remove(self.scene)
        self.temp.cleanup()

    def _source(self, format_name="TEXT", model="PINHOLE"):
        """실제 이미지와 순서가 섞인 최소 COLMAP 파일을 생성합니다."""
        sparse = self.root / format_name / "sparse"
        images_root = self.root / format_name / "images"
        images_root.mkdir(parents=True, exist_ok=True)
        camera = CameraRecord(1, 1 if model == "PINHOLE" else 2, model, 32, 24, (20.0, 20.0, 16.0, 12.0) if model == "PINHOLE" else (20.0, 16.0, 12.0, 0.1))
        images = tuple(ImagePoseRecord(i, 1, (1.0, 0.0, 0.0, 0.0), (float(i), 0.0, 0.0), f"{i}.png") for i in (20, 5))
        point = Point3DRecord(7, (1.0, 2.0, 3.0), (12, 34, 56), 0.2, ())
        write_layout(sparse, format_name, cameras=(camera,), images=images, points3d=(point,))
        image = bpy.data.images.new("입력 검증 이미지", width=32, height=24)
        try:
            for record in images:
                image.filepath_raw = str(images_root / record.image_name)
                image.file_format = "PNG"
                image.save()
        finally:
            bpy.data.images.remove(image)
        self.settings.dataset_root = ""
        self.settings.sparse_directory = str(sparse)
        self.settings.images_directory = str(images_root)
        self.settings.input_format = format_name
        return sparse

    def _import(self):
        """파일 준비와 원자적 장면 확정을 직접 실행합니다."""
        request = build_camera_import_request(self.context)
        result = CameraImportJob(self.scene, request).run_to_completion()
        return request, result

    def test_text_and_binary_ready_frame_order_and_ownership(self):
        """TXT와 BIN이 준비 상태·출처·프레임 순서·전용 점군을 구성합니다."""
        for format_name in ("TEXT", "BINARY"):
            self._source(format_name)
            request, result = self._import()
            self.assertEqual("FILE", self.settings.source_kind)
            self.assertEqual("", self.settings.source_modifier_dataset_uuid)
            self.assertTrue(snapshot_is_ready(self.scene))
            self.assertEqual([5, 20], [item.image_id for item in scene_manifest(self.scene).entries])
            self.assertEqual(1, result.point_count)
            self.assertFalse(isinstance(request.points, (tuple, list)))
            self.assertTrue(self.settings.snapshot_created_at)

    def test_unsupported_camera_preserves_snapshot_and_layers(self):
        """왜곡 카메라 거부 시 기존 장면과 레이어를 유지합니다."""
        self._source()
        self._import()
        before = self.settings.dataset_manifest_json
        layer = self.settings.layers.add()
        layer.layer_uuid = "기존 레이어"
        self._source("BINARY", "SIMPLE_RADIAL")
        with self.assertRaisesRegex(Exception, "지원하지 않는"):
            build_camera_import_request(self.context)
        self.assertEqual(before, self.settings.dataset_manifest_json)
        self.assertEqual("기존 레이어", self.settings.layers[0].layer_uuid)
        self.assertTrue(snapshot_is_ready(self.scene))

    def test_source_change_abort_and_fault_preserve_previous_snapshot(self):
        """취소·원본 변경·확정 실패가 이전 카메라와 점군을 보존합니다."""
        sparse = self._source()
        self._import()
        before = self.settings.dataset_manifest_json
        objects = set(obj.as_pointer() for obj in self.scene.objects)
        request = build_camera_import_request(self.context)
        job = CameraImportJob(self.scene, request)
        job.step(time_budget=1.0, hard_cap=1)
        job.abort()
        self.assertEqual(objects, set(obj.as_pointer() for obj in self.scene.objects))
        request = build_camera_import_request(self.context)
        with (sparse / "cameras.txt").open("a", encoding="utf-8") as stream:
            stream.write("# 원본 변경\n")
        with self.assertRaisesRegex(Exception, "식별자|내용|Fingerprint"):
            CameraImportJob(self.scene, request).run_to_completion()
        self.assertEqual(before, self.settings.dataset_manifest_json)
        request = build_camera_import_request(self.context)
        def fail(label):
            """확정 도중 장애를 주입합니다."""
            if label == "before_runtime":
                raise RuntimeError("확정 실패 검증")
        with self.assertRaisesRegex(RuntimeError, "확정 실패 검증"):
            CameraImportJob(self.scene, request, fault_injector=fail).run_to_completion()
        self.assertEqual(before, self.settings.dataset_manifest_json)
        self.assertEqual(objects, set(obj.as_pointer() for obj in self.scene.objects))
        self.assertTrue(snapshot_is_ready(self.scene))

    def test_model_axis_rna_defaults_are_independent(self):
        """모델 원본 축 RNA가 카메라 축과 독립적인 기본값을 제공합니다."""
        self.assertEqual(("BLENDER", "Y", "Z"), (self.settings.model_axis_preset, self.settings.model_forward, self.settings.model_up))
        self.settings.model_axis_preset = "CUSTOM"
        self.settings.model_forward = "-Z"
        self.assertEqual("BLENDER", self.settings.camera_axis_preset)

    def test_directory_browser_cancel_keeps_input_settings(self):
        """브라우저를 열고 점 상한까지 편집해도 취소하면 기존 입력 설정을 유지합니다."""
        self._source()
        self.settings.import_point_limit = 321
        before = (self.settings.dataset_root, self.settings.sparse_directory, self.settings.images_directory)
        operator = SimpleNamespace(target="SPARSE", directory="")
        opened = []
        context = SimpleNamespace(scene=self.scene, window_manager=SimpleNamespace(fileselect_add=opened.append))
        self.assertEqual({"RUNNING_MODAL"}, PP_OT_select_colmap_directory.invoke(operator, context, None))
        operator.directory = str(self.root / "취소한 선택")
        self.assertEqual(321, operator.point_limit)
        operator.point_limit = 123
        self.assertEqual([operator], opened)
        self.assertEqual(before, (self.settings.dataset_root, self.settings.sparse_directory, self.settings.images_directory))
        self.assertEqual(321, self.settings.import_point_limit)

    def test_reference_limit_metadata_and_mesh_attributes(self):
        """설정한 상한과 원본 메타데이터, 64비트 식별자 메시 기록을 검증합니다."""
        import json
        import numpy as np
        from support import write_points3d_text
        sparse = self._source()
        points = tuple(Point3DRecord((1 << 63) + index + 1, (float(index), 2.0, 3.0), (12, 64, 255), 0.25, ()) for index in range(51))
        write_points3d_text(sparse / "points3D.txt", points)
        self.assertEqual(20_000, self.settings.import_point_limit)
        self.settings.import_point_limit = 7
        request, result = self._import()
        self.assertEqual(7, result.point_count)
        metadata = json.loads(self.settings.reference_metadata_json)
        self.assertEqual((51, 7, 7), (metadata["source_point_count"], metadata["imported_point_count"], metadata["point_limit"]))
        self.assertEqual([50.0, 2.0, 3.0], metadata["bounds_max"])
        mesh = next(obj.data for obj in self.scene.objects if obj.get("pp_record_kind") == "POINT_MESH")
        low, high = np.empty(7, np.int32), np.empty(7, np.int32)
        mesh.attributes["colmap_point3d_id_low"].data.foreach_get("value", low)
        mesh.attributes["colmap_point3d_id_high"].data.foreach_get("value", high)
        ids = low.view(np.uint32).astype(np.uint64) | (high.view(np.uint32).astype(np.uint64) << np.uint64(32))
        np.testing.assert_array_equal(ids, [point.point3d_id for point in request.points])

    def test_directory_browser_draw_exposes_reference_limit(self):
        """파일 탐색기 옵션에 참고 점 상한을 표시하고 일반 폴더 선택에서는 숨깁니다."""
        calls = []
        layout = SimpleNamespace(prop=lambda _op, name: calls.append(name), label=lambda **kwargs: None)
        operator = SimpleNamespace(layout=layout, import_after_select=True)
        PP_OT_select_colmap_directory.draw(operator, self.context)
        self.assertEqual(["point_limit"], calls)
        operator.import_after_select = False
        PP_OT_select_colmap_directory.draw(operator, self.context)
        self.assertEqual(["point_limit"], calls)

    def test_directory_browser_confirm_uses_selected_limit(self):
        """파일 탐색기의 선택 확정값이 실제 가져온 점 수와 다음 입력 설정에 반영됩니다."""
        from support import write_points3d_text
        sparse = self._source()
        points = tuple(Point3DRecord(index + 1, (float(index), 0.0, 0.0), (32, 64, 128), 0.25, ()) for index in range(31))
        write_points3d_text(sparse / "points3D.txt", points)
        with bpy.context.temp_override(scene=self.scene):
            outcome = bpy.ops.projection_paint.select_colmap_directory(
                "EXEC_DEFAULT", directory=str(sparse.parent), target="ROOT", import_after_select=True, point_limit=5,
            )
        self.assertEqual({"FINISHED"}, outcome)
        self.assertEqual(5, self.settings.import_point_limit)
        self.assertEqual(5, self.settings.snapshot_point_count)

    def test_painting_blocks_input_replacement(self):
        """페인팅 중 새 파일 요청과 가져오기 실행을 차단합니다."""
        self._source()
        self.settings.paint_state = "ACTIVE"
        self.assertFalse(PP_OT_import_colmap.poll(self.context))
        with self.assertRaisesRegex(Exception, "페인팅"):
            CameraImportRequestJob(self.context)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
