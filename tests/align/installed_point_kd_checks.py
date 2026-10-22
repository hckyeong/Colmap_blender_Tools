"""설치본 점 기반 엔진의 네 입력 조합·양방향·승인 이력을 실제 실행으로 검증합니다."""

from __future__ import annotations

from importlib import import_module
import hashlib
import json
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import numpy as np

from installed_cccore_checks import (_installed_modules, _mesh_model, _point_model, _raw_fingerprints,
                                     _similarity, _surface_geometry, _transform)


def _refined_geometry(levels: int) -> tuple[np.ndarray, np.ndarray]:
    """자동 거리 정책을 만족하는 충분히 촘촘한 비대칭 닫힌 표면을 만듭니다."""
    vertices, triangles, _ = _surface_geometry()
    for _ in range(levels):
        expanded = vertices.tolist()
        edges = {}
        faces = []
        for a, b, c in triangles:
            middle = []
            for start, end in ((a, b), (b, c), (c, a)):
                key = tuple(sorted((int(start), int(end))))
                if key not in edges:
                    edges[key] = len(expanded)
                    expanded.append(((vertices[start] + vertices[end]) * .5).tolist())
                middle.append(edges[key])
            ab, bc, ca = middle
            faces.extend(((int(a), ab, ca), (ab, int(b), bc), (ca, bc, int(c)), (ab, bc, ca)))
        vertices = np.asarray(expanded, dtype=np.float64)
        triangles = np.asarray(faces, dtype=np.uint32)
    return vertices, triangles


def _fixture(modules: dict[str, Any], root: Path, moving_mesh: bool, reference_mesh: bool, larger_moving: bool,
             *, mismatched_density: bool = False, additional_scale: float = 1.0):
    """개별 형상 축·기존 배율·알려진 추가 배율을 갖는 설치본 런타임을 구성합니다."""
    initial = _similarity(1.2, -3, (-.04, .02, -.01))
    expected = _similarity(additional_scale, .4, (.003, -.002, .004)) @ initial
    reference_alignment = _similarity(1.1, 5, (.03, -.02, .04))
    axes = modules["axes"]
    main_axes = axes.create_model_display_transform("REALITY_CAPTURE", "Z", "-Y")
    mesh_axes = axes.create_model_display_transform("REALITY_CAPTURE_OBJ", "Y", "Z", rotation_xyz_degrees=(11, -7, 19))
    runtime = modules["state"].RuntimeState()
    runtime.session_active = True
    for model_id, surface in (("reference", reference_mesh), ("moving", moving_mesh)):
        dense = larger_moving and (model_id == "moving" or (moving_mesh != reference_mesh and not mismatched_density))
        vertices, triangles = _refined_geometry(6 if dense else 5)
        local = np.linalg.inv(reference_alignment) @ expected if model_id == "reference" else np.eye(4)
        raw_points = _transform(vertices, np.linalg.inv(main_axes.matrix_float64) @ local)
        raw_vertices = _transform(vertices, np.linalg.inv(mesh_axes.matrix_float64) @ local)
        model = _point_model(modules, model_id, raw_points, root / model_id / "sparse")
        mesh = _mesh_model(modules, model_id, raw_vertices, triangles, root / model_id / "surface.obj")
        repository = runtime.model_repository
        repository.add_model(model, main_axes)
        runtime.model_catalog.add_imported_model(model_id, model_id, select_after_import=model_id == "moving")
        repository.add_companion_mesh(model_id, mesh, modules["repository"].CompanionMeshSourceMetadata(
            str(mesh.source_path), hashlib.sha256(raw_vertices.tobytes() + triangles.tobytes()).hexdigest()), mesh_axes)
        repository.set_alignment_transform(model_id, modules["transforms"].Sim3Transform(
            matrix_float64=reference_alignment if model_id == "reference" else initial))
        runtime.model_catalog.set_geometry_visibility(model_id, show_surface=surface)
    assert runtime.model_catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
    return runtime, expected


