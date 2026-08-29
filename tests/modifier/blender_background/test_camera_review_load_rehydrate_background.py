"""Blender 5.2에서 저장 Camera Review Timeline의 비파괴 재결합을 검증합니다."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier as blender_colmap
from tools.modifier.blender import (
    camera_filter_queue,
    camera_review_timeline,
    state_handlers,
)
from tools.modifier.blender.camera_review_session import (
    current_review_session,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _write_ppm(path: Path, rgb: tuple[int, int, int]) -> None:
    path.write_bytes(b"P6\n2 2\n255\n" + bytes(rgb * 4))


def _make_fixture(root: Path) -> None:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    for name, color in (
        ("two.ppm", (255, 0, 0)),
        ("five.ppm", (0, 255, 0)),
        ("nine.ppm", (0, 0, 255)),
    ):
        _write_ppm(images / name, color)
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 640 480 510 500 320 240\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n"
        "2 1 0 0 0 0 0 0 1 two.ppm\n"
        "10 10 100 20 20 200\n"
        "5 1 0 0 0 1 0 0 1 five.ppm\n"
        "20 20 200 30 30 300\n"
        "9 1 0 0 0 2 0 0 1 nine.ppm\n"
        "30 30 300\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n"
        "100 0 0 0 255 0 0 0.1 2 0\n"
        "200 1 0 0 0 255 0 0.1 2 1 5 0\n"
        "300 2 0 0 0 0 255 0.1 5 1 9 0\n",
        encoding="utf-8",
    )


def _marker_snapshot(scene) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                marker.name,
                marker.frame,
                marker.as_pointer(),
                marker.camera.as_pointer() if marker.camera is not None else 0,
            )
            for marker in scene.timeline_markers
        )
    )


def _process_load_restore() -> None:
    result = state_handlers.process_pending_load_restore()
    assert_true(result is None, "로드 복원 작업이 예상 밖의 재시도를 요청했습니다.")


blender_colmap.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        _make_fixture(root)
        scene = bpy.context.scene
        settings = scene.colmap_import
        camera_review_timeline.acquire_modifier_marker_session(scene)
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )

        session = current_review_session(bpy.context, strict=True)
        scene.frame_set(3)
        camera_review_timeline.process_pending_timeline_updates()
        assert_true(
            session.current_image_id == 9,
            "저장 기준 frame의 Camera Review를 준비하지 못했습니다.",
        )

        # 저장 Property가 이전 항목이어도 현재 Playhead frame이 복원 우선권을 가집니다.
        settings.review_current_index = 0
        settings.review_current_image_id = "2"
        marker_manifest_before_save = settings.review_marker_manifest_json
        camera_review_timeline.detach_review_timeline_markers(session)
        camera_review_timeline.release_modifier_marker_session(scene)
        blend_path = Path(directory) / "review_timeline.blend"
        assert_true(
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_path)) == {"FINISHED"},
            "Camera Review 기준 파일 저장에 실패했습니다.",
        )
        assert_true(
            bpy.ops.wm.open_mainfile(filepath=str(blend_path)) == {"FINISHED"},
            "Camera Review 기준 파일 재열기에 실패했습니다.",
        )

        scene = bpy.context.scene
        settings = scene.colmap_import
        loaded_manifest_json = settings.review_marker_manifest_json
        assert_true(
            current_review_session(bpy.context) is None,
            "로드 복원 Timer 전 구 Camera Review Session이 남았습니다.",
        )
        _process_load_restore()
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            not session.timeline_active
            and not session.timeline_refresh_enabled
            and session.current_image_id == 2
            and settings.review_current_image_id == "2"
            and scene.frame_current == 3
            and not _marker_snapshot(scene)
            and loaded_manifest_json == marker_manifest_before_save
            and settings.review_marker_manifest_json == loaded_manifest_json,
            "비활성 Modifier 로드가 Marker를 분리하거나 Manifest를 보존하지 못했습니다.",
        )

        camera_review_timeline.acquire_modifier_marker_session(scene)
        assert_true(
            camera_review_timeline.attach_review_timeline_markers(session) == 3,
            "활성 Marker 세션이 저장 Manifest를 다시 연결하지 못했습니다.",
        )
        scene.frame_set(3)
        camera_review_timeline.process_pending_timeline_updates()
        scene.frame_set(2)
        camera_review_timeline.process_pending_timeline_updates()
        assert_true(
            session.current_image_id == 5
            and settings.review_current_image_id == "5"
            and scene.camera is session.camera_objects[5],
            "로드 복원 뒤 frame 이동이 Camera Review를 갱신하지 못했습니다.",
        )

        # Pending Marker가 빠진 저장 상태도 Pending을 먼저 복원한 뒤 재결합합니다.
        scene.frame_set(2)
        camera_review_timeline.process_pending_timeline_updates()
        assert_true(
            bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        pending_session = current_review_session(bpy.context, strict=True)
        assert_true(
            pending_session.pending_image_ids == frozenset({5})
            and pending_session.current_image_id == 9,
            "Pending 저장 기준 상태를 만들지 못했습니다.",
        )
        camera_review_timeline.detach_review_timeline_markers(pending_session)
        camera_review_timeline.release_modifier_marker_session(scene)
        pending_blend_path = Path(directory) / "review_pending.blend"
        assert_true(
            bpy.ops.wm.save_as_mainfile(filepath=str(pending_blend_path))
            == {"FINISHED"}
            and bpy.ops.wm.open_mainfile(filepath=str(pending_blend_path))
            == {"FINISHED"},
            "Pending Camera Review 파일 저장·재열기에 실패했습니다.",
        )

        scene = bpy.context.scene
        settings = scene.colmap_import
        pending_loaded_manifest = settings.review_marker_manifest_json
        _process_load_restore()
        pending_session = current_review_session(bpy.context, strict=True)
        assert_true(
            settings.review_pending_filter_status
            == camera_filter_queue.PENDING_STATUS_READY
            and settings.review_pending_filter_order_json == "[5]"
            and pending_session.pending_image_ids == frozenset({5})
            and not pending_session.timeline_active
            and pending_session.current_image_id == 9
            and pending_session.camera_objects[5].hide_get()
            and not pending_session.timeline_manifest_signature
            and not _marker_snapshot(scene)
            and settings.review_marker_manifest_json == pending_loaded_manifest,
            "비활성 로드에서 Pending 복원과 Marker 분리가 올바르지 않습니다.",
        )
        camera_review_timeline.acquire_modifier_marker_session(scene)
        assert_true(
            camera_review_timeline.attach_review_timeline_markers(pending_session) == 2
            and len(pending_session.timeline_manifest_signature) == 2,
            "활성 재진입에서 Pending Camera를 제외한 Marker를 복원하지 못했습니다.",
        )

        # 이전 generation의 Callback은 최신 Scene 상태를 변경하지 않습니다.
        state_handlers._schedule_load_restore()
        stale_pending = state_handlers._PENDING_LOAD_RESTORE
        state_handlers._schedule_load_restore()
        current_pending = state_handlers._PENDING_LOAD_RESTORE
        assert_true(
            stale_pending is not None
            and current_pending is not None
            and stale_pending.generation < current_pending.generation
            and not state_handlers._pending_load_restore_is_current(
                stale_pending,
                scene,
                settings,
            )
            and state_handlers._pending_load_restore_is_current(
                current_pending,
                scene,
                settings,
            ),
            "로드 복원 generation token이 오래된 Callback을 구분하지 못했습니다.",
        )
        status_before_stale_callback = settings.review_status
        state_handlers._PENDING_LOAD_RESTORE = stale_pending
        _process_load_restore()
        assert_true(
            settings.review_status == status_before_stale_callback,
            "오래된 로드 복원 Callback이 최신 Scene 상태를 변경했습니다.",
        )
finally:
    try:
        session = current_review_session(bpy.context)
        if session is not None:
            camera_review_timeline.detach_review_timeline_markers(session)
        camera_review_timeline.release_modifier_marker_session(bpy.context.scene)
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        pass
    blender_colmap.unregister()

assert_true(
    not bpy.app.timers.is_registered(
        state_handlers.process_pending_load_restore
    ),
    "Extension 해제 뒤 Camera Review 로드 복원 Timer가 남았습니다.",
)
print("BLENDER_COLMAP_CAMERA_REVIEW_LOAD_REHYDRATE_BACKGROUND_OK")
