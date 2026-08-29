"""Blender Background에서 Camera Review 배경 연결과 사용자 슬롯 보존을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender.camera_import import (
    CameraImportJob,
    CameraImportRequest,
    _complete_import,
)
from tools.projection_paint.blender.camera_review import (
    PP_OT_review_background_toggle,
    clear_camera_review_runtime,
    look_through_current_camera,
    sync_camera_review,
)
from tools.projection_paint.blender.camera_scene import (
    _repair_timer,
    runtime_for_scene,
)
from shared.blender_runtime.scene_marker_session import (
    acquire_scene_marker_session,
    release_scene_marker_session,
)


def _save_test_image(path: Path, color: tuple[float, float, float, float]) -> None:
    """지정 경로에 64×32 PNG 테스트 이미지를 저장합니다."""

    image = bpy.data.images.new(path.stem, width=64, height=32, alpha=True)
    image.generated_color = color
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


def _request(images: Path) -> CameraImportRequest:
    """같은 Camera Data를 공유하는 연속 Frame 두 개의 Import 요청을 만듭니다."""

    camera = SimpleNamespace(
        camera_id=7,
        model_name="PINHOLE",
        width=64,
        height=32,
        params=(40.0, 40.0, 32.0, 16.0),
    )
    records = (
        SimpleNamespace(
            image_id=10,
            camera_id=7,
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(0.0, 0.0, 0.0),
            name="a.png",
        ),
        SimpleNamespace(
            image_id=30,
            camera_id=7,
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(1.0, 0.0, 0.0),
            name="b.png",
        ),
    )
    return CameraImportRequest(
        dataset_uuid="camera-review-test",
        dataset_revision=1,
        source_fingerprint="camera-review-fingerprint",
        cameras={7: camera},
        images=records,
        frame_start=1,
        axis_preset="BLENDER",
        images_root=str(images),
        image_paths={
            10: str(images / "a.png"),
            30: str(images / "b.png"),
        },
    )


class CameraReviewBackgroundTests(unittest.TestCase):
    """프레임별 배경 교체와 표시 설정의 Blender 저장 상태를 검증합니다."""

    def setUp(self) -> None:
        """빈 파일과 유효한 두 Source Image를 준비합니다."""

        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.register()
        self.directory = tempfile.TemporaryDirectory()
        images = Path(self.directory.name)
        _save_test_image(images / "a.png", (1.0, 0.0, 0.0, 1.0))
        _save_test_image(images / "b.png", (0.0, 1.0, 0.0, 1.0))
        self.scene = bpy.context.scene
        acquire_scene_marker_session(self.scene, "projection_paint")
        CameraImportJob(self.scene, _request(images)).run_to_completion()
        settings = self.scene.projection_paint
        settings.image_path_manifest_json = json.dumps(
            {"10": str(images / "a.png"), "30": str(images / "b.png")}
        )

    def tearDown(self) -> None:
        """Extension과 임시 Source Image 파일을 정리합니다."""

        release_scene_marker_session(self.scene, "projection_paint")
        addon.unregister()
        self.directory.cleanup()

    def test_background_defaults_are_hidden_and_opaque(self) -> None:
        """새 Scene의 Camera Review 배경 기본값이 OFF와 Alpha 1인지 검증합니다."""

        settings = self.scene.projection_paint
        self.assertEqual(settings.review_background_alpha, 1.0)
        self.assertFalse(settings.review_background_enabled)

    def test_frame_switch_reuses_owned_slot_and_preserves_user_slot(self) -> None:
        """Frame 전환이 Review 슬롯만 재사용하고 사용자 배경 슬롯은 보존합니다."""

        settings = self.scene.projection_paint
        settings.review_background_enabled = True
        runtime = runtime_for_scene(self.scene)
        camera_data = next(iter(runtime.camera_data.values()))
        user_image = bpy.data.images.new("USER_BACKGROUND", width=8, height=8)
        user_slot = camera_data.background_images.new()
        user_slot.image = user_image
        user_slot.alpha = 0.75
        user_pointer = user_slot.as_pointer()

        self.assertTrue(sync_camera_review(self.scene))
        review_pointer = camera_data.background_images[
            settings.review_background_slot_index
        ].as_pointer()
        self.assertEqual(settings.review_background_image_id, 10)
        self.assertEqual(len(camera_data.background_images), 2)
        review_slot = camera_data.background_images[
            settings.review_background_slot_index
        ]
        self.assertAlmostEqual(review_slot.alpha, 1.0)

        settings.review_background_alpha = 0.25
        review_slot = camera_data.background_images[
            settings.review_background_slot_index
        ]
        self.assertAlmostEqual(review_slot.alpha, 0.25)
        settings.review_background_enabled = False
        self.assertFalse(review_slot.show_background_image)
        settings.review_background_enabled = True
        self.assertTrue(review_slot.show_background_image)

        self.scene.frame_current = 2
        runtime.pending_frame = 2
        _repair_timer()
        review_slot = camera_data.background_images[
            settings.review_background_slot_index
        ]
        self.assertEqual(review_slot.as_pointer(), review_pointer)
        self.assertEqual(settings.review_background_image_id, 30)
        self.assertEqual(Path(review_slot.image.filepath).name, "b.png")
        self.assertAlmostEqual(review_slot.alpha, 0.25)
        self.assertEqual(len(camera_data.background_images), 2)
        self.assertIn(
            user_pointer,
            {slot.as_pointer() for slot in camera_data.background_images},
        )
        self.assertIs(user_slot.image, user_image)
        self.assertAlmostEqual(user_slot.alpha, 0.75)

    def test_saved_slot_state_recovers_after_runtime_cache_clear(self) -> None:
        """Runtime Cache 삭제 뒤 Blender Pointer와 Index로 같은 슬롯을 복구합니다."""

        self.scene.projection_paint.review_background_enabled = True
        self.assertTrue(sync_camera_review(self.scene))
        settings = self.scene.projection_paint
        camera_data = settings.review_background_camera_data
        slot = camera_data.background_images[settings.review_background_slot_index]
        pointer = slot.as_pointer()

        clear_camera_review_runtime()
        settings.review_background_alpha = 0.4

        restored = camera_data.background_images[settings.review_background_slot_index]
        self.assertEqual(restored.as_pointer(), pointer)
        self.assertAlmostEqual(restored.alpha, 0.4)

    def test_camera_view_toggle_requires_projection_workspace(self) -> None:
        """Camera View 단축키가 Projection Paint Workspace에서만 동작하는지 검증합니다."""

        settings = self.scene.projection_paint
        paint_workspace = object()
        foreign_workspace = object()
        poll_scene = SimpleNamespace(
            projection_paint=SimpleNamespace(
                paint_workspace=paint_workspace,
            )
        )
        camera_context = SimpleNamespace(
            scene=self.scene,
            window=SimpleNamespace(workspace=paint_workspace),
            area=SimpleNamespace(type="VIEW_3D"),
            space_data=SimpleNamespace(
                region_3d=SimpleNamespace(view_perspective="CAMERA")
            ),
        )
        projection_context = SimpleNamespace(
            scene=poll_scene,
            window=SimpleNamespace(workspace=paint_workspace),
            area=SimpleNamespace(type="VIEW_3D"),
            space_data=SimpleNamespace(
                region_3d=SimpleNamespace(view_perspective="CAMERA")
            ),
        )
        foreign_context = SimpleNamespace(
            scene=poll_scene,
            window=SimpleNamespace(workspace=foreign_workspace),
            area=SimpleNamespace(type="VIEW_3D"),
            space_data=SimpleNamespace(
                region_3d=SimpleNamespace(view_perspective="CAMERA")
            ),
        )
        perspective_context = SimpleNamespace(
            scene=poll_scene,
            window=SimpleNamespace(workspace=paint_workspace),
            area=SimpleNamespace(type="VIEW_3D"),
            space_data=SimpleNamespace(
                region_3d=SimpleNamespace(view_perspective="PERSP")
            ),
        )
        operator = SimpleNamespace(report=lambda *_args: None)

        self.assertTrue(PP_OT_review_background_toggle.poll(projection_context))
        self.assertFalse(PP_OT_review_background_toggle.poll(foreign_context))
        self.assertFalse(PP_OT_review_background_toggle.poll(perspective_context))
        self.assertFalse(settings.review_background_enabled)
        self.assertEqual(
            PP_OT_review_background_toggle.execute(operator, camera_context),
            {"FINISHED"},
        )
        self.assertTrue(settings.review_background_enabled)
        self.assertEqual(
            PP_OT_review_background_toggle.execute(operator, camera_context),
            {"FINISHED"},
        )
        self.assertFalse(settings.review_background_enabled)

    def test_look_through_uses_current_frame_camera_in_current_viewport(self) -> None:
        """Look Through가 현재 Frame Camera와 호출 Viewport만 Camera View로 전환합니다."""

        runtime = runtime_for_scene(self.scene)
        entry = runtime.entries_by_frame[int(self.scene.frame_current)]
        expected_camera = runtime.objects[entry.camera_uuid]
        rv3d = SimpleNamespace(view_perspective="PERSP")
        space = SimpleNamespace(type="VIEW_3D", region_3d=rv3d, camera=None)
        redraws = []
        area = SimpleNamespace(
            type="VIEW_3D",
            tag_redraw=lambda: redraws.append(True),
        )
        context = SimpleNamespace(
            scene=self.scene,
            area=area,
            space_data=space,
            window_manager=None,
        )

        image_id = look_through_current_camera(context)

        self.assertEqual(image_id, entry.image_id)
        self.assertIs(self.scene.camera, expected_camera)
        self.assertEqual(self.scene.render.resolution_x, 64)
        self.assertEqual(self.scene.render.resolution_y, 32)
        self.assertEqual(self.scene.render.resolution_percentage, 100)
        self.assertIs(space.camera, expected_camera)
        self.assertEqual(rv3d.view_perspective, "CAMERA")
        self.assertTrue(redraws)

    def test_import_completion_runs_initial_look_through_once(self) -> None:
        """Import 완료가 현재 Camera와 원본 해상도를 호출 Viewport에 즉시 적용합니다."""

        rv3d = SimpleNamespace(view_perspective="PERSP")
        space = SimpleNamespace(type="VIEW_3D", region_3d=rv3d, camera=None)
        overlay = SimpleNamespace(show_relationship_lines=True)
        window_space = SimpleNamespace(overlay=overlay)
        redraws = []
        viewport_area = SimpleNamespace(
            type="VIEW_3D",
            spaces=SimpleNamespace(active=window_space),
            tag_redraw=lambda: redraws.append(True),
        )
        context = SimpleNamespace(
            scene=self.scene,
            area=SimpleNamespace(
                type="VIEW_3D",
                tag_redraw=lambda: redraws.append(True),
            ),
            space_data=space,
            window_manager=None,
            window=SimpleNamespace(
                screen=SimpleNamespace(areas=(viewport_area,)),
            ),
        )
        session = SimpleNamespace(
            result=SimpleNamespace(camera_count=2, point_count=0),
            stage="COMMIT",
        )
        self.scene.render.resolution_x = 1920
        self.scene.render.resolution_y = 1080

        _complete_import(context, session)

        self.assertEqual(session.stage, "DONE")
        self.assertIs(space.camera, self.scene.camera)
        self.assertEqual(rv3d.view_perspective, "CAMERA")
        self.assertEqual(
            (self.scene.render.resolution_x, self.scene.render.resolution_y),
            (64, 32),
        )
        self.assertTrue(redraws)
        self.assertFalse(overlay.show_relationship_lines)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
