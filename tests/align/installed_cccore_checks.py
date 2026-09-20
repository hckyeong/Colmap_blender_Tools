"""설치된 애드온의 실제 CC 네 형상 조합과 승인·Undo/Redo를 검증합니다."""

from __future__ import annotations

from importlib import import_module
import hashlib
import json
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import numpy as np


_GEOMETRY_CASES = (
    ("POINT_TO_POINT", False, False),
    ("POINT_TO_MESH", False, True),
    ("MESH_TO_POINT", True, False),
    ("MESH_TO_MESH", True, True),
)
_NATIVE_OPERATION_SEQUENCES = {
    "POINT_TO_POINT": ("distance", "distance", "register", "distance", "distance", "register", "distance"),
    "POINT_TO_MESH": ("distance", "distance", "register", "distance", "distance", "register", "distance"),
    "MESH_TO_POINT": ("distance", "distance", "register", "distance", "distance", "register", "distance"),
    "MESH_TO_MESH": (
        "distance", "distance", "distance", "register", "distance",
        "distance", "distance", "register", "distance", "distance",
    ),
}
_NATIVE_OPERATION_STAGES = {
    "POINT_TO_POINT": ("BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION"),
    "POINT_TO_MESH": ("BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION"),
    "MESH_TO_POINT": ("BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION"),
    "MESH_TO_MESH": (
        "BASELINE", "BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION",
        "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION", "REFINE_EVALUATION",
    ),
}
_MATRIX_TOLERANCE = 1.5e-3


def _installed_modules(module_prefix: str) -> tuple[dict[str, Any], Path]:
    """모든 정렬 모듈을 지정한 설치 패키지에서만 불러오고 위치를 확인합니다."""
    if not isinstance(module_prefix, str) or not module_prefix.strip():
        raise ValueError("검증할 설치 패키지 식별자가 없습니다.")
    package = import_module(module_prefix)
    package_root = Path(package.__file__).resolve().parent
    base = module_prefix + ".tools.align."
    names = {
        "state": "state",
        "colmap": "colmap_io.types",
        "mesh": "mesh_io.types",
        "repository": "model_repository",
        "axes": "display_axes",
        "transforms": "alignment.types",
        "runtime": "alignment.icp_runtime",
        "cccore": "alignment.icp_cccore_runtime",
        "properties": "properties",
    }
    modules = {name: import_module(base + relative) for name, relative in names.items()}
    for module in modules.values():
        Path(module.__file__).resolve().relative_to(package_root)
    return modules, package_root


def _surface_geometry() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """회전·이동·배율을 식별할 비대칭 닫힌 메시와 면 내부 표본을 만듭니다."""
    vertices = np.asarray((
        (-1.0, -0.7, -0.5), (1.4, -0.8, -0.4), (0.9, 1.2, -0.3), (-0.8, 0.7, -0.2),
        (-0.4, -0.5, 1.4), (0.7, -0.4, 1.1), (0.8, 0.9, 1.3), (-0.5, 0.8, 1.6),
    ), dtype=np.float64)
    triangles = np.asarray((
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ), dtype=np.uint32)
    faces = vertices[triangles]
    area = np.linalg.norm(np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0]), axis=1)
    random = np.random.default_rng(9761)
    selected = random.choice(len(triangles), size=4000, p=area / area.sum())
    first = np.sqrt(random.random(4000))
    second = random.random(4000)
    samples = ((1.0 - first)[:, None] * faces[selected, 0]
               + (first * (1.0 - second))[:, None] * faces[selected, 1]
               + (first * second)[:, None] * faces[selected, 2])
    return vertices, triangles, samples


def _similarity(scale: float, angle_degrees: float, translation: tuple[float, float, float]) -> np.ndarray:
    """검증 정답으로 사용할 균일 배율·Z 회전·이동 행렬을 독립적으로 만듭니다."""
    angle = np.deg2rad(angle_degrees)
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = scale * np.asarray(((np.cos(angle), -np.sin(angle), 0.0),
                                       (np.sin(angle), np.cos(angle), 0.0), (0.0, 0.0, 1.0)))
    matrix[:3, 3] = translation
    return matrix


