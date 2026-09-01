"""Blender Background에서 Modifier 데이터 전용 View Layer 격리를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools.modifier.blender.modifier_data_visibility import (  # noqa: E402
    _layer_collection_for_collection,
    is_modifier_data_view_layer,
    synchronize_modifier_data_visibility,
)
from tools.modifier.blender.ownership import (  # noqa: E402
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from shared.colmap_dataset.blender.ownership import (  # noqa: E402
    mark_dataset_owned,
)


def assert_true(condition: bool, message: str) -> None:
    """조건이 거짓이면 Modifier View Layer 계약 위반을 보고합니다."""

    if not condition:
        raise AssertionError(message)


def main() -> None:
    """전용 Layer의 관리 데이터 단독 표시와 일반 Layer 보존을 검증합니다."""

    scene = bpy.context.scene
    ordinary_view_layer = bpy.context.view_layer

    user_collection = bpy.data.collections.new("User Collection")
    user_object = bpy.data.objects.new("User Object", None)
    user_collection.objects.link(user_object)
    scene.collection.children.link(user_collection)

    direct_user_object = bpy.data.objects.new("Direct User Object", None)
    scene.collection.objects.link(direct_user_object)

    managed_root = bpy.data.collections.new("COLMAP Managed Root")
    managed_root[OWNER_KEY] = OWNER_TOKEN
    managed_root[RECORD_KIND_KEY] = "ROOT"
    managed_object = bpy.data.objects.new("COLMAP Managed Camera", None)
    managed_object[OWNER_KEY] = OWNER_TOKEN
    managed_object[RECORD_KIND_KEY] = "CAMERA"
    managed_root.objects.link(managed_object)
    scene.collection.children.link(managed_root)

    shared_root = bpy.data.collections.new("COLMAP Shared Dataset Root")
    mark_dataset_owned(
        shared_root,
        dataset_uuid="shared-dataset",
        role="ROOT",
        source_fingerprint="f" * 64,
    )
    shared_object = bpy.data.objects.new("COLMAP Shared Dataset Camera", None)
    mark_dataset_owned(
        shared_object,
        dataset_uuid="shared-dataset",
        role="CAMERA",
        record_id=1,
        source_fingerprint="f" * 64,
    )
    shared_root.objects.link(shared_object)
    scene.collection.children.link(shared_root)

    modifier_view_layer = synchronize_modifier_data_visibility(
        scene,
        create_view_layer=True,
    )
    assert_true(
        modifier_view_layer is not None
        and is_modifier_data_view_layer(modifier_view_layer),
        "Modifier 데이터 전용 View Layer 소유권이 올바르지 않습니다.",
    )
    modifier_layer_collection = _layer_collection_for_collection(
        modifier_view_layer.layer_collection,
        managed_root,
    )
    ordinary_layer_collection = _layer_collection_for_collection(
        ordinary_view_layer.layer_collection,
        managed_root,
    )
    modifier_shared_layer_collection = _layer_collection_for_collection(
        modifier_view_layer.layer_collection,
        shared_root,
    )
    ordinary_shared_layer_collection = _layer_collection_for_collection(
        ordinary_view_layer.layer_collection,
        shared_root,
    )
    modifier_user_layer_collection = _layer_collection_for_collection(
        modifier_view_layer.layer_collection,
        user_collection,
    )
    assert_true(
        modifier_layer_collection is not None
        and ordinary_layer_collection is not None,
        "관리 Root의 Layer Collection을 찾지 못했습니다.",
    )
    assert_true(
        not modifier_layer_collection.exclude and ordinary_layer_collection.exclude,
        "관리 Root의 View Layer별 제외 상태가 올바르지 않습니다.",
    )
    assert_true(
        modifier_shared_layer_collection is not None
        and ordinary_shared_layer_collection is not None
        and not modifier_shared_layer_collection.exclude
        and ordinary_shared_layer_collection.exclude
        and shared_object.visible_get(view_layer=modifier_view_layer)
        and not shared_object.visible_get(view_layer=ordinary_view_layer),
        "중립 공용 Dataset Root가 Modifier 전용 Layer에만 표시되지 않았습니다.",
    )
    assert_true(
        modifier_user_layer_collection is not None
        and modifier_user_layer_collection.exclude,
        "Modifier 전용 View Layer에서 사용자 Collection이 제외되지 않았습니다.",
    )
    assert_true(
        managed_object.visible_get(view_layer=modifier_view_layer),
        "Modifier 전용 View Layer에서 관리 Object가 보이지 않습니다.",
    )
    assert_true(
        not managed_object.visible_get(view_layer=ordinary_view_layer),
        "일반 View Layer에서 관리 Object가 계속 보입니다.",
    )
    assert_true(
        not user_object.visible_get(view_layer=modifier_view_layer)
        and not direct_user_object.visible_get(view_layer=modifier_view_layer),
        "Modifier 전용 View Layer에서 일반 Scene Object가 계속 보입니다.",
    )
    assert_true(
        user_object.visible_get(view_layer=ordinary_view_layer)
        and direct_user_object.visible_get(view_layer=ordinary_view_layer),
        "일반 View Layer의 사용자 Object 표시 상태가 변경되었습니다.",
    )

    additional_view_layer = scene.view_layers.new("Additional User View Layer")
    synchronize_modifier_data_visibility(scene, create_view_layer=True)
    assert_true(
        _layer_collection_for_collection(
            additional_view_layer.layer_collection,
            managed_root,
        ).exclude
        and _layer_collection_for_collection(
            additional_view_layer.layer_collection,
            shared_root,
        ).exclude
        and not managed_object.visible_get(view_layer=additional_view_layer)
        and not shared_object.visible_get(view_layer=additional_view_layer)
        and user_object.visible_get(view_layer=additional_view_layer)
        and direct_user_object.visible_get(view_layer=additional_view_layer),
        "새 사용자 View Layer에서 Modifier 데이터만 제외되지 않았습니다.",
    )
    assert_true(
        managed_object.name in bpy.data.objects,
        "View Layer 격리 중 관리 Object 데이터블록이 삭제되었습니다.",
    )
    print("COLMAP_MODIFIER_DATA_VISIBILITY_BACKGROUND_OK")


main()
