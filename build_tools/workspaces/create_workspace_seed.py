"""Blender 5.2 Factory Startup에서 중립 Workspace Seed를 생성합니다."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
import traceback

import bpy


TOOL_ID_KEY = "colmap_tools_workspace_tool_id"
SEED_SCHEMA_KEY = "colmap_tools_workspace_seed_schema"
SEED_BLENDER_VERSION_KEY = "colmap_tools_seed_blender_version"


def _script_arguments() -> list[str]:
    """Blender의 `--` 뒤에 전달된 Seed 생성 인자만 반환합니다."""

    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def _parse_arguments() -> argparse.Namespace:
    """도구 ID와 출력 경로, Sidebar 폭 또는 기존 중립 Seed 사용 옵션을 읽습니다."""

    parser = argparse.ArgumentParser(description="COLMAP Tools Workspace Seed 생성기")
    parser.add_argument("--tool-id", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-schema", type=int, default=1)
    parser.add_argument("--sidebar-width", type=int, default=0, help="화면 배율을 적용하기 전 Sidebar 기본 폭")
    parser.add_argument("--template", type=Path, help="저장된 패널 폭과 중립 화면 배치를 재사용할 Workspace Seed")
    arguments = parser.parse_args(_script_arguments())
    if arguments.seed_schema < 1:
        parser.error("--seed-schema는 1 이상이어야 합니다.")
    if arguments.sidebar_width and not 100 <= arguments.sidebar_width <= 800:
        parser.error("--sidebar-width는 100~800 사이여야 합니다.")
    if arguments.template and arguments.sidebar_width:
        parser.error("--template과 --sidebar-width는 동시에 사용할 수 없습니다.")
    return arguments


def _load_template_workspace(path: Path) -> bpy.types.WorkSpace:
    """런타임 데이터가 없는 중립 Seed를 읽어 저장된 패널 폭과 화면 배치를 보존합니다."""

    path = path.expanduser().resolve(strict=True)
    with bpy.data.libraries.load(str(path), link=False) as (source, target):
        if len(source.workspaces) != 1:
            raise RuntimeError("템플릿에는 Workspace가 정확히 하나 있어야 합니다.")
        for collection in (
            "scenes", "objects", "collections", "meshes", "curves",
            "cameras", "lights", "materials", "images",
        ):
            if getattr(source, collection):
                raise RuntimeError(f"템플릿에 런타임 데이터가 포함되어 있습니다: {collection}")
        target.workspaces = list(source.workspaces)
    workspace = target.workspaces[0]
    if not str(workspace.get(SEED_BLENDER_VERSION_KEY, "")).startswith("5.2."):
        raise RuntimeError("Blender 5.2에서 생성된 Seed만 템플릿으로 사용할 수 있습니다.")
    return workspace


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


def _resize_sidebar_and_write_seed(workspace: bpy.types.WorkSpace, arguments: argparse.Namespace) -> None:
    """단일 Viewport의 Sidebar 폭을 저장하고 Align Seed에서는 기본 표시를 닫힘으로 설정합니다."""

    if bpy.app.background or not bpy.app.use_event_simulate:
        raise RuntimeError("Sidebar 폭 지정은 GUI에서 --enable-event-simulate와 함께 실행해야 합니다.")
    window = bpy.context.window
    bpy.context.preferences.view.show_splash = False
    area = None
    deadline = time.monotonic() + 30.0
    step = -2
    start_x = end_x = y = 0

    def advance() -> float | None:
        """드래그 이벤트를 화면 갱신 사이에 전달하고 목표 폭을 확인한 경우에만 기록합니다."""

        nonlocal area, step, start_x, end_x, y
        try:
            if time.monotonic() > deadline:
                raise RuntimeError("Sidebar 기본 폭 설정이 제한 시간 안에 끝나지 않았습니다.")
            if step == -2:
                # 시작 안내 팝업이 생성되어 있으면 경계 입력 전에 닫습니다.
                window.event_simulate(type="ESC", value="PRESS")
                window.event_simulate(type="ESC", value="RELEASE")
                step = -1
                return 0.2
            if step == -1:
                # Align의 영역 결합에서 다른 편집기가 남아 기본 폭을 잃지 않도록 단일 Viewport로 만듭니다.
                if _collapse_seed_area(window):
                    return 0.2
                area = window.screen.areas[0]
                area.type = "VIEW_3D"
                area.spaces.active.show_region_ui = True
                _enable_all_object_filters(workspace)
                step = 0
                return 0.2
            region = next(region for region in area.regions if region.type == "UI")
            target = round(arguments.sidebar_width * bpy.context.preferences.system.ui_scale)
            if step == 0:
                if region.width < 100 or region.height < 100:
                    return 0.1
                # 겹침 표시에서 빈 하단은 뷰포트 입력으로 통과하므로 실제 패널 상단 경계를 잡습니다.
                start_x, y = region.x, region.y + region.height - 60
                end_x = start_x + region.width - target
                window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=start_x, y=y)
            elif step == 1:
                window.event_simulate(type="LEFTMOUSE", value="PRESS", x=start_x, y=y)
            elif step == 2:
                window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=start_x - 16, y=y)
            elif step == 3:
                window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=end_x, y=y)
            elif step == 4:
                window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=end_x, y=y)
            else:
                if abs(region.width - target) > 2:
                    raise RuntimeError(f"Sidebar 폭이 기대값과 다릅니다: {region.width}, 목표 {target}")
                configured_width = region.width
                # Align은 닫힌 상태를 복제하고 세션 구성 시 오른쪽 P 뷰의 패널만 엽니다.
                if arguments.tool_id == "align":
                    area.spaces.active.show_region_ui = False
                _write_seed(workspace, arguments.output)
                print(f"Workspace Seed 생성 완료: {arguments.output.resolve()}, Sidebar 기본 폭 {configured_width}px", flush=True)
                bpy.ops.wm.quit_blender()
                return None
            step += 1
            return 0.2
        except Exception:
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)

    bpy.app.timers.register(advance, first_interval=0.3)


def _collapse_seed_area(window: bpy.types.Window) -> bool:
    """생성 중인 Factory 화면에서 전체 경계를 공유하는 영역 한 쌍을 결합합니다."""

    areas = tuple(window.screen.areas)
    if len(areas) == 1:
        return False
    for source in areas:
        for target in areas:
            if source == target:
                continue
            vertical = (
                abs(source.y - target.y) <= 1
                and abs(source.height - target.height) <= 2
                and min(abs(source.x + source.width - target.x), abs(target.x + target.width - source.x)) <= 4
            )
            horizontal = (
                abs(source.x - target.x) <= 1
                and abs(source.width - target.width) <= 2
                and min(abs(source.y + source.height - target.y), abs(target.y + target.height - source.y)) <= 4
            )
            if not (vertical or horizontal):
                continue
            with bpy.context.temp_override(window=window, screen=window.screen, area=source):
                bpy.ops.screen.area_join(
                    source_xy=(source.x + source.width // 2, source.y + source.height // 2),
                    target_xy=(target.x + target.width // 2, target.y + target.height // 2),
                )
            if len(window.screen.areas) != len(areas) - 1:
                raise RuntimeError("Seed 화면의 영역 결합에 실패했습니다.")
            return True
    raise RuntimeError("Seed 화면에서 결합 가능한 영역을 찾지 못했습니다.")


def main() -> None:
    """Factory Startup 또는 기존 중립 템플릿에서 도구별 Workspace Seed를 생성합니다."""

    arguments = _parse_arguments()
    if bpy.app.version[:2] != (5, 2):
        raise RuntimeError(f"Blender 5.2에서만 Seed를 생성할 수 있습니다: {bpy.app.version_string}")
    workspace = bpy.data.workspaces.get("Layout")
    if workspace is None:
        raise RuntimeError("--factory-startup의 Layout Workspace를 찾을 수 없습니다.")
    _clear_scene_runtime_data()
    if arguments.template:
        workspace = _load_template_workspace(arguments.template)
    workspace = _prepare_workspace(
        arguments.tool_id,
        arguments.workspace_name,
        arguments.seed_schema,
        workspace=workspace,
    )
    if arguments.sidebar_width:
        _resize_sidebar_and_write_seed(workspace, arguments)
        return
    _write_seed(workspace, arguments.output)
    print(f"Workspace Seed 생성 완료: {arguments.output.resolve()}")


if __name__ == "__main__":
    main()