def _transform(positions: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """행 좌표 배열에 독립적으로 동차 변환을 적용해 합성 원본을 준비합니다."""
    homogeneous = np.column_stack((positions, np.ones(len(positions))))
    return (matrix @ homogeneous.T).T[:, :3]


def _point_model(modules: dict[str, Any], model_id: str, positions: np.ndarray, source_path: Path):
    """설치본의 실제 COLMAP 자료형으로 모든 희소 등록점을 구성합니다."""
    types = modules["colmap"]
    count = len(positions)
    points = types.PointTable(
        point_ids=np.arange(101, 101 + count, dtype=np.uint64), positions=positions,
        colors=np.full((count, 3), 127, dtype=np.uint8), errors=np.zeros(count, dtype=np.float64),
        track_offsets=np.zeros(count + 1, dtype=np.uint64), track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint32), display_indices=np.arange(count, dtype=np.uint32),
    )
    return types.ColmapModelData(
        model_id=model_id, display_name=model_id, source_path=source_path, source_format="text", cameras={},
        images=types.ImageTable(image_records={}, point2d_offsets=np.zeros(1, dtype=np.uint64),
                               point2d_xy=np.empty((0, 2), dtype=np.float64),
                               point2d_point3d_ids=np.empty(0, dtype=np.uint64)),
        rigs={}, frames={}, points=points, bounds_min=positions.min(axis=0), bounds_max=positions.max(axis=0),
        geometry_revision=1,
    )


def _mesh_model(modules: dict[str, Any], model_id: str, vertices: np.ndarray,
                triangles: np.ndarray, source_path: Path):
    """설치본의 실제 메시 자료형으로 원본 정점과 삼각면 연결을 보관합니다."""
    indices = np.arange(len(vertices), dtype=np.uint32)
    return modules["mesh"].TriangleMeshModelData(
        model_id=model_id, display_name=model_id, source_path=source_path,
        vertices_float64=vertices, triangles_uint32=triangles,
        registration_vertex_indices=indices, display_vertex_indices=indices,
        bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0), geometry_revision=1,
    )


def _runtime_fixture(modules: dict[str, Any], output_root: Path, mode: str,
                     moving_mesh: bool, reference_mesh: bool):
    """독립 형상 축과 비항등 A/P 공통 C를 가진 설치본 런타임을 구성합니다."""
    vertices, triangles, points = _surface_geometry()
    initial = _similarity(1.2, -3.0, (-0.04, 0.02, -0.01))
    delta = _similarity(1.035, 2.0, (0.015, -0.01, 0.02))
    expected = delta @ initial
    reference_alignment = _similarity(1.1, 5.0, (0.03, -0.02, 0.04))
    runtime = modules["state"].RuntimeState()
    runtime.session_active = True
    axes = modules["axes"]
    main_axis = axes.create_model_display_transform("REALITY_CAPTURE", "Z", "-Y")
    mesh_axis = axes.create_model_display_transform("REALITY_CAPTURE_OBJ", "Y", "Z",
                                                     rotation_xyz_degrees=(11.0, -7.0, 19.0))
    for model_id, surface in (("reference", reference_mesh), ("moving", moving_mesh)):
        local_shape = (np.linalg.inv(reference_alignment) @ expected
                       if model_id == "reference" else np.eye(4))
        raw_points = _transform(points, np.linalg.inv(main_axis.matrix_float64) @ local_shape)
        raw_vertices = _transform(vertices, np.linalg.inv(mesh_axis.matrix_float64) @ local_shape)
        source_path = output_root / "합성원본" / mode / model_id
        model = _point_model(modules, model_id, raw_points, source_path / "sparse")
        mesh = _mesh_model(modules, model_id, raw_vertices, triangles, source_path / "surface.obj")
        repository = runtime.model_repository
        repository.add_model(model, main_axis)
        runtime.model_catalog.add_imported_model(model_id, model_id, select_after_import=model_id == "moving")
        repository.add_companion_mesh(model_id, mesh, modules["repository"].CompanionMeshSourceMetadata(
            str(mesh.source_path), hashlib.sha256(raw_vertices.tobytes() + triangles.tobytes()).hexdigest()), mesh_axis)
        repository.set_alignment_transform(model_id, modules["transforms"].Sim3Transform(
            matrix_float64=reference_alignment if model_id == "reference" else initial))
        runtime.model_catalog.set_geometry_visibility(model_id, show_surface=surface)
    assert runtime.model_catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
    return runtime, expected


