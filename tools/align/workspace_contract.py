"""COLMAP Align이 공통 Workspace Factory에 전달하는 불변 계약을 정의합니다."""

try:
    from ...shared.blender_runtime.workspace_factory import WorkspaceSeedSpec
except ImportError:  # Source Tree를 최상위 tools 패키지로 실행하는 기존 테스트를 지원합니다.
    from shared.blender_runtime.workspace_factory import WorkspaceSeedSpec

from .constants import (
    WORKSPACE_NAME,
    WORKSPACE_SCHEMA_VERSION,
    WORKSPACE_SEED_NAME,
    WORKSPACE_SEED_PATH,
    WORKSPACE_SEED_SCHEMA_VERSION,
    WORKSPACE_TOOL_ID,
)


# 모든 Align 진입·복구 경로가 같은 Seed와 Schema를 사용합니다.
ALIGN_WORKSPACE_SPEC = WorkspaceSeedSpec(
    tool_id=WORKSPACE_TOOL_ID,
    display_name=WORKSPACE_NAME,
    seed_workspace_name=WORKSPACE_SEED_NAME,
    seed_path=WORKSPACE_SEED_PATH,
    workspace_schema=WORKSPACE_SCHEMA_VERSION,
    seed_schema=WORKSPACE_SEED_SCHEMA_VERSION,
)


__all__ = ["ALIGN_WORKSPACE_SPEC"]
