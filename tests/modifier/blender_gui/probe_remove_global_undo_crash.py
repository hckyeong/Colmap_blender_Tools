"""GUI Blender에서 Point Remove 직후 Global Undo 충돌 회귀를 검증합니다.

실행 예:

    blender --factory-startup --python tests/blender_gui/probe_remove_global_undo_crash.py

이 프로브는 실제 3D Viewport가 있는 GUI 프로세스에서 Remove와
``bpy.ops.ed.undo()``를 20회 반복합니다. 후자는 ``Ctrl+Z``가 실행하는
Blender Global Undo Operator와 동일한 경로입니다.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import traceback
from pathlib import Path
from time import monotonic

import bmesh
import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

USE_INSTALLED = os.environ.get("BLENDER_COLMAP_USE_INSTALLED") == "1"
PACKAGE_NAME = (
    "bl_ext.user_default.colmap_tools.tools.modifier"
    if USE_INSTALLED
    else "tools.modifier"
)
blender_colmap = importlib.import_module(PACKAGE_NAME)
camera_review_session = importlib.import_module(
    f"{PACKAGE_NAME}.blender.camera_review_session"
)
import_progress_session = importlib.import_module(
    f"{PACKAGE_NAME}.blender.import_progress_session"
)
ownership = importlib.import_module(f"{PACKAGE_NAME}.blender.ownership")
runtime_state = importlib.import_module(f"{PACKAGE_NAME}.blender.runtime_state")
undo_coordinator = importlib.import_module(
    f"{PACKAGE_NAME}.blender.undo_domain_coordinator"
)


REPEAT_COUNT = 20
STATE = {
    "phase": "START",
    "started": monotonic(),
    "deadline": monotonic() + 180.0,
    "cycle": 0,
    "temp_directory": None,
}


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def view3d_override():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
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
    raise RuntimeError("GUI 검증에 사용할 3D Viewport가 없습니다.")


def make_dataset(root: Path) -> None:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame.ppm").write_bytes(
        b"P6\n2 2\n255\n" + bytes((255, 64, 32) * 4)
    )
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 640 480 510 500 320 240\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n"
        "7 1 0 0 0 0 0 0 1 frame.ppm\n"
        "10 20 100 30 40 200 50 60 300\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n"
        "100 0 0 0 255 0 0 0.1 7 0\n"
        "200 1 0 0 0 255 0 0.1 7 1\n"
        "300 2 0 0 0 0 255 0.1 7 2\n",
        encoding="utf-8",
    )


def managed_point_object():
    matches = [
        obj
        for obj in bpy.data.objects
        if obj.get(ownership.OWNER_KEY) == ownership.OWNER_TOKEN
        and obj.get(ownership.RECORD_KIND_KEY) == "POINT_MESH"
        and obj.users_collection
    ]
    assert_true(len(matches) == 1, "관리 Point Object가 정확히 하나가 아닙니다.")
    return matches[0]


def select_first_point_and_remove():
    settings = bpy.context.scene.colmap_import
    with view3d_override():
        assert_true(
            bpy.ops.colmap.toggle_edit_point_mode() == {"FINISHED"},
            settings.point_selection_status,
        )
        point_object = managed_point_object()
        mesh = point_object.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        assert_true(len(bm.verts) == 3, "Undo 기준 Point 수가 3이 아닙니다.")
        for vertex in bm.verts:
            vertex.select = False
        bm.verts[0].select = True
        bmesh.update_edit_mesh(mesh)
        assert_true(
            bpy.ops.colmap.remove() == {"FINISHED"},
            settings.point_selection_status,
        )
        # Timer에서 실행한 Python Operator 호출은 UI Event 완료 경계처럼
        # ``UNDO`` Step을 자동 확정하지 않으므로 실제 Remove 버튼 완료와
        # 같은 Global Undo Step을 명시적으로 materialize합니다.
        bpy.ops.ed.undo_push(message="Point Remove Operator Step")


def assert_restored_state():
    scene = bpy.context.scene
    settings = scene.colmap_import
    state = runtime_state.ensure_runtime(scene)
    session = camera_review_session.current_review_session(
        bpy.context,
        strict=True,
    )
    assert_true(
        settings.edit_revision == 0
        and state.edit_revision == 0
        and state.index_revision == 0
        and not state.operations,
        "Global Undo가 Scene과 Runtime 편집 이력을 기준 상태로 복원하지 못했습니다.",
    )
    assert_true(
        len(state.current_reconstruction.points3d) == 3
        and len(state.point_id_order) == 3
        and len(managed_point_object().data.vertices) == 3,
        "Global Undo 뒤 Reconstruction, 인덱스 또는 Point Mesh가 복원되지 않았습니다.",
    )
    assert_true(
        not settings.point_edit_mode_active
        and not settings.camera_edit_mode_active
        and settings.remove_target == "NONE",
        "Global Undo 뒤 Point·Camera Edit Mode가 비활성으로 정규화되지 않았습니다.",
    )
    assert_true(
        session.edit_revision == 0
        and session.index_revision == 0
        and session.timeline_active
        and session.current_image_id == 7
        and bool(session.resident_images),
        "Global Undo 뒤 Camera Review·Timeline·Background가 새 상태로 연결되지 않았습니다.",
    )


def finish_success():
    elapsed = monotonic() - STATE["started"]
    print(
        "BLENDER_COLMAP_REMOVE_GLOBAL_UNDO_GUI_OK "
        f"cycles={STATE['cycle']} elapsed={elapsed:.3f}s"
    )
    if not USE_INSTALLED:
        blender_colmap.unregister()
    directory = STATE.get("temp_directory")
    if directory is not None:
        directory.cleanup()
    bpy.ops.wm.quit_blender()
    return None


def fail(exc):
    traceback.print_exc()
    print(
        "BLENDER_COLMAP_REMOVE_GLOBAL_UNDO_GUI_FAILED "
        f"phase={STATE['phase']} cycle={STATE['cycle']} error={exc}"
    )
    if not USE_INSTALLED:
        try:
            blender_colmap.unregister()
        except Exception:
            traceback.print_exc()
    directory = STATE.get("temp_directory")
    if directory is not None:
        directory.cleanup()
    bpy.ops.wm.quit_blender()
    return None


def advance():
    try:
        if monotonic() > STATE["deadline"]:
            raise TimeoutError(
                f"GUI Undo 프로브 제한 시간을 초과했습니다: {STATE['phase']}"
            )

        phase = STATE["phase"]
        settings = bpy.context.scene.colmap_import

        if phase == "START":
            directory = tempfile.TemporaryDirectory()
            STATE["temp_directory"] = directory
            dataset_root = Path(directory.name) / "remove_undo_dataset"
            make_dataset(dataset_root)
            settings.dataset_root = str(dataset_root)
            settings.input_format = "TEXT"
            with view3d_override():
                result = bpy.ops.colmap.import_model("INVOKE_DEFAULT")
            assert_true(
                result == {"RUNNING_MODAL"},
                f"GUI Import 시작에 실패했습니다: {result}",
            )
            STATE["phase"] = "WAIT_IMPORT"
            return 0.1

        if phase == "WAIT_IMPORT":
            if settings.status_kind == "ERROR":
                raise RuntimeError(settings.status)
            if (
                settings.status_kind != "SUCCESS"
                or import_progress_session.active_import_progress_session()
                is not None
            ):
                return 0.1
            assert_restored_state()
            STATE["phase"] = "REMOVE"
            return 0.1

        if phase == "REMOVE":
            select_first_point_and_remove()
            settings = bpy.context.scene.colmap_import
            edited = runtime_state.ensure_runtime(bpy.context.scene)
            assert_true(
                settings.edit_revision == 1
                and edited.edit_revision == 1
                and len(edited.current_reconstruction.points3d) == 2
                and not settings.point_edit_mode_active,
                "Point Remove 기준 상태가 만들어지지 않았습니다.",
            )
            STATE["phase"] = "GLOBAL_UNDO"
            return 0.1

        if phase == "GLOBAL_UNDO":
            with view3d_override():
                result = bpy.ops.ed.undo()
            assert_true(
                result == {"FINISHED"},
                f"Remove 직후 Blender Global Undo가 실패했습니다: {result}",
            )
            STATE["phase"] = "WAIT_GLOBAL_UNDO"
            return 0.1

        if phase == "WAIT_GLOBAL_UNDO":
            if undo_coordinator.undo_domain_reconcile_pending():
                return 0.05
            settings = bpy.context.scene.colmap_import
            if settings.edit_revision != 0:
                return 0.05
            assert_restored_state()
            STATE["cycle"] += 1
            if STATE["cycle"] >= REPEAT_COUNT:
                return finish_success()
            STATE["phase"] = "REMOVE"
            return 0.1

        raise RuntimeError(f"알 수 없는 GUI 검증 단계입니다: {phase}")
    except Exception as exc:
        return fail(exc)


def start():
    try:
        if USE_INSTALLED:
            assert_true(
                hasattr(bpy.types.Scene, "colmap_import"),
                "설치된 Extension이 등록되지 않았습니다.",
            )
        else:
            blender_colmap.register()
        bpy.app.timers.register(advance, first_interval=0.2)
    except Exception as exc:
        fail(exc)


start()
