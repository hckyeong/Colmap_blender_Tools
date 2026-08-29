"""기존 단일 FILE 관리 Layer Image를 Tiled 1001로 원자적으로 교체합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import bpy

from .material_setup import (
    MANAGED_OVERLAY_INVALID,
    MANAGED_OVERLAY_LEGACY_VALID,
    MANAGED_OVERLAY_RECOVERABLE_FILE_1001,
    MANAGED_OVERLAY_VALID,
    PaintSetupError,
    classify_managed_overlay,
    setup_from_scene_state,
)
from .ownership import UUID_KEY
from .udim_setup import (
    STAGING_DIRECTORY_KEY,
    UDIM_KIND_KEY,
    copy_single_file_to_tiled_image,
    remove_paint_staging_path,
)


class OverlayMigrationError(RuntimeError):
    """관리 Overlay 형식 변환과 원상복구 실패를 나타냅니다."""


@dataclass(frozen=True, slots=True)
class OverlayMigrationResult:
    """자동 복구가 실제 교체를 수행했는지와 최종 Image를 반환합니다."""

    image: Any
    changed: bool


@dataclass(frozen=True, slots=True)
class _ImageReference:
    """Image Pointer 하나의 소유 객체와 속성 이름을 보관합니다."""

    owner: Any
    attribute: str
    label: str

    def assign(self, image: Any) -> None:
        """기록한 PointerProperty 또는 Node Image를 지정 Image로 교체합니다."""
        setattr(self.owner, self.attribute, image)


_SETTINGS_IMAGE_ATTRIBUTES = (
    "review_background_image",
    "overlay_image",
    "active_paint_image",
    "composite_saved_clone_image",
    "original_source_image",
    "stencil_source_image",
    "original_canvas",
)


def _pointer(value: Any) -> int:
    """삭제되었거나 무효한 Blender RNA Pointer를 0으로 정규화합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _append_reference(
    references: list[_ImageReference],
    seen: set[tuple[int, str]],
    owner: Any,
    attribute: str,
    label: str,
    source: Any,
) -> None:
    """실제로 Source를 가리키는 고유 Pointer만 Snapshot에 추가합니다."""
    try:
        if getattr(owner, attribute, None) is not source:
            return
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return
    key = (_pointer(owner), str(attribute))
    if not key[0] or key in seen:
        return
    seen.add(key)
    references.append(_ImageReference(owner, str(attribute), str(label)))


