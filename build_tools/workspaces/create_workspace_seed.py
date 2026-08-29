"""Blender 5.2 Factory Startup에서 중립 Workspace Seed를 생성합니다."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import bpy


TOOL_ID_KEY = "colmap_tools_workspace_tool_id"
SEED_SCHEMA_KEY = "colmap_tools_workspace_seed_schema"
SEED_BLENDER_VERSION_KEY = "colmap_tools_seed_blender_version"


def _script_arguments() -> list[str]:
    """Blender의 `--` 뒤에 전달된 Seed 생성 인자만 반환합니다."""

    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def _parse_arguments() -> argparse.Namespace:
    """도구 ID, 내부 Workspace ID와 출력 경로를 검증해 읽습니다."""

    parser = argparse.ArgumentParser(description="COLMAP Tools Workspace Seed 생성기")
    parser.add_argument("--tool-id", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-schema", type=int, default=1)
    arguments = parser.parse_args(_script_arguments())
    if arguments.seed_schema < 1:
        parser.error("--seed-schema는 1 이상이어야 합니다.")
    return arguments


def _clear_scene_runtime_data() -> None:
    """Seed에 포함될 수 있는 Factory Scene의 런타임 데이터 블록을 제거합니다."""

    for collection in (
        bpy.data.objects,
        bpy.data.collections,
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.cameras,
        bpy.data.lights,
        bpy.data.materials,
        bpy.data.images,
    ):
        for data_block in tuple(collection):
            collection.remove(data_block, do_unlink=True)


def _enable_all_object_filters(workspace: bpy.types.WorkSpace) -> None:
    """Seed의 모든 3D Viewport 오브젝트 타입 필터를 중립 True로 설정합니다."""

    for screen in workspace.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                for property_definition in space.bl_rna.properties:
                    if property_definition.identifier.startswith("show_object_viewport_"):
                        setattr(space, property_definition.identifier, True)


def _prepare_workspace(
    tool_id: str,
    workspace_name: str,
    seed_schema: int,
    workspace: bpy.types.WorkSpace,
) -> bpy.types.WorkSpace:
    """활성 Layout Workspace를 내부 ID와 Seed 메타데이터로 정규화합니다."""

    workspace.name = workspace_name
    workspace.use_pin_scene = False
    workspace.object_mode = "OBJECT"
    workspace[TOOL_ID_KEY] = tool_id
    workspace[SEED_SCHEMA_KEY] = seed_schema
    workspace[SEED_BLENDER_VERSION_KEY] = ".".join(str(value) for value in bpy.app.version)
    _enable_all_object_filters(workspace)
    return workspace


def _write_seed(workspace: bpy.types.WorkSpace, output_path: Path) -> None:
    """Workspace와 의존 Screen만 압축 Blender Library로 기록합니다."""

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_blocks = {workspace, *tuple(workspace.screens)}
    bpy.data.libraries.write(
        str(output_path),
        data_blocks,
        path_remap="RELATIVE_ALL",
        fake_user=False,
        compress=True,
    )


def main() -> None:
    """Factory Startup 상태를 확인하고 요청한 Workspace Seed를 생성합니다."""

    arguments = _parse_arguments()
    if bpy.app.version[:2] != (5, 2):
        raise RuntimeError(f"Blender 5.2에서만 Seed를 생성할 수 있습니다: {bpy.app.version_string}")
    workspace = bpy.data.workspaces.get("Layout")
    if workspace is None:
        raise RuntimeError("--factory-startup의 Layout Workspace를 찾을 수 없습니다.")
    _clear_scene_runtime_data()
    workspace = _prepare_workspace(
        arguments.tool_id,
        arguments.workspace_name,
        arguments.seed_schema,
        workspace=workspace,
    )
    _write_seed(workspace, arguments.output)
    print(f"Workspace Seed 생성 완료: {arguments.output.resolve()}")


if __name__ == "__main__":
    main()
