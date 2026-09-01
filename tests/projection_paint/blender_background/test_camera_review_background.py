"""Blender Background에서 Camera Review 배경 연결과 사용자 슬롯 보존을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

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
    FRAME_SOURCE_DEBOUNCE_SECONDS,
    CameraSceneError,
    _frame_source_debounce_tick,
    _repair_timer,
    activate_managed_frame,
    clear_camera_runtime,
    queue_frame_change,
    runtime_for_scene,
)
from tools.projection_paint.blender import paint_runtime as paint_runtime_module
from tools.projection_paint.blender.paint_tool_bridge import active_tool_id
from tools.projection_paint.blender.paint_runtime import (
    reconnect_current_frame,
    refresh_frame_source,
)
from tools.projection_paint.blender.paint_session import (
    finish_paint_session,
    start_paint_session,
)
from tools.projection_paint.blender.stencil_session import start_stencil_session
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
        snapshot_schema_version=1,
        source_dataset_uuid="modifier-camera-review-test",
        source_revision=0,
        content_fingerprint="camera-review-fingerprint",
        snapshot_created_at="2026-08-29T00:00:00+00:00",
    )


def _new_paint_target():
    """Frame Source 실 Commit 회귀에 사용할 64×32 UV Paint Target을 만듭니다."""

    mesh = bpy.data.meshes.new("CameraDebouncePaintTargetMesh")
    mesh.from_pydata(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("CameraDebouncePaintTarget", mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new("CameraDebouncePaintMaterial")
    material.use_nodes = True
    target.data.materials.append(material)
    return target


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
        activate_managed_frame(self.scene, 2, refresh_source=False)
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

    def test_frame_source_debounce_commits_only_latest_of_twenty_changes(self) -> None:
        """0.30초 이내 20회 변경은 0회, 최종 대기 후는 1회만 Commit합니다."""

        runtime = runtime_for_scene(self.scene)
        base = 100.0
        with patch(
            "tools.projection_paint.blender.camera_scene._commit_pending_frame_source",
            side_effect=lambda current, frame, _serial: current.entries_by_frame[frame],
        ) as commit:
            for index in range(20):
                self.scene.frame_current = 1 + (index % 2)
                changed_at = base + index * 0.01
                queue_frame_change(self.scene, changed_at=changed_at)
                _frame_source_debounce_tick(now=changed_at)
            final_changed_at = base + 0.19
            _frame_source_debounce_tick(now=final_changed_at + 0.29)
            self.assertEqual(commit.call_count, 0)

            _frame_source_debounce_tick(
                now=final_changed_at + FRAME_SOURCE_DEBOUNCE_SECONDS
            )

        self.assertEqual(commit.call_count, 1)
        self.assertEqual(commit.call_args.args[1], 2)
        self.assertIsNone(runtime.pending_frame)

    def test_new_frame_restarts_debounce_wait(self) -> None:
        """대기 중 새 Frame이 들어오면 마지막 입력 시각부터 0.30초를 다시 기다립니다."""

        with patch(
            "tools.projection_paint.blender.camera_scene._commit_pending_frame_source",
            side_effect=lambda current, frame, _serial: current.entries_by_frame[frame],
        ) as commit:
            self.scene.frame_current = 1
            queue_frame_change(self.scene, changed_at=200.0)
            _frame_source_debounce_tick(now=200.29)
            self.scene.frame_current = 2
            queue_frame_change(self.scene, changed_at=200.29)
            _frame_source_debounce_tick(now=200.30)
            _frame_source_debounce_tick(now=200.58)
            self.assertEqual(commit.call_count, 0)

            _frame_source_debounce_tick(now=200.59)

        self.assertEqual(commit.call_count, 1)
        self.assertEqual(commit.call_args.args[1], 2)

    def test_source_failure_retries_twice_then_succeeds(self) -> None:
        """동일 최신 Source는 초기 실패 후 최대 2회만 재시도하고 성공 후 소비합니다."""

        runtime = runtime_for_scene(self.scene)
        entry = runtime.entries_by_frame[1]
        with patch(
            "tools.projection_paint.blender.camera_scene._commit_pending_frame_source",
            side_effect=(
                CameraSceneError("의도한 Source 실패 1"),
                CameraSceneError("의도한 Source 실패 2"),
                entry,
            ),
        ) as commit:
            self.scene.frame_current = 1
            queue_frame_change(self.scene, changed_at=300.0)
            _frame_source_debounce_tick(now=300.30)
            self.assertEqual(runtime.pending_frame_retry_count, 1)
            self.assertEqual(commit.call_count, 1)
            _frame_source_debounce_tick(now=runtime.pending_frame_next_attempt_at)
            self.assertEqual(runtime.pending_frame_retry_count, 2)
            self.assertEqual(commit.call_count, 2)
            _frame_source_debounce_tick(now=runtime.pending_frame_next_attempt_at)

        self.assertEqual(commit.call_count, 3)
        self.assertIsNone(runtime.pending_frame)
        self.assertEqual(runtime.pending_frame_retry_count, 0)
        self.assertEqual(runtime.pending_frame_error, "")

    def test_source_failure_keeps_pending_after_retry_limit(self) -> None:
        """최대 2회 재시도가 모두 실패해도 Pending과 최종 오류를 유지합니다."""

        runtime = runtime_for_scene(self.scene)
        with patch(
            "tools.projection_paint.blender.camera_scene._commit_pending_frame_source",
            side_effect=CameraSceneError("계속되는 Source 실패"),
        ) as commit:
            self.scene.frame_current = 1
            queue_frame_change(self.scene, changed_at=350.0)
            _frame_source_debounce_tick(now=350.30)
            _frame_source_debounce_tick(now=runtime.pending_frame_next_attempt_at)
            _frame_source_debounce_tick(now=runtime.pending_frame_next_attempt_at)
            self.assertIsNone(_frame_source_debounce_tick(now=351.0))

        self.assertEqual(commit.call_count, 3)
        self.assertEqual(runtime.pending_frame, 1)
        self.assertEqual(runtime.pending_frame_retry_count, 2)
        self.assertEqual(runtime.pending_frame_error, "계속되는 Source 실패")

    def test_new_frame_supersedes_old_retry(self) -> None:
        """재시도 대기 중 새 Frame이 오면 이전 Serial은 Commit하지 않습니다."""

        runtime = runtime_for_scene(self.scene)
        attempted_frames: list[int] = []

        def commit_side_effect(current, frame, _serial):
            """첫 Frame만 실패시키고 이후 최신 Frame을 성공 처리합니다."""

            attempted_frames.append(frame)
            if len(attempted_frames) == 1:
                raise CameraSceneError("의도한 이전 Frame 실패")
            return current.entries_by_frame[frame]

        with patch(
            "tools.projection_paint.blender.camera_scene._commit_pending_frame_source",
            side_effect=commit_side_effect,
        ):
            self.scene.frame_current = 1
            queue_frame_change(self.scene, changed_at=400.0)
            _frame_source_debounce_tick(now=400.30)
            old_retry_at = runtime.pending_frame_next_attempt_at
            self.scene.frame_current = 2
            queue_frame_change(self.scene, changed_at=400.31)
            _frame_source_debounce_tick(now=old_retry_at)
            self.assertEqual(attempted_frames, [1])
            _frame_source_debounce_tick(now=400.61)

        self.assertEqual(attempted_frames, [1, 2])
        self.assertIsNone(runtime.pending_frame)

    def test_normal_frame_changes_do_not_increment_guard_repairs(self) -> None:
        """정상 Frame Pending은 Camera Guard 복구 횟수에 절대 포함되지 않습니다."""

        runtime = runtime_for_scene(self.scene)
        self.scene.frame_current = 2
        queue_frame_change(self.scene, changed_at=500.0)

        for _index in range(7):
            _repair_timer()

        self.assertEqual(runtime.pending_frame, 2)
        self.assertEqual(runtime.repair_attempts, 0)
        self.assertEqual(runtime.error, "")

    def test_guard_counts_only_actual_camera_repair(self) -> None:
        """Camera Object 손상을 실제로 복구한 Tick만 Guard 횟수를 증가시킵니다."""

        runtime = runtime_for_scene(self.scene)
        entry = runtime.entries_by_frame[1]
        camera = runtime.objects[entry.camera_uuid]
        camera.lock_location = (False, False, False)
        runtime.dirty_objects.append(entry.camera_uuid)
        runtime.queued.add(("OBJECT", entry.camera_uuid))

        _repair_timer()

        self.assertEqual(tuple(camera.lock_location), (True, True, True))
        self.assertEqual(runtime.repair_attempts, 1)
        self.assertEqual(runtime.error, "")

    def test_real_debounce_commit_matches_camera_marker_and_source(self) -> None:
        """실제 Source Commit 후 Camera·Marker·IMAGE_ID·Texture Pointer가 최종 Frame과 일치합니다."""

        target = _new_paint_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        start_stencil_session(
            bpy.context,
            dataset_uuid=str(self.scene.projection_paint.dataset_uuid),
        )
        try:
            self.assertTrue(refresh_frame_source(self.scene))
            settings = self.scene.projection_paint
            paint_image = settings.active_paint_image
            brush = self.scene.tool_settings.image_paint.brush
            material = target.active_material
            paint_slot = int(material.paint_active_slot)
            tool_id = active_tool_id(bpy.context)
            active_layer_index = int(settings.active_layer_index)

            self.scene.frame_current = 2
            queue_frame_change(self.scene, changed_at=700.0)
            _frame_source_debounce_tick(now=700.30)

            runtime = runtime_for_scene(self.scene)
            entry = runtime.entries_by_frame[2]
            camera = runtime.objects[entry.camera_uuid]
            marker = runtime.markers[entry.image_id]
            self.assertIsNone(runtime.pending_frame)
            self.assertIs(self.scene.camera, camera)
            self.assertEqual(int(marker.frame), 2)
            self.assertIs(marker.camera, camera)
            self.assertEqual(int(settings.active_image_id), 30)
            self.assertEqual(str(settings.current_image_id), "30")
            self.assertEqual(str(settings.current_image_name), "b.png")
            self.assertEqual(
                Path(settings.stencil_source_image.filepath).name,
                "b.png",
            )
            self.assertIs(settings.stencil_texture.image, settings.stencil_source_image)
            self.assertIs(settings.active_paint_image, paint_image)
            self.assertIs(self.scene.tool_settings.image_paint.brush, brush)
            self.assertEqual(int(material.paint_active_slot), paint_slot)
            self.assertEqual(active_tool_id(bpy.context), tool_id)
            self.assertEqual(int(settings.active_layer_index), active_layer_index)
        finally:
            finish_paint_session(bpy.context)

    def test_explicit_reconnect_commits_immediately_and_consumes_pending(self) -> None:
        """명시적 Reconnect는 0.30초 대기를 건너뛰고 현재 Frame Source를 즉시 완료합니다."""

        target = _new_paint_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        start_stencil_session(
            bpy.context,
            dataset_uuid=str(self.scene.projection_paint.dataset_uuid),
        )
        try:
            self.assertTrue(refresh_frame_source(self.scene))
            self.scene.frame_current = 2
            queue_frame_change(self.scene, changed_at=800.0)
            runtime = runtime_for_scene(self.scene)
            self.assertEqual(runtime.pending_frame, 2)

            with patch.object(
                paint_runtime_module,
                "_projection_runtime_is_active",
                return_value=True,
            ):
                image_id = reconnect_current_frame(bpy.context)

            self.assertEqual(image_id, 30)
            self.assertEqual(int(self.scene.projection_paint.active_image_id), 30)
            self.assertIsNone(runtime.pending_frame)
            self.assertIsNone(_frame_source_debounce_tick(now=800.30))
        finally:
            finish_paint_session(bpy.context)

    def test_camera_runtime_clear_unregisters_debounce_timer(self) -> None:
        """Runtime 해제 경계에서 이전 Epoch Debounce Timer와 Pending을 함께 폐기합니다."""

        from tools.projection_paint.blender.lifecycle import lifecycle_registry

        self.scene.frame_current = 2
        queue_frame_change(self.scene, changed_at=600.0)
        registry = lifecycle_registry()
        self.assertIn(_frame_source_debounce_tick, registry._timers)

        clear_camera_runtime()

        self.assertNotIn(_frame_source_debounce_tick, registry._timers)
        self.assertIsNone(runtime_for_scene(self.scene))

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

        with patch(
            "tools.projection_paint.blender.snapshot_update.snapshot_is_ready",
            return_value=True,
        ):
            self.assertTrue(PP_OT_review_background_toggle.poll(projection_context))
            self.assertFalse(PP_OT_review_background_toggle.poll(foreign_context))
            self.assertFalse(PP_OT_review_background_toggle.poll(perspective_context))
        with patch(
            "tools.projection_paint.blender.snapshot_update.snapshot_is_ready",
            return_value=False,
        ):
            self.assertFalse(PP_OT_review_background_toggle.poll(projection_context))
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
