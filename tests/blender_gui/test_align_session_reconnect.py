"""정식 설치본의 실제 데이터 저장과 새 프로세스의 Align 자동 재연결을 검사합니다."""
from __future__ import annotations

import argparse
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import time
import traceback

import addon_utils
import bpy
import numpy as np

MODULE = os.environ["COLMAP_TOOLS_INSTALLED_MODULE"]
OUTPUT = Path(os.environ["COLMAP_TOOLS_TEST_OUTPUT"]).resolve() / "session-reconnect"
PARSER = argparse.ArgumentParser()
PARSER.add_argument("--mode", choices=("prepare", "reopen", "layout"), required=True)
ARGS = PARSER.parse_args(sys.argv[sys.argv.index("--") + 1:])
STARTED = time.monotonic()
PHASE = "OPEN" if ARGS.mode == "prepare" else "WAIT_RESTORE"
ORIGINAL_WORKSPACE = None


def align_module(name):
    """검증 대상 설치 패키지 내부의 Align 모듈만 불러옵니다."""
    return import_module(MODULE + ".tools.align." + name)


def finish(passed, **details):
    """현재 새 프로세스 검증의 결과를 남기고 테스트 Blender만 종료합니다."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"passed": passed, "mode": ARGS.mode, "elapsed_seconds": time.monotonic() - STARTED, **details}
    (OUTPUT / (ARGS.mode + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.flush()
    sys.stderr.flush()
    if passed:
        bpy.ops.wm.quit_blender()
    else:
        os._exit(1)


def create_fixture(runtime):
    """두 COLMAP·연결 쿼드 OBJ·마커·확정 변환을 실제 가져오기 경계로 구성합니다."""
    for model_id in ("reconnect-a", "reconnect-p"):
        root = OUTPUT / model_id
        sparse = root / "sparse"
        sparse.mkdir(parents=True, exist_ok=True)
        (root / "images").mkdir(exist_ok=True)
        (sparse / "cameras.txt").write_text("1 SIMPLE_PINHOLE 640 480 500 320 240\n", encoding="utf-8")
        (sparse / "images.txt").write_text("1 1 0 0 0 0 0 0 1 fixture.jpg\n\n", encoding="utf-8")
        (sparse / "points3D.txt").write_text("101 0 0 0 255 0 0 0.1\n102 2 0 0 0 255 0 0.1\n103 0 2 0 0 0 255 0.1\n", encoding="utf-8")
        (root / "fixture.obj").write_text(
            "v 0 0 0\nv 2 0 0\nv 2 2 0\nv 0 2 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3 4/4\n", encoding="utf-8")
        # 반복마다 고정한 모델 ID를 실제 ImportJob의 식별자 생성기로 전달합니다.
        job = align_module("importer").create_import_job(root, input_format="TEXT", model_id_factory=lambda value=model_id: value)
        for _ in range(1000):
            status = job.step(runtime)
            if status != "RUNNING":
                break
        assert status == "FINISHED", job.error_message
    state = align_module("state")
    runtime.marker_selection.restore_snapshot(state.MarkerSelectionSnapshot(
        phase="IDLE", next_pair_id=2, active_pair_id=1,
        completed_pairs=(state.MarkerPair(
            pair_id=1,
            reference=state.SelectedPoint(model_role="REFERENCE", model_id="reconnect-a", point3d_id=101, local_position=(0., 0., 0.)),
            moving=state.SelectedPoint(model_role="MOVING", model_id="reconnect-p", point3d_id=101, local_position=(0., 0., 0.))),),
        draft=None, message=""))
    matrix = np.eye(4)
    matrix[:3, 3] = (1.0, 2.0, 3.0)
    runtime.model_repository.set_alignment_transform("reconnect-p", align_module("alignment.types").Sim3Transform(matrix_float64=matrix))
    persistence = align_module("project_persistence.runtime")
    align_module("project_persistence.store").write_active_project_state(persistence.build_project_state_snapshot(runtime))


def assert_restored(runtime):
    """재실행 뒤 실제 점군·내장 쿼드·마커·확정 행렬과 CPU 설정을 확인합니다."""
    repository = runtime.model_repository
    assert set(repository.models) == {"reconnect-a", "reconnect-p"}
    assert len(repository.companion_meshes) == 2
    assert len(runtime.marker_selection.completed_pairs) == 1
    np.testing.assert_allclose(repository.alignment_transform_for_model_id("reconnect-p").matrix_float64[:3, 3], (1., 2., 3.))
    meshes = [mesh for mesh in bpy.data.meshes if mesh.get("owner") == "COLMAP_ALIGN_EMBEDDED_OBJ"]
    assert len(meshes) == 2
    assert all([p.loop_total for p in mesh.polygons] == [4] for mesh in meshes)
    assert all(mesh.uv_layers.active is not None for mesh in meshes)
    assert bpy.context.preferences.addons[MODULE].preferences.cc_cpu_thread_limit == 3


def tick():
    """저장 프로세스와 새 재실행 프로세스에서 세션 생명주기를 순서대로 검사합니다."""
    global PHASE, ORIGINAL_WORKSPACE
    try:
        if time.monotonic() - STARTED > 60:
            raise RuntimeError(f"정식 Align 재연결 검증 시간 초과: {PHASE}")
        runtime = align_module("state").RUNTIME
        if runtime.workspace_setup_error:
            raise RuntimeError(runtime.workspace_setup_error)
        if PHASE == "OPEN":
            ORIGINAL_WORKSPACE = bpy.context.window.workspace
            assert bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") == {"FINISHED"}
            PHASE = "WAIT_ACTIVE"
        elif PHASE == "WAIT_ACTIVE":
            if runtime.session.phase != "ACTIVE":
                return 0.1
            create_fixture(runtime)
            bpy.context.preferences.addons[MODULE].preferences.cc_cpu_thread_limit = 3
            bpy.ops.wm.save_userpref()
            bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT / "align.blend"), check_existing=False)
            bpy.context.window.workspace = ORIGINAL_WORKSPACE
            PHASE = "WAIT_EXIT"
        elif PHASE == "WAIT_EXIT":
            if runtime.session.phase != "INACTIVE":
                return 0.1
            bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT / "layout.blend"), check_existing=False)
            finish(True, model_count=2, embedded_quads=2, marker_pairs=1)
            return None
        elif PHASE == "WAIT_RESTORE":
            if runtime.project_restore.issues:
                raise RuntimeError(str(runtime.project_restore.issues))
            if runtime.model_catalog.model_count != 2:
                return 0.1
            if ARGS.mode == "reopen" and runtime.session.phase != "ACTIVE":
                return 0.1
            if ARGS.mode == "layout":
                assert runtime.session.phase == "INACTIVE"
                assert not align_module("session").is_align_workspace_for_window(bpy.context.window)
            else:
                assert align_module("session").is_session_active_for_window(bpy.context.window)
            assert_restored(runtime)
            finish(True, model_count=2, embedded_quads=2, marker_pairs=1,
                   session_phase=runtime.session.phase, manual_workspace_switches=0)
            return None
        return 0.1
    except Exception as error:
        traceback.print_exc()
        finish(False, error=str(error), phase=PHASE)
        return None


def main():
    """격리 설치본을 활성화하고 저장 또는 새 프로세스 복원 검사를 예약합니다."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if not addon_utils.check(MODULE)[1]:
        addon_utils.enable(MODULE, default_set=True, persistent=True)
    assert addon_utils.check(MODULE)[1]
    bpy.app.timers.register(tick, first_interval=0.5)


main()
