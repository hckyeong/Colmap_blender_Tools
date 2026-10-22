"""ICP 수치 backend 식별자와 버전 계약을 관리합니다."""

from __future__ import annotations

from enum import Enum

from ..constants import (
    ICP_BACKEND_ALGORITHM_VERSION_CUSTOM_HYBRID,
    ICP_BACKEND_ALGORITHM_VERSION_OPEN3D_GICP,
    ICP_BACKEND_ALGORITHM_VERSION_OPEN3D_ROBUST_POINT_TO_PLANE,
    ICP_BACKEND_ALGORITHM_VERSION_SMALL_GICP,
    ICP_BACKEND_ID_CUSTOM_HYBRID,
    ICP_BACKEND_ID_OPEN3D_GICP,
    ICP_BACKEND_ID_OPEN3D_ROBUST_POINT_TO_PLANE,
    ICP_BACKEND_ID_SMALL_GICP,
)


class IcpSolverBackend(str, Enum):
    """사용자가 명시적으로 선택하는 ICP 수치 backend입니다."""

    POINT_KD_ICP = "POINT_KD_ICP"
    CCCORELIB = "CCCORELIB"
    CUSTOM_HYBRID = ICP_BACKEND_ID_CUSTOM_HYBRID
    OPEN3D_GICP = ICP_BACKEND_ID_OPEN3D_GICP
    SMALL_GICP = ICP_BACKEND_ID_SMALL_GICP
    OPEN3D_ROBUST_POINT_TO_PLANE = ICP_BACKEND_ID_OPEN3D_ROBUST_POINT_TO_PLANE


_BACKEND_ALGORITHM_VERSIONS = {
    IcpSolverBackend.POINT_KD_ICP: "point-kd-icp-v3",
    IcpSolverBackend.CCCORELIB: "cccore-icp-v7",
    IcpSolverBackend.CUSTOM_HYBRID: ICP_BACKEND_ALGORITHM_VERSION_CUSTOM_HYBRID,
    IcpSolverBackend.OPEN3D_GICP: ICP_BACKEND_ALGORITHM_VERSION_OPEN3D_GICP,
    IcpSolverBackend.SMALL_GICP: ICP_BACKEND_ALGORITHM_VERSION_SMALL_GICP,
    IcpSolverBackend.OPEN3D_ROBUST_POINT_TO_PLANE: (
        ICP_BACKEND_ALGORITHM_VERSION_OPEN3D_ROBUST_POINT_TO_PLANE
    ),
}


def normalize_solver_backend(value: IcpSolverBackend | str) -> IcpSolverBackend:
    """문자열 또는 enum backend를 고정 enum 값으로 정규화합니다."""

    if isinstance(value, IcpSolverBackend):
        return value
    if not isinstance(value, str):
        raise ValueError("ICP backend는 문자열 또는 IcpSolverBackend여야 합니다.")
    normalized = value.strip().upper()
    try:
        return IcpSolverBackend(normalized)
    except ValueError as error:
        raise ValueError(f"지원하지 않는 ICP backend입니다: {value}") from error


def backend_algorithm_version(value: IcpSolverBackend | str) -> str:
    """선택 backend의 고정 수치·평가 계약 버전을 반환합니다."""

    return _BACKEND_ALGORITHM_VERSIONS[normalize_solver_backend(value)]


def is_open3d_backend(value: IcpSolverBackend | str) -> bool:
    """선택 backend가 외부 CPython Open3D worker를 쓰는지 반환합니다."""

    backend = normalize_solver_backend(value)
    return backend in {
        IcpSolverBackend.OPEN3D_GICP,
        IcpSolverBackend.OPEN3D_ROBUST_POINT_TO_PLANE,
    }


def is_external_python_backend(value: IcpSolverBackend | str) -> bool:
    """선택 backend가 Blender 밖의 CPython worker를 쓰는지 반환합니다."""

    backend = normalize_solver_backend(value)
    return is_open3d_backend(backend) or backend is IcpSolverBackend.SMALL_GICP


__all__ = (
    "IcpSolverBackend",
    "backend_algorithm_version",
    "is_external_python_backend",
    "is_open3d_backend",
    "normalize_solver_backend",
)