def _collect_persistent_references(scene: Any, material: Any, source: Any) -> tuple[_ImageReference, ...]:
    """현재 관리 Scene과 Material에서 허용한 영구 Image 사용처를 모두 수집합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        raise OverlayMigrationError("Projection Paint Scene 설정이 없습니다.")
    references: list[_ImageReference] = []
    seen: set[tuple[int, str]] = set()
    for attribute in _SETTINGS_IMAGE_ATTRIBUTES:
        _append_reference(
            references,
            seen,
            settings,
            attribute,
            f"Scene 설정.{attribute}",
            source,
        )
    for layer_index, layer in enumerate(settings.layers):
        _append_reference(
            references,
            seen,
            layer,
            "image",
            f"Layer[{layer_index}].image",
            source,
        )
        _append_reference(
            references,
            seen,
            layer.mask,
            "image",
            f"Layer[{layer_index}].mask.image",
            source,
        )
        for adjustment_index, adjustment in enumerate(layer.adjustments):
            _append_reference(
                references,
                seen,
                adjustment,
                "image",
                f"Layer[{layer_index}].adjustments[{adjustment_index}].image",
                source,
            )
            _append_reference(
                references,
                seen,
                adjustment.mask,
                "image",
                f"Layer[{layer_index}].adjustments[{adjustment_index}].mask.image",
                source,
            )
    image_paint = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    if image_paint is not None:
        for attribute in ("canvas", "clone_image"):
            _append_reference(
                references,
                seen,
                image_paint,
                attribute,
                f"ImagePaint.{attribute}",
                source,
            )
    if material is None or material.node_tree is None:
        raise OverlayMigrationError("관리 Overlay 참조를 교체할 Material이 없습니다.")
    for node in material.node_tree.nodes:
        if hasattr(node, "image"):
            _append_reference(
                references,
                seen,
                node,
                "image",
                f"Material Node {node.name}.image",
                source,
            )
    return tuple(references)


def _collect_ui_references(source: Any) -> tuple[_ImageReference, ...]:
    """ID user 수에 포함되지 않는 Image Editor 표시 Pointer를 별도로 수집합니다."""
    references: list[_ImageReference] = []
    seen: set[tuple[int, str]] = set()
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if hasattr(space, "image"):
                    _append_reference(
                        references,
                        seen,
                        space,
                        "image",
                        f"Image Editor {screen.name}.image",
                        source,
                    )
    return tuple(references)


def _copy_logical_metadata(source: Any, replacement: Any) -> None:
    """새 Staging 경로를 유지하면서 기존 Overlay의 논리 custom property를 승계합니다."""
    for key in source.keys():
        if str(key) == STAGING_DIRECTORY_KEY:
            continue
        value = source[key]
        try:
            replacement[key] = value
        except TypeError:
            to_list = getattr(value, "to_list", None)
            if not callable(to_list):
                raise
            replacement[key] = to_list()
    if not str(replacement.get(UUID_KEY, "") or ""):
        raise OverlayMigrationError("기존 Overlay의 논리 UUID가 없습니다.")


def _validate_replacement(scene: Any, target: Any, material: Any, replacement: Any) -> None:
    """참조 교체 뒤 Material·Layer 활성 체인과 Tiled 계약을 함께 검증합니다."""
    classification = classify_managed_overlay(target, replacement)
    if classification.state != MANAGED_OVERLAY_VALID:
        raise OverlayMigrationError(classification.reason or "새 Tiled Overlay 계약이 유효하지 않습니다.")
    settings = scene.projection_paint
    if settings.overlay_image is replacement:
        setup_from_scene_state(scene)
    from .layer_shader import validate_active_layer_graph

    validate_active_layer_graph(scene, material)


def _adopt_legacy_single_backend(
    scene: Any,
    target: Any,
    material: Any,
    source: Any,
) -> None:
    """구형 FILE Stack 전체의 소유·참조·SINGLE 계약을 검증한 뒤 백엔드를 기록합니다."""
    from .image_backend import (
        IMAGE_BACKEND_KEY,
        PaintImageContractError,
        SINGLE_BACKEND,
        image_contract,
    )
    from .ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY

    settings = scene.projection_paint
    if (
        str(target.get(UDIM_KIND_KEY, "") or "") not in {"SINGLE_1001", "UV_ONLY"}
        or str(getattr(source, "source", "") or "") != "FILE"
    ):
        raise OverlayMigrationError("구형 SINGLE 채택 대상이 FILE 1001 계약이 아닙니다.")
    images = []
    seen = set()
    for layer in settings.layers:
        candidates = [
            layer.image,
            layer.mask.image,
        ]
        for adjustment in layer.adjustments:
            candidates.append(adjustment.mask.image)
            if str(adjustment.adjustment_type) == "MASK":
                candidates.append(adjustment.image)
        for image in candidates:
            pointer = _pointer(image)
            if pointer and pointer not in seen:
                seen.add(pointer)
                images.append(image)
    if _pointer(source) not in seen:
        raise OverlayMigrationError("구형 SINGLE Overlay가 현재 Layer Stack에 없습니다.")
    target_uuid = str(target.get("pp_target_uuid", "") or "")
    expected_size = None
    for image in images:
        try:
            contract = image_contract(image)
        except PaintImageContractError as exc:
            raise OverlayMigrationError(
                f"구형 SINGLE Stack Image 계약이 유효하지 않습니다: {exc}"
            ) from exc
        if contract.backend != SINGLE_BACKEND or contract.tile_numbers != (1001,):
            raise OverlayMigrationError("구형 SINGLE Stack에 FILE이 아닌 관리 Image가 섞였습니다.")
        if expected_size is None:
            expected_size = contract.tile_sizes
        elif contract.tile_sizes != expected_size:
            raise OverlayMigrationError("구형 SINGLE Stack의 Image 해상도가 서로 다릅니다.")
        if (
            image.get(OWNER_KEY) != OWNER_TOKEN
            or image.get(RECORD_KIND_KEY) not in {"OVERLAY_PERSISTENT", "MASK_PERSISTENT"}
            or str(image.get("pp_target_uuid", "") or "") != target_uuid
        ):
            raise OverlayMigrationError("구형 SINGLE Stack의 Image 소유권이 유효하지 않습니다.")
        references = _collect_persistent_references(scene, material, image)
        expected_users = len(references) + int(bool(getattr(image, "use_fake_user", False)))
        if int(image.users) != expected_users:
            raise OverlayMigrationError(
                "구형 SINGLE Stack Image가 관리 범위 밖 데이터에서도 사용 중입니다: "
                f"확인된 참조 {len(references)}개, Blender user {int(image.users)}개"
            )
    target[IMAGE_BACKEND_KEY] = SINGLE_BACKEND
    try:
        classification = classify_managed_overlay(target, source)
        if classification.state != MANAGED_OVERLAY_VALID:
            raise OverlayMigrationError(
                classification.reason or "구형 SINGLE 백엔드 채택 검증에 실패했습니다."
            )
        from .layer_shader import validate_active_layer_graph

        validate_active_layer_graph(scene, material)
    except Exception:
        del target[IMAGE_BACKEND_KEY]
        raise


def _remove_unreferenced_replacement(replacement: Any, staging_path: str) -> None:
    """Rollback 뒤 참조가 없는 새 Image와 검증된 소유 Staging을 제거합니다."""
    if replacement is not None and _pointer(replacement):
        try:
            replacement.use_fake_user = False
            if int(replacement.users) == 0:
                bpy.data.images.remove(replacement)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    if staging_path:
        remove_paint_staging_path(staging_path)


def repair_managed_overlay_if_needed(
    context: Any,
    *,
    overlay: Any = None,
) -> OverlayMigrationResult:
    """명시 백엔드는 유지하고 구형 Stack을 검증된 백엔드로 채택합니다."""
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "projection_paint", None) if scene is not None else None
    if settings is None:
        raise OverlayMigrationError("Projection Paint Scene 설정이 없습니다.")
    target = settings.paint_target
    material = settings.paint_material
    source = overlay if overlay is not None else settings.overlay_image
    if target is None or source is None:
        return OverlayMigrationResult(source, False)
    classification = classify_managed_overlay(target, source)
    if classification.state == MANAGED_OVERLAY_VALID:
        return OverlayMigrationResult(source, False)
    if classification.state == MANAGED_OVERLAY_LEGACY_VALID:
        if str(getattr(source, "source", "") or "") == "FILE":
            _adopt_legacy_single_backend(scene, target, material, source)
        return OverlayMigrationResult(source, False)
    if classification.state == MANAGED_OVERLAY_INVALID:
        raise OverlayMigrationError(classification.reason)
    if classification.state != MANAGED_OVERLAY_RECOVERABLE_FILE_1001:
        raise OverlayMigrationError("관리 Overlay 복구 상태를 판정할 수 없습니다.")
    composite_state = str(getattr(settings, "composite_state", "NONE"))
    if composite_state not in {"NONE", "ACTIVE"}:
        raise OverlayMigrationError(
            "Composite 처리 중이거나 손상된 상태에서는 관리 Overlay를 자동 변환하지 않습니다."
        )
    references = _collect_persistent_references(scene, material, source)
    expected_users = len(references) + int(bool(getattr(source, "use_fake_user", False)))
    if int(source.users) != expected_users:
        raise OverlayMigrationError(
            "관리 Overlay가 다른 데이터에서도 사용 중이라 자동 변환하지 않았습니다: "
            f"확인된 참조 {len(references)}개, Blender user {int(source.users)}개"
        )
    ui_references = _collect_ui_references(source)
    replacement = None
    staging_path = ""
    old_name = str(source.name)
    old_fake_user = bool(source.use_fake_user)
    registry_swapped = False
    references_swapped = False
    names_swapped = False
    try:
        replacement = copy_single_file_to_tiled_image(
            source,
            f"{old_name}__TILED_MIGRATION",
        )
        staging_path = str(replacement.get(STAGING_DIRECTORY_KEY, "") or "")
        _copy_logical_metadata(source, replacement)
        for reference in references:
            reference.assign(replacement)
        for reference in ui_references:
            reference.assign(replacement)
        references_swapped = True
        if int(source.users) != int(old_fake_user):
            raise OverlayMigrationError(
                "관리 Overlay의 알려진 참조를 모두 교체하지 못했습니다."
            )
        _validate_replacement(scene, target, material, replacement)
        from .overlay_durability import (
            ensure_overlay_durable,
            register_overlay,
            unregister_overlay,
        )

        unregister_overlay(source)
        try:
            register_overlay(replacement)
            ensure_overlay_durable(replacement, scene=scene, force=True)
        except Exception:
            unregister_overlay(replacement)
            register_overlay(source)
            raise
        registry_swapped = True
        source.name = f"{old_name}__FILE_BACKUP"
        replacement.name = old_name
        names_swapped = True
        source.use_fake_user = False
        if int(source.users) != 0:
            raise OverlayMigrationError("기존 FILE Overlay 참조가 남아 삭제를 중지했습니다.")
        old_staging_path = str(source.get(STAGING_DIRECTORY_KEY, "") or "")
        bpy.data.images.remove(source)
        if old_staging_path:
            remove_paint_staging_path(old_staging_path)
        return OverlayMigrationResult(replacement, True)
    except Exception as exc:
        rollback_errors: list[str] = []
        if names_swapped and replacement is not None and _pointer(source):
            try:
                replacement.name = f"{old_name}__TILED_MIGRATION"
                source.name = old_name
            except Exception as rollback_error:
                rollback_errors.append(f"이름 복구: {rollback_error}")
        if references_swapped and _pointer(source):
            for reference in (*references, *ui_references):
                try:
                    reference.assign(source)
                except Exception as rollback_error:
                    rollback_errors.append(f"{reference.label}: {rollback_error}")
        if _pointer(source):
            try:
                source.use_fake_user = old_fake_user
            except Exception as rollback_error:
                rollback_errors.append(f"Fake User 복구: {rollback_error}")
        if registry_swapped and replacement is not None:
            try:
                from .overlay_durability import register_overlay, unregister_overlay

                unregister_overlay(replacement)
                register_overlay(source)
            except Exception as rollback_error:
                rollback_errors.append(f"Registry 복구: {rollback_error}")
        _remove_unreferenced_replacement(replacement, staging_path)
        if rollback_errors:
            raise OverlayMigrationError(
                f"관리 Overlay UDIM 변환 실패: {exc}; Rollback 실패: "
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, (OverlayMigrationError, PaintSetupError)):
            raise OverlayMigrationError(str(exc)) from exc
        raise OverlayMigrationError(f"관리 Overlay UDIM 변환 실패: {exc}") from exc


__all__ = [
    "OverlayMigrationError",
    "OverlayMigrationResult",
    "repair_managed_overlay_if_needed",
]