def _run_case(modules: dict[str, Any], root: Path, moving_mesh: bool, reference_mesh: bool,
              larger_moving: bool, python: Path, native: Path, *, require_exact: bool = True,
              mismatched_density: bool = False, sim3: bool = False) -> dict[str, Any]:
    """설치본의 강체·Sim(3) 정렬과 동반 형상·승인·실행 취소·출처를 검증합니다."""
    additional_scale = .99 if sim3 else 1.0
    runtime, expected = _fixture(modules, root, moving_mesh, reference_mesh, larger_moving,
                                 mismatched_density=mismatched_density, additional_scale=additional_scale)
    api = modules["runtime"]
    repository = runtime.model_repository
    before_p = repository.alignment_transform_for_model_id("moving").matrix_float64.copy()
    before_a = repository.alignment_transform_for_model_id("reference").matrix_float64.copy()
    source_hashes = _raw_fingerprints(runtime)
    started = monotonic()
    try:
        handle = modules["point_kd"].begin_point_kd_job(runtime, str(python), str(native),
                    runtime_parameters=modules["point_kd"].PointKdRuntimeParameters(max_threads=1, scale_mode='SIM3' if sim3 else 'FIXED'))
        deadline = monotonic() + 90
        while runtime.icp_alignment.is_running:
            api.poll_icp_job(runtime)
            assert monotonic() < deadline, "설치본 점 기반 정렬 시간 제한을 넘었습니다."
            sleep(.02)
        assert runtime.icp_alignment.has_preview, runtime.icp_alignment.last_error
        result = runtime.icp_alignment.result
        assert not handle.files.job_directory.exists()
        assert result.solver_backend == "POINT_KD_ICP" and result.algorithm_version == "point-kd-icp-v3"
        assert result.metadata["input_policy"] == "ALL_VALID_POINTS"
        assert result.metadata["independent_evaluation_performed"] is False
        assert result.metadata["downsampling_performed"] is False
        expected_reverse = ((moving_mesh and not reference_mesh) or (moving_mesh == reference_mesh and larger_moving))
        assert result.metadata["reverse_registration"] is expected_reverse
        if require_exact:
            np.testing.assert_allclose(result.final_alignment.matrix_float64, expected, rtol=0, atol=3e-5)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("moving").matrix_float64, before_p)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("reference").matrix_float64, before_a)
        assert np.isclose(result.icp_delta.uniform_scale, additional_scale, rtol=0, atol=1e-6 if sim3 else 1e-12)
        assert result.metadata['scale_mode'] == ('SIM3' if sim3 else 'FIXED')
        commit = api.approve_icp_result(runtime)
        provenance = runtime.model_catalog.models["moving"].alignment_provenance
        assert provenance.backend_id == "POINT_KD_ICP" and provenance.algorithm_version == "point-kd-icp-v3"
        assert json.loads(provenance.engine_metadata_json)["independent_evaluation_performed"] is False
        api.restore_icp_commit_snapshot(runtime, commit.before)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("moving").matrix_float64, before_p)
        api.restore_icp_commit_snapshot(runtime, commit.after)
        np.testing.assert_allclose(repository.alignment_transform_for_model_id("moving").matrix_float64,
                                  result.final_alignment.matrix_float64, rtol=0, atol=1e-12)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("reference").matrix_float64, before_a)
        assert _raw_fingerprints(runtime) == source_hashes
        actual = result.final_alignment.matrix_float64
        relative = actual @ np.linalg.inv(expected)
        reference_scale = np.cbrt(np.linalg.det(relative[:3, :3]))
        rotation = relative[:3, :3] / reference_scale
        angle = np.rad2deg(np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1)))
        raw = repository.models["moving"].points.positions
        axes = repository.display_transform_for_model_id("moving").matrix_float64
        source = _transform(raw, axes)
        truth = _transform(source, expected)
        residuals = np.linalg.norm(_transform(source, actual) - truth, axis=1)
        diagonal = float(np.linalg.norm(np.ptp(truth, axis=0)))
        truth_rms = float(np.sqrt(np.mean(residuals ** 2)))
        return {"moving_mesh": moving_mesh, "reference_mesh": reference_mesh, "sim3": sim3,
                "larger_moving": larger_moving and moving_mesh == reference_mesh,
                "dense_both": larger_moving and moving_mesh != reference_mesh and not mismatched_density,
                "mismatched_density": mismatched_density,
                "reverse_registration": expected_reverse, "quality": result.quality_status,
                "maximum_matrix_error": float(np.max(np.abs(result.final_alignment.matrix_float64 - expected))),
                "method": result.metadata["method"], "native_result": result.metadata["native_result"],
                "approved": True, "undo_redo": True, "source_arrays_unchanged": True,
                "reference_unchanged": True, "independent_evaluation_performed": False,
                "synthetic_truth": {"rms": truth_rms, "diagonal": diagonal,
                                    "normalized_rms": truth_rms / diagonal, "target_normalized_rms": 1e-6,
                                    "target_met": truth_rms / diagonal <= 1e-6,
                                    "rotation_error_degrees": float(angle),
                                    "matrix_translation_error": float(np.linalg.norm(actual[:3, 3] - expected[:3, 3]))},
                "elapsed_seconds": monotonic() - started}
    finally:
        if runtime.icp_alignment.is_running or runtime.icp_alignment.job_handle is not None:
            api.invalidate_icp(runtime, "설치본 점 기반 정렬 검증 종료")


def check_point_kd_alignment(module_prefix: str, output_root: Path) -> dict[str, Any]:
    """설치 패키지의 강체·Sim(3) 네 조합·양방향과 밀도 차이 경계를 검증합니다."""
    modules, package_root = _installed_modules(module_prefix)
    modules["point_kd"] = import_module(module_prefix + ".tools.align.alignment.icp_point_kd_runtime")
    python = Path(modules["properties"].bundled_point_kd_python_path()).resolve()
    native = Path(modules["point_kd"].bundled_point_kd_worker_path()).resolve()
    assert python == package_root / "tools/align/runtime/python/python.exe" and python.is_file()
    assert native == package_root / "tools/align/runtime/point_kd/colmap_point_kd_worker.exe" and native.is_file()
    environment = modules["point_kd"].validate_point_kd_environment(str(python), str(native))
    report = {"passed": False, "module": module_prefix, "environment": environment, "cases": [], "accuracy_limitations": []}
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        for moving_mesh, reference_mesh in ((False, False), (False, True), (True, False), (True, True)):
            for larger in (False, True):
                report["cases"].append(_run_case(modules, output_root, moving_mesh, reference_mesh, larger, python, native))
                report["cases"].append(_run_case(modules, output_root, moving_mesh, reference_mesh, larger, python, native, sim3=True))
                print(f"설치본 점 기반 정렬 검증 완료: 이동 메시={moving_mesh} · 기준 메시={reference_mesh} · 큰 이동={larger}", flush=True)
        for moving_mesh, reference_mesh in ((False, True), (True, False)):
            report["accuracy_limitations"].append(_run_case(modules, output_root, moving_mesh, reference_mesh,
                True, python, native, require_exact=False, mismatched_density=True))
        report["passed"] = True
        return report
    finally:
        path = output_root / "installed-point-kd-alignment.json"
        path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
