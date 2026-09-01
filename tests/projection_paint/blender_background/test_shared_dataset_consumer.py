"""Blender Background에서 수동 PP 독립 Snapshot 계약을 검증합니다."""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import zlib

import bpy


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.blender_runtime.scene_marker_session import acquire_scene_marker_session, release_scene_marker_session  # noqa: E402
from shared.colmap_dataset import register as register_legacy_dataset, unregister as unregister_legacy_dataset  # noqa: E402
from shared.colmap_dataset.blender.ownership import is_dataset_owned  # noqa: E402
from shared.colmap_transfer import registry as transfer_registry  # noqa: E402
from tools import modifier as modifier_addon  # noqa: E402
from tools import projection_paint as projection_paint_addon  # noqa: E402
from tools.modifier.blender.runtime_state import cached_runtime_matches_scene, clear_runtime  # noqa: E402
from tools.projection_paint.blender.camera_scene import runtime_for_scene  # noqa: E402
from tools.projection_paint.blender import state_handlers as paint_state_handlers  # noqa: E402
from tools.projection_paint.blender.ownership import SNAPSHOT_OWNER_KEY, SNAPSHOT_OWNER_TOKEN  # noqa: E402
from tools.projection_paint.blender.snapshot_update import (  # noqa: E402
    PP_OT_update_colmap_snapshot,
    snapshot_is_ready,
    snapshot_update_available,
)


def assert_true(condition: bool, message: str) -> None:
    """검증 조건이 거짓이면 지정한 한국어 원인으로 실패합니다."""

    if not condition:
        raise AssertionError(message)


def _png_chunk(name: bytes, payload: bytes) -> bytes:
    """테스트 PNG의 길이와 CRC를 포함한 Chunk를 만듭니다."""

    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)


def _write_png(path: Path) -> None:
    """Camera Review가 읽을 수 있는 4×3 RGB PNG를 기록합니다."""

    rows = b"".join(b"\x00" + bytes((index * 31) % 256 for index in range(12)) for _ in range(3))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def _write_dataset(root: Path, image_ids: tuple[int, ...]) -> None:
    """비연속 IMAGE_ID의 최소 COLMAP TXT Dataset을 생성합니다."""

    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    (model / "cameras.txt").write_text("# cameras\n7 PINHOLE 4 3 4 4 2 1.5\n", encoding="utf-8")
    image_lines = ["# images"]
    point_lines = ["# points"]
    for offset, image_id in enumerate(image_ids):
        name = f"frame_{image_id}.png"
        _write_png(images / name)
        point_id = 1000 + image_id
        image_lines.extend((f"{image_id} 1 0 0 0 {offset} 0 0 7 {name}", f"1 1 {point_id}"))
        point_lines.append(f"{point_id} {offset} 0 1 10 20 30 0.1 {image_id} 0")
    (model / "images.txt").write_text("\n".join(image_lines) + "\n", encoding="utf-8")
    (model / "points3D.txt").write_text("\n".join(point_lines) + "\n", encoding="utf-8")


def _import_modifier(scene: bpy.types.Scene, root: Path) -> None:
    """Modifier 공개 Operator로 테스트 Dataset을 확정 Import합니다."""

    settings = scene.colmap_import
    settings.input_format = "TEXT"
    settings.dataset_root = str(root)
    result = bpy.ops.colmap.import_model()
    assert_true(result == {"FINISHED"}, f"Modifier Import 실패: {settings.status}")


def _snapshot_pointers() -> set[int]:
    """현재 PP Snapshot 소유 Blender ID 포인터 집합을 반환합니다."""

    values = []
    for value_store in (bpy.data.collections, bpy.data.objects, bpy.data.cameras, bpy.data.meshes):
        values.extend(item for item in value_store if item.get(SNAPSHOT_OWNER_KEY) == SNAPSHOT_OWNER_TOKEN)
    return {int(item.as_pointer()) for item in values}


