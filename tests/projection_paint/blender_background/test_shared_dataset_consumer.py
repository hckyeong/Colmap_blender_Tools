"""Modifier와 독립적인 Painter 파일 입력·재입력·페인트 보존을 검증합니다."""

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
from tools.projection_paint.blender.snapshot_update import snapshot_is_ready


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
    """파일 입력이 Modifier와 자원을 공유하지 않고 실패 시 이전 자료를 보존하는지 검사합니다."""
    register_legacy_dataset()
    modifier_addon.register()
    projection_paint_addon.register()
    scene = bpy.context.scene
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        first, second = base / "first", base / "second"
        _write_dataset(first, (2, 11))
        _write_dataset(second, (5,))
        _import_modifier(scene, second)
        assert_true(not transfer_registry.provider_available("modifier"), "Modifier 전달 제공자가 자동 등록됐습니다.")
        modifier_ids = _modifier_pointers()
        acquire_scene_marker_session(scene, "projection_paint")
        target_mesh = bpy.data.meshes.new("파일 입력 보존 메시")
        target = bpy.data.objects.new("파일 입력 보존 대상", target_mesh)
        scene.collection.objects.link(target)
        paint_image = bpy.data.images.new("파일 입력 보존 이미지", 2, 2, alpha=True)
        paint_image.pixels[:] = [0.125, 0.25, 0.5, 1.0] * 4
        settings = scene.projection_paint
        settings.paint_target = target
        layer = settings.layers.add()
        layer.name, layer.image = "보존 레이어", paint_image
        original_pixels = tuple(paint_image.pixels)
        settings.dataset_root = str(first)
        settings.input_format = "TEXT"
        clear_runtime()
        result = bpy.ops.projection_paint.import_colmap()
        assert_true(result == {"FINISHED"}, settings.status)
        assert_true(snapshot_is_ready(scene), "직접 입력 결과가 준비 상태가 아닙니다.")
        assert_true(settings.source_kind == "FILE", "직접 입력 출처가 파일로 기록되지 않았습니다.")
        assert_true(not cached_runtime_matches_scene(scene), "Painter가 Modifier 실행 상태를 복원했습니다.")
        assert_true(settings.snapshot_image_count == 2 and settings.snapshot_point_count == 2, "직접 입력 수량이 다릅니다.")
        assert_true(_modifier_pointers() == modifier_ids, "Painter 입력이 Modifier 자원을 변경했습니다.")
        assert_true(not (_snapshot_pointers() & modifier_ids), "두 도구가 Blender 자원을 공유합니다.")
        settings.dataset_root = str(second)
        result = bpy.ops.projection_paint.import_colmap()
        assert_true(result == {"FINISHED"} and settings.snapshot_image_count == 1, settings.status)
        assert_true(settings.paint_target is target and layer.image is paint_image, "재입력이 페인트 자산을 교체했습니다.")
        assert_true(tuple(paint_image.pixels) == original_pixels, "재입력이 페인트 픽셀을 변경했습니다.")
        stable_uuid, stable_pointers = str(settings.snapshot_uuid), _snapshot_pointers()
        camera_path = second / "sparse" / "0" / "cameras.txt"
        camera_path.write_text("7 SIMPLE_RADIAL 4 3 4 2 1.5 0.1\n", encoding="utf-8")
        try:
            result = bpy.ops.projection_paint.import_colmap()
        except RuntimeError:
            result = {"CANCELLED"}
        assert_true(result == {"CANCELLED"}, "지원하지 않는 투영 모델을 허용했습니다.")
        assert_true(str(settings.snapshot_uuid) == stable_uuid and snapshot_is_ready(scene), "입력 실패가 이전 준비 상태를 바꿨습니다.")
        assert_true(_snapshot_pointers() == stable_pointers, "입력 실패가 이전 Blender 자원을 바꿨습니다.")
        assert_true(settings.paint_target is target and tuple(paint_image.pixels) == original_pixels, "실패가 페인트 자산을 바꿨습니다.")
    release_scene_marker_session(scene, "projection_paint")
    projection_paint_addon.unregister()
    modifier_addon.unregister()
    unregister_legacy_dataset()
    print("Painter 독립 파일 입력·도구 공존 회귀 통과")


if __name__ == "__main__":
    main()