def _raw_fingerprints(runtime) -> dict[str, str]:
    """계산·승인·이력 복원 중 원본 좌표와 삼각면이 변경되지 않았는지 해시합니다."""
    fingerprints = {}
    for model_id, model in runtime.model_repository.models.items():
        fingerprints[model_id + "/MAIN"] = hashlib.sha256(model.points.positions.tobytes()).hexdigest()
        mesh = runtime.model_repository.companion_meshes[model_id]
        fingerprints[model_id + "/COMPANION"] = hashlib.sha256(
            mesh.vertices_float64.tobytes() + mesh.triangles_uint32.tobytes()).hexdigest()
    return fingerprints


def _verified_thread_metadata(
    metadata: dict[str, Any],
    expected_requested: int,
    *,
    geometry_mode: str | None = None,
    require_calls: bool = True,
) -> dict[str, Any]:
    """설치본의 안전 패치·oneTBB 값·v4 준비 접두부와 고정 호출 골격을 검증합니다."""
    assert "farthest-mesh-correspondence-v1" in metadata["core_patch"].split("+")
    assert metadata["thread_backend"] == "oneTBB"
    assert metadata["tbb_version"] == "2022.0.0"
    assert metadata["requested_max_threads"] == expected_requested
    assert metadata["distance_multithread_support"] is True
    assert metadata["octree_multithread_support"] is True
    hardware = int(metadata["hardware_thread_count"])
    effective = int(metadata["effective_max_threads"])
    assert hardware >= 1 and effective == min(expected_requested, hardware)
    calls = metadata.get("logical_operations")
    if require_calls:
        assert metadata["native_process_count"] == 1
        assert metadata.get("native_calls") == calls
        assert isinstance(calls, list) and calls
        physical_calls = metadata.get("physical_native_calls")
        assert isinstance(physical_calls, list) and physical_calls
        assert metadata["physical_native_command_count"] == len(physical_calls)
        assert metadata["physical_native_command_count"] <= len(calls)
        assert geometry_mode in _NATIVE_OPERATION_SEQUENCES
        preparing_count = sum(call.get("stage") == "PREPARING" for call in calls)
        assert 1 <= preparing_count <= 9999 - max(map(len, _NATIVE_OPERATION_SEQUENCES.values())) - 1
        assert all(call.get("stage") == "PREPARING" and call.get("operation") == "distance"
                   for call in calls[:preparing_count])
        assert tuple(call.get("operation") for call in calls[preparing_count:]) == _NATIVE_OPERATION_SEQUENCES[geometry_mode]
        assert tuple(call.get("stage") for call in calls[preparing_count:]) == _NATIVE_OPERATION_STAGES[geometry_mode]
        assert metadata["session_exit_reason"] == "closed"
        assert metadata["session_statistics"]["geometry_load_count"] == len(metadata["geometry_statistics"])
        assert metadata["session_statistics"]["geometry_cache_hits"] > 0
        for index, call in enumerate(calls, start=1):
            assert set(call) == {
                "operation", "index", "sequence", "physical_sequence", "source_sequence",
                "source_logical_index", "stage", "execution_source", "evaluation_key_sha256",
                "elapsed_seconds", "roundtrip_seconds", "protocol_overhead_seconds",
                "requested_max_threads", "effective_max_threads", "data_geometry_id", "model_geometry_id",
                "timings", "geometry_load_count", "geometry_cache_hits", "octree_build_count",
                "octree_cache_hits", "mesh_grid_build_count", "mesh_grid_cache_hits",
            }
            assert call["operation"] in {"register", "distance"}
            assert call["index"] == index and call["sequence"] == index
            assert call["execution_source"] in {"native_session", "exact_evaluation_cache"}
            assert call["elapsed_seconds"] >= 0.0
            assert call["roundtrip_seconds"] >= 0.0
            assert call["protocol_overhead_seconds"] >= 0.0
            assert call["requested_max_threads"] == expected_requested
            assert call["effective_max_threads"] == effective
        assert [call["logical_index"] for call in physical_calls] == [
            call["index"] for call in calls if call["execution_source"] == "native_session"
        ]
        assert metadata["exact_evaluation_cache"]["hit_count"] == sum(
            call["execution_source"] == "exact_evaluation_cache" for call in calls
        )
        protocol_fields = (
            "physical_roundtrip_seconds", "protocol_overhead_seconds",
            "protocol_overhead_ratio_of_roundtrip", "protocol_overhead_fraction_of_total_alignment",
        )
        protocol = metadata["session_protocol_statistics"]
        assert set(protocol) == set(protocol_fields)
        assert protocol == {name: metadata[name] for name in protocol_fields}
        physical_roundtrip = sum(call["roundtrip_seconds"] for call in physical_calls)
        protocol_overhead = sum(call["protocol_overhead_seconds"] for call in physical_calls)
        assert np.isclose(protocol["physical_roundtrip_seconds"], physical_roundtrip, rtol=1.0e-12, atol=1.0e-12)
        assert np.isclose(protocol["protocol_overhead_seconds"], protocol_overhead, rtol=1.0e-12, atol=1.0e-12)
        assert np.isclose(
            protocol["protocol_overhead_fraction_of_total_alignment"],
            protocol_overhead / metadata["elapsed_seconds"], rtol=1.0e-12, atol=1.0e-12,
        )
    result = {
        "thread_backend": metadata["thread_backend"],
        "tbb_version": metadata["tbb_version"],
        "requested_max_threads": expected_requested,
        "effective_max_threads": effective,
        "hardware_thread_count": hardware,
    }
    if calls is not None:
        result["logical_operations"] = calls
        result["native_calls"] = calls
        result["physical_native_calls"] = metadata["physical_native_calls"]
        result["physical_native_command_count"] = metadata["physical_native_command_count"]
        result["exact_evaluation_cache"] = metadata["exact_evaluation_cache"]
        result["session_protocol_statistics"] = metadata["session_protocol_statistics"]
    return result


