"""Align 진입 전 Blender 편집·페인트 모드의 Object Mode 전환을 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy


def _add_source_path() -> None:
    """통합 Source 패키지를 Blender 테스트 모듈 경로에 추가합니다."""

    source = str(Path(__file__).resolve().parents[3])
    if source not in sys.path:
        sys.path.insert(0, source)


def _prepare_paint_target():
    """Edit·Sculpt·Texture Paint 전환을 모두 지원하는 단일 Mesh를 만듭니다."""

    bpy.ops.mesh.primitive_plane_add(size=2.0)
    target = bpy.context.active_object
    target.name = "COLMAP Align Object Mode Entry Target"
    material = bpy.data.materials.new("COLMAP Align Object Mode Entry Material")
    material.use_nodes = True
    image = bpy.data.images.new("COLMAP Align Object Mode Entry Image", 16, 16)
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.select = True
    material.node_tree.nodes.active = node
    target.data.materials.append(material)
    return target


def _assert_mode_returns_to_object(target, mode: str, expected_context_mode: str) -> None:
    """지정 Blender 모드에서 Align 진입 경계가 Object Mode를 확정하는지 검사합니다."""

    from tools.align.operators.workspace import ensure_object_mode_before_align

    result = bpy.ops.object.mode_set(mode=mode)
    assert result == {"FINISHED"}, f"{mode} 테스트 모드에 진입하지 못했습니다."
    assert bpy.context.mode == expected_context_mode
    assert ensure_object_mode_before_align(bpy.context) is True
    assert bpy.context.mode == "OBJECT"
    assert target.mode == "OBJECT"


def run() -> None:
    """Edit·Sculpt·Texture Paint와 기존 Object Mode를 차례대로 검증합니다."""

    _add_source_path()
    target = _prepare_paint_target()
    _assert_mode_returns_to_object(target, "EDIT", "EDIT_MESH")
    _assert_mode_returns_to_object(target, "SCULPT", "SCULPT")
    _assert_mode_returns_to_object(target, "TEXTURE_PAINT", "PAINT_TEXTURE")

    from tools.align.operators.workspace import ensure_object_mode_before_align

    assert ensure_object_mode_before_align(bpy.context) is False
    print("COLMAP_ALIGN_OBJECT_MODE_ENTRY_OK")


if __name__ == "__main__":
    run()
