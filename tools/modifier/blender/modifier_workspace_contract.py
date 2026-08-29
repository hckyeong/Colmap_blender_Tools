"""COLMAP Modifier Workspace Seed와 Schema 계약을 정의합니다."""

from pathlib import Path

try:
    from ....shared.blender_runtime.workspace_factory import WorkspaceSeedSpec
except ImportError:
    from shared.blender_runtime.workspace_factory import WorkspaceSeedSpec

from .modifier_session_constants import WORKSPACE_NAME


MODIFIER_WORKSPACE_SPEC = WorkspaceSeedSpec(
    tool_id="modifier",
    display_name=WORKSPACE_NAME,
    seed_workspace_name="__COLMAP_MODIFIER_SEED_5_2__",
    seed_path=(
        Path(__file__).resolve().parents[1]
        / "resources"
        / "workspaces"
        / "colmap_modifier_seed_5_2.blend"
    ),
    workspace_schema=2,
    seed_schema=1,
)


__all__ = ["MODIFIER_WORKSPACE_SPEC"]