def _verified_sampling_metadata(metadata: dict[str, Any], moving_mesh: bool, reference_mesh: bool,
                                native_path: Path) -> dict[str, Any]:
    """설치본의 고정 범위 패치와 표본 목표·방향별 평가 분모·주지표를 검증합니다."""
    assert metadata["algorithm_version"] == metadata["backend_algorithm_version"] == "cccore-icp-v5"
    assert metadata["job_protocol_version"] == "cccore-job-v4"
    identity = metadata["native_build_identity"]
    assert identity["protocol_version"] == "cccore-native-v5"
    assert identity["registration_safety_patch"] == "farthest-mesh-correspondence-v1"
    runtime_manifest = json.loads(native_path.with_name("runtime_manifest.json").read_text(encoding="utf-8"))
    safety = runtime_manifest["dependencies"]["cccore"]["registration_safety_patch"]
    assert safety["id"] == identity["registration_safety_patch"]
    assert identity["registration_safety_patch_sha256"] == safety["sha256"]
    assert identity["patched_registration_sha256"] == safety["patched_sha256"]
    grid = runtime_manifest["dependencies"]["cccore"]["mesh_grid_memory_patch"]
    assert grid["id"] == identity["mesh_grid_memory_patch"] == "mesh-grid-memory-budget-v1"
    assert grid["id"] in identity["core_patch"].split("+")
    assert grid["id"] in metadata["core_patch"].split("+")
    assert identity["mesh_grid_memory_patch_sha256"] == grid["sha256"]
    assert identity["mesh_grid_source_sha256"] == {
        item["path"]: item["patched_sha256"] for item in grid["files"]
    }
    reuse = runtime_manifest["dependencies"]["cccore"]["mesh_grid_reuse_patch"]
    assert identity["mesh_grid_reuse_patch"] == reuse["id"] == "mesh-grid-reuse-v1"
    assert reuse["id"] in identity["core_patch"].split("+")
    assert identity["mesh_grid_reuse_patch_sha256"] == reuse["sha256"]
    assert identity["mesh_grid_reuse_source_sha256"] == {
        item["path"]: item["patched_sha256"] for item in reuse["files"]
    }
    domain = runtime_manifest["dependencies"]["cccore"]["mesh_grid_fixed_domain_patch"]
    assert identity["mesh_grid_fixed_domain_patch"] == domain["id"] == "mesh-grid-fixed-domain-v1"
    assert domain["id"] in identity["core_patch"].split("+")
    assert identity["mesh_grid_fixed_domain_patch_sha256"] == domain["sha256"]
    assert identity["mesh_grid_fixed_domain_source_sha256"] == {
        item["path"]: item["patched_sha256"] for item in domain["files"]
    }
    parameters = metadata["parameters"]
    assert parameters["point_sample_count"] == parameters["mesh_sample_count"] == 50000
    assert parameters["evaluation_sample_count"] == 30000
    assert parameters["overlap_mode"] == "ESTIMATED_ON_CANDIDATES"
    assert parameters["filter_out_farthest_points"] is True
    assert "overlap_ratio" not in parameters
    sampling = metadata["sampling_diagnostics"]
    assert sampling["optimizer_seed"] != sampling["evaluation_seed"]
    directions = sampling["directions"]
    expected_directions = ({"moving_to_reference", "reference_to_moving"} if moving_mesh and reference_mesh
                           else {"reference_to_moving"} if moving_mesh else {"moving_to_reference"})
    assert set(directions) == expected_directions
    total_evaluation = 0
    registration_count = 0
    for name, direction in directions.items():
        source_is_mesh = moving_mesh if name == "moving_to_reference" else reference_mesh
        count = direction["evaluation_sample_count"]
        assert type(count) is int and direction["evaluation"]["sample_count"] == count
        assert direction["evaluation"]["requested_sample_count"] == 30000
        if source_is_mesh:
            assert count == 30000
        else:
            assert 3 <= count <= min(30000, direction["domain"]["candidate_point_count"])
        total_evaluation += count
        if "registration" in direction:
            registration = direction["registration"]
            assert registration["requested_sample_count"] == 50000
            expected_count = 50000 if source_is_mesh else min(50000, direction["domain"]["candidate_point_count"])
            assert registration["sample_count"] == expected_count
            registration_count += expected_count
    assert sampling["registration_sample_count"] == registration_count
    assert all(stage["data_point_count"] == registration_count for stage in metadata["registration_stages"])
    for metric in metadata["candidate_metrics"].values():
        assert metric["count"] == metric["evaluation_sample_count"] == total_evaluation
        for name, direction in directions.items():
            assert metric["directions"][name]["count"] == direction["evaluation_sample_count"]
        directional_rms = [summary["rms"] for summary in metric["directions"].values()]
        assert np.isclose(metric["overlap_mean_rms"], np.mean(directional_rms), rtol=1e-12, atol=1e-12)
        assert np.isclose(metric["overlap_rms"], metric["rms"], rtol=1e-12, atol=1e-12)
        assert isinstance(metric["full_reference_metrics"], dict)
    return sampling