def _modifier_pointers() -> set[int]:
    """현재 Modifier 소유 Blender ID 포인터 집합을 반환합니다."""

    values = []
    for value_store in (bpy.data.collections, bpy.data.objects, bpy.data.cameras, bpy.data.meshes):
        values.extend(item for item in value_store if is_dataset_owned(item))
    return {int(item.as_pointer()) for item in values}


def main() -> None:
    """최초·재업데이트·독립성·Paint 보존·실패 Rollback을 검증합니다."""

    register_legacy_dataset()
    modifier_addon.register()
    projection_paint_addon.register()
    scene = bpy.context.scene
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        first, second = base / "first", base / "second"
        _write_dataset(first, (2, 11))
        _write_dataset(second, (5,))
        _import_modifier(scene, first)
        acquire_scene_marker_session(scene, "projection_paint")

        target_mesh = bpy.data.meshes.new("PP Snapshot Preserve Mesh")
        target = bpy.data.objects.new("PP Snapshot Preserve Target", target_mesh)
        scene.collection.objects.link(target)
        paint_image = bpy.data.images.new("PP Snapshot Preserve Image", 2, 2, alpha=True)
        paint_image.pixels[:] = [0.125, 0.25, 0.5, 1.0] * 4
        settings = scene.projection_paint
        settings.paint_target = target
        layer = settings.layers.add()
        layer.name = "보존 Layer"
        layer.image = paint_image
        original_pixels = tuple(paint_image.pixels)

        clear_runtime()
        assert_true(
            not cached_runtime_matches_scene(scene),
            "COLD Provider 회귀 전 Modifier Runtime이 남았습니다.",
        )
        result = bpy.ops.projection_paint.update_colmap_snapshot()
        assert_true(result == {"FINISHED"}, f"최초 Snapshot 실패: {settings.status}")
        assert_true(
            cached_runtime_matches_scene(scene),
            "PP 업데이트가 COLD Modifier Runtime을 증분 복원하지 못했습니다.",
        )
        assert_true(snapshot_is_ready(scene), "최초 PP Snapshot이 READY가 아닙니다.")
        assert_true(settings.snapshot_image_count == 2 and settings.snapshot_point_count == 2, "Snapshot 수량이 다릅니다.")
        assert_true(settings.paint_target is target and layer.image is paint_image, "Paint 자산 포인터가 변경됐습니다.")
        assert_true(tuple(paint_image.pixels) == original_pixels, "Paint Texture pixel이 변경됐습니다.")
        first_uuid = str(settings.snapshot_uuid)
        first_pointers = _snapshot_pointers()
        assert_true(bool(first_pointers), "PP Snapshot 소유 ID가 없습니다.")
        assert_true(
            first_pointers.isdisjoint(_modifier_pointers()),
            "Modifier와 PP Snapshot이 Blender ID를 공유합니다.",
        )

        modifier_point_size = float(scene.colmap_import.point_size)
        settings.point_size = modifier_point_size * 2.0
        independent_point_size = float(settings.point_size)
        scene.colmap_import.point_size = modifier_point_size * 3.0
        assert_true(float(settings.point_size) == independent_point_size, "Modifier 표시 변경이 PP에 자동 반영됐습니다.")

        release_scene_marker_session(scene, "projection_paint")
        _import_modifier(scene, second)
        assert_true(str(settings.snapshot_uuid) == first_uuid and settings.snapshot_image_count == 2, "Modifier 재Import가 PP Snapshot을 바꿨습니다.")
        assert_true(snapshot_update_available(scene), "Modifier 변경 뒤 업데이트 가능 상태가 표시되지 않습니다.")
        acquire_scene_marker_session(scene, "projection_paint")
        result = bpy.ops.projection_paint.update_colmap_snapshot()
        assert_true(result == {"FINISHED"}, f"재업데이트 실패: {settings.status}")
        assert_true(settings.snapshot_image_count == 1, "재업데이트 Camera 수가 다릅니다.")
        assert_true(float(settings.point_size) == independent_point_size, "재업데이트가 PP 표시값을 덮었습니다.")
        assert_true(settings.paint_target is target and layer.image is paint_image, "재업데이트가 Paint 자산을 교체했습니다.")
        assert_true(tuple(paint_image.pixels) == original_pixels, "재업데이트가 Paint pixel을 변경했습니다.")
        assert_true(not snapshot_update_available(scene), "재업데이트 뒤에도 업데이트 가능 상태가 남았습니다.")
        assert_true(settings.current_image_id == "5", "삭제된 IMAGE_ID가 새 Snapshot의 유효 Camera로 전환되지 않았습니다.")

        stable_uuid = str(settings.snapshot_uuid)
        stable_pointers = _snapshot_pointers()
        clear_runtime()
        cancel_operator = type(
            "CancelSnapshotOperator",
            (),
            {
                "_prepare": PP_OT_update_colmap_snapshot._prepare,
                "_advance": PP_OT_update_colmap_snapshot._advance,
                "_update_progress_feedback": (
                    PP_OT_update_colmap_snapshot._update_progress_feedback
                ),
                "_cleanup_progress_ui": (
                    PP_OT_update_colmap_snapshot._cleanup_progress_ui
                ),
                "cancel": PP_OT_update_colmap_snapshot.cancel,
                "_timer": None,
                "_overlay_handle": None,
            },
        )()
        cancel_operator._prepare(bpy.context)
        cancel_operator._advance(bpy.context)
        cancel_operator._advance(bpy.context)
        cancel_operator.cancel(bpy.context)
        assert_true(
            settings.snapshot_state == "READY"
            and str(settings.snapshot_uuid) == stable_uuid
            and _snapshot_pointers() == stable_pointers
            and settings.paint_target is target
            and layer.image is paint_image
            and tuple(paint_image.pixels) == original_pixels,
            "Provider 복원 중 취소가 기존 READY Snapshot 또는 Paint 자산을 변경했습니다.",
        )
        with patch(
            "tools.modifier.blender.runtime_state.ensure_runtime"
        ) as ensure_runtime, patch(
            "tools.modifier.core.loader.load_reconstruction"
        ) as load_reconstruction:
            paint_state_handlers._rebuild_transient_state(scene, "undo_post")
        ensure_runtime.assert_not_called()
        load_reconstruction.assert_not_called()
        assert_true(
            settings.snapshot_state == "READY"
            and str(settings.snapshot_uuid) == stable_uuid,
            "READY PP Undo 복구가 Modifier Runtime을 깨우거나 Snapshot을 변경했습니다.",
        )
        acquire_scene_marker_session(scene, "projection_paint")
        original_callbacks = transfer_registry._PROVIDERS["modifier"]
        transfer_registry._PROVIDERS["modifier"] = (original_callbacks[0], lambda _context, _snapshot: False)
        try:
            try:
                result = bpy.ops.projection_paint.update_colmap_snapshot()
            except RuntimeError:
                result = {"CANCELLED"}
        finally:
            transfer_registry._PROVIDERS["modifier"] = original_callbacks
        assert_true(result == {"CANCELLED"}, "Source 변경 오류 주입이 실패하지 않았습니다.")
        assert_true(str(settings.snapshot_uuid) == stable_uuid, "실패 Rollback이 Snapshot UUID를 바꿨습니다.")
        assert_true(_snapshot_pointers() == stable_pointers, "실패 Rollback이 Snapshot ID를 바꿨습니다.")
        assert_true(runtime_for_scene(scene) is not None, "Rollback 뒤 Camera Runtime이 복원되지 않았습니다.")

    release_scene_marker_session(scene, "projection_paint")
    projection_paint_addon.unregister()
    modifier_addon.unregister()
    unregister_legacy_dataset()
    print("Projection Painting 독립 Snapshot 회귀 통과")


if __name__ == "__main__":
    main()
