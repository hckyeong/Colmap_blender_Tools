"""명시된 CPython 3.12 외부 ICP worker의 실제 파일·수치 통합을 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import numpy as np


_ALGORITHM_VERSION = "hybrid-point-to-plane-icp-v2"
_JOB_PROTOCOL_VERSION = "icp-job-protocol-v1"
_OPEN3D_VERSION = "0.19.0"
_SMALL_GICP_VERSION = "1.0.1"
_TEST_JOB_DIRECTORY_PREFIX = "colmap_align_icp_open3d_test_"
_BACKEND_ALGORITHM_VERSIONS = {
    "OPEN3D_GICP": "open3d-gicp-v1",
    "SMALL_GICP": "small-gicp-v6",
    "OPEN3D_ROBUST_POINT_TO_PLANE": "open3d-robust-point-to-plane-v1",
}


@dataclass(frozen=True, slots=True)
class _Open3dJobFiles:
    """통합 테스트가 만든 독립 Open3D worker job 파일 경로를 보관합니다."""

    job_directory: Path

    @property
    def result_path(self) -> Path:
        """worker의 JSON 결과 파일 경로를 반환합니다."""

        return self.job_directory / "result.json"

    @property
    def result_array_path(self) -> Path:
        """worker의 NPZ 결과 파일 경로를 반환합니다."""

        return self.job_directory / "result.npz"

    @property
    def error_path(self) -> Path:
        """worker의 구조화 오류 파일 경로를 반환합니다."""

        return self.job_directory / "error.json"

    @property
    def cancel_path(self) -> Path:
        """worker가 확인할 취소 sentinel 경로를 반환합니다."""

        return self.job_directory / "cancel.request"


def _project_root() -> Path:
    """worker 스크립트와 독립 fixture가 있는 프로젝트 루트를 반환합니다."""

    return Path(__file__).resolve().parents[3]


def _worker_script_path(project_root: Path, backend_id: str) -> Path:
    """선택 backend의 실제 배포 worker 절대 경로를 반환합니다."""

    worker_name = (
        "icp_small_gicp_worker.py"
        if str(backend_id).strip().upper() == "SMALL_GICP"
        else "icp_open3d_worker.py"
    )
    script_path = project_root / "tools" / "align" / "alignment" / worker_name
    if not script_path.is_file():
        raise AssertionError(f"외부 ICP worker 스크립트를 찾을 수 없습니다: {script_path}")
    return script_path


def _run_worker(
    project_root: Path,
    *arguments: str,
    cwd: Path | None = None,
    backend_id: str = "OPEN3D_GICP",
) -> subprocess.CompletedProcess[str]:
    """현재 명시 CPython으로 선택 backend worker를 격리 실행하고 출력을 수집합니다."""

    command = [
        sys.executable,
        "-I",
        str(_worker_script_path(project_root, backend_id)),
        *arguments,
    ]
    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else str(project_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        shell=False,
        env=environment,
    )


def _surface_points() -> np.ndarray:
    """GICP와 Point-to-Plane 모두에 쓸 결정적 비평면 전체점 surface를 생성합니다."""

    coordinates = np.linspace(-1.0, 1.0, num=15, dtype=np.float64)
    x_coordinates, y_coordinates = np.meshgrid(coordinates, coordinates, indexing="ij")
    z_coordinates = (
        0.16 * np.sin(1.7 * x_coordinates)
        + 0.11 * np.cos(1.3 * y_coordinates)
        + 0.07 * x_coordinates * y_coordinates
    )
    return np.ascontiguousarray(
        np.column_stack(
            (
                x_coordinates.reshape(-1),
                y_coordinates.reshape(-1),
                z_coordinates.reshape(-1),
            )
        ),
        dtype=np.float64,
    )


def _rigid_delta() -> np.ndarray:
    """P(source)에서 A(target)로 향하는 알려진 작은 rigid 변환을 반환합니다."""

    angle = float(np.deg2rad(2.5))
    matrix = np.identity(4, dtype=np.float64)
    matrix[:3, :3] = np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    matrix[:3, 3] = np.asarray((0.018, -0.014, 0.009), dtype=np.float64)
    return matrix


def _current_alignment() -> np.ndarray:
    """직접 Align fixture에 사용할 비Identity Sim(3) 확정 변환을 반환합니다."""

    angle = float(np.deg2rad(7.0))
    scale = 1.25
    matrix = np.identity(4, dtype=np.float64)
    matrix[:3, :3] = scale * np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    matrix[:3, 3] = np.asarray((1.5, -0.75, 0.25), dtype=np.float64)
    return matrix


def _apply_matrix(matrix: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """column-vector 4×4 변환을 테스트 N×3 위치 배열에 한 번 적용합니다."""

    return np.ascontiguousarray(
        positions @ matrix[:3, :3].T + matrix[:3, 3],
        dtype=np.float64,
    )


def _parameters(*, scale_refine: bool = False) -> dict[str, object]:
    """전체점 외부 worker에 필요한 거리 단계와 선택적 제한형 scale 설정을 반환합니다."""

    return {
        "scale_mode": "REFINE" if scale_refine else "LOCKED",
        "scale_refine_enabled": bool(scale_refine),
        "scale_refine_max_relative_change": 0.05,
        "scale_refine_max_step": 0.005,
        "scale_refine_max_iterations": 5,
        "scale_refine_min_rmse_improvement": 0.02,
        "scale_refine_min_inliers": 100,
        "scale_refine_min_fitness": 0.30,
        "scale_refine_min_reciprocal": 0.40,
        "scale_refine_min_overlap_coverage": 0.30,
        "minimum_correspondences": 30,
        "max_rotation_degrees": 20.0,
        "max_translation_fraction": 0.5,
        "robust_kernel_scale": 1.345,
        "levels": [
            {
                "name": "coarse",
                "max_iterations": 8,
                "normal_radius_multiplier": 3.0,
                "normal_maximum_neighbors": 32,
            },
            {
                "name": "medium",
                "max_iterations": 8,
                "normal_radius_multiplier": 3.0,
                "normal_maximum_neighbors": 32,
            },
            {
                "name": "fine",
                "max_iterations": 8,
                "normal_radius_multiplier": 3.0,
                "normal_maximum_neighbors": 32,
            },
        ],
    }


def _create_open3d_job_directory() -> _Open3dJobFiles:
    """애드온 package 없이 테스트 전용 임시 worker job 디렉터리를 만듭니다."""

    directory = Path(tempfile.mkdtemp(prefix=_TEST_JOB_DIRECTORY_PREFIX))
    return _Open3dJobFiles(job_directory=directory)


def _write_array(path: Path, array: np.ndarray) -> None:
    """worker가 읽을 C-contiguous pickle 없는 NPY 입력 하나를 기록합니다."""

    np.save(path, np.ascontiguousarray(array), allow_pickle=False)


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    """worker 실행 전에 UTF-8 JSON manifest를 직접 기록합니다."""

    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _write_open3d_job(
    backend_id: str,
    *,
    use_marker_seed: bool = True,
    scale_refine: bool = False,
):
    """실제 worker가 읽을 전체점 NPY·manifest fixture와 기대 Sim(3)을 기록합니다."""

    if backend_id not in _BACKEND_ALGORITHM_VERSIONS:
        raise AssertionError(f"지원하지 않는 Open3D 통합 backend입니다: {backend_id}")
    if scale_refine and backend_id != "SMALL_GICP":
        raise AssertionError("Scale Refine fixture는 Small GICP에서만 사용할 수 있습니다.")
    initial_alignment = (
        np.identity(4, dtype=np.float64)
        if use_marker_seed
        else _current_alignment()
    )
    source_positions = _apply_matrix(initial_alignment, _surface_points())
    expected_delta = _rigid_delta()
    if scale_refine:
        scale_delta = np.identity(4, dtype=np.float64)
        scale_delta[:3, :3] *= 1.025
        expected_delta = scale_delta @ expected_delta
    target_positions = _apply_matrix(expected_delta, source_positions)
    source_ids = np.arange(10_001, 10_001 + source_positions.shape[0], dtype=np.uint64)
    target_ids = np.arange(20_001, 20_001 + target_positions.shape[0], dtype=np.uint64)
    files = _create_open3d_job_directory()
    try:
        _write_array(files.job_directory / "moving_positions.npy", source_positions)
        _write_array(files.job_directory / "moving_point_ids.npy", source_ids)
        _write_array(files.job_directory / "reference_positions.npy", target_positions)
        _write_array(files.job_directory / "reference_point_ids.npy", target_ids)
        _write_array(
            files.job_directory / "moving_confidence_weights.npy",
            np.ones(source_positions.shape[0], dtype=np.float64),
        )
        _write_array(
            files.job_directory / "moving_isolation_candidates.npy",
            np.zeros(source_positions.shape[0], dtype=np.uint8),
        )
        _write_array(
            files.job_directory / "reference_confidence_weights.npy",
            np.ones(target_positions.shape[0], dtype=np.float64),
        )
        _write_array(
            files.job_directory / "reference_isolation_candidates.npy",
            np.zeros(target_positions.shape[0], dtype=np.uint8),
        )
        _write_manifest(
            files.job_directory / "manifest.json",
            {
                "manifest_version": 2,
                "job_id": files.job_directory.name,
                "input_fingerprint": (
                    f"open3d-integration-{backend_id.lower()}"
                    if use_marker_seed
                    else f"open3d-integration-{backend_id.lower()}-direct"
                ) + ("-scale-refine" if scale_refine else ""),
                "algorithm_version": _ALGORITHM_VERSION,
                "backend_id": backend_id,
                "backend_algorithm_version": _BACKEND_ALGORITHM_VERSIONS[backend_id],
                "job_protocol_version": _JOB_PROTOCOL_VERSION,
                "moving_cloud_frame": "INITIAL_PROJECT_WORLD",
                "parameters": _parameters(scale_refine=scale_refine),
                "moving_model_id": "moving",
                "reference_model_ids": ["reference"],
                "initial_alignment": initial_alignment.tolist(),
                "marker_uniform_scale": _uniform_scale(initial_alignment),
                "marker_max_error": 0.03 if use_marker_seed else 0.0,
                "marker_rms": 0.01 if use_marker_seed else 0.0,
                "marker_reference_project": (
                    target_positions[:3].tolist() if use_marker_seed else None
                ),
                "marker_moving_project": (
                    source_positions[:3].tolist() if use_marker_seed else None
                ),
                "marker_signature": (
                    "open3d-integration-marker"
                    if use_marker_seed
                    else "open3d-integration-current-alignment"
                ),
                "model_revision_snapshot": [["moving", 1], ["reference", 2]],
                "model_state_snapshot": [
                    ["moving", 1, "moving-display", "moving-alignment"],
                    ["reference", 2, "reference-display", "reference-alignment"],
                ],
                "confidence_preprocessing": {
                    "enabled": True,
                    "k": 24,
                    "cache_hit_count": 0,
                    "cache_miss_count": 2,
                    "calculation_seconds": 0.0,
                    "models": [],
                },
                "arrays": {
                    "moving": {
                        "model_id": "moving",
                        "positions_file": "moving_positions.npy",
                        "point_ids_file": "moving_point_ids.npy",
                        "confidence_weights_file": "moving_confidence_weights.npy",
                        "isolation_candidates_file": "moving_isolation_candidates.npy",
                    },
                    "references": [
                        {
                            "model_id": "reference",
                            "positions_file": "reference_positions.npy",
                            "point_ids_file": "reference_point_ids.npy",
                            "confidence_weights_file": "reference_confidence_weights.npy",
                            "isolation_candidates_file": "reference_isolation_candidates.npy",
                        }
                    ],
                },
            },
        )
    except BaseException:
        _cleanup_job(files)
        raise
    return files, source_positions, target_positions, expected_delta, initial_alignment


def _cleanup_job(files: Any) -> None:
    """통합 테스트가 만든 임시 worker job 디렉터리만 안전하게 정리합니다."""

    job_directory = Path(files.job_directory).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if not job_directory.exists():
        return
    if (
        not job_directory.is_dir()
        or job_directory.parent != temporary_root
        or not job_directory.name.startswith(_TEST_JOB_DIRECTORY_PREFIX)
    ):
        raise AssertionError(f"통합 테스트 job 정리 대상이 올바르지 않습니다: {job_directory}")
    shutil.rmtree(job_directory)


def _assert_worker_success(completed: subprocess.CompletedProcess[str]) -> None:
    """실제 worker가 오류 없이 종료했는지 stdout·stderr를 포함해 확인합니다."""

    if completed.returncode != 0:
        raise AssertionError(
            "Open3D worker 실행이 실패했습니다.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def _result_arrays(path: Path) -> dict[str, np.ndarray]:
    """pickle 없이 기록된 Open3D 결과 NPZ의 세 행렬을 읽어 반환합니다."""

    with np.load(path, allow_pickle=False) as archive:
        return {name: np.ascontiguousarray(archive[name]) for name in archive.files}


def _assert_result_metadata(
    metadata: object,
    files: _Open3dJobFiles,
    backend_id: str,
    *,
    direct: bool = False,
    scale_refine: bool = False,
) -> None:
    """worker JSON 결과의 backend·환경·미리보기 식별자 계약을 직접 확인합니다."""

    if not isinstance(metadata, dict):
        raise AssertionError("Open3D worker 결과 JSON 최상위 값이 객체가 아닙니다.")
    required_fields = {
        "job_id",
        "input_fingerprint",
        "algorithm_version",
        "backend_id",
        "backend_algorithm_version",
        "job_protocol_version",
        "moving_model_id",
        "reference_model_ids",
        "quality",
        "level_results",
        "parameter_fingerprint",
        "model_revision_snapshot",
        "model_state_snapshot",
        "marker_signature",
        "solver_stop_reason",
        "user_review_required",
        "full_input_audit",
        "worker_python_version",
        "worker_numpy_version",
        "platform_architecture",
        "local_origin",
        "moving_diagonal",
        "delta_translation",
        "delta_rotation_degrees",
    }
    missing = required_fields.difference(metadata)
    assert not missing, f"Open3D worker 결과 필수 키 누락: {sorted(missing)}"
    assert metadata["job_id"] == files.job_directory.name
    expected_fingerprint = f"open3d-integration-{backend_id.lower()}"
    if direct:
        expected_fingerprint += "-direct"
    if scale_refine:
        expected_fingerprint += "-scale-refine"
    assert metadata["input_fingerprint"] == expected_fingerprint
    assert metadata["algorithm_version"] == _ALGORITHM_VERSION
    # 결과 backend가 요청 backend와 다르면 custom fallback 또는 혼합 결과입니다.
    assert metadata["backend_id"] == backend_id
    assert metadata["backend_algorithm_version"] == _BACKEND_ALGORITHM_VERSIONS[backend_id]
    assert metadata["job_protocol_version"] == _JOB_PROTOCOL_VERSION
    assert metadata["moving_model_id"] == "moving"
    assert metadata["reference_model_ids"] == ["reference"]
    assert isinstance(metadata["parameter_fingerprint"], str) and metadata["parameter_fingerprint"]
    assert isinstance(metadata["solver_stop_reason"], str) and metadata["solver_stop_reason"]
    assert metadata["worker_python_version"].startswith("3.12.")
    assert isinstance(metadata["worker_numpy_version"], str) and metadata["worker_numpy_version"]
    if backend_id == "SMALL_GICP":
        assert metadata["small_gicp_version"] == _SMALL_GICP_VERSION
        assert not metadata.get("open3d_version")
        metrics = metadata["best_candidate_metrics"]
        assert metrics["distance_unit"] == "PROJECT_WORLD_UNIT"
        assert np.isfinite(metrics["point_to_point_rmse"])
        assert np.isfinite(metrics["weighted_point_to_point_rmse"])
        assert np.isfinite(metrics["residual_median"])
        assert np.isfinite(metrics["residual_p90"])
        assert metrics["evaluation_correspondence_count"] == metrics["inlier_count"]
        assert metadata["confidence_preprocessing"]["enabled"] is True
        assert metadata["confidence_preprocessing"]["k"] == 24
    else:
        assert metadata["open3d_version"] == _OPEN3D_VERSION
    assert metadata["platform_architecture"] == "64bit"
    quality = metadata["quality"]
    assert isinstance(quality, dict)
    assert quality.get("status") in {"PASS", "REVIEW"}
    assert type(metadata["user_review_required"]) is bool
    if quality["status"] == "REVIEW":
        assert metadata["user_review_required"] is True
    assert isinstance(metadata["level_results"], list) and metadata["level_results"]
    assert isinstance(metadata["full_input_audit"], dict)


def _uniform_scale(matrix: np.ndarray) -> float:
    """검증용 Sim(3) 행렬의 균일 scale을 특이값 평균으로 계산합니다."""

    return float(np.mean(np.linalg.svd(matrix[:3, :3], compute_uv=False)))


def _assert_result_diagnostics_match_npz(
    metadata: dict[str, Any],
    arrays: dict[str, np.ndarray],
) -> None:
    """worker JSON 진단값이 최종 NPZ icp_delta 공통 계산값과 정확히 같은지 확인합니다."""

    from tools.align.alignment.icp_external_contract import analyze_external_icp_delta

    diagnostics = analyze_external_icp_delta(
        arrays["icp_delta"],
        np.asarray(metadata["local_origin"], dtype=np.float64),
        field_name="외부 worker 결과 fixture",
    )
    assert metadata["delta_translation"] == diagnostics.translation_distance
    assert metadata["delta_rotation_degrees"] == diagnostics.rotation_degrees


def _assert_full_input_audit(metadata: dict[str, Any], source_count: int, target_count: int) -> None:
    """worker가 crop 이후 전체점 집합을 모든 거리 단계에서 재사용했는지 확인합니다."""

    audit = metadata["full_input_audit"]
    assert audit["input_policy"] == "FULL_OVERLAP_NO_DOWNSAMPLING"
    assert audit["source_original_count"] == source_count
    assert audit["target_original_count"] == target_count
    # fixture는 넉넉한 overlap margin 안에 있으므로 crop도 전체점이어야 합니다.
    assert audit["source_crop_count"] == source_count
    assert audit["target_crop_count"] == target_count
    assert audit["downsampling"] == {
        "used": False,
        "voxel_down_sample": False,
        "point_budget_sampling": False,
    }
    expected_stage = {
        "source_positions_fingerprint": audit["source_crop_positions_fingerprint"],
        "target_positions_fingerprint": audit["target_crop_positions_fingerprint"],
        "source_point_ids_fingerprint": audit["source_crop_point_ids_fingerprint"],
        "target_point_ids_fingerprint": audit["target_crop_point_ids_fingerprint"],
    }
    stage_audit = audit["stage_input_fingerprints"]
    expected_stage_names = (
        {"coarse", "medium", "fine", "scale_refine"}
        if metadata.get("backend_id") == "SMALL_GICP" and metadata.get("scale_mode") == "REFINE"
        else {"coarse", "medium", "fine"}
    )
    assert set(stage_audit) == expected_stage_names
    for stage_name, stage in stage_audit.items():
        for field_name, expected_value in expected_stage.items():
            assert stage[field_name] == expected_value, stage_name
        assert stage["source_point_count"] == source_count, stage_name
        assert stage["target_point_count"] == target_count, stage_name
    if metadata.get("backend_id") == "SMALL_GICP":
        expected_level_count = 5 if metadata.get("scale_mode") == "REFINE" else 4
    else:
        expected_level_count = 3
    assert len(metadata["level_results"]) == expected_level_count
    for level in metadata["level_results"]:
        assert level["source_point_count"] == source_count
        assert level["target_point_count"] == target_count
        for field_name, expected_value in expected_stage.items():
            assert level[field_name] == expected_value


def test_explicit_cpython_reports_required_open3d_environment(project_root: Path) -> None:
    """명시 CPython이 격리 모드의 3.12 x64 Open3D 0.19 환경인지 실제 확인합니다."""

    assert sys.flags.isolated == 1
    assert sys.version_info[:2] == (3, 12)
    completed = _run_worker(project_root, "--check-environment")
    _assert_worker_success(completed)
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["worker_python_version"].startswith("3.12.")
    assert payload["open3d_version"] == "0.19.0"
    assert payload["platform_architecture"] == "64bit"
    assert payload["synthetic_gicp_smoke"]["passed"] is True


def test_explicit_cpython_reports_required_small_gicp_environment(project_root: Path) -> None:
    """명시 CPython이 small_gicp 1.0.1 GICP를 실제 실행하는지 확인합니다."""

    completed = _run_worker(
        project_root,
        "--check-environment",
        "--backend",
        "SMALL_GICP",
        backend_id="SMALL_GICP",
    )
    _assert_worker_success(completed)
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["worker_python_version"].startswith("3.12.")
    assert payload["small_gicp_version"] == _SMALL_GICP_VERSION
    assert payload["platform_architecture"] == "64bit"
    assert payload["synthetic_gicp_smoke"]["passed"] is True


def test_external_worker_runs_all_backends_without_downsampling(
    project_root: Path,
    *,
    backends: tuple[str, ...] = (
        "OPEN3D_GICP",
        "SMALL_GICP",
        "OPEN3D_ROBUST_POINT_TO_PLANE",
    ),
) -> None:
    """선택한 외부 backend가 실제 worker에서 전체점 rigid 결과를 기록하는지 확인합니다."""

    for backend in backends:
        files, source_positions, target_positions, expected_delta, _initial_alignment = (
            _write_open3d_job(backend)
        )
        try:
            completed = _run_worker(
                project_root,
                "--job-dir",
                str(files.job_directory),
                cwd=files.job_directory,
                backend_id=backend,
            )
            _assert_worker_success(completed)
            assert files.result_path.is_file()
            assert files.result_array_path.is_file()
            assert not files.error_path.exists()
            metadata = json.loads(files.result_path.read_text(encoding="utf-8"))
            arrays = _result_arrays(files.result_array_path)
            _assert_result_metadata(metadata, files, backend)
            _assert_result_diagnostics_match_npz(metadata, arrays)
            if backend == "SMALL_GICP":
                assert metadata["execution_audit"]["pipeline"] == [
                    "CURRENT_OR_MARKER_ALIGNMENT",
                    "SMALL_GICP_RIGID_PASS_1",
                    "SMALL_GICP_RIGID_PASS_2",
                ]
                assert metadata["execution_audit"]["scale_refine"]["second_gicp_ran"] is True
            assert set(arrays) == {"initial_alignment", "final_alignment", "icp_delta"}
            assert all(value.dtype == np.dtype(np.float64) and value.shape == (4, 4) for value in arrays.values())
            np.testing.assert_allclose(
                arrays["final_alignment"],
                arrays["icp_delta"] @ arrays["initial_alignment"],
                rtol=0.0,
                atol=1.0e-8,
            )
            assert np.isclose(_uniform_scale(arrays["icp_delta"]), 1.0, rtol=0.0, atol=1.0e-8)
            assert np.isclose(_uniform_scale(arrays["final_alignment"]), 1.0, rtol=0.0, atol=1.0e-8)
            aligned_positions = _apply_matrix(arrays["final_alignment"], source_positions)
            expected_positions = _apply_matrix(expected_delta, source_positions)
            np.testing.assert_allclose(aligned_positions, expected_positions, rtol=0.0, atol=3.0e-2)
            np.testing.assert_allclose(expected_positions, target_positions, rtol=0.0, atol=1.0e-12)
            _assert_full_input_audit(
                metadata,
                source_positions.shape[0],
                target_positions.shape[0],
            )
        finally:
            _cleanup_job(files)


def test_open3d_worker_runs_direct_align_without_marker_points(project_root: Path) -> None:
    """현재 P 변환을 초기값으로 쓰는 GICP가 가짜 마커 없이 합성되는지 확인합니다."""

    backend = "OPEN3D_GICP"
    files, source_positions, target_positions, expected_delta, initial_alignment = (
        _write_open3d_job(backend, use_marker_seed=False)
    )
    try:
        completed = _run_worker(
            project_root,
            "--job-dir",
            str(files.job_directory),
            cwd=files.job_directory,
        )
        _assert_worker_success(completed)
        metadata = json.loads(files.result_path.read_text(encoding="utf-8"))
        arrays = _result_arrays(files.result_array_path)
        _assert_result_metadata(metadata, files, backend, direct=True)
        _assert_result_diagnostics_match_npz(metadata, arrays)
        np.testing.assert_allclose(
            arrays["initial_alignment"],
            initial_alignment,
            rtol=0.0,
            atol=1.0e-12,
        )
        np.testing.assert_allclose(
            arrays["final_alignment"],
            arrays["icp_delta"] @ initial_alignment,
            rtol=0.0,
            atol=1.0e-8,
        )
        np.testing.assert_allclose(
            _apply_matrix(arrays["icp_delta"], source_positions),
            target_positions,
            rtol=0.0,
            atol=3.0e-2,
        )
        np.testing.assert_allclose(
            arrays["final_alignment"],
            expected_delta @ initial_alignment,
            rtol=0.0,
            atol=3.0e-2,
        )
        assert np.isclose(_uniform_scale(arrays["icp_delta"]), 1.0, rtol=0.0, atol=1.0e-8)
        assert np.isclose(
            _uniform_scale(arrays["final_alignment"]),
            _uniform_scale(initial_alignment),
            rtol=0.0,
            atol=1.0e-8,
        )
        _assert_full_input_audit(
            metadata,
            source_positions.shape[0],
            target_positions.shape[0],
        )
    finally:
        _cleanup_job(files)


def test_refine_combines_translation_rotation_and_uniform_scale(project_root: Path) -> None:
    """Refine이 현재 정렬에서 Small GICP 이동·회전과 제한형 균일 스케일을 함께 계산하는지 확인합니다."""

    backend = "SMALL_GICP"
    files, source_positions, target_positions, expected_delta, _initial_alignment = (
        _write_open3d_job(backend, use_marker_seed=False, scale_refine=True)
    )
    try:
        completed = _run_worker(
            project_root,
            "--job-dir",
            str(files.job_directory),
            cwd=files.job_directory,
            backend_id=backend,
        )
        _assert_worker_success(completed)
        metadata = json.loads(files.result_path.read_text(encoding="utf-8"))
        arrays = _result_arrays(files.result_array_path)
        _assert_result_metadata(
            metadata,
            files,
            backend,
            direct=True,
            scale_refine=True,
        )
        _assert_result_diagnostics_match_npz(metadata, arrays)
        assert metadata["scale_mode"] == "REFINE"
        assert metadata["scale_refine_status"] == "ACCEPTED"
        assert metadata["quality"]["scale_refine_status"] == "ACCEPTED"
        assert metadata["execution_audit"]["pipeline"] == [
            "CURRENT_OR_MARKER_ALIGNMENT",
            "SMALL_GICP_RIGID_PASS_1",
            "BOUNDED_UNIFORM_SCALE_REFINE",
            "SMALL_GICP_RIGID_PASS_2",
        ]
        assert metadata["execution_audit"]["scale_refine"]["second_gicp_ran"] is True
        assert np.isclose(
            _uniform_scale(arrays["icp_delta"]),
            _uniform_scale(expected_delta),
            rtol=0.0,
            atol=5.0e-3,
        )
        np.testing.assert_allclose(
            _apply_matrix(arrays["icp_delta"], source_positions),
            target_positions,
            rtol=0.0,
            atol=2.0e-2,
        )
        _assert_full_input_audit(
            metadata,
            source_positions.shape[0],
            target_positions.shape[0],
        )
    finally:
        _cleanup_job(files)


def test_open3d_worker_honors_preexisting_cancel_request(project_root: Path) -> None:
    """실제 외부 worker가 custom backend로 전환하지 않고 취소 결과만 남기는지 확인합니다."""

    backend_id = "OPEN3D_GICP"
    files, _source_positions, _target_positions, _expected_delta, _initial_alignment = (
        _write_open3d_job(backend_id)
    )
    try:
        files.cancel_path.write_text("cancel\n", encoding="utf-8")
        completed = _run_worker(
            project_root,
            "--job-dir",
            str(files.job_directory),
            cwd=files.job_directory,
        )
        _assert_worker_success(completed)
        assert files.error_path.is_file()
        error_payload = json.loads(files.error_path.read_text(encoding="utf-8"))
        assert error_payload["status"] == "CANCELLED"
        assert error_payload["backend_id"] == backend_id
        progress_payload = json.loads((files.job_directory / "progress.json").read_text(encoding="utf-8"))
        assert progress_payload["stage"] == "CANCELLED"
        assert progress_payload["backend_id"] == backend_id
        assert not files.result_path.exists()
        assert not files.result_array_path.exists()
    finally:
        _cleanup_job(files)


def run_all() -> None:
    """명시 외부 ICP CPython에서 실행할 worker 통합 테스트를 순서대로 실행합니다."""

    project_root = _project_root()
    test_explicit_cpython_reports_required_open3d_environment(project_root)
    test_explicit_cpython_reports_required_small_gicp_environment(project_root)
    test_external_worker_runs_all_backends_without_downsampling(project_root)
    test_open3d_worker_runs_direct_align_without_marker_points(project_root)
    test_refine_combines_translation_rotation_and_uniform_scale(project_root)
    test_open3d_worker_honors_preexisting_cancel_request(project_root)
    print("test_icp_open3d_worker_integration: 통과")


if __name__ == "__main__":
    run_all()