def _wait_for_result(runtime_module, runtime, *, timeout_seconds: float = 180.0):
    """실제 작업 파일을 공개 진행 API로 읽고 결과 미리보기까지 기다립니다."""
    deadline = monotonic() + timeout_seconds
    while runtime.icp_alignment.is_running:
        runtime_module.poll_icp_job(runtime)
        if monotonic() > deadline:
            runtime_module.invalidate_icp(runtime, "설치본 CC 검증 시간 제한")
            raise AssertionError("설치본 CC 작업이 제한 시간 안에 끝나지 않았습니다.")
        sleep(0.02)
    assert runtime.icp_alignment.has_preview, runtime.icp_alignment.last_error
    result = runtime.icp_alignment.result
    assert result is not None, "설치본 CC 작업 결과가 없습니다."
    return result


def _run_case(modules: dict[str, Any], output_root: Path, mode: str,
              moving_mesh: bool, reference_mesh: bool, python_path: Path, native_path: Path) -> dict[str, Any]:
    """실제 v4 정렬의 고정 표본·겹침 RMS와 기존 정답 행렬·승인·Undo/Redo를 검증합니다."""
    runtime, expected = _runtime_fixture(modules, output_root, mode, moving_mesh, reference_mesh)
    api = modules["runtime"]
    repository = runtime.model_repository
    before_p = repository.alignment_transform_for_model_id("moving").matrix_float64.copy()
    before_a = repository.alignment_transform_for_model_id("reference").matrix_float64.copy()
    before_catalog = runtime.model_catalog.snapshot()
    source_fingerprints = _raw_fingerprints(runtime)
    started = monotonic()
    try:
        handle = api.begin_icp_job(runtime, preset="PRECISE", scale_refine_enabled=True,
                                  solver_backend="CCCORELIB", open3d_python_path=str(python_path))
        arguments = tuple(handle.process.args)
        assert Path(arguments[0]).resolve() == python_path
        assert Path(arguments[2]).resolve() == Path(modules["cccore"].__file__).with_name("icp_cccore_worker.py").resolve()
        assert Path(arguments[arguments.index("--native-worker") + 1]).resolve() == native_path
        result = _wait_for_result(api, runtime)
        actual = result.final_alignment.matrix_float64
        assert result.solver_backend == "CCCORELIB"
        assert result.metadata["geometry_mode"] == mode
        assert bool(result.metadata["reverse_registration"]) == (mode == "MESH_TO_POINT")
        expected_threads = modules["properties"].configured_cc_cpu_thread_limit()
        thread_metadata = _verified_thread_metadata(
            dict(result.metadata),
            expected_threads,
            geometry_mode=mode,
        )
        sampling_metadata = _verified_sampling_metadata(dict(result.metadata), moving_mesh, reference_mesh, native_path)
        np.testing.assert_allclose(actual, expected, atol=_MATRIX_TOLERANCE, rtol=0.0)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("moving").matrix_float64, before_p)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("reference").matrix_float64, before_a)
        assert _raw_fingerprints(runtime) == source_fingerprints
        assert not handle.files.job_directory.exists(), "완료된 정렬 임시 폴더가 남아 있습니다."
        core_commit = str(result.metadata["core_commit"])
        native_hash = str(result.metadata["native_worker_sha256"])
        assert len(core_commit) == 40
        assert native_hash == hashlib.sha256(native_path.read_bytes()).hexdigest()
        quality = result.quality_status
        metrics = dict(result.metadata["best_candidate_metrics"])
        assert result.quality["rmse"] == metrics["overlap_mean_rms"]
        commit = api.approve_icp_result(runtime)
        provenance = runtime.model_catalog.models["moving"].alignment_provenance
        assert provenance.backend_id == "CCCORELIB"
        assert provenance.algorithm_version == "cccore-icp-v5"
        assert provenance.icp_rmse == metrics["overlap_mean_rms"]
        assert runtime.model_catalog.models["moving"].registration_status == "REGISTERED"
        expected_dependencies = tuple(sorted((("moving", "COMPANION" if moving_mesh else "MAIN"),
                                              ("reference", "COMPANION" if reference_mesh else "MAIN"))))
        assert provenance.solver_geometry_dependencies == expected_dependencies
        audit = json.loads(provenance.engine_metadata_json)
        assert len(provenance.engine_metadata_json.encode("utf-8")) <= 262144
        trace_summary = audit["execution_audit_storage"]
        assert trace_summary["version"] == 1
        for field in ("logical_operations", "native_calls", "physical_native_calls", "geometry_statistics"):
            records = result.metadata[field]
            expected_records = [item for item in records if (
                item.get("cache_scope") == "SESSION" if field == "geometry_statistics"
                else item.get("stage") != "PREPARING")]
            assert audit[field] == expected_records
            full_trace = json.dumps(records, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")
            assert trace_summary["fields"][field] == {
                "source_count": len(records), "retained_count": len(expected_records),
                "source_sha256": hashlib.sha256(full_trace).hexdigest(),
            }
        for field in (
            "thread_backend", "tbb_version", "requested_max_threads", "effective_max_threads",
            "hardware_thread_count", "distance_multithread_support",
            "octree_multithread_support", "native_process_count",
            "physical_native_command_count", "exact_evaluation_cache",
            "process_startup_seconds", "session_elapsed_seconds", "session_exit_reason",
            "session_statistics", "session_protocol_statistics",
            "physical_roundtrip_seconds", "protocol_overhead_seconds",
            "protocol_overhead_ratio_of_roundtrip", "protocol_overhead_fraction_of_total_alignment",
            "sampling_diagnostics", "baseline_metrics", "best_candidate_metrics", "native_build_identity",
        ):
            assert audit[field] == result.metadata[field]
        np.testing.assert_allclose(repository.alignment_transform_for_model_id("moving").matrix_float64, actual, atol=1e-12, rtol=0.0)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("reference").matrix_float64, before_a)

        def restore(snapshot, kind) -> None:
            """실제 UndoJournal이 보관한 CC 승인 스냅샷을 공개 복원 API로 적용합니다."""
            assert kind == api.icp_commit_command_kind()
            api.restore_icp_commit_snapshot(runtime, snapshot)

        assert runtime.undo_journal.undo_committed(restore)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("moving").matrix_float64, before_p)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("reference").matrix_float64, before_a)
        assert runtime.model_catalog.snapshot() == before_catalog
        assert runtime.model_catalog.models["moving"].registration_status == "PENDING"
        assert runtime.undo_journal.redo_committed(restore)
        np.testing.assert_allclose(repository.alignment_transform_for_model_id("moving").matrix_float64,
                                   commit.after.alignment.matrix_float64, atol=1e-12, rtol=0.0)
        np.testing.assert_array_equal(repository.alignment_transform_for_model_id("reference").matrix_float64, before_a)
        assert runtime.model_catalog.models["moving"].alignment_provenance == provenance
        assert runtime.model_catalog.models["moving"].registration_status == "REGISTERED"
        assert _raw_fingerprints(runtime) == source_fingerprints
        return {
            "geometry_mode": mode, "reverse_registration": mode == "MESH_TO_POINT",
            "core_commit": core_commit, "native_worker_sha256": native_hash,
            "quality": quality, "expected_alignment_4x4": expected.tolist(), "actual_alignment_4x4": actual.tolist(),
            "maximum_matrix_error": float(np.max(np.abs(actual - expected))),
            "matrix_tolerance": _MATRIX_TOLERANCE, "reference_unchanged": True, "source_arrays_unchanged": True,
            "approved": True, "undo_redo": True, "geometry_dependencies": [list(item) for item in expected_dependencies],
            "rms": float(metrics["rms"]), "trimmed_rms_90": float(metrics["trimmed_rms_90"]),
            "overlap_mean_rms": float(metrics["overlap_mean_rms"]),
            "overlap_rms": float(metrics["overlap_rms"]),
            "full_reference_rms": float(metrics["full_reference_metrics"]["rms"]),
            "sampling_diagnostics": sampling_metadata,
            "threading": thread_metadata,
            "elapsed_seconds": monotonic() - started,
        }
    except Exception:
        if runtime.icp_alignment.is_running or runtime.icp_alignment.job_handle is not None:
            api.invalidate_icp(runtime, "설치본 CC 검증 실패 후 작업 정리")
        raise


