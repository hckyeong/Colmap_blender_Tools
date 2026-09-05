"""실제 설치된 실험 애드온에서 표면 정렬 결과를 열고 화면과 복원 행렬을 확인합니다."""
from importlib import import_module
from pathlib import Path
import hashlib
import json
import os
import sys
import time
import traceback
import addon_utils
import bpy
import numpy as np

OUTPUT = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
ROOT = Path(os.environ["COLMAP_ALIGN_TEST_ENV_ROOT"]).resolve()
MODULE = os.environ["COLMAP_ALIGN_TEST_MODULE"]
STARTED = time.monotonic()
REQUESTED = False
READY = None


def write_report(value):
    """화면 검증의 성공과 실패를 결과 폴더에 명확히 기록합니다."""
    (OUTPUT / "gui_result.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def tick():
    """정상 복원 후 A 화면을 맞추고 결과 행렬과 실제 화면을 기록합니다."""
    global REQUESTED, READY
    try:
        if time.monotonic() - STARTED > 150:
            raise RuntimeError("결과 프로젝트 복원 시간 초과")
        runtime = import_module(MODULE + ".tools.align.state").RUNTIME
        if runtime.workspace_setup_error:
            raise RuntimeError(runtime.workspace_setup_error)
        if runtime.project_restore.phase in {"FAILED", "NEEDS_RELINK"}:
            raise RuntimeError(runtime.project_restore.last_message)
        if not REQUESTED:
            bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT")
            REQUESTED = True
            return .3
        if runtime.session.phase != "ACTIVE" or runtime.project_restore.phase != "IDLE" or len(runtime.model_repository.models) != 2:
            return .3
        report = json.loads((OUTPUT / "evaluation.json").read_text(encoding="utf-8"))
        manifest = json.loads((OUTPUT / "input_manifest.json").read_text(encoding="utf-8"))
        repository = runtime.model_repository
        moving_id = manifest["models"]["moving"]["id"]
        reference_id = manifest["models"]["reference"]["id"]
        expected = np.asarray(report["candidates"][report["selected"]]["alignment"])
        actual = repository.alignment_transform_for_model_id(moving_id).matrix_float64
        assert np.allclose(actual, expected, rtol=0, atol=1e-12), "이동 모델의 복원 정렬이 결과와 다릅니다."
        assert np.allclose(repository.alignment_transform_for_model_id(reference_id).matrix_float64,
                           manifest["models"]["reference"]["alignment"], rtol=0, atol=1e-12), "기준 모델이 바뀌었습니다."
        assert len(repository.companion_meshes) == 2, "연결 메시 복원 실패"
        renderer = import_module(MODULE + ".tools.align.viewport.renderer")
        assert set(renderer.render_model_ids_for_role("REFERENCE")) == {moving_id, reference_id}, "A 뷰에 두 결과가 모두 표시되지 않습니다."
        if READY is None:
            import_module(MODULE + ".tools.align.operators.workspace").fit_role_view(bpy.context.window, "REFERENCE")
            runtime.model_catalog.message = f"CloudCompare 표면 정렬 결과: 큰 거리 10% 제외 RMS {report['trimmed90_improvement_percent']:.1f}% 감소"
            for area in bpy.context.window.screen.areas:
                area.tag_redraw()
            # 검증한 결과 사본에만 화면 맞춤을 저장합니다.
            assert Path(bpy.data.filepath).resolve() == OUTPUT / "surface_aligned.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT / "surface_aligned.blend"), check_existing=False)
            READY = time.monotonic()
            return .5
        if time.monotonic() - READY < 2:
            return .5
        bpy.ops.screen.screenshot(filepath=str(OUTPUT / "blender_result.png"))
        items = {}
        for model_id, model in repository.models.items():
            mesh = repository.companion_meshes[model_id]
            items[model_id] = dict(points=int(model.points.count), vertices=mesh.vertex_count,
                                  triangles=mesh.face_count,
                                  alignment=repository.alignment_transform_for_model_id(model_id).matrix_float64.tolist())
        write_report(dict(passed=True, workspace=bpy.context.window.workspace.name,
            blend=bpy.data.filepath, installed_module=MODULE, models=items,
            screenshot=str(OUTPUT / "blender_result.png")))
        print("표면 정렬 결과의 설치본 복원과 화면 확인 완료", flush=True)
        return None
    except Exception as error:
        write_report(dict(passed=False, error=str(error), traceback=traceback.format_exc()))
        traceback.print_exc()
        return None


def main():
    """전용 설치본 경로와 배포 파일 무결성을 확인한 뒤 화면 검증을 시작합니다."""
    OUTPUT.relative_to(ROOT / "outputs")
    assert not bpy.app.background
    if not addon_utils.check(MODULE)[1]:
        addon_utils.enable(MODULE, default_set=False, persistent=True)
    addon = import_module(MODULE)
    package = Path(addon.__file__).resolve().parent
    assert package == ROOT / "blender_user/repository/colmap_align_test_env"
    report = json.loads((ROOT / "outputs/build-report.json").read_text(encoding="utf-8"))
    for relative, expected in report["package_hashes"].items():
        assert hashlib.sha256((package / relative).read_bytes()).hexdigest() == expected, relative
    bpy.app.timers.register(tick, first_interval=1)


main()
