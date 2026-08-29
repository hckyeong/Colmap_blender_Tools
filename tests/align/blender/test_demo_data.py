"""결정적 런타임 점군과 이전 버전 Scene 데이터 마이그레이션을 검증합니다."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _all_positions_are_finite(positions: tuple[tuple[float, float, float], ...]) -> bool:
    """점군 좌표 배열에 NaN 또는 무한대가 없는지 반환합니다."""
    return all(math.isfinite(coordinate) for position in positions for coordinate in position)


def _legacy_id_pointers() -> dict[str, int | None]:
    """이전 버전 이름을 가진 Object·Collection의 현재 포인터를 보관합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES

    pointers: dict[str, int | None] = {}
    for name in LEGACY_DEMO_COLLECTION_NAMES:
        collection = bpy.data.collections.get(name)
        pointers[f"collection:{name}"] = collection.as_pointer() if collection else None
    for name in LEGACY_DEMO_OBJECT_NAMES:
        object_data = bpy.data.objects.get(name)
        pointers[f"object:{name}"] = object_data.as_pointer() if object_data else None
    return pointers


def _remove_test_ids() -> None:
    """이 테스트가 만든 사용자 충돌용 Object·Collection만 명시적으로 제거합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES

    for name in LEGACY_DEMO_OBJECT_NAMES:
        object_data = bpy.data.objects.get(name)
        if object_data is not None:
            bpy.data.objects.remove(object_data, do_unlink=True)
    for name in LEGACY_DEMO_COLLECTION_NAMES:
        collection = bpy.data.collections.get(name)
        if collection is not None:
            bpy.data.collections.remove(collection)


def _create_legacy_owned_ids() -> None:
    """과거 버전이 기록한 bool 소유 표식과 정확한 이름의 테스트 데이터를 만듭니다."""
    from tools.align.constants import (
        LEGACY_DEMO_COLLECTION_NAMES,
        LEGACY_DEMO_OBJECT_NAMES,
        LEGACY_OWNED_DATA_KEY,
    )

    _remove_test_ids()
    for name in LEGACY_DEMO_COLLECTION_NAMES:
        collection = bpy.data.collections.new(name)
        collection[LEGACY_OWNED_DATA_KEY] = True
        assert collection.get(LEGACY_OWNED_DATA_KEY) is True
    for name in LEGACY_DEMO_OBJECT_NAMES:
        object_data = bpy.data.objects.new(name, None)
        object_data[LEGACY_OWNED_DATA_KEY] = True
        assert object_data.get(LEGACY_OWNED_DATA_KEY) is True


def _create_user_named_ids() -> dict[str, int | None]:
    """소유 표식이 없는 사용자 동명 Object·Collection을 만들어 보존 검사를 준비합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES

    _remove_test_ids()
    for name in LEGACY_DEMO_COLLECTION_NAMES:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    for name in LEGACY_DEMO_OBJECT_NAMES:
        object_data = bpy.data.objects.new(name, None)
        bpy.context.scene.collection.objects.link(object_data)
    return _legacy_id_pointers()


def test_demo_point_clouds_are_deterministic() -> None:
    """네 Data Layers 점군의 ID, 개수와 고정 seed 재현성을 확인합니다."""
    from tools.align.constants import DEMO_MODEL_IDS
    from tools.align.operators.demo_data import generate_demo_point_clouds

    first_clouds = generate_demo_point_clouds(point_count=15)
    second_clouds = generate_demo_point_clouds(point_count=15)
    assert tuple(first_clouds) == DEMO_MODEL_IDS
    assert first_clouds == second_clouds

    expected_ids = tuple(range(15))
    assert tuple(cloud.model_id for cloud in first_clouds.values()) == DEMO_MODEL_IDS
    assert tuple(cloud.name for cloud in first_clouds.values()) == (
        "Demo_1",
        "Demo_2",
        "Demo_3",
        "Demo_4",
    )
    assert all(cloud.point_ids == expected_ids for cloud in first_clouds.values())
    assert all(len(cloud.positions) == 15 for cloud in first_clouds.values())
    assert all(_all_positions_are_finite(cloud.positions) for cloud in first_clouds.values())
    assert len({cloud.positions for cloud in first_clouds.values()}) == 4

    try:
        generate_demo_point_clouds(point_count=2)
    except ValueError:
        pass
    else:
        raise AssertionError("3점 미만의 임시 점군은 거부해야 합니다.")


def test_runtime_point_clouds_create_no_blender_scene_data() -> None:
    """점군 런타임 준비가 어떤 Object·Collection도 만들지 않는지 확인합니다."""
    from tools.align.operators.demo_data import (
        cleanup_legacy_owned_demo_data,
        ensure_demo_point_clouds,
    )
    from tools.align.state import RUNTIME

    cleanup_legacy_owned_demo_data()
    RUNTIME.point_clouds.clear()
    RUNTIME.gpu_batches.clear()
    before = _legacy_id_pointers()
    try:
        first_clouds = ensure_demo_point_clouds()
        second_clouds = ensure_demo_point_clouds()

        assert first_clouds is second_clouds
        assert len(first_clouds) == 4
        assert _legacy_id_pointers() == before
        assert all(pointer is None for pointer in before.values())
    finally:
        RUNTIME.point_clouds.clear()
        RUNTIME.gpu_batches.clear()


def test_legacy_owned_data_cleanup_accepts_blender_bool_id_property() -> None:
    """Blender IDProperty bool True를 가진 정확한 legacy 데이터만 정리하는지 확인합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES
    from tools.align.operators.demo_data import cleanup_legacy_owned_demo_data

    _create_legacy_owned_ids()
    cleanup_legacy_owned_demo_data()
    assert all(bpy.data.collections.get(name) is None for name in LEGACY_DEMO_COLLECTION_NAMES)
    assert all(bpy.data.objects.get(name) is None for name in LEGACY_DEMO_OBJECT_NAMES)


def test_user_named_collisions_are_preserved() -> None:
    """소유 표식이 없는 사용자 동명 Object·Collection을 삭제하거나 변경하지 않는지 확인합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES
    from tools.align.operators.demo_data import cleanup_legacy_owned_demo_data

    before = _create_user_named_ids()
    try:
        cleanup_legacy_owned_demo_data()
        assert _legacy_id_pointers() == before
        assert all(bpy.data.collections.get(name) is not None for name in LEGACY_DEMO_COLLECTION_NAMES)
        assert all(bpy.data.objects.get(name) is not None for name in LEGACY_DEMO_OBJECT_NAMES)
    finally:
        _remove_test_ids()


def run_all() -> None:
    """이 파일의 런타임 점군과 legacy 데이터 정리 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_demo_point_clouds_are_deterministic()
    test_runtime_point_clouds_create_no_blender_scene_data()
    test_legacy_owned_data_cleanup_accepts_blender_bool_id_property()
    test_user_named_collisions_are_preserved()
    print("test_demo_data: 통과")


if __name__ == "__main__":
    run_all()