def _write_report(output_root: Path, report: dict[str, Any]) -> None:
    """설치본 검증 결과를 UTF-8 JSON으로 원자 교체하여 기록합니다."""
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / "installed-cccore-alignment.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def check_cccore_alignment(module_prefix: str, output_root: Path) -> dict[str, Any]:
    """설치본의 네 형상 조합을 실제 CC 엔진으로 승인하고 검증 요약 JSON을 저장합니다."""
    output_root = Path(output_root).resolve()
    report: dict[str, Any] = {"passed": False, "module": module_prefix, "cases": []}
    try:
        modules, package_root = _installed_modules(module_prefix)
        expected_python = (package_root / "tools/align/runtime/python/python.exe").resolve()
        expected_native = (package_root / "tools/align/runtime/cccore/colmap_cccore_worker.exe").resolve()
        python_path = Path(modules["properties"].bundled_cccore_python_path()).resolve()
        native_path = Path(modules["cccore"].bundled_cccore_worker_path()).resolve()
        assert python_path == expected_python and python_path.is_file(), "설치본의 전용 Python 경로가 아닙니다."
        assert native_path == expected_native and native_path.is_file(), "설치본의 CC 엔진 경로가 아닙니다."
        environment = modules["cccore"].validate_cccore_environment(str(python_path), str(native_path))
        native_environment = environment["native_environment"]
        expected_threads = int(native_environment["requested_max_threads"])
        environment_threads = _verified_thread_metadata(
            dict(native_environment),
            expected_threads,
            require_calls=False,
        )
        report.update(
            package_root=str(package_root),
            python_binary_path=str(python_path),
            native_worker_path=str(native_path),
            environment=environment,
            environment_threading=environment_threads,
        )
        for mode, moving_mesh, reference_mesh in _GEOMETRY_CASES:
            report["active_case"] = mode
            report["cases"].append(_run_case(modules, output_root, mode, moving_mesh, reference_mesh, python_path, native_path))
            print(f"설치본 CC 검증 완료: {mode}", flush=True)
        report.pop("active_case", None)
        report["passed"] = True
        _write_report(output_root, report)
        return report
    except Exception as error:
        report["error"] = str(error)
        _write_report(output_root, report)
        raise
