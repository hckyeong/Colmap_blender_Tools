"""배포용 Workspace Seed의 내부 ID, 메타데이터와 Screen 중립성을 검증합니다."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import bpy


TOOL_ID_KEY = "colmap_tools_workspace_tool_id"
SEED_SCHEMA_KEY = "colmap_tools_workspace_seed_schema"
SEED_BLENDER_VERSION_KEY = "colmap_tools_seed_blender_version"


def _script_arguments() -> list[str]:
    """Blender의 `--` 뒤에 전달된 Validator 인자만 반환합니다."""

    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def _parse_arguments() -> argparse.Namespace:
    """검증할 Seed 경로와 기대 메타데이터를 읽습니다."""

    parser = argparse.ArgumentParser(description="COLMAP Tools Workspace Seed Validator")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tool-id", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--seed-schema", type=int, default=1)
    return parser.parse_args(_script_arguments())


def _library_inventory(seed_path: Path) -> dict[str, tuple[str, ...]]:
    """Seed Library의 주요 ID 목록을 실제 Append 없이 수집합니다."""

    with bpy.data.libraries.load(str(seed_path), link=False) as (data_from, _data_to):
        return {
            name: tuple(getattr(data_from, name))
            for name in (
                "workspaces",
                "scenes",
                "objects",
                "collections",
                "meshes",
                "curves",
                "cameras",
                "lights",
                "materials",
                "images",
            )
        }


def _append_workspace(seed_path: Path, workspace_name: str) -> bpy.types.WorkSpace:
    """검증 대상 Workspace와 의존 Screen을 현재 임시 데이터에 Append합니다."""

    before = {workspace.as_pointer() for workspace in bpy.data.workspaces}
    with bpy.data.libraries.load(str(seed_path), link=False) as (_data_from, data_to):
        data_to.workspaces = [workspace_name]
    appended = [
        workspace
        for workspace in bpy.data.workspaces
        if workspace.as_pointer() not in before
    ]
    if len(appended) != 1:
        raise AssertionError(f"Workspace Append 결과가 정확히 하나가 아닙니다: {len(appended)}개")
    return appended[0]


def _validate_viewport_filters(workspace: bpy.types.WorkSpace) -> None:
    """모든 Screen의 3D Viewport 타입 필터가 중립 True인지 검증합니다."""

    if not workspace.screens:
        raise AssertionError("Seed Workspace에 의존 Screen이 없습니다.")
    view_3d_count = 0
    for screen in workspace.screens:
        if not screen.areas:
            raise AssertionError(f"Seed Screen에 Area가 없습니다: {screen.name}")
        for area in screen.areas:
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                view_3d_count += 1
                disabled = [
                    property_definition.identifier
                    for property_definition in space.bl_rna.properties
                    if property_definition.identifier.startswith("show_object_viewport_")
                    and not getattr(space, property_definition.identifier)
                ]
                if disabled:
                    raise AssertionError(f"비활성 Viewport 타입 필터가 있습니다: {disabled}")
    if view_3d_count < 1:
        raise AssertionError("Seed Workspace에 3D Viewport가 없습니다.")


def _validate_metadata(
    workspace: bpy.types.WorkSpace,
    tool_id: str,
    seed_schema: int,
) -> None:
    """Seed Tool ID, Schema와 생성 Blender Version을 검증합니다."""

    if workspace.get(TOOL_ID_KEY) != tool_id:
        raise AssertionError("Seed Tool ID가 기대값과 다릅니다.")
    if workspace.get(SEED_SCHEMA_KEY) != seed_schema:
        raise AssertionError("Seed Schema가 기대값과 다릅니다.")
    version = workspace.get(SEED_BLENDER_VERSION_KEY, "")
    if not isinstance(version, str) or not version.startswith("5.2."):
        raise AssertionError(f"Seed 생성 Blender Version이 5.2가 아닙니다: {version}")


def main() -> None:
    """Seed의 배포 불변식을 검증하고 위반 시 Blender 실행을 실패시킵니다."""

    arguments = _parse_arguments()
    seed_path = arguments.input.expanduser().resolve()
    if not seed_path.is_file():
        raise FileNotFoundError(f"Workspace Seed 파일이 없습니다: {seed_path}")
    inventory = _library_inventory(seed_path)
    if inventory["workspaces"] != (arguments.workspace_name,):
        raise AssertionError(f"Seed Workspace ID 목록이 다릅니다: {inventory['workspaces']}")
    runtime_ids = {
        name: values
        for name, values in inventory.items()
        if name != "workspaces" and values
    }
    if runtime_ids:
        raise AssertionError(f"Seed에 Scene 런타임 ID가 포함되었습니다: {runtime_ids}")
    workspace = _append_workspace(seed_path, arguments.workspace_name)
    _validate_metadata(workspace, arguments.tool_id, arguments.seed_schema)
    _validate_viewport_filters(workspace)
    print(f"Workspace Seed 검증 통과: {seed_path}")


if __name__ == "__main__":
    main()
