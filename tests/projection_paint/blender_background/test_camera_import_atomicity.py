"""Blender Background에서 Camera Import와 Marker 복구 원자성을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
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
)
from tools.projection_paint.blender.camera_scene import (
    _repair_timer,
    attach_projection_markers,
    clear_camera_runtime,
    detach_projection_markers,
    projection_markers_attached,
    rebuild_camera_runtime,
    runtime_for_scene,
    scene_manifest,
)
from tools.projection_paint.blender.ownership import MANIFEST_KEY
from tools.projection_paint.blender.lifecycle import _rebuild_projection_marker_session
from shared.blender_runtime.scene_marker_session import (
    acquire_scene_marker_session,
    release_scene_marker_session,
    scene_marker_session_owned_by,
)


def _request(
    dataset_uuid: str,
    revision: int = 1,
    camera_frustum_size: float = 1.0,
) -> CameraImportRequest:
    """두 Image가 Intrinsics 하나를 공유하는 최소 Import 요청을 만듭니다."""

    camera = SimpleNamespace(
        camera_id=7,
        model_name="PINHOLE",
        width=1920,
        height=1080,
        params=(1200.0, 1190.0, 960.0, 540.0),
    )
    images = (
        SimpleNamespace(
            image_id=10,
            camera_id=7,
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(0.0, 0.0, 0.0),
            name="a.jpg",
        ),
        SimpleNamespace(
            image_id=30,
            camera_id=7,
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(1.0, 0.0, 0.0),
            name="b.jpg",
        ),
    )
    points = (
        SimpleNamespace(
            point3d_id=7,
            xyz=(1.0, 2.0, 3.0),
            rgb=(255, 64, 0),
            error=0.25,
        ),
        SimpleNamespace(
            point3d_id=(1 << 40) + 9,
            xyz=(-1.0, 0.5, 4.0),
            rgb=(12, 34, 56),
            error=1.5,
        ),
    )
    return CameraImportRequest(
        dataset_uuid=dataset_uuid,
        dataset_revision=revision,
        source_fingerprint=f"fingerprint-{revision}",
        cameras={7: camera},
        images=images,
        points=points,
        frame_start=1,
        axis_preset="BLENDER",
        camera_frustum_size=camera_frustum_size,
    )


class CameraImportAtomicityTest(unittest.TestCase):
    """Camera Data 공유, Marker 보호와 실패 Rollback을 검증합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """Extension을 테스트 Process에 한 번 등록합니다."""

        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """Extension과 Camera Runtime을 테스트 Process에서 해제합니다."""

        clear_camera_runtime()
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트에 독립 Scene을 생성합니다."""

        self.scene = bpy.data.scenes.new(f"PP_TEST_{self._testMethodName}")

    def tearDown(self) -> None:
        """독립 Scene과 애드온 소유 데이터블록을 제거합니다."""

        clear_camera_runtime()
        release_scene_marker_session(self.scene, "projection_paint")
        if self.scene.name in bpy.data.scenes:
            bpy.data.scenes.remove(self.scene)
        for collection in tuple(bpy.data.collections):
            if collection.get("pp_owner") == "blender_projection_painting":
                bpy.data.collections.remove(collection)
        for obj in tuple(bpy.data.objects):
            if obj.get("pp_owner") == "blender_projection_painting":
                bpy.data.objects.remove(obj, do_unlink=True)
        for data in tuple(bpy.data.cameras):
            if (
                data.get("pp_owner") == "blender_projection_painting"
                and data.users == 0
            ):
                bpy.data.cameras.remove(data)
        for values in (bpy.data.meshes, bpy.data.node_groups, bpy.data.materials):
            for data in tuple(values):
                if (
                    data.get("pp_owner") == "blender_projection_painting"
                    and data.users == 0
                ):
                    values.remove(data)

    def _acquire_marker_session(self) -> None:
        """성공 Import가 사용할 Projection Marker 세션을 획득합니다."""

        acquire_scene_marker_session(self.scene, "projection_paint")

    def test_shared_camera_data_and_marker_repair(self) -> None:
        """IMAGE별 Object와 Marker가 공유 Data를 쓰고 변경이 복구되는지 확인합니다."""

        self._acquire_marker_session()
        ordinary = self.scene.timeline_markers.new("사용자 메모", frame=1)
        result = CameraImportJob(self.scene, _request("dataset-a")).run_to_completion()
        self.assertEqual(2, result.camera_count)
        self.assertEqual(1, result.camera_data_count)
        self.assertEqual(2, result.point_count)
        point_objects = [
            obj
            for obj in self.scene.objects
            if obj.get("pp_record_kind") == "POINT_MESH"
        ]
        self.assertEqual(len(point_objects), 1)
        self.assertEqual(len(point_objects[0].data.vertices), 2)
        self.assertIn("colmap_color", point_objects[0].data.color_attributes)
        self.assertEqual(len(point_objects[0].modifiers), 1)
        manifest = scene_manifest(self.scene)
        self.assertIsNotNone(manifest)
        runtime = runtime_for_scene(self.scene)
        self.assertIsNotNone(runtime)
        objects = list(runtime.objects.values())
        self.assertIs(objects[0].data, objects[1].data)
        entry = manifest.entries[0]
        marker = runtime.markers[entry.image_id]
        marker.name = "사용자 변경"
        marker.frame = 99
        marker.camera = objects[1]
        _repair_timer()
        self.assertEqual(entry.marker_name, marker.name)
        self.assertEqual(entry.frame, marker.frame)
        self.assertIs(runtime.objects[entry.camera_uuid], marker.camera)
        self.assertIn(ordinary, self.scene.timeline_markers.values())
        removed_camera = runtime.objects[entry.camera_uuid]
        bpy.data.objects.remove(removed_camera, do_unlink=True)
        _repair_timer()
        recreated = runtime.objects[entry.camera_uuid]
        self.assertNotEqual(removed_camera, recreated)
        self.assertEqual(entry.camera_uuid, recreated.get("pp_uuid"))
        self.assertIs(recreated, runtime.markers[entry.image_id].camera)
        recreated.lock_location = (False, False, False)
        recreated.data.lens = 1.0
        runtime.dirty_objects.append(entry.camera_uuid)
        runtime.queued.add(("OBJECT", entry.camera_uuid))
        data_uuid = manifest.camera_data[0].owned_uuid
        runtime.dirty_data.append(data_uuid)
        runtime.queued.add(("DATA", data_uuid))
        _repair_timer()
        self.assertEqual(tuple(recreated.lock_location), (True, True, True))
        self.assertEqual(recreated.data.lens, manifest.camera_data[0].lens)

    def test_camera_frustum_size_import_and_live_sync(self) -> None:
        """Camera 표시 크기를 Import와 UI 변경에 적용하고 사용자 Camera는 보존합니다."""

        self._acquire_marker_session()
        settings = self.scene.projection_paint
        self.assertAlmostEqual(1.0, settings.camera_frustum_size)
        settings.camera_frustum_size = 0.4
        CameraImportJob(
            self.scene,
            _request("dataset-frustum", camera_frustum_size=0.4),
        ).run_to_completion()
        runtime = runtime_for_scene(self.scene)
        managed_data = next(iter(runtime.camera_data.values()))
        self.assertAlmostEqual(0.4, managed_data.display_size)

        user_data = bpy.data.cameras.new("PP_TEST_USER_CAMERA_DATA")
        user_data.display_size = 2.0
        user_object = bpy.data.objects.new("PP_TEST_USER_CAMERA", user_data)
        self.scene.collection.objects.link(user_object)
        try:
            settings.camera_frustum_size = 0.75
            self.assertAlmostEqual(0.75, managed_data.display_size)
            self.assertAlmostEqual(2.0, user_data.display_size)
        finally:
            bpy.data.objects.remove(user_object, do_unlink=True)
            bpy.data.cameras.remove(user_data)

    def test_zero_camera_frustum_hides_only_managed_camera_objects(self) -> None:
        """프러스텀 0이 Camera 뷰용 Object를 숨기고 이전 눈 상태를 복구합니다."""

        self._acquire_marker_session()
        settings = self.scene.projection_paint
        settings.camera_frustum_size = 0.0
        CameraImportJob(
            self.scene,
            _request("dataset-zero-frustum", camera_frustum_size=0.0),
        ).run_to_completion()
        runtime = runtime_for_scene(self.scene)
        managed_objects = tuple(runtime.objects.values())
        managed_data = next(iter(runtime.camera_data.values()))
        view_layer = self.scene.view_layers[0]
        self.assertAlmostEqual(0.0, settings.camera_frustum_size)
        self.assertAlmostEqual(0.01, managed_data.display_size)
        self.assertTrue(
            all(obj.hide_get(view_layer=view_layer) for obj in managed_objects)
        )
        self.assertTrue(all(not obj.hide_viewport for obj in managed_objects))

        user_data = bpy.data.cameras.new("PP_TEST_ZERO_USER_CAMERA_DATA")
        user_object = bpy.data.objects.new("PP_TEST_ZERO_USER_CAMERA", user_data)
        self.scene.collection.objects.link(user_object)
        try:
            settings.camera_frustum_size = 0.6
            self.assertTrue(
                all(not obj.hide_get(view_layer=view_layer) for obj in managed_objects)
            )
            self.assertFalse(user_object.hide_get(view_layer=view_layer))

            managed_objects[0].hide_set(True, view_layer=view_layer)
            settings.camera_frustum_size = 0.0
            settings.camera_frustum_size = 0.8
            self.assertTrue(managed_objects[0].hide_get(view_layer=view_layer))
            self.assertTrue(
                all(
                    not obj.hide_get(view_layer=view_layer)
                    for obj in managed_objects[1:]
                )
            )
        finally:
            bpy.data.objects.remove(user_object, do_unlink=True)
            bpy.data.cameras.remove(user_data)

    def test_point_scale_updates_the_settings_owner_scene(self) -> None:
        """Point Scale Callback이 현재 Context가 아닌 소유 Scene을 갱신합니다."""

        self._acquire_marker_session()
        CameraImportJob(
            self.scene,
            _request("dataset-point-scale"),
        ).run_to_completion()
        point_object = next(
            obj
            for obj in self.scene.objects
            if obj.get("pp_record_kind") == "POINT_MESH"
        )
        modifier = point_object.modifiers[0]
        identifier = modifier.node_group.get("pp_point_scale_socket")

        self.scene.projection_paint.point_size = 0.25

        modifier_input = getattr(modifier.properties.inputs, identifier)
        self.assertAlmostEqual(0.25, modifier_input.value)
        self.assertAlmostEqual(0.25, point_object["colmap_point_size"])

    def test_commit_failure_restores_previous_dataset(self) -> None:
        """새 Commit 오류가 이전 Manifest와 Scene Camera를 보존하는지 확인합니다."""

        self._acquire_marker_session()
        CameraImportJob(self.scene, _request("dataset-old")).run_to_completion()
        old_payload = str(self.scene[MANIFEST_KEY])
        old_camera = self.scene.camera
        old_markers = {
            (marker.name, marker.frame, marker.camera.get("pp_uuid"))
            for marker in self.scene.timeline_markers
        }

        def fail_before_runtime(label: str) -> None:
            """Runtime 설치 직전 의도한 Commit 오류를 발생시킵니다."""

            if label == "before_runtime":
                raise RuntimeError("의도한 Commit 실패")

        with self.assertRaises(RuntimeError):
            CameraImportJob(
                self.scene,
                _request("dataset-new", revision=2),
                fault_injector=fail_before_runtime,
            ).run_to_completion()
        self.assertEqual(old_payload, str(self.scene[MANIFEST_KEY]))
        self.assertIs(old_camera, self.scene.camera)
        self.assertEqual("dataset-old", scene_manifest(self.scene).dataset_uuid)
        self.assertEqual(
            old_markers,
            {
                (marker.name, marker.frame, marker.camera.get("pp_uuid"))
                for marker in self.scene.timeline_markers
            },
        )

    def test_external_camera_marker_conflict_is_fail_closed(self) -> None:
        """외부 Camera Marker 충돌이 Scene 변경 전에 Import를 거부하는지 확인합니다."""

        camera_data = bpy.data.cameras.new("USER_CAMERA_DATA")
        camera_object = bpy.data.objects.new("USER_CAMERA", camera_data)
        self.scene.collection.objects.link(camera_object)
        marker = self.scene.timeline_markers.new("USER_CAMERA_MARKER", frame=1)
        marker.camera = camera_object
        before_objects = set(self.scene.objects)
        with self.assertRaises(RuntimeError):
            CameraImportJob(self.scene, _request("dataset-conflict")).validate()
        self.assertEqual(before_objects, set(self.scene.objects))
        self.assertEqual("USER_CAMERA_MARKER", marker.name)

    def test_marker_detach_attach_and_inactive_guard(self) -> None:
        """Marker 분리·재연결과 비활성 Guard의 무생성 계약을 검증합니다."""

        self._acquire_marker_session()
        CameraImportJob(self.scene, _request("dataset-session")).run_to_completion()
        manifest = scene_manifest(self.scene)
        runtime = runtime_for_scene(self.scene)
        modifier_marker = self.scene.timeline_markers.new(
            "COLMAP Review modifier IMAGE_ID 99",
            frame=99,
        )
        modifier_marker.camera = next(iter(runtime.objects.values()))

        detach_projection_markers(self.scene)
        self.assertEqual(1, len(self.scene.timeline_markers))
        self.assertEqual(modifier_marker.name, self.scene.timeline_markers[0].name)
        self.assertIs(modifier_marker.camera, self.scene.timeline_markers[0].camera)
        self.assertTrue(all(marker is None for marker in runtime.markers.values()))
        self.assertIsNotNone(scene_manifest(self.scene))
        release_scene_marker_session(self.scene, "projection_paint")
        _repair_timer()
        self.assertEqual(1, len(self.scene.timeline_markers))
        clear_camera_runtime()
        rebuild_camera_runtime(self.scene, "undo", 0)
        self.assertEqual(1, len(self.scene.timeline_markers))

        active_window = SimpleNamespace(
            scene=self.scene,
            workspace=object(),
            as_pointer=lambda: 987654,
        )
        lifecycle_bpy = SimpleNamespace(
            context=SimpleNamespace(
                window_manager=SimpleNamespace(windows=(active_window,)),
            ),
        )
        with (
            patch(
                "tools.projection_paint.blender.lifecycle.bpy",
                lifecycle_bpy,
            ),
            patch(
                "shared.blender_runtime.workspace_factory.workspace_matches_spec",
                return_value=True,
            ),
        ):
            _rebuild_projection_marker_session(self.scene, "undo", 0)
        runtime = runtime_for_scene(self.scene)
        self.assertTrue(projection_markers_attached(self.scene))
        for entry in manifest.entries:
            marker = runtime.markers[entry.image_id]
            self.assertEqual(entry.marker_name, marker.name)
            self.assertEqual(entry.frame, marker.frame)
            self.assertEqual(entry.camera_uuid, marker.camera.get("pp_uuid"))
        deleted_entry = manifest.entries[0]
        self.scene.timeline_markers.remove(runtime.markers[deleted_entry.image_id])
        _repair_timer()
        restored = runtime.markers[deleted_entry.image_id]
        self.assertEqual(deleted_entry.marker_name, restored.name)
        self.assertEqual(deleted_entry.frame, restored.frame)

    def test_import_commit_requires_projection_marker_session(self) -> None:
        """비활성 Projection Workspace의 Import Commit을 차단합니다."""

        with self.assertRaises(RuntimeError):
            CameraImportJob(
                self.scene,
                _request("dataset-without-session"),
            ).run_to_completion()
        self.assertFalse(scene_marker_session_owned_by(self.scene, "projection_paint"))
        self.assertIsNone(scene_manifest(self.scene))

    def test_unregister_detaches_markers_and_releases_projection_lease(self) -> None:
        """Extension 해제가 Manifest와 Camera를 보존하고 Marker lease만 정리하는지 검증합니다."""

        self._acquire_marker_session()
        CameraImportJob(self.scene, _request("dataset-unregister")).run_to_completion()
        manifest_payload = str(self.scene[MANIFEST_KEY])
        camera_uuids = {
            obj.get("pp_uuid")
            for obj in self.scene.objects
            if obj.get("pp_record_kind") == "CAMERA_OBJECT"
        }
        try:
            addon.unregister()
            self.assertFalse(tuple(self.scene.timeline_markers))
            self.assertEqual(manifest_payload, str(self.scene[MANIFEST_KEY]))
            self.assertEqual(
                camera_uuids,
                {
                    obj.get("pp_uuid")
                    for obj in self.scene.objects
                    if obj.get("pp_record_kind") == "CAMERA_OBJECT"
                },
            )
            self.assertFalse(
                scene_marker_session_owned_by(self.scene, "projection_paint")
            )
        finally:
            addon.register()


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
