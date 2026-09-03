"""Blender Global Undo와 COLMAP 권위 상태 재생성 계약을 검증합니다."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import bmesh
import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier as blender_colmap
from tools.modifier.blender.camera_review_session import (
    current_review_session,
    ensure_review_session,
)
from tools.modifier.blender.camera_review_timeline import (
    process_pending_timeline_updates,
)
from tools.modifier.blender.operators_camera_review import (
    COLMAP_OT_review_apply_pending_filters,
)
from tools.modifier.blender.operators_point_selection import (
    COLMAP_OT_remove,
    COLMAP_OT_reset_edits,
    COLMAP_OT_undo_last_edit,
)
from tools.modifier.blender.ownership import (
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from tools.modifier.blender.runtime_state import ensure_runtime
from tools.modifier.blender.state_handlers import reconcile_after_undo_redo
from tools.modifier.blender.undo_domain_coordinator import (
    COLMAPUndoProtectionSnapshot,
    capture_undo_protection_snapshot,
    clear_undo_domain_state,
    current_undo_protection_snapshot,
    process_pending_undo_reconcile,
    schedule_undo_domain_reconcile,
    undo_domain_reconcile_pending,
)
from tests.modifier.fixture_utils import make_legacy_text_multi_point


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def managed_objects(kind):
    return [
        obj
        for obj in bpy.data.objects
        if obj.get(OWNER_KEY) == OWNER_TOKEN
        and obj.get(RECORD_KIND_KEY) == kind
        and obj.users_collection
    ]


def view3d_override():
    window = bpy.context.window
    area = next(
        candidate
        for candidate in window.screen.areas
        if candidate.type == "VIEW_3D"
    )
    region = next(
        candidate
        for candidate in area.regions
        if candidate.type == "WINDOW"
    )
    return bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    )


def make_review_fixture(root: Path) -> None:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    for name, color in (
        ("one.ppm", (255, 0, 0)),
        ("two.ppm", (0, 255, 0)),
        ("three.ppm", (0, 0, 255)),
    ):
        (images / name).write_bytes(
            b"P6\n2 2\n255\n" + bytes(color * 4)
        )
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 640 480 510 500 320 240\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n"
        "1 1 0 0 0 0 0 0 1 one.ppm\n10 10 100\n"
        "2 1 0 0 0 1 0 0 1 two.ppm\n20 20 200\n"
        "3 1 0 0 0 2 0 0 1 three.ppm\n30 30 300\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n"
        "100 0 0 0 255 0 0 0.1 1 0\n"
        "200 1 0 0 0 255 0 0.1 2 0\n"
        "300 2 0 0 0 0 255 0.1 3 0\n",
        encoding="utf-8",
    )


def process_rehydrate():
    """Background mode에서 예약된 최신 Timer 작업을 동기적으로 완료합니다."""

    if not undo_domain_reconcile_pending():
        schedule_undo_domain_reconcile(current_undo_protection_snapshot())
    for _index in range(8):
        process_pending_undo_reconcile()
        if not undo_domain_reconcile_pending():
            return
    raise AssertionError("Global Undo 재생성 Timer가 완료되지 않았습니다.")


def assert_index_symmetry(state):
    indexes = state.indexes
    reconstruction = state.current_reconstruction
    assert_true(
        set(indexes.point_to_images) == set(reconstruction.points3d)
        and set(indexes.image_to_points) == set(reconstruction.images)
        and set(indexes.camera_to_images) == set(reconstruction.cameras),
        "RelationshipIndexes의 key 집합이 복원 Reconstruction과 다릅니다.",
    )
    for point_id, image_ids in indexes.point_to_images.items():
        for image_id in image_ids:
            assert_true(
                point_id in indexes.image_to_points[image_id],
                "Point→Image와 Image→Point 인덱스가 대칭이 아닙니다.",
            )
    for image_id, point_ids in indexes.image_to_points.items():
        for point_id in point_ids:
            assert_true(
                image_id in indexes.point_to_images[point_id],
                "Image→Point와 Point→Image 인덱스가 대칭이 아닙니다.",
            )


def assert_authoritative_state(
    scene,
    *,
    revision,
    point_count,
    image_count,
    previous_runtime,
):
    """Global Undo 뒤 독립 Runtime identity와 권위 상태가 그대로인지 검사합니다."""

    settings = scene.colmap_import
    state = ensure_runtime(scene)
    assert_true(
        state is previous_runtime
        and state.current_reconstruction
        is previous_runtime.current_reconstruction
        and state.indexes is previous_runtime.indexes,
        "Global Undo가 독립 Runtime·Reconstruction·Indexes identity를 변경했습니다.",
    )
    assert_true(
        settings.edit_revision == revision
        and state.edit_revision == revision
        and state.index_revision == revision
        and len(state.operations) == revision
        and state.history_hash == settings.history_hash,
        "복원 Scene history와 Runtime/index revision이 일치하지 않습니다.",
    )
    assert_true(
        len(state.current_reconstruction.points3d) == point_count
        and len(state.point_id_order) == point_count
        and len(state.current_reconstruction.images) == image_count
        and len(managed_objects("POINT_MESH")[0].data.vertices) == point_count
        and len(managed_objects("CAMERA")) == image_count,
        "복원 Reconstruction과 관리 Scene의 레코드 수가 일치하지 않습니다.",
    )
    assert_true(
        not settings.point_edit_mode_active
        and not settings.camera_edit_mode_active
        and settings.remove_target == "NONE"
        and settings.selected_point_count == 0
        and settings.camera_target_count == 0,
        "Global Undo 뒤 Point·Camera Edit Mode가 비활성 상태로 정규화되지 않았습니다.",
    )
    assert_index_symmetry(state)
    return state


for operator_class in (
    COLMAP_OT_remove,
    COLMAP_OT_undo_last_edit,
    COLMAP_OT_reset_edits,
    COLMAP_OT_review_apply_pending_filters,
):
    assert_true(
        "UNDO" in operator_class.bl_options,
        f"{operator_class.bl_idname}이 Blender Global Undo Step을 생성하지 않습니다.",
    )

snapshot_fields = tuple(COLMAPUndoProtectionSnapshot.__dataclass_fields__)
assert_true(
    "scene" not in snapshot_fields
    and "window" not in snapshot_fields
    and "object" not in snapshot_fields
    and "image" not in snapshot_fields
    and "runtime" not in snapshot_fields
    and "reconstruction" not in snapshot_fields
    and "indexes" not in snapshot_fields,
    "Undo Snapshot이 Blender ID 또는 Runtime 객체를 직접 보관합니다.",
)

clear_undo_domain_state()
blender_colmap.register()
try:
    assert_true(
        bpy.app.handlers.undo_pre.count(capture_undo_protection_snapshot) == 1
        and bpy.app.handlers.redo_pre.count(
            capture_undo_protection_snapshot
        )
        == 1
        and bpy.app.handlers.undo_post.count(reconcile_after_undo_redo) == 1
        and bpy.app.handlers.redo_post.count(reconcile_after_undo_redo) == 1,
        "Global Undo 수명주기 Handler가 없거나 중복 등록되었습니다.",
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "undo_point_dataset"
        make_legacy_text_multi_point(root)
        settings = bpy.context.scene.colmap_import
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        scene = bpy.context.scene
        baseline_runtime = ensure_runtime(scene)
        ensure_review_session(bpy.context)
        # Background mode는 시작 시 Undo System이 비활성이라 기준 Step을
        # 명시적으로 한 번 초기화해야 합니다. 이후에는 Remove가 만든 Step을
        # 즉시 Undo하므로 별도의 작업 Step을 사이에 끼우지 않습니다.
        with view3d_override():
            bpy.ops.ed.undo_push(message="Initialize Before Point Remove")
            assert_true(
                bpy.ops.colmap.toggle_edit_point_mode() == {"FINISHED"},
                settings.point_selection_status,
            )
        point_object = managed_objects("POINT_MESH")[0]
        bm = bmesh.from_edit_mesh(point_object.data)
        bm.verts.ensure_lookup_table()
        for vertex in bm.verts:
            vertex.select = False
        bm.verts[0].select = True
        bmesh.update_edit_mesh(point_object.data)
        with view3d_override():
            assert_true(
                bpy.ops.colmap.remove() == {"FINISHED"},
                settings.point_selection_status,
            )
            # Background Python 호출은 Operator의 ``UNDO`` 옵션을 자동으로
            # Stack에 materialize하지 않으므로 GUI와 같은 완료 Step을 만듭니다.
            bpy.ops.ed.undo_push(message="Point Remove Operator Step")
        edited_runtime = ensure_runtime(scene)
        ensure_review_session(bpy.context)
        assert_true(
            edited_runtime.edit_revision == 1
            and len(edited_runtime.current_reconstruction.points3d) == 2,
            "Point Remove 기준 상태를 만들지 못했습니다.",
        )
        del bm, point_object, settings, scene

        # Remove가 만든 Global Undo Step을 즉시 되돌립니다. 별도의 사용자
        # Object용 undo_push를 끼우지 않아 Operator 소유권도 함께 검증합니다.
        with view3d_override():
            assert_true(
                bpy.ops.ed.undo() == {"FINISHED"},
                "Point Remove 직후 Blender Global Undo 실행에 실패했습니다.",
            )
        process_rehydrate()
        scene = bpy.context.scene
        restored_runtime = assert_authoritative_state(
            scene,
            revision=1,
            point_count=2,
            image_count=1,
            previous_runtime=edited_runtime,
        )
        del scene

        # Redo도 Undo가 복원한 Scene history를 권위로 새 Runtime을 만듭니다.
        with view3d_override():
            assert_true(
                bpy.ops.ed.redo() == {"FINISHED"},
                "Point Remove Blender Global Redo 실행에 실패했습니다.",
            )
        process_rehydrate()
        scene = bpy.context.scene
        redone_runtime = assert_authoritative_state(
            scene,
            revision=1,
            point_count=2,
            image_count=1,
            previous_runtime=restored_runtime,
        )
        del scene

        # 다음 Dataset으로 전환하기 전 Modifier 전용 Undo로 기준 상태를 되돌립니다.
        with view3d_override():
            assert_true(
                bpy.ops.colmap.undo_last_edit() == {"FINISHED"},
                "Modifier 전용 기준 상태 Undo에 실패했습니다.",
            )
        assert_authoritative_state(
            bpy.context.scene,
            revision=0,
            point_count=3,
            image_count=1,
            previous_runtime=ensure_runtime(bpy.context.scene),
        )

        review_root = Path(directory) / "undo_camera_filter_dataset"
        make_review_fixture(review_root)
        settings = bpy.context.scene.colmap_import
        settings.dataset_root = str(review_root)
        settings.input_format = "TEXT"
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        scene = bpy.context.scene
        filter_baseline_runtime = ensure_runtime(scene)
        ensure_review_session(bpy.context)
        with view3d_override():
            bpy.ops.ed.undo_push(message="Initialize Before Camera Filter")
            assert_true(
                bpy.ops.colmap.review_filter_current() == {"FINISHED"}
                and bpy.ops.colmap.review_apply_pending_filters()
                == {"FINISHED"},
                settings.review_status,
            )
            bpy.ops.ed.undo_push(message="Camera Filter Operator Step")
        filtered_runtime = ensure_runtime(scene)
        ensure_review_session(bpy.context)
        assert_true(
            filtered_runtime.edit_revision == 1
            and len(filtered_runtime.current_reconstruction.images) == 2
            and len(filtered_runtime.current_reconstruction.points3d) == 2,
            "Camera Filter 기준 상태를 만들지 못했습니다.",
        )
        del settings, scene

        with view3d_override():
            assert_true(
                bpy.ops.ed.undo() == {"FINISHED"},
                "Camera Filter Apply 직후 Blender Global Undo 실행에 실패했습니다.",
            )
        process_rehydrate()
        scene = bpy.context.scene
        restored_filter_runtime = assert_authoritative_state(
            scene,
            revision=1,
            point_count=2,
            image_count=2,
            previous_runtime=filtered_runtime,
        )
        restored_filter_session = current_review_session(
            bpy.context,
            strict=True,
        )
        restored_filter_settings = scene.colmap_import
        assert_true(
            restored_filter_runtime is filtered_runtime
            and restored_filter_runtime is not filter_baseline_runtime
            and restored_filter_session.edit_revision == 1
            and restored_filter_settings.edit_revision == 1,
            "Camera Filter Global Undo가 독립 Runtime 편집 결과를 변경했습니다.",
        )
        next_frame = restored_filter_session.frame_start + min(
            1,
            len(restored_filter_session.review_index.entries) - 1,
        )
        scene.frame_set(next_frame)
        process_pending_timeline_updates()
        assert_true(
            ensure_runtime(scene) is restored_filter_runtime
            and ensure_runtime(scene).edit_revision == 1,
            "비활성 Modifier의 Timeline 이동이 독립 Runtime을 변경했습니다.",
        )
finally:
    blender_colmap.unregister()

assert_true(
    capture_undo_protection_snapshot not in bpy.app.handlers.undo_pre
    and capture_undo_protection_snapshot not in bpy.app.handlers.redo_pre
    and reconcile_after_undo_redo not in bpy.app.handlers.undo_post
    and reconcile_after_undo_redo not in bpy.app.handlers.redo_post
    and not bpy.app.timers.is_registered(process_pending_undo_reconcile),
    "Extension 해제 뒤 Global Undo Handler 또는 Timer가 남았습니다.",
)
print("BLENDER_COLMAP_GLOBAL_UNDO_REHYDRATE_BACKGROUND_OK")
