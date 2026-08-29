"""배포 프로필별 ICP backend 노출 범위를 제공합니다."""

from __future__ import annotations

from ._build_profile import DISTRIBUTION_PROFILE


FULL_PROFILE = "FULL"
SMALL_GICP_PROFILE = "SMALL_GICP"
_SUPPORTED_PROFILES = frozenset({FULL_PROFILE, SMALL_GICP_PROFILE})

if DISTRIBUTION_PROFILE not in _SUPPORTED_PROFILES:
    raise RuntimeError(f"지원하지 않는 COLMAP Align 배포 프로필입니다: {DISTRIBUTION_PROFILE}")


def includes_optional_backends() -> bool:
    """현재 빌드가 Open3D·Hybrid 선택형 backend를 포함하는지 반환합니다."""

    return DISTRIBUTION_PROFILE == FULL_PROFILE


def icp_backend_enum_items() -> tuple[tuple[str, str, str], ...]:
    """Open3D를 제외하고 현재 활성화한 ICP backend 항목만 반환합니다."""

    small_gicp = (
        "SMALL_GICP",
        "small_gicp GICP",
        "전체 희소점으로 small_gicp GICP를 실행합니다.",
    )
    if not includes_optional_backends():
        return (small_gicp,)
    return (
        ("CUSTOM_HYBRID", "기존 Hybrid", "현재 커스텀 Hybrid ICP를 비교 기준으로 사용합니다."),
        # Open3D backend는 구현 보존 상태로 선택 목록에서 비활성화합니다.
        # ("OPEN3D_GICP", "Open3D GICP", "전체 희소점으로 Open3D Generalized ICP를 실행합니다."),
        small_gicp,
        # (
        #     "OPEN3D_ROBUST_POINT_TO_PLANE",
        #     "Open3D Robust Point-to-Plane",
        #     "전체 희소점과 Tukey 강건 커널로 Point-to-Plane ICP를 실행합니다.",
        # ),
    )


def default_icp_backend() -> str:
    """Open3D 비활성화 상태의 기본 Small GICP backend 식별자를 반환합니다."""

    return "SMALL_GICP"


__all__ = (
    "DISTRIBUTION_PROFILE",
    "FULL_PROFILE",
    "SMALL_GICP_PROFILE",
    "default_icp_backend",
    "icp_backend_enum_items",
    "includes_optional_backends",
)
