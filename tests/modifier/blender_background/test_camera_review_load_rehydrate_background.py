"""Blender 5.2에서 저장 Camera Review Timeline의 비파괴 재결합을 검증합니다."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier as blender_colmap
from tools.modifier.blender import (
    camera_review_timeline,
    modifier_workspace,
    runtime_state,
    state_handlers,
)
from tools.modifier.blender.camera_review_session import (
    current_review_session,
)
from tools.modifier.blender.edit_workflows import (
    PendingRemove,
    apply_pending_remove,
    undo_last_edit,
)
from tools.modifier.blender.modifier_runtime_domain import domain_for_scene
from tools.modifier.blender.runtime_state import ensure_runtime
from tools.modifier.core.removal import plan_point_removal


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
    """자동 Runtime 복원 Timer를 완료될 때까지 수동으로 전진시킵니다."""

    if bpy.app.timers.is_registered(state_handlers.process_pending_load_restore):
        bpy.app.timers.unregister(state_handlers.process_pending_load_restore)
    for _index in range(512):
        if state_handlers.process_pending_load_restore() is None:
            return
    raise AssertionError("저장된 Modifier Runtime 자동 복원이 완료되지 않았습니다.")


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

        state = ensure_runtime(scene)
        removal_plan = plan_point_removal(
            state.current_reconstruction,
            state.indexes,
            (100,),
            source_revision=state.edit_revision,
        )
        pending_remove = PendingRemove(
            target_kind="POINTS",
            dataset_uuid=state.dataset_uuid,
            source_fingerprint=state.source.source_fingerprint,
            edit_revision=state.edit_revision,
            index_revision=state.index_revision,
            history_hash=state.history_hash,
            selected_point3d_ids=(100,),
            selected_image_ids=(),
            plan=removal_plan,
        )
        apply_pending_remove(bpy.context, state, pending_remove)
        assert_true(
            100 not in ensure_runtime(scene).current_reconstruction.points3d,
            "저장 전 Modifier 독립 편집을 준비하지 못했습니다.",
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
            not bpy.data.is_dirty,
            "Modifier Domain 저장 완료 처리가 .blend를 다시 미저장 상태로 만들었습니다.",
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
        state_handlers.invalidate_loaded_state()
        _process_load_restore()
        assert_true(
            runtime_state.cached_runtime_matches_scene(scene)
            and settings.status_kind == "SUCCESS"
            and not settings.domain_restore_error
            and not bpy.app.timers.is_registered(
                state_handlers.process_pending_load_restore
            )
            and loaded_manifest_json == marker_manifest_before_save
            and settings.review_marker_manifest_json == loaded_manifest_json,
            "파일 Load가 저장된 Domain 작업 이력을 자동 Runtime으로 복원하지 못했습니다. "
            f"status={settings.status!r}, error={settings.domain_restore_error!r}",
        )
        restored_domain = domain_for_scene(scene, required=True)
        assert_true(
            restored_domain.journal.poll_undo()
            and not restored_domain.journal.poll_redo()
            and 100 not in restored_domain.dataset_state.current_reconstruction.points3d,
            "파일 Load가 저장 Operation의 Undo Delta를 다시 만들지 못했습니다.",
        )
        undo_last_edit(bpy.context)
        assert_true(
            100 in ensure_runtime(scene).current_reconstruction.points3d
            and domain_for_scene(scene, required=True).journal.poll_redo(),
            "재실행 뒤 Modifier 전용 Undo가 저장 편집을 복구하지 못했습니다.",
        )

        fake_window = SimpleNamespace(scene=scene)
        fake_area = object()
        with patch.object(
            modifier_workspace.SESSION,
            "phase",
            modifier_workspace.SESSION_ACTIVE,
        ), patch.object(
            modifier_workspace,
            "is_session_active_for_window",
            return_value=True,
        ), patch.object(
            modifier_workspace,
            "_rehydrate_viewport_runtime",
            return_value=(),
        ) as rehydrate, patch.object(
            modifier_workspace,
            "_activate_modifier_marker_session",
        ) as activate_markers:
            modifier_workspace._schedule_runtime_restore(fake_window, fake_area)
            assert_true(
                modifier_workspace._RUNTIME_RESTORE_TASK is None
                and runtime_state.cached_runtime_matches_scene(scene)
                and settings.status_kind == "SUCCESS",
                "Modifier Workspace 재진입이 자동 복원된 WARM Runtime을 재사용하지 못했습니다.",
            )
        if bpy.app.timers.is_registered(modifier_workspace._process_runtime_restore):
            bpy.app.timers.unregister(modifier_workspace._process_runtime_restore)
        assert_true(
            runtime_state.cached_runtime_matches_scene(scene)
            and settings.status_kind == "SUCCESS"
            and rehydrate.call_count == 1
            and activate_markers.call_count == 1,
            "Modifier Runtime 완료 뒤 Review·Marker를 정확히 한 번 연결하지 못했습니다.",
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
