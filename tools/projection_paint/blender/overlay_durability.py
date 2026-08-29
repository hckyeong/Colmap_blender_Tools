"""Paint Overlay를 Pack하고 원자적 복구 Sidecar로 보호합니다."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

import bpy

from .ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY, UUID_KEY


ROLE_KEY = RECORD_KIND_KEY
CONTENT_REVISION_KEY = "pp_content_revision"
PACKED_REVISION_KEY = "pp_packed_revision"
RECOVERY_PATH_KEY = "pp_recovery_path"
RECOVERY_REVISION_KEY = "pp_recovery_revision"
RECOVERY_SHA256_KEY = "pp_recovery_sha256"
LAST_ERROR_KEY = "pp_durability_error"
OVERLAY_ROLE = "OVERLAY_PERSISTENT"
MASK_ROLE = "MASK_PERSISTENT"


class OverlayDurabilityError(RuntimeError):
    """Overlay 내구성 확보 실패를 나타냅니다."""


def _validated_backend_contract(image):
    """관리 Image source와 명시 Target 백엔드·타일 계약을 검증합니다."""
    from .image_backend import (
        IMAGE_BACKEND_KEY,
        PaintImageContract,
        backend_from_target,
        image_contract,
        validate_image_contract,
    )
    from .udim_setup import UDIM_TILES_KEY

    try:
        actual = image_contract(image)
        target_uuid = str(image.get("pp_target_uuid", "") or "")
        target = next(
            (
                candidate
                for candidate in bpy.data.objects
                if str(candidate.get("pp_target_uuid", "") or "") == target_uuid
            ),
            None,
        )
        if target is None or not str(target.get(IMAGE_BACKEND_KEY, "") or ""):
            return actual
        numbers = tuple(
            sorted(int(number) for number in target.get(UDIM_TILES_KEY, (1001,)))
        )
        expected = PaintImageContract(
            backend_from_target(target, image, allow_legacy=False),
            numbers,
            actual.tile_sizes,
        )
        return validate_image_contract(image, expected)
    except Exception as exc:
        raise OverlayDurabilityError(
            f"관리 Overlay 백엔드 계약이 유효하지 않습니다: {exc}"
        ) from exc


def _pointer(value) -> int:
    """삭제된 Blender ID를 안전하게 0으로 변환합니다."""
    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _valid_overlay(image) -> bool:
    """Image가 애드온 소유 영구 Layer 또는 Mask인지 확인합니다."""
    try:
        return bool(
            _pointer(image)
            and image.get(OWNER_KEY) == OWNER_TOKEN
            and image.get(ROLE_KEY) in {OVERLAY_ROLE, MASK_ROLE}
            and str(image.get(UUID_KEY, ""))
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


@dataclass
class OverlayRecord:
    """영구 Overlay와 최근 복구 상태를 보관합니다."""

    image: object
    image_uuid: str
    image_pointer: int
    recovery_path: str = ""
    last_error: str = ""

    def valid(self) -> bool:
        """Registry 항목이 현재 Blender Image와 같은지 확인합니다."""
        return _valid_overlay(self.image) and _pointer(self.image) == self.image_pointer


_REGISTRY: dict[str, OverlayRecord] = {}


def register_overlay(image) -> OverlayRecord:
    """영구 Overlay를 O(1) 저장 Registry에 등록합니다."""
    if image is None or _pointer(image) == 0:
        raise OverlayDurabilityError("등록할 Overlay Image가 없습니다.")
    if (
        image.get(OWNER_KEY) != OWNER_TOKEN
        or image.get(ROLE_KEY) not in {OVERLAY_ROLE, MASK_ROLE}
    ):
        raise OverlayDurabilityError("애드온 소유 영구 Layer·Mask만 등록할 수 있습니다.")
    _validated_backend_contract(image)
    image_uuid = str(image.get(UUID_KEY, ""))
    if not image_uuid:
        image_uuid = uuid.uuid4().hex
        image[UUID_KEY] = image_uuid
    existing = _REGISTRY.get(image_uuid)
    if existing is not None and existing.valid() and existing.image is not image:
        raise OverlayDurabilityError("Overlay UUID가 중복되어 자동 등록을 중지했습니다.")
    record = OverlayRecord(
        image=image,
        image_uuid=image_uuid,
        image_pointer=_pointer(image),
        recovery_path=str(image.get(RECOVERY_PATH_KEY, "")),
        last_error=str(image.get(LAST_ERROR_KEY, "")),
    )
    _REGISTRY[image_uuid] = record
    return record


def unregister_overlay(image) -> None:
    """Image를 삭제하지 않고 Runtime Registry에서만 해제합니다."""
    if image is None:
        return
    try:
        image_uuid = str(image.get(UUID_KEY, ""))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return
    record = _REGISTRY.get(image_uuid)
    if record is not None and record.image is image:
        _REGISTRY.pop(image_uuid, None)


def clear_runtime_registry() -> None:
    """파일 로드 전에 Blender ID를 건드리지 않고 Runtime Registry를 비웁니다."""
    _REGISTRY.clear()


def authoritative_overlays() -> tuple[object, ...]:
    """모든 Scene Layer와 Mask Pointer가 가리키는 권위 Paint Image를 반환합니다."""
    from .paint_state import resolve_overlay

    values = []
    seen = set()
    for scene in bpy.data.scenes:
        settings = getattr(scene, "projection_paint", None)
        candidates = [resolve_overlay(scene)]
        if settings is not None:
            candidates.extend(
                layer.image
                for layer in settings.layers
            )
            candidates.extend(
                mask.image
                for layer in settings.layers
                for mask in (
                    layer.mask,
                    *(adjustment.mask for adjustment in layer.adjustments),
                )
                if str(mask.mask_uuid or "")
            )
        for image in candidates:
            pointer = _pointer(image)
            if image is None or pointer in seen or not _valid_overlay(image):
                continue
            seen.add(pointer)
            values.append(image)
    return tuple(values)


def rebuild_overlay_registry() -> tuple[OverlayRecord, ...]:
    """Scene Pointer를 우선 등록하고 전체 Image의 UUID 충돌을 검증합니다."""
    _REGISTRY.clear()
    duplicates: list[str] = []
    authorities = authoritative_overlays()
    authority_pointers = {_pointer(image) for image in authorities}
    candidates = authorities + tuple(
        image for image in bpy.data.images if _pointer(image) not in authority_pointers
    )
    for image in candidates:
        if not _valid_overlay(image):
            continue
        image_uuid = str(image.get(UUID_KEY, ""))
        if image_uuid in _REGISTRY:
            duplicates.append(image_uuid)
            continue
        register_overlay(image)
    if duplicates:
        raise OverlayDurabilityError(
            f"중복 Overlay UUID가 있어 자동 복구를 중지했습니다: {sorted(set(duplicates))}"
        )
    return registered_overlays()


def registered_overlays() -> tuple[OverlayRecord, ...]:
    """현재 유효한 Overlay Registry Snapshot을 반환합니다."""
    return tuple(record for record in _REGISTRY.values() if record.valid())


def mark_overlay_dirty(image) -> int:
    """Stroke 완료 시 Overlay Content Revision을 한 번 증가시킵니다."""
    if not _valid_overlay(image):
        raise OverlayDurabilityError("Dirty 표시 대상이 관리 Overlay가 아닙니다.")
    revision = int(image.get(CONTENT_REVISION_KEY, 0)) + 1
    image[CONTENT_REVISION_KEY] = revision
    return revision


def _content_revision(image) -> int:
    """Image Dirty 상태를 포함한 현재 Content Revision을 반환합니다."""
    revision = int(image.get(CONTENT_REVISION_KEY, 0))
    packed = int(image.get(PACKED_REVISION_KEY, -1))
    if bool(image.is_dirty) and revision <= packed:
        revision = packed + 1
        image[CONTENT_REVISION_KEY] = revision
    return revision


def _packed_complete(image) -> bool:
    """검증된 SINGLE 또는 UDIM Image의 전체 Packed 데이터 존재를 확인합니다."""
    from .image_backend import UDIM_BACKEND

    if _validated_backend_contract(image).backend == UDIM_BACKEND:
        expected = len(tuple(image.tiles))
        return expected > 0 and len(tuple(image.packed_files)) == expected
    return image.packed_file is not None


def _recovery_root(root: str | os.PathLike[str] | None) -> Path:
    """현재 Blend 또는 명시 경로 아래 복구 Directory를 결정합니다."""
    if root is not None:
        return Path(root).expanduser().resolve()
    if bpy.data.filepath:
        return (Path(bpy.data.filepath).resolve().parent / ".projection_paint_recovery")
    return (Path(tempfile.gettempdir()) / "blender_projection_paint_recovery").resolve()


def _write_recovery_sidecar(image, scene, root: Path, revision: int) -> Path:
    """Overlay Pixel을 임시 PNG에 쓴 뒤 원자적으로 교체합니다."""
    from .image_backend import SINGLE_BACKEND, UDIM_BACKEND, managed_staging_paths

    backend = _validated_backend_contract(image).backend
    if backend == UDIM_BACKEND:
        return _write_tiled_recovery_sidecar(image, root, revision)
    root.mkdir(parents=True, exist_ok=True)
    image_uuid = str(image[UUID_KEY])
    destination = root / f"{image_uuid}.png"
    temporary = root / f".{image_uuid}.{uuid.uuid4().hex}.tmp.png"
    try:
        if backend != SINGLE_BACKEND:
            raise OverlayDurabilityError("SINGLE 복구 Sidecar의 백엔드가 유효하지 않습니다.")
        _staging_root, staging_paths = managed_staging_paths(image)
        image.save()
        staging_path = staging_paths[0]
        if not staging_path.is_file() or staging_path.stat().st_size <= 0:
            raise OverlayDurabilityError("SINGLE 관리 Staging 저장에 실패했습니다.")
        shutil.copy2(staging_path, temporary)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise OverlayDurabilityError("복구 Sidecar가 비어 있습니다.")
        os.replace(temporary, destination)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    image[RECOVERY_PATH_KEY] = str(destination)
    image[RECOVERY_REVISION_KEY] = int(revision)
    image[RECOVERY_SHA256_KEY] = _file_sha256(destination)
    return destination


def _write_tiled_recovery_sidecar(image, root: Path, revision: int) -> Path:
    """UDIM 전체 타일을 개별 PNG와 원자적 Manifest로 저장합니다."""
    root.mkdir(parents=True, exist_ok=True)
    image_uuid = str(image[UUID_KEY])
    token = uuid.uuid4().hex
    temporary_dir = root / f".{image_uuid}.{token}.tmp"
    tile_dir = root / f"{image_uuid}.{int(revision)}.{token}.udim"
    destination = root / f"{image_uuid}.udim.json"
    temporary_manifest = root / f".{image_uuid}.{token}.tmp.json"
    temporary_dir.mkdir(parents=False, exist_ok=False)
    try:
        from .image_backend import managed_staging_paths

        _staging_root, staging_paths = managed_staging_paths(image)
        image.save()
        tiles = []
        for tile, staging_path in zip(image.tiles, staging_paths):
            number = int(tile.number)
            path = temporary_dir / f"tile.{number}.png"
            if not staging_path.is_file() or staging_path.stat().st_size <= 0:
                raise OverlayDurabilityError(
                    f"복구 UDIM {number} Sidecar가 비어 있습니다."
                )
            shutil.copy2(staging_path, path)
            tiles.append(
                {
                    "number": number,
                    "width": int(tile.size[0]),
                    "height": int(tile.size[1]),
                    "filename": path.name,
                    "sha256": _file_sha256(path),
                }
            )
        temporary_dir.replace(tile_dir)
        manifest = {
            "schema": 1,
            "image_uuid": image_uuid,
            "revision": int(revision),
            "tile_directory": tile_dir.name,
            "tiles": sorted(tiles, key=lambda item: int(item["number"])),
        }
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_manifest, destination)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        try:
            if temporary_manifest.exists():
                temporary_manifest.unlink()
        except OSError:
            pass
    image[RECOVERY_PATH_KEY] = str(destination)
    image[RECOVERY_REVISION_KEY] = int(revision)
    image[RECOVERY_SHA256_KEY] = _file_sha256(destination)
    return destination


def _file_sha256(path: Path) -> str:
    """복구 Sidecar의 SHA-256을 Streaming 방식으로 계산합니다."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _pack_image(image) -> None:
    """검증된 백엔드별 Pack 경계를 제공해 테스트 실패 주입을 허용합니다."""
    from .image_backend import UDIM_BACKEND, managed_staging_paths

    if _validated_backend_contract(image).backend != UDIM_BACKEND:
        image.pack()
        return

    _root, paths = managed_staging_paths(image)
    image.save()
    if any(not path.is_file() or path.stat().st_size <= 0 for path in paths):
        raise OverlayDurabilityError("UDIM Pack용 Staging 타일 저장에 실패했습니다.")
    image.unpack(method="REMOVE")
    image.reload()
    image.pack()


def _update_scene_state(scene, record: OverlayRecord, state: str) -> None:
    """Overlay 복구 경로와 내구성 상태를 Scene UI에 반영합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return
    settings.durability_state = str(state)
    settings.recovery_available = bool(
        state == "DATA_AT_RISK" and record.recovery_path
    )
    settings.recovery_path = (
        str(record.recovery_path) if settings.recovery_available else ""
    )


def ensure_overlay_durable(
    image,
    *,
    scene=None,
    recovery_root: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> OverlayRecord:
    """Dirty Overlay를 Sidecar와 Packed 데이터로 내구성 있게 확정합니다."""
    record = register_overlay(image)
    revision = _content_revision(image)
    packed_revision = int(image.get(PACKED_REVISION_KEY, -1))
    if (
        not force
        and not bool(image.is_dirty)
        and _packed_complete(image)
        and packed_revision == revision
    ):
        return record
    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    if active_scene is None:
        raise OverlayDurabilityError("복구 Sidecar 저장에 사용할 Scene이 없습니다.")
    pack_error = None
    try:
        _pack_image(image)
        if not _packed_complete(image) or bool(image.is_dirty):
            raise OverlayDurabilityError("Pack 완료 상태를 검증하지 못했습니다.")
        image[PACKED_REVISION_KEY] = int(revision)
    except Exception as exc:
        pack_error = exc
    if pack_error is None:
        record.last_error = ""
        image[LAST_ERROR_KEY] = ""
        _update_scene_state(active_scene, record, "DURABLE")
        return record
    sidecar_error = None
    try:
        path = _write_recovery_sidecar(
            image,
            active_scene,
            _recovery_root(recovery_root),
            revision,
        )
        record.recovery_path = str(path)
    except Exception as exc:
        sidecar_error = exc
    record.last_error = (
        f"Overlay Pack 실패: {pack_error}; "
        f"복구 Sidecar: {record.recovery_path or f'생성 실패({sidecar_error})'}"
    )
    image[LAST_ERROR_KEY] = record.last_error
    _update_scene_state(active_scene, record, "DATA_AT_RISK")
    raise OverlayDurabilityError(record.last_error) from pack_error


def pack_registered_overlays(
    *,
    scene=None,
    recovery_root: str | os.PathLike[str] | None = None,
) -> tuple[OverlayRecord, ...]:
    """Blender Pointer에서 Registry를 다시 만들고 모든 Dirty Overlay를 처리합니다."""
    rebuild_overlay_registry()
    completed: list[OverlayRecord] = []
    failures: list[str] = []
    for record in tuple(_REGISTRY.values()):
        if not record.valid():
            failures.append(f"{record.image_uuid}: Image가 삭제되었습니다.")
            continue
        image = record.image
        revision = _content_revision(image)
        packed_revision = int(image.get(PACKED_REVISION_KEY, -1))
        if (
            not bool(image.is_dirty)
            and _packed_complete(image)
            and revision == packed_revision
        ):
            continue
        try:
            completed.append(
                ensure_overlay_durable(
                    image,
                    scene=scene,
                    recovery_root=recovery_root,
                )
            )
        except OverlayDurabilityError as exc:
            failures.append(f"{record.image_uuid}: {exc}")
    if failures:
        raise OverlayDurabilityError(" | ".join(failures))
    return tuple(completed)


def verify_registered_overlays() -> None:
    """저장 후 모든 Registry Overlay의 Packed 상태를 재검증합니다."""
    failures = []
    for record in tuple(_REGISTRY.values()):
        if not record.valid():
            failures.append(f"{record.image_uuid}: Image 삭제")
            continue
        image = record.image
        if bool(image.is_dirty):
            failures.append(f"{record.image_uuid}: Dirty 상태")
            continue
        if not _packed_complete(image):
            failures.append(f"{record.image_uuid}: Packed 데이터 없음")
            continue
        if int(image.get(PACKED_REVISION_KEY, -1)) != int(
            image.get(CONTENT_REVISION_KEY, 0)
        ):
            failures.append(f"{record.image_uuid}: Revision 불일치")
    if failures:
        raise OverlayDurabilityError(" | ".join(failures))


def recover_overlay_from_sidecar(image, path: str | os.PathLike[str] | None = None) -> int:
    """동일 해상도의 Sidecar Pixel을 관리 Overlay로 복구합니다."""
    if not _valid_overlay(image):
        raise OverlayDurabilityError("복구 대상이 관리 Overlay가 아닙니다.")
    source_path = Path(path or str(image.get(RECOVERY_PATH_KEY, ""))).resolve()
    if not source_path.is_file():
        raise OverlayDurabilityError("복구 Sidecar 파일이 없습니다.")
    expected_checksum = str(image.get(RECOVERY_SHA256_KEY, ""))
    if not expected_checksum:
        raise OverlayDurabilityError("복구 Sidecar Checksum이 없습니다.")
    if _file_sha256(source_path) != expected_checksum:
        raise OverlayDurabilityError("복구 Sidecar Checksum이 일치하지 않습니다.")
    from .image_backend import UDIM_BACKEND

    if _validated_backend_contract(image).backend == UDIM_BACKEND:
        return _recover_tiled_overlay(image, source_path)
    source = bpy.data.images.load(str(source_path), check_existing=False)
    try:
        if tuple(int(value) for value in source.size) != tuple(
            int(value) for value in image.size
        ):
            raise OverlayDurabilityError("복구 Sidecar와 Overlay 해상도가 다릅니다.")
        length = len(source.pixels)
        values = array("f", [0.0]) * length
        source.pixels.foreach_get(values)
        image.pixels.foreach_set(values)
        image.update()
        revision = mark_overlay_dirty(image)
        return revision
    finally:
        try:
            bpy.data.images.remove(source)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass


def _recover_tiled_overlay(image, manifest_path: Path) -> int:
    """검증한 UDIM Manifest 전체를 같은 Tiled Image에 다시 불러옵니다."""
    from .image_backend import UDIM_BACKEND, managed_staging_paths

    if _validated_backend_contract(image).backend != UDIM_BACKEND:
        raise OverlayDurabilityError("UDIM Manifest를 SINGLE Overlay에 복구할 수 없습니다.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OverlayDurabilityError("UDIM 복구 Manifest를 읽지 못했습니다.") from exc
    if (
        int(manifest.get("schema", 0)) != 1
        or str(manifest.get("image_uuid", "")) != str(image.get(UUID_KEY, ""))
    ):
        raise OverlayDurabilityError("UDIM 복구 Manifest 소유권이 일치하지 않습니다.")
    manifest_root = manifest_path.parent.resolve()
    tile_dir = (manifest_root / str(manifest.get("tile_directory", ""))).resolve()
    if tile_dir.parent != manifest_root:
        raise OverlayDurabilityError("UDIM 복구 타일 경로가 Manifest 범위를 벗어났습니다.")
    entries = tuple(manifest.get("tiles", ()))
    expected_numbers = tuple(sorted(int(tile.number) for tile in image.tiles))
    manifest_numbers = tuple(sorted(int(entry.get("number", -1)) for entry in entries))
    if manifest_numbers != expected_numbers:
        raise OverlayDurabilityError("UDIM 복구 Manifest의 타일 집합이 다릅니다.")
    paths = {}
    for entry in entries:
        number = int(entry["number"])
        path = (tile_dir / str(entry.get("filename", ""))).resolve()
        if path.parent != tile_dir.resolve() or not path.is_file():
            raise OverlayDurabilityError(f"UDIM {number} 복구 파일이 없습니다.")
        if _file_sha256(path) != str(entry.get("sha256", "")):
            raise OverlayDurabilityError(f"UDIM {number} 복구 Checksum이 다릅니다.")
        paths[number] = path
    if any(paths[number].name != f"tile.{number}.png" for number in expected_numbers):
        raise OverlayDurabilityError("UDIM 복구 파일명이 타일 토큰 규칙과 다릅니다.")
    _staging_root, staging_paths = managed_staging_paths(image)
    staging_by_number = {
        int(tile.number): path
        for tile, path in zip(image.tiles, staging_paths)
    }
    token = uuid.uuid4().hex
    temporary_paths = {
        number: target.with_name(f".{target.name}.{token}.tmp")
        for number, target in staging_by_number.items()
    }
    backup_paths = {
        number: target.with_name(f".{target.name}.{token}.bak")
        for number, target in staging_by_number.items()
    }
    replaced = []
    try:
        for number in expected_numbers:
            shutil.copy2(paths[number], temporary_paths[number])
            if _file_sha256(temporary_paths[number]) != _file_sha256(paths[number]):
                raise OverlayDurabilityError(f"UDIM {number} 복구 임시 파일 검증에 실패했습니다.")
        for number in expected_numbers:
            target = staging_by_number[number]
            backup = backup_paths[number]
            if target.exists():
                os.replace(target, backup)
            os.replace(temporary_paths[number], target)
            replaced.append(number)
        image.unpack(method="REMOVE")
        image.reload()
        actual_sizes = {
            int(tile.number): tuple(int(value) for value in tile.size)
            for tile in image.tiles
        }
        expected_sizes = {
            int(entry["number"]): (
                int(entry.get("width", 0)),
                int(entry.get("height", 0)),
            )
            for entry in entries
        }
        if actual_sizes != expected_sizes:
            raise OverlayDurabilityError("복구된 UDIM 타일 해상도가 Manifest와 다릅니다.")
    except Exception:
        for number in reversed(replaced):
            target = staging_by_number[number]
            backup = backup_paths[number]
            try:
                if target.exists():
                    target.unlink()
                if backup.exists():
                    os.replace(backup, target)
            except OSError:
                pass
        raise
    finally:
        for path in (*temporary_paths.values(), *backup_paths.values()):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
    return mark_overlay_dirty(image)


class PP_OT_recover_overlay(bpy.types.Operator):
    """현재 Paint Overlay를 마지막 복구 Sidecar에서 복원합니다."""

    bl_idname = "projection_paint.recover_overlay"
    bl_label = "Recover Paint Overlay"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        """Scene Pointer의 권위 Overlay를 검증된 Sidecar로 복구합니다."""
        settings = context.scene.projection_paint
        overlay = settings.active_paint_image or settings.overlay_image
        if not _valid_overlay(overlay):
            candidates = tuple(
                record
                for record in registered_overlays()
                if not settings.recovery_path
                or record.recovery_path == settings.recovery_path
            )
            if len(candidates) != 1:
                self.report({"ERROR"}, "복구할 Overlay를 하나로 확정할 수 없습니다.")
                return {"CANCELLED"}
            overlay = candidates[0].image
        path = settings.recovery_path or str(overlay.get(RECOVERY_PATH_KEY, ""))
        try:
            recover_overlay_from_sidecar(overlay, path)
        except (OverlayDurabilityError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.durability_state = "DIRTY"
        settings.recovery_available = True
        settings.recovery_path = path
        return {"FINISHED"}


class PP_OT_repair_overlay_uuids(bpy.types.Operator):
    """중복 Overlay UUID를 Pixel 변경 없이 개별 UUID로 분리합니다."""

    bl_idname = "projection_paint.repair_overlay_uuids"
    bl_label = "Repair Overlay UUIDs"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        """현재 Scene 권위 Overlay를 보존하고 비권위 중복 후보에 새 UUID를 발급합니다."""
        groups: dict[str, list[object]] = {}
        for image in bpy.data.images:
            if _valid_overlay(image):
                groups.setdefault(str(image.get(UUID_KEY, "")), []).append(image)
        repaired = 0
        settings = context.scene.projection_paint
        current = settings.active_paint_image or settings.overlay_image
        authority_pointers = {_pointer(image) for image in authoritative_overlays()}
        for images in groups.values():
            if len(images) < 2:
                continue
            keep = current if current in images else next(
                (image for image in images if _pointer(image) in authority_pointers),
                images[0],
            )
            for image in images:
                if image is keep:
                    continue
                image[UUID_KEY] = uuid.uuid4().hex
                repaired += 1
        try:
            rebuild_overlay_registry()
        except OverlayDurabilityError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Overlay UUID {repaired}개를 데이터 손실 없이 분리했습니다.")
        return {"FINISHED"}


CLASSES = (PP_OT_recover_overlay, PP_OT_repair_overlay_uuids)


__all__ = [
    "CLASSES",
    "OverlayDurabilityError",
    "OverlayRecord",
    "authoritative_overlays",
    "clear_runtime_registry",
    "ensure_overlay_durable",
    "mark_overlay_dirty",
    "pack_registered_overlays",
    "rebuild_overlay_registry",
    "recover_overlay_from_sidecar",
    "register_overlay",
    "registered_overlays",
    "unregister_overlay",
    "verify_registered_overlays",
]
