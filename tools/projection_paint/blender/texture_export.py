"""현재 가시 Base Color Layer Graph를 UDIM 또는 Atlas 파일로 내보냅니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import sys
from time import monotonic
from typing import Any
from uuid import uuid4

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

from .composite_layer import (
    COMPOSITE_BAKE_SAMPLES,
    combine_color_alpha_numpy,
    composite_native_bake_pending,
    create_final_texture_export_image,
    create_texture_export_bake_images,
    invoke_prepared_emit_bake,
    native_object_bake_running,
    prepare_emit_bake,
    remove_final_texture_export_image,
    restore_emit_bake_persistent_state,
    restore_emit_bake_ui_state,
)
from .image_backend import (
    PaintImageContract,
    PaintImageContractError,
    SINGLE_BACKEND,
    UDIM_BACKEND,
    image_contract,
)
from .layer_shader import (
    LayerShaderError,
    active_base_color_graph_layers,
    validate_active_layer_graph,
)
from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    native_bake_progress_view,
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)


class TextureExportError(RuntimeError):
    """최종 Texture Export 입력·Blender 저장·파일 확정 실패를 나타냅니다."""


class TextureExportCommittedCleanupError(TextureExportError):
    """출력 확정은 성공했지만 임시 Backup 정리만 실패한 상태를 명시합니다."""

    def __init__(self, output_paths: tuple[Path, ...], message: str) -> None:
        """확정된 출력 경로와 정리 실패 설명을 함께 보관합니다."""
        super().__init__(message)
        self.output_paths = tuple(output_paths)


@dataclass(frozen=True, slots=True)
class ExportPathPlan:
    """사용자가 고른 기준 경로와 실제 생성할 파일 목록을 보관합니다."""

    directory: Path
    stem: str
    extension: str
    tile_numbers: tuple[int, ...]
    output_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class AtomicCommitResult:
    """새 출력 확정 뒤 남아 있는 이전 Backup과 정리 오류를 구분해 반환합니다."""

    output_paths: tuple[Path, ...]
    retained_backup_paths: tuple[Path, ...]
    cleanup_errors: tuple[str, ...]


@dataclass(slots=True)
class TextureExportProgress:
    """중앙 진행 Overlay의 단계와 수명주기 자원을 보관합니다."""

    session_id: str
    started_at: float
    phase: str = "Preparing"
    detail: str = "가시 Layer Graph와 출력 경로를 준비합니다"
    completed_steps: int = 0
    total_steps: int = 5
    overlay_handle: Any = None
    indeterminate: bool = False
    bake_device_detail: str = ""
    image_size: tuple[int, int] = (0, 0)
    tile_count: int = 0
    sample_count: int = COMPOSITE_BAKE_SAMPLES

    @property
    def elapsed_seconds(self) -> float:
        """Texture Export 시작 뒤 경과한 단조 시간을 초 단위로 반환합니다."""
        return max(0.0, monotonic() - self.started_at)


@dataclass(slots=True)
class TextureExportJob:
    """GUI Texture Export의 Native Bake·파일 Transaction·정리 상태를 보관합니다."""

    session_id: str
    scene: Any
    settings: Any
    paint_target: Any
    paint_target_pointer: int
    view_layer: Any
    visible_layer_uuids: tuple[str, ...]
    source_contract: PaintImageContract
    plan: ExportPathPlan
    output_mode: str
    file_format: str
    color_mode: str
    udim_naming: str
    atlas_max_size: int
    png_compression: int
    jpeg_quality: int
    overwrite: bool
    progress: TextureExportProgress | None = None
    phase: str = "PREPARING"
    color_image: Any = None
    alpha_image: Any = None
    bake_preparation: Any = None
    ui_bake_preparation: Any = None
    native_bake_stage: str = ""
    native_bake_outcome: str = "PENDING"
    native_bake_started: bool = False
    native_bake_job_stopped: bool = False
    cancel_requested: bool = False
    abort_after_bake: bool = False
    pending_error: str = ""
    temporary_directory: Path | None = None
    tile_paths: tuple[Path, ...] = ()
    staged_paths: tuple[Path, ...] = ()
    committed_paths: tuple[Path, ...] = ()
    commit_result: AtomicCommitResult | None = None
    error_message: str = ""
    cleanup_errors: tuple[str, ...] = ()
    cleanup_complete: bool = False
    ui_restored: bool = False


_EXPORT_PROGRESS: dict[str, TextureExportProgress] = {}
_MODAL_EXPORT_OPERATORS: dict[int, Any] = {}
_EXPORT_JOBS: dict[str, TextureExportJob] = {}
_EXPORT_RUNNING = False
_TOKEN_PATTERN = re.compile(r"(?i)(?:[._-]?<(?:UDIM|UVTILE)>[._-]?)")
_VALID_STEM_PATTERN = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]+$")


def _pointer(value: Any) -> int:
    """삭제되었거나 무효한 Blender RNA Pointer를 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _export_image_contract(image: Any) -> PaintImageContract:
    """Paint Image 계약 오류를 Texture Export 도메인 오류로 변환합니다."""
    try:
        return image_contract(image)
    except PaintImageContractError as exc:
        raise TextureExportError(str(exc)) from exc


def udim_to_uvtile(tile_number: int) -> str:
    """UDIM 번호를 Blender 표준 uN_vN 이름으로 변환합니다."""
    index = int(tile_number) - 1001
    if index < 0:
        raise TextureExportError(f"지원하지 않는 UDIM 번호입니다: {tile_number}")
    return f"u{index % 10 + 1}_v{index // 10 + 1}"


def atlas_grid(tile_numbers: tuple[int, ...]) -> tuple[int, int, int, int]:
    """UDIM 집합을 감싸는 Atlas 열·행 수와 최소 U·V를 반환합니다."""
    if not tile_numbers or len(tile_numbers) != len(set(tile_numbers)):
        raise TextureExportError("Atlas UDIM 타일 목록이 비었거나 중복되었습니다.")
    coordinates = tuple(
        ((number - 1001) % 10, (number - 1001) // 10)
        for number in tile_numbers
    )
    if any(number < 1001 for number in tile_numbers):
        raise TextureExportError("Atlas는 1001 이상의 UDIM 타일만 지원합니다.")
    min_u = min(value[0] for value in coordinates)
    max_u = max(value[0] for value in coordinates)
    min_v = min(value[1] for value in coordinates)
    max_v = max(value[1] for value in coordinates)
    return max_u - min_u + 1, max_v - min_v + 1, min_u, min_v


def atlas_dimensions(tile_numbers: tuple[int, ...], max_size: int) -> tuple[int, int, int]:
    """UDIM 배치 비율을 유지하면서 긴 변이 Max Size인 Atlas 크기를 계산합니다."""
    columns, rows, _min_u, _min_v = atlas_grid(tile_numbers)
    cell_size = int(max_size) // max(columns, rows)
    if cell_size <= 0:
        raise TextureExportError("Atlas Max Size가 UDIM 배치보다 작습니다.")
    return columns * cell_size, rows * cell_size, cell_size


def blender_png_compression_to_oiio(value: int) -> int:
    """Blender PNG 압축 0~100을 같은 방향의 OIIO zlib Level 0~9로 변환합니다."""
    clamped = max(0, min(int(value), 100))
    return int((clamped * 9 + 50) // 100)


def atlas_output_attribute(
    output_path: Path,
    *,
    png_compression: int,
    jpeg_quality: int,
) -> tuple[str, int]:
    """Atlas 포맷에 맞는 OIIO PNG 압축 또는 JPEG 품질 출력 힌트를 반환합니다."""
    if output_path.suffix.lower() == ".png":
        return (
            "png:compressionLevel",
            blender_png_compression_to_oiio(png_compression),
        )
    return "CompressionQuality", int(jpeg_quality)


def normalize_export_filepath(filepath: str, file_format: str) -> str:
    """파일 탐색기 경로의 기존 토큰·확장자를 제거하고 선택 포맷 확장자를 붙입니다."""
    path = Path(str(filepath))
    extension = ".png" if str(file_format) == "PNG" else ".jpg"
    stem = _TOKEN_PATTERN.sub("_", path.stem).strip(" ._-")
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        path = path.with_name(stem + extension)
    else:
        path = path.with_name(_TOKEN_PATTERN.sub("_", path.name).strip(" ._-") + extension)
    return str(path)


def build_export_path_plan(
    filepath: str,
    *,
    file_format: str,
    output_mode: str,
    udim_naming: str,
    tile_numbers: tuple[int, ...],
) -> ExportPathPlan:
    """정규화한 사용자 경로에서 SINGLE·UDIM·Atlas 목적 파일을 확정합니다."""
    normalized = Path(normalize_export_filepath(filepath, file_format)).resolve()
    directory = normalized.parent
    stem = normalized.stem.strip(" .")
    extension = normalized.suffix.lower()
    numbers = tuple(sorted(int(number) for number in tile_numbers))
    if not stem or not _VALID_STEM_PATTERN.fullmatch(stem):
        raise TextureExportError("내보낼 파일 이름에 사용할 수 없는 문자가 있습니다.")
    if not numbers or len(numbers) != len(set(numbers)):
        raise TextureExportError("Composite 논리 타일 목록이 비었거나 중복되었습니다.")
    if output_mode == "SINGLE":
        if numbers != (1001,):
            raise TextureExportError("SINGLE 출력은 논리 타일 1001 하나만 지원합니다.")
        outputs = (directory / f"{stem}{extension}",)
    elif output_mode == "ATLAS":
        outputs = (directory / f"{stem}{extension}",)
    elif output_mode == "UDIM":
        labels = (
            tuple(str(number) for number in numbers)
            if udim_naming == "UDIM"
            else tuple(udim_to_uvtile(number) for number in numbers)
        )
        outputs = tuple(directory / f"{stem}.{label}{extension}" for label in labels)
    else:
        raise TextureExportError("지원하지 않는 Texture Export 출력 방식입니다.")
    if any(path.resolve().parent != directory for path in outputs):
        raise TextureExportError("내보낼 파일이 선택한 폴더 밖을 가리킵니다.")
    return ExportPathPlan(directory, stem, extension, numbers, outputs)


def _image_tile_numbers(image: Any) -> tuple[int, ...]:
    """Paint Image 계약에서 정렬된 논리 타일 번호를 반환합니다."""
    if _pointer(image) == 0:
        raise TextureExportError("내보낼 Composite Image가 유효하지 않습니다.")
    try:
        return _export_image_contract(image).tile_numbers
    except TextureExportError:
        raise


def _export_graph_inputs(scene: Any) -> tuple[Any, tuple[Any, ...], tuple[Any, ...], Any]:
    """현재 Base Color Graph와 그중 가시 Layer 및 출력 계약 기준 Image를 읽습니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        raise TextureExportError("Projection Painting 설정이 없습니다.")
    state = str(getattr(settings, "composite_state", "NONE"))
    if state not in {"NONE", "ACTIVE"}:
        raise TextureExportError("Composite 처리 중이거나 손상된 상태에서는 Texture를 내보낼 수 없습니다.")
    target = getattr(settings, "paint_target", None)
    material = getattr(settings, "paint_material", None)
    if _pointer(target) == 0 or _pointer(material) == 0 or material.node_tree is None:
        raise TextureExportError("Texture Export에 필요한 Paint Target과 관리 Material이 없습니다.")
    if len(settings.layers) == 0:
        raise TextureExportError("Texture Export에 필요한 논리 Layer가 없습니다.")
    try:
        validate_active_layer_graph(scene, material)
        graph_layers = active_base_color_graph_layers(settings)
    except LayerShaderError as exc:
        raise TextureExportError(f"활성 Layer Graph가 유효하지 않습니다: {exc}") from exc
    if not graph_layers or any(
        not str(getattr(layer, "layer_uuid", "") or "")
        or _pointer(getattr(layer, "image", None)) == 0
        for layer in graph_layers
    ):
        raise TextureExportError("활성 Layer Graph의 논리 Layer 또는 Image가 유효하지 않습니다.")
    contracts = tuple(_export_image_contract(layer.image) for layer in graph_layers)
    if any(contract != contracts[0] for contract in contracts[1:]):
        raise TextureExportError("활성 Layer Graph의 Image 백엔드 계약이 서로 다릅니다.")
    visible_layers = tuple(
        layer for layer in graph_layers if bool(getattr(layer, "visible", False))
    )
    if not visible_layers:
        raise TextureExportError("내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.")
    return settings, graph_layers, visible_layers, graph_layers[0].image


def _restore_dirty_state(image: Any, was_dirty: bool, active_tile_index: int) -> None:
    """Blender Save a Copy가 지운 Dirty 표식을 동일 Pixel 재대입으로 원래 상태에 맞춥니다."""
    if hasattr(image, "tiles") and len(image.tiles):
        image.tiles.active_index = max(0, min(active_tile_index, len(image.tiles) - 1))
    if was_dirty and not bool(image.is_dirty):
        value = float(image.pixels[0])
        image.pixels[0] = value
        image.update()
    if bool(image.is_dirty) != bool(was_dirty):
        raise TextureExportError("Source Image의 Dirty 상태를 원래 값으로 복구하지 못했습니다.")


def _save_image_copy(
    context: Any,
    image: Any,
    filepath: Path,
    expected: tuple[Path, ...],
    *,
    file_format: str,
    color_mode: str,
    png_compression: int,
    jpeg_quality: int,
) -> tuple[Path, ...]:
    """Blender Image Copy 저장 뒤 Source·Render 설정을 원래 상태로 복구합니다."""
    scene = context.scene
    image_settings = scene.render.image_settings
    old_image_state = (
        str(image.filepath),
        str(image.filepath_raw),
        str(image.file_format),
        bool(image.is_dirty),
        int(getattr(image.tiles, "active_index", 0)),
        str(image.alpha_mode),
        str(image.colorspace_settings.name),
        tuple(_pointer(item) for item in getattr(image, "packed_files", ())),
    )
    old_render_state = (
        str(image_settings.file_format),
        str(image_settings.color_mode),
        str(image_settings.color_depth),
        int(image_settings.compression),
        int(image_settings.quality),
    )
    filepath.parent.mkdir(parents=True, exist_ok=False)
    primary_error: Exception | None = None
    try:
        image_settings.file_format = str(file_format)
        image_settings.color_mode = "RGB" if file_format == "JPEG" else str(color_mode)
        image_settings.color_depth = "8"
        image_settings.compression = int(png_compression)
        image_settings.quality = int(jpeg_quality)
        image.file_format = str(file_format)
        image.filepath_raw = str(filepath)
        with context.temp_override(edit_image=image):
            result = bpy.ops.image.save_as(
                "EXEC_DEFAULT",
                filepath=str(filepath),
                save_as_render=False,
                copy=True,
                relative_path=False,
            )
        if "FINISHED" not in result:
            raise TextureExportError("Blender가 Texture Image 저장을 완료하지 못했습니다.")
        if any(not path.is_file() or path.stat().st_size <= 0 for path in expected):
            raise TextureExportError("저장된 Texture 파일 수 또는 크기가 예상과 다릅니다.")
        return expected
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        restore_errors: list[str] = []
        for label, callback in (
            ("Image filepath", lambda: setattr(image, "filepath", old_image_state[0])),
            ("Image filepath_raw", lambda: setattr(image, "filepath_raw", old_image_state[1])),
            ("Image file_format", lambda: setattr(image, "file_format", old_image_state[2])),
            ("Image alpha_mode", lambda: setattr(image, "alpha_mode", old_image_state[5])),
            ("Image color space", lambda: setattr(image.colorspace_settings, "name", old_image_state[6])),
            ("Render file_format", lambda: setattr(image_settings, "file_format", old_render_state[0])),
            ("Render color_mode", lambda: setattr(image_settings, "color_mode", old_render_state[1])),
            ("Render color_depth", lambda: setattr(image_settings, "color_depth", old_render_state[2])),
            ("Render compression", lambda: setattr(image_settings, "compression", old_render_state[3])),
            ("Render quality", lambda: setattr(image_settings, "quality", old_render_state[4])),
        ):
            try:
                callback()
            except Exception as exc:
                restore_errors.append(f"{label}: {exc}")
        try:
            _restore_dirty_state(image, old_image_state[3], old_image_state[4])
        except Exception as exc:
            restore_errors.append(f"Image Dirty: {exc}")
        if tuple(_pointer(item) for item in getattr(image, "packed_files", ())) != old_image_state[7]:
            restore_errors.append("Image Pack 구성이 변경되었습니다.")
        if restore_errors:
            message = "Texture 저장 상태 복구 실패: " + "; ".join(restore_errors)
            if primary_error is not None:
                raise TextureExportError(message) from primary_error
            raise TextureExportError(message)


def save_single_image(
    context: Any,
    image: Any,
    filepath: Path,
    **options: Any,
) -> tuple[Path, ...]:
    """SINGLE Image를 토큰 없는 한 파일로 저장합니다."""
    if _export_image_contract(image).backend != SINGLE_BACKEND:
        raise TextureExportError("SINGLE 저장에는 SINGLE Paint Image가 필요합니다.")
    path = Path(filepath)
    return _save_image_copy(context, image, path, (path,), **options)


def save_image_tiles(
    context: Any,
    image: Any,
    pattern: Path,
    **options: Any,
) -> tuple[Path, ...]:
    """기존 호환 API로 UDIM Image를 타일별 파일에 저장합니다."""
    contract = _export_image_contract(image)
    if contract.backend != UDIM_BACKEND:
        raise TextureExportError("UDIM 저장에는 UDIM Paint Image가 필요합니다.")
    path = Path(pattern)
    expected = tuple(
        path.parent / path.name.replace("<UDIM>", str(number))
        for number in contract.tile_numbers
    )
    return _save_image_copy(context, image, path, expected, **options)


def create_atlas(
    tile_paths: tuple[Path, ...],
    tile_numbers: tuple[int, ...],
    output_path: Path,
    *,
    max_size: int,
    color_mode: str,
    png_compression: int,
    jpeg_quality: int,
) -> tuple[int, int]:
    """OpenImageIO로 UDIM을 비율 보존 Atlas에 배치하고 8-bit 파일로 기록합니다."""
    try:
        import OpenImageIO as oiio
    except ImportError as exc:
        raise TextureExportError("Blender 번들 OpenImageIO를 불러오지 못했습니다.") from exc
    if len(tile_paths) != len(tile_numbers):
        raise TextureExportError("Atlas Source 파일과 UDIM 번호 수가 다릅니다.")
    width, height, cell_size = atlas_dimensions(tile_numbers, max_size)
    columns, rows, min_u, min_v = atlas_grid(tile_numbers)
    atlas = oiio.ImageBuf(oiio.ImageSpec(width, height, 4, oiio.FLOAT))
    if not oiio.ImageBufAlgo.zero(atlas):
        raise TextureExportError("Atlas 투명 Buffer를 초기화하지 못했습니다.")
    max_v = min_v + rows - 1
    for path, number in zip(tile_paths, tile_numbers):
        source = oiio.ImageBuf(str(path))
        if source.has_error:
            raise TextureExportError(f"Atlas Source를 읽지 못했습니다: {path.name}")
        resized = oiio.ImageBufAlgo.resize(
            source,
            roi=oiio.ROI(0, cell_size, 0, cell_size, 0, 1, 0, 4),
        )
        u = (number - 1001) % 10
        v = (number - 1001) // 10
        x = (u - min_u) * cell_size
        y = (max_v - v) * cell_size
        if not oiio.ImageBufAlgo.paste(atlas, x, y, 0, 0, resized):
            raise TextureExportError(f"UDIM {number}을 Atlas에 배치하지 못했습니다.")
    output = atlas
    if color_mode == "RGB" or output_path.suffix.lower() in {".jpg", ".jpeg"}:
        output = oiio.ImageBufAlgo.channels(atlas, (0, 1, 2))
    output.set_write_format(oiio.UINT8)
    attribute_name, attribute_value = atlas_output_attribute(
        output_path,
        png_compression=png_compression,
        jpeg_quality=jpeg_quality,
    )
    output.specmod().attribute(attribute_name, attribute_value)
    if not output.write(str(output_path)):
        raise TextureExportError("Atlas 파일 기록에 실패했습니다: " + output.geterror())
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise TextureExportError("Atlas 결과 파일이 생성되지 않았습니다.")
    return width, height


def commit_staged_files(
    staged_paths: tuple[Path, ...],
    output_paths: tuple[Path, ...],
    *,
    overwrite: bool,
) -> AtomicCommitResult:
    """전체 출력 묶음을 원자 확정하고 확정 뒤 삭제 못한 이전 Backup을 별도 반환합니다."""
    if len(staged_paths) != len(output_paths) or not staged_paths:
        raise TextureExportError("확정할 Texture 파일 묶음이 유효하지 않습니다.")
    existing = tuple(path for path in output_paths if path.exists())
    if existing and not overwrite:
        raise TextureExportError("같은 이름의 파일이 이미 있습니다. 덮어쓰기를 허용하십시오.")
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    backup_directory = staged_paths[0].parent / "backups"
    try:
        if existing:
            backup_directory.mkdir(exist_ok=False)
            for index, destination in enumerate(output_paths):
                if destination.exists():
                    backup = backup_directory / f"{index:04d}_{destination.name}"
                    os.replace(destination, backup)
                    backups.append((destination, backup))
        for staged, destination in zip(staged_paths, output_paths):
            os.replace(staged, destination)
            committed.append(destination)
    except Exception as exc:
        rollback_errors: list[str] = []
        for destination in reversed(committed):
            try:
                destination.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for destination, backup in reversed(backups):
            try:
                os.replace(backup, destination)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        suffix = f" Rollback 실패: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise TextureExportError(f"Texture 파일 묶음 확정에 실패했습니다.{suffix}") from exc
    cleanup_errors: list[str] = []
    retained_backups: list[Path] = []
    for _destination, backup in backups:
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            retained_backups.append(backup)
            cleanup_errors.append(f"{backup.name}: {exc}")
    return AtomicCommitResult(
        output_paths=tuple(output_paths),
        retained_backup_paths=tuple(retained_backups),
        cleanup_errors=tuple(cleanup_errors),
    )


def _progress_view(progress: TextureExportProgress) -> ProgressOverlayView:
    """Texture Export 진행 상태를 공용 중앙 Overlay View로 변환합니다."""
    if progress.indeterminate:
        return native_bake_progress_view(
            title="Final Texture Export",
            phase=progress.phase,
            elapsed_seconds=progress.elapsed_seconds,
            detail=progress.detail,
            device_detail=progress.bake_device_detail,
            image_size=progress.image_size,
            tile_count=progress.tile_count,
            sample_count=progress.sample_count,
        )
    return ProgressOverlayView(
        title="Final Texture Export",
        phase=progress.phase,
        fraction=float(progress.completed_steps) / float(progress.total_steps),
        completed_units=progress.completed_steps,
        total_units=progress.total_steps,
        elapsed_seconds=progress.elapsed_seconds,
        detail_lines=(progress.detail, f"경과 시간 {progress.elapsed_seconds:.1f}초"),
    )


def _draw_progress(session_id: str) -> None:
    """현재 등록된 Texture Export 세션 하나를 중앙 3D Viewport에 그립니다."""
    from .progress_overlay import draw_progress_overlay

    progress = _EXPORT_PROGRESS.get(session_id)
    if progress is not None:
        draw_progress_overlay(_progress_view(progress))


def create_export_progress() -> TextureExportProgress:
    """Texture Export용 중앙 진행 Overlay를 등록합니다."""
    progress = TextureExportProgress(uuid4().hex, monotonic())
    _EXPORT_PROGRESS[progress.session_id] = progress
    progress.overlay_handle = add_progress_overlay_draw_handler(
        lambda: _draw_progress(progress.session_id)
    )
    request_progress_overlay_redraw()
    return progress


def update_export_progress(progress: TextureExportProgress, step: int, phase: str, detail: str) -> None:
    """실제로 완료된 Export 단계만 중앙 진행 Overlay에 반영합니다."""
    progress.indeterminate = False
    progress.completed_steps = max(0, min(int(step), progress.total_steps))
    progress.phase = str(phase)
    progress.detail = str(detail)
    request_progress_overlay_redraw()


def begin_export_native_bake(
    progress: TextureExportProgress | None,
    *,
    stage: str,
    detail: str,
    device_detail: str,
    image: Any,
) -> None:
    """Color·Alpha Native Bake를 가짜 백분율 없는 공통 활동형 Overlay에 표시합니다."""
    if progress is None:
        return
    contract = _export_image_contract(image)
    progress.indeterminate = True
    progress.phase = "Color Bake" if str(stage) == "COLOR" else "Alpha Bake"
    progress.detail = str(detail)
    progress.bake_device_detail = str(device_detail)
    progress.image_size = tuple(int(value) for value in getattr(image, "size", (0, 0)))
    progress.tile_count = len(contract.tile_numbers)
    progress.sample_count = COMPOSITE_BAKE_SAMPLES
    request_progress_overlay_redraw()


def end_export_native_bake(progress: TextureExportProgress | None) -> None:
    """Native Bake 종료 뒤 실제 후처리 단계 표시로 Overlay를 되돌립니다."""
    if progress is None:
        return
    progress.indeterminate = False
    request_progress_overlay_redraw()


def release_export_progress(progress: TextureExportProgress | None) -> None:
    """성공·오류·취소 뒤 Texture Export 중앙 Overlay를 제거합니다."""
    if progress is None:
        return
    _EXPORT_PROGRESS.pop(progress.session_id, None)
    remove_progress_overlay_draw_handler(progress.overlay_handle)
    progress.overlay_handle = None
    request_progress_overlay_redraw()


def texture_export_running() -> bool:
    """동시에 하나만 허용하는 Texture Export Job 실행 여부를 반환합니다."""
    return bool(_EXPORT_RUNNING)


def texture_export_native_bake_pending() -> bool:
    """수명주기 경계가 조기 정리하면 안 되는 Export OBJECT_BAKE Job 여부를 반환합니다."""
    return any(
        job.phase in {"COLOR_BAKING", "ALPHA_BAKING"}
        for job in _EXPORT_JOBS.values()
    )


def reserve_texture_export_job() -> None:
    """File Browser와 첫 Modal Timer 사이에도 다른 Export가 시작되지 않도록 Job을 예약합니다."""
    global _EXPORT_RUNNING
    if _EXPORT_RUNNING:
        raise TextureExportError("동시에 실행할 수 있는 Texture Export Job은 하나뿐입니다.")
    _EXPORT_RUNNING = True


def release_texture_export_reservation() -> None:
    """완료되었거나 시작 전에 취소된 단일 Texture Export 예약을 해제합니다."""
    global _EXPORT_RUNNING
    _EXPORT_RUNNING = False


def clear_texture_export_jobs(*_args: Any) -> None:
    """Load·Undo·Redo·Extension 해제 경계에서 Modal Export의 안전 취소를 요청합니다."""
    for operator_id, operator in tuple(_MODAL_EXPORT_OPERATORS.items()):
        try:
            operator._export_cancel_requested = True
        except (AttributeError, ReferenceError, RuntimeError):
            _MODAL_EXPORT_OPERATORS.pop(operator_id, None)
    for job in tuple(_EXPORT_JOBS.values()):
        job.cancel_requested = True
        job.abort_after_bake = True
    if _MODAL_EXPORT_OPERATORS or _EXPORT_JOBS:
        return
    for progress in tuple(_EXPORT_PROGRESS.values()):
        release_export_progress(progress)
    release_texture_export_reservation()


def _texture_export_target_belongs_to_scene(job: TextureExportJob, target: Any) -> bool:
    """Handler Object가 Job의 Scene과 Paint Target 양쪽에 정확히 속하는지 확인합니다."""
    if _pointer(target) != job.paint_target_pointer:
        return False
    try:
        return any(_pointer(candidate) == job.paint_target_pointer for candidate in job.scene.objects)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def mark_texture_export_native_bake_outcome(
    scene_or_target: Any,
    target_or_outcome: Any,
    outcome: str | None = None,
) -> None:
    """Bake Handler 신호를 Scene·Paint Target·활성 단계가 일치하는 Export Job에만 기록합니다."""
    if outcome is None:
        scene = None
        target = scene_or_target
        outcome = str(target_or_outcome)
    elif target_or_outcome is None:
        scene = None
        target = scene_or_target
    else:
        scene = scene_or_target
        target = target_or_outcome
    if outcome not in {"RUNNING", "COMPLETE", "CANCELLED"}:
        return
    for job in tuple(_EXPORT_JOBS.values()):
        if (
            job.phase not in {"COLOR_BAKING", "ALPHA_BAKING"}
            or not job.native_bake_stage
            or (scene is not None and _pointer(scene) != _pointer(job.scene))
            or not _texture_export_target_belongs_to_scene(job, target)
        ):
            continue
        if outcome == "RUNNING":
            if job.native_bake_outcome == "PENDING":
                job.native_bake_outcome = "RUNNING"
            return
        if job.native_bake_outcome in {"COMPLETE", "CANCELLED"}:
            return
        job.native_bake_outcome = outcome
        return


def _start_texture_export_native_bake(
    context: Any,
    job: TextureExportJob,
    stage: str,
) -> None:
    """Color 또는 Alpha 준비 상태를 고정하고 INVOKE_DEFAULT OBJECT_BAKE Job을 시작합니다."""
    normalized_stage = str(stage)
    if normalized_stage not in {"COLOR", "ALPHA"}:
        raise TextureExportError("Texture Export Native Bake 단계가 유효하지 않습니다.")
    if native_object_bake_running():
        raise TextureExportError("다른 OBJECT_BAKE Job이 실행 중입니다.")
    if job.bake_preparation is not None or job.native_bake_stage:
        raise TextureExportError("이전 Texture Export Native Bake 상태가 아직 복구되지 않았습니다.")
    image = job.color_image if normalized_stage == "COLOR" else job.alpha_image
    if _pointer(image) == 0:
        raise TextureExportError(f"{normalized_stage.title()} Bake 대상 Image가 없습니다.")
    preparation = prepare_emit_bake(
        context,
        image,
        job.settings,
        job.visible_layer_uuids,
        channel=normalized_stage,
    )
    job.bake_preparation = preparation
    if job.ui_bake_preparation is None:
        job.ui_bake_preparation = preparation
    job.native_bake_stage = normalized_stage
    job.native_bake_outcome = "PENDING"
    job.native_bake_started = False
    job.native_bake_job_stopped = False
    job.phase = "COLOR_BAKING" if normalized_stage == "COLOR" else "ALPHA_BAKING"
    begin_export_native_bake(
        job.progress,
        stage=normalized_stage,
        detail=(
            "가시 Layer Graph의 Color를 EMIT로 계산하고 Blender Image로 변환 중"
            if normalized_stage == "COLOR"
            else "가시 Layer Graph의 Alpha coverage를 EMIT로 계산하고 Blender Image로 변환 중"
        ),
        device_detail=preparation.device_detail,
        image=image,
    )
    try:
        invoke_prepared_emit_bake(preparation, execution_mode="INVOKE_DEFAULT")
    except Exception as exc:
        if native_object_bake_running():
            job.abort_after_bake = True
            job.pending_error = str(exc)
            return
        raise
    job.native_bake_started = True


def start_texture_export_job(
    context: Any,
    filepath: str,
    *,
    output_mode: str,
    file_format: str,
    color_mode: str,
    udim_naming: str,
    atlas_max_size: int,
    png_compression: int,
    jpeg_quality: int,
    overwrite: bool,
    progress: TextureExportProgress | None = None,
    reserved: bool = False,
) -> TextureExportJob:
    """GUI Export 입력과 임시 Image를 고정하고 첫 Color Native Bake를 시작합니다."""
    if not reserved:
        reserve_texture_export_job()
    elif not _EXPORT_RUNNING:
        raise TextureExportError("예약한 Texture Export Job 상태가 유실되었습니다.")
    if _EXPORT_JOBS:
        raise TextureExportError("동시에 실행할 수 있는 GUI Texture Export Job은 하나뿐입니다.")
    settings, _graph_layers, visible_layers, source_image = _export_graph_inputs(context.scene)
    source_contract = _export_image_contract(source_image)
    if source_contract.backend == SINGLE_BACKEND and output_mode != "SINGLE":
        raise TextureExportError("SINGLE Paint Image는 SINGLE 출력으로만 저장할 수 있습니다.")
    if source_contract.backend == UDIM_BACKEND and output_mode == "SINGLE":
        raise TextureExportError("UDIM Paint Image를 SINGLE 출력으로 변환할 수 없습니다.")
    plan = build_export_path_plan(
        filepath,
        file_format=file_format,
        output_mode=output_mode,
        udim_naming=udim_naming,
        tile_numbers=source_contract.tile_numbers,
    )
    if not plan.directory.is_dir():
        raise TextureExportError("선택한 Texture Export 폴더가 존재하지 않습니다.")
    if not overwrite and any(path.exists() for path in plan.output_paths):
        raise TextureExportError("같은 이름의 파일이 이미 있습니다. 덮어쓰기를 허용하십시오.")
    job = TextureExportJob(
        session_id=uuid4().hex,
        scene=context.scene,
        settings=settings,
        paint_target=settings.paint_target,
        paint_target_pointer=_pointer(settings.paint_target),
        view_layer=context.view_layer,
        visible_layer_uuids=tuple(str(layer.layer_uuid) for layer in visible_layers),
        source_contract=source_contract,
        plan=plan,
        output_mode=str(output_mode),
        file_format=str(file_format),
        color_mode=str(color_mode),
        udim_naming=str(udim_naming),
        atlas_max_size=int(atlas_max_size),
        png_compression=int(png_compression),
        jpeg_quality=int(jpeg_quality),
        overwrite=bool(overwrite),
        progress=progress,
    )
    _EXPORT_JOBS[job.session_id] = job
    try:
        update_export_progress(
            progress,
            1,
            "Preparing",
            "가시 Layer Graph와 출력 경로를 검증했습니다",
        ) if progress is not None else None
        color_image, alpha_image, layer_uuids = create_texture_export_bake_images(
            context,
            visible_layers,
        )
        job.color_image = color_image
        job.alpha_image = alpha_image
        if layer_uuids != job.visible_layer_uuids:
            raise TextureExportError("Texture Export Bake Layer UUID가 준비 입력과 다릅니다.")
        if (
            _export_image_contract(color_image) != source_contract
            or _export_image_contract(alpha_image) != source_contract
        ):
            raise TextureExportError("최종 Bake Image가 Layer Graph 백엔드 계약을 변경했습니다.")
        _start_texture_export_native_bake(context, job, "COLOR")
        return job
    except Exception:
        if not native_object_bake_running():
            cleanup_texture_export_job(context, job)
        raise


def _write_texture_export_job(context: Any, job: TextureExportJob) -> None:
    """결합된 임시 Image를 기존 포맷 규칙대로 Transaction 임시 폴더에 기록합니다."""
    job.temporary_directory = job.plan.directory / f".pp_texture_export_{uuid4().hex}"
    tile_directory = job.temporary_directory / "tiles"
    intermediate_format = "PNG" if job.output_mode == "ATLAS" else job.file_format
    intermediate_color = "RGBA" if job.output_mode == "ATLAS" else job.color_mode
    intermediate_extension = "png" if intermediate_format == "PNG" else "jpg"
    save_options = {
        "file_format": intermediate_format,
        "color_mode": intermediate_color,
        "png_compression": job.png_compression,
        "jpeg_quality": job.jpeg_quality,
    }
    if job.source_contract.backend == SINGLE_BACKEND:
        job.tile_paths = save_single_image(
            context,
            job.color_image,
            tile_directory / f"{job.plan.stem}.{intermediate_extension}",
            **save_options,
        )
    else:
        job.tile_paths = save_image_tiles(
            context,
            job.color_image,
            tile_directory / f"{job.plan.stem}.<UDIM>.{intermediate_extension}",
            **save_options,
        )


def _stage_texture_export_job(job: TextureExportJob) -> None:
    """기록된 타일을 Atlas 또는 최종 파일명 묶음으로 Staging합니다."""
    if job.temporary_directory is None:
        raise TextureExportError("Texture Export Staging 임시 폴더가 없습니다.")
    staged_directory = job.temporary_directory / "staged"
    staged_directory.mkdir(exist_ok=False)
    if job.output_mode == "ATLAS":
        job.staged_paths = (staged_directory / job.plan.output_paths[0].name,)
        create_atlas(
            job.tile_paths,
            job.source_contract.tile_numbers,
            job.staged_paths[0],
            max_size=job.atlas_max_size,
            color_mode="RGB" if job.file_format == "JPEG" else job.color_mode,
            png_compression=job.png_compression,
            jpeg_quality=job.jpeg_quality,
        )
        return
    job.staged_paths = tuple(
        staged_directory / path.name for path in job.plan.output_paths
    )
    for source, staged in zip(job.tile_paths, job.staged_paths):
        os.replace(source, staged)


def _commit_texture_export_job(job: TextureExportJob) -> None:
    """Staged 출력 전체를 기존 Backup·Rollback 규칙으로 원자 확정합니다."""
    job.commit_result = commit_staged_files(
        job.staged_paths,
        job.plan.output_paths,
        overwrite=job.overwrite,
    )
    job.committed_paths = job.commit_result.output_paths


def _set_texture_export_failed(job: TextureExportJob, exc: Exception) -> None:
    """Texture Export 실패 원인을 보존하고 정리 대기 상태로 전환합니다."""
    job.error_message = str(exc)
    job.phase = "FAILED"
    end_export_native_bake(job.progress)


def advance_texture_export_job(context: Any, job: TextureExportJob) -> str:
    """Modal TIMER 한 틱에서 Native Bake 또는 후처리 한 단계만 안전하게 진행합니다."""
    try:
        if job.phase in {"COLOR_BAKING", "ALPHA_BAKING"}:
            if native_object_bake_running():
                request_progress_overlay_redraw()
                return job.phase
            if not job.native_bake_job_stopped:
                job.native_bake_job_stopped = True
                return job.phase
            if job.cancel_requested or job.abort_after_bake:
                end_export_native_bake(job.progress)
                job.phase = "CANCELLED"
                return job.phase
            if job.pending_error:
                raise TextureExportError(job.pending_error)
            if job.native_bake_outcome == "CANCELLED":
                end_export_native_bake(job.progress)
                job.phase = "CANCELLED"
                return job.phase
            if job.native_bake_outcome != "COMPLETE":
                raise TextureExportError(
                    f"{job.native_bake_stage.title()} Bake 결과 신호를 확인하지 못했습니다."
                )
            stage = str(job.native_bake_stage)
            image = job.color_image if stage == "COLOR" else job.alpha_image
            preparation = job.bake_preparation
            if preparation is None or _pointer(image) == 0:
                raise TextureExportError(f"{stage.title()} Bake 복구 상태가 없습니다.")
            restore_emit_bake_persistent_state(preparation)
            image.update()
            job.bake_preparation = None
            job.native_bake_stage = ""
            if stage == "COLOR":
                _start_texture_export_native_bake(context, job, "ALPHA")
                return job.phase
            job.phase = "COMBINING"
            if job.progress is not None:
                update_export_progress(
                    job.progress,
                    2,
                    "Combining",
                    "Color와 Alpha Image 채널을 결합합니다",
                )
            return job.phase
        if job.phase == "COMBINING":
            combine_color_alpha_numpy(job.color_image, job.alpha_image)
            remove_final_texture_export_image(job.alpha_image)
            job.alpha_image = None
            job.phase = "WRITING"
            if job.progress is not None:
                update_export_progress(
                    job.progress,
                    3,
                    "Writing",
                    "Texture 파일을 임시 경로에 기록합니다",
                )
            return job.phase
        if job.phase == "WRITING":
            _write_texture_export_job(context, job)
            job.phase = "STAGING"
            if job.progress is not None:
                update_export_progress(
                    job.progress,
                    3,
                    "Staging",
                    f"Texture 파일 {len(job.tile_paths)}개를 Staging합니다",
                )
            return job.phase
        if job.phase == "STAGING":
            _stage_texture_export_job(job)
            job.phase = "COMMITTING"
            if job.progress is not None:
                update_export_progress(
                    job.progress,
                    4,
                    "Committing",
                    "기존 출력 Backup 뒤 Staged 파일 전체를 확정합니다",
                )
            return job.phase
        if job.phase == "COMMITTING":
            _commit_texture_export_job(job)
            job.phase = "SUCCESS"
            if job.progress is not None:
                detail = f"Texture 파일 {len(job.plan.output_paths)}개를 확정했습니다"
                if job.commit_result is not None and job.commit_result.cleanup_errors:
                    detail += " · 이전 Backup은 임시 폴더 정리에서 다시 제거합니다"
                update_export_progress(job.progress, 5, "Complete", detail)
            return job.phase
        return job.phase
    except Exception as exc:
        _set_texture_export_failed(job, exc)
        return job.phase


def cleanup_texture_export_job(context: Any, job: TextureExportJob) -> tuple[str, ...]:
    """Native Job 종료 뒤 Blender 상태·임시 Image·파일을 정확히 한 번 정리합니다."""
    if job.cleanup_complete:
        return job.cleanup_errors
    if native_object_bake_running() and job.phase in {"COLOR_BAKING", "ALPHA_BAKING"}:
        raise TextureExportError("OBJECT_BAKE Job 종료 전에는 Texture Export를 정리할 수 없습니다.")
    errors: list[str] = []
    if job.bake_preparation is not None:
        try:
            restore_emit_bake_persistent_state(job.bake_preparation)
        except Exception as exc:
            errors.append(f"Bake 임시 상태: {exc}")
        job.bake_preparation = None
    if job.ui_bake_preparation is not None and not job.ui_restored:
        try:
            restore_emit_bake_ui_state(context, job.ui_bake_preparation)
            job.ui_restored = True
        except Exception as exc:
            errors.append(f"선택·Mode 상태: {exc}")
    for label, image in (
        ("Alpha Image", job.alpha_image),
        ("Color Image", job.color_image),
    ):
        if _pointer(image) == 0:
            continue
        try:
            remove_final_texture_export_image(image)
        except Exception as exc:
            errors.append(f"임시 Export {label}: {exc}")
    job.alpha_image = None
    job.color_image = None
    if job.temporary_directory is not None and job.temporary_directory.is_dir():
        try:
            shutil.rmtree(job.temporary_directory)
        except OSError as exc:
            errors.append(f"임시 출력 폴더 {job.temporary_directory}: {exc}")
    job.cleanup_errors = tuple(errors)
    job.cleanup_complete = True
    _EXPORT_JOBS.pop(job.session_id, None)
    return job.cleanup_errors


def texture_export_steps(
    context: Any,
    filepath: str,
    *,
    output_mode: str,
    file_format: str,
    color_mode: str,
    udim_naming: str,
    atlas_max_size: int,
    png_compression: int,
    jpeg_quality: int,
    overwrite: bool,
    progress: TextureExportProgress | None = None,
    reserved: bool = False,
):
    """Modal 안전 경계마다 양보하며 최종 준비·저장·원자 확정 Transaction을 실행합니다."""
    global _EXPORT_RUNNING
    if not reserved:
        reserve_texture_export_job()
    elif not _EXPORT_RUNNING:
        raise TextureExportError("예약한 Texture Export Job 상태가 유실되었습니다.")
    export_image = None
    temporary_directory = None
    committed_paths: tuple[Path, ...] = ()
    try:
        _settings, _graph_layers, visible_layers, source_image = _export_graph_inputs(
            context.scene
        )
        source_contract = _export_image_contract(source_image)
        if source_contract.backend == SINGLE_BACKEND and output_mode != "SINGLE":
            raise TextureExportError("SINGLE Paint Image는 SINGLE 출력으로만 저장할 수 있습니다.")
        if source_contract.backend == UDIM_BACKEND and output_mode == "SINGLE":
            raise TextureExportError("UDIM Paint Image를 SINGLE 출력으로 변환할 수 없습니다.")
        tile_numbers = _image_tile_numbers(source_image)
        plan = build_export_path_plan(
            filepath,
            file_format=file_format,
            output_mode=output_mode,
            udim_naming=udim_naming,
            tile_numbers=tile_numbers,
        )
        if not plan.directory.is_dir():
            raise TextureExportError("선택한 Texture Export 폴더가 존재하지 않습니다.")
        if not overwrite and any(path.exists() for path in plan.output_paths):
            raise TextureExportError("같은 이름의 파일이 이미 있습니다. 덮어쓰기를 허용하십시오.")
        if progress is not None:
            update_export_progress(progress, 1, "Preparing", "가시 Layer Graph와 출력 경로를 검증했습니다")
            update_export_progress(progress, 1, "Compositing", "가시 Layer Graph를 임시 RGBA Image로 합성합니다")
        export_image = create_final_texture_export_image(context, visible_layers)
        source_image = export_image
        if _export_image_contract(source_image) != source_contract:
            raise TextureExportError("최종 Bake Image가 Layer Graph 백엔드 계약을 변경했습니다.")
        if progress is not None:
            update_export_progress(progress, 2, "Compositing", "가시 Layer Graph 임시 합성을 완료했습니다")
        yield "PREPARED"
        temporary_directory = plan.directory / f".pp_texture_export_{uuid4().hex}"
        tile_directory = temporary_directory / "tiles"
        intermediate_format = "PNG" if output_mode == "ATLAS" else file_format
        intermediate_color = "RGBA" if output_mode == "ATLAS" else color_mode
        intermediate_extension = "png" if intermediate_format == "PNG" else "jpg"
        save_options = {
            "file_format": intermediate_format,
            "color_mode": intermediate_color,
            "png_compression": png_compression,
            "jpeg_quality": jpeg_quality,
        }
        if progress is not None:
            update_export_progress(progress, 2, "Writing", "Texture 파일을 임시 경로에 기록합니다")
        if source_contract.backend == SINGLE_BACKEND:
            tile_paths = save_single_image(
                context,
                source_image,
                tile_directory / f"{plan.stem}.{intermediate_extension}",
                **save_options,
            )
        else:
            tile_paths = save_image_tiles(
                context,
                source_image,
                tile_directory / f"{plan.stem}.<UDIM>.{intermediate_extension}",
                **save_options,
            )
        if progress is not None:
            update_export_progress(progress, 3, "Writing", f"Texture 파일 {len(tile_paths)}개를 임시 경로에 기록했습니다")
        yield "WRITTEN"
        staged_directory = temporary_directory / "staged"
        staged_directory.mkdir(exist_ok=False)
        if output_mode == "ATLAS":
            staged_paths = (staged_directory / plan.output_paths[0].name,)
            create_atlas(
                tile_paths,
                tile_numbers,
                staged_paths[0],
                max_size=int(atlas_max_size),
                color_mode="RGB" if file_format == "JPEG" else color_mode,
                png_compression=png_compression,
                jpeg_quality=jpeg_quality,
            )
            if progress is not None:
                update_export_progress(progress, 3, "Writing", "UDIM 배치 비율을 유지한 Atlas를 생성했습니다")
        else:
            staged_paths = tuple(staged_directory / path.name for path in plan.output_paths)
            for source, staged in zip(tile_paths, staged_paths):
                os.replace(source, staged)
            if progress is not None:
                update_export_progress(progress, 3, "Writing", "전체 Texture 파일 수와 크기를 검증했습니다")
        yield "STAGED"
        if progress is not None:
            update_export_progress(progress, 3, "Committing", "임시 출력을 원자적으로 확정합니다")
        commit_result = commit_staged_files(
            staged_paths,
            plan.output_paths,
            overwrite=overwrite,
        )
        committed_paths = commit_result.output_paths
        if progress is not None:
            update_export_progress(progress, 4, "Committing", f"Texture 파일 {len(plan.output_paths)}개를 확정했습니다")
            detail = f"Texture 파일 {len(plan.output_paths)}개를 확정했습니다"
            if commit_result.cleanup_errors:
                detail += " · 이전 Backup은 임시 폴더 정리에서 다시 제거합니다"
            update_export_progress(progress, 5, "Complete", detail)
        return plan.output_paths
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        if export_image is not None:
            try:
                remove_final_texture_export_image(export_image)
            except Exception as exc:
                cleanup_errors.append(f"임시 Export Image: {exc}")
        if temporary_directory is not None and temporary_directory.is_dir():
            try:
                shutil.rmtree(temporary_directory)
            except OSError as exc:
                cleanup_errors.append(f"임시 출력 폴더 {temporary_directory}: {exc}")
        _EXPORT_RUNNING = False
        if cleanup_errors:
            message = "Texture Export 정리 실패: " + "; ".join(cleanup_errors)
            if committed_paths:
                raise TextureExportCommittedCleanupError(
                    committed_paths,
                    "새 Texture 출력은 확정되었지만 " + message,
                ) from primary_error
            if primary_error is not None:
                raise TextureExportError(message) from primary_error
            raise TextureExportError(message)


def export_texture_sync(
    context: Any,
    filepath: str,
    **options: Any,
) -> tuple[Path, ...]:
    """Background와 테스트에서 단계형 Export Transaction을 끝까지 동기로 실행합니다."""
    steps = texture_export_steps(context, filepath, **options)
    while True:
        try:
            next(steps)
        except StopIteration as completed:
            return tuple(completed.value)


class PP_OT_texture_export(bpy.types.Operator, ExportHelper):
    """Blender 파일 탐색기에서 최종 Texture Export 옵션과 실행을 제공합니다."""

    bl_idname = "projection_paint.texture_export"
    bl_label = "Export Texture"
    bl_options = {"REGISTER", "BLOCKING"}

    filename_ext = ".png"
    check_existing: BoolProperty(
        name="Check Existing",
        description="애드온의 Overwrite Existing 옵션만 덮어쓰기 권한으로 사용합니다",
        default=False,
        options={"HIDDEN"},
    )
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg", options={"HIDDEN"})
    output_mode: EnumProperty(
        name="Output",
        items=(("SINGLE", "Single", "단일 이미지 파일로 저장합니다"), ("UDIM", "UDIM Tiles", "UDIM 타일별 파일로 저장합니다"), ("ATLAS", "Atlas", "UDIM을 단일 이미지로 배치합니다")),
        default="UDIM",
    )
    file_format: EnumProperty(
        name="Format",
        items=(("PNG", "PNG", "PNG 파일로 저장합니다"), ("JPEG", "JPEG", "JPEG 파일로 저장합니다")),
        default="PNG",
    )
    color_mode: EnumProperty(
        name="Color",
        items=(("RGB", "RGB", "Alpha 없이 저장합니다"), ("RGBA", "RGBA", "Alpha를 포함해 저장합니다")),
        default="RGBA",
    )
    udim_naming: EnumProperty(
        name="UDIM Naming",
        items=(("UDIM", "name.<UDIM>.ext", "1001 형식으로 저장합니다"), ("UVTILE", "name.<UVTILE>.ext", "u1_v1 형식으로 저장합니다")),
        default="UDIM",
    )
    atlas_max_size: EnumProperty(
        name="Atlas Max Size",
        items=(("2048", "2048 px", "긴 변을 최대 2048px로 제한합니다"), ("4096", "4096 px", "긴 변을 최대 4096px로 제한합니다"), ("8192", "8192 px", "긴 변을 최대 8192px로 제한합니다"), ("16384", "16384 px", "긴 변을 최대 16384px로 제한합니다")),
        default="8192",
    )
    png_compression: IntProperty(name="Compression", default=15, min=0, max=100)
    jpeg_quality: IntProperty(name="Quality", default=90, min=0, max=100)
    overwrite: BoolProperty(name="Overwrite Existing", default=False)

    @classmethod
    def poll(cls, context):
        """준비된 Paint Layer Graph와 배타 Job 상태로 Export 버튼 활성 여부를 판정합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        if settings is None or texture_export_running() or composite_native_bake_pending():
            return False
        if str(getattr(settings, "composite_state", "NONE")) not in {"NONE", "ACTIVE"}:
            return False
        material = getattr(settings, "paint_material", None)
        if (
            _pointer(getattr(settings, "paint_target", None)) == 0
            or _pointer(material) == 0
            or getattr(material, "node_tree", None) is None
            or len(settings.layers) == 0
        ):
            return False
        try:
            validate_active_layer_graph(context.scene, material)
            graph_layers = active_base_color_graph_layers(settings)
            return bool(
                graph_layers
                and all(
                    str(getattr(layer, "layer_uuid", "") or "")
                    and _pointer(getattr(layer, "image", None))
                    for layer in graph_layers
                )
            )
        except (LayerShaderError, ReferenceError, RuntimeError, TypeError, ValueError):
            return False

    def invoke(self, context, event):
        """Blend 파일 인접 textures 폴더와 Paint Target 기반 기본 파일명을 제안합니다."""
        blend_path = Path(bpy.data.filepath) if bpy.data.filepath else None
        directory = (blend_path.parent / "textures") if blend_path else Path(bpy.app.tempdir)
        if not directory.is_dir():
            directory = blend_path.parent if blend_path else Path(bpy.app.tempdir)
        target = context.scene.projection_paint.paint_target
        try:
            _settings, _graph_layers, _visible_layers, image = _export_graph_inputs(context.scene)
        except TextureExportError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.output_mode = (
            "SINGLE" if _export_image_contract(image).backend == SINGLE_BACKEND else "UDIM"
        )
        self.filepath = str(directory / f"{target.name}_diffuse.png")
        return ExportHelper.invoke(self, context, event)

    def check(self, _context):
        """Format 변경 때 파일 탐색기의 확장자와 UDIM 토큰 없는 기준 이름을 정규화합니다."""
        normalized = normalize_export_filepath(self.filepath, self.file_format)
        changed = normalized != self.filepath
        self.filepath = normalized
        self.filename_ext = ".png" if self.file_format == "PNG" else ".jpg"
        return changed

    def draw(self, context):
        """Blender 파일 탐색기 오른쪽 Export Options에 출력·포맷별 세부 설정을 표시합니다."""
        layout = self.layout
        _settings, _graph_layers, _visible_layers, image = _export_graph_inputs(context.scene)
        backend = _export_image_contract(image).backend
        if backend == SINGLE_BACKEND:
            self.output_mode = "SINGLE"
            layout.label(text="Single Image", icon="IMAGE_DATA")
        else:
            if self.output_mode == "SINGLE":
                self.output_mode = "UDIM"
            layout.prop(self, "output_mode", expand=True)
        if self.output_mode == "ATLAS":
            layout.prop(self, "atlas_max_size")
            box = layout.box()
            box.label(text="Atlas는 현재 Object의 UDIM UV를 변경하지 않습니다.", icon="INFO")
            if _pointer(image):
                width, height, _cell = atlas_dimensions(
                    _image_tile_numbers(image),
                    int(self.atlas_max_size),
                )
                channels = 4 if self.file_format == "PNG" and self.color_mode == "RGBA" else 3
                memory_bytes = width * height * channels
                box.label(text=f"예상 Atlas: {width}×{height} · {memory_bytes / (1024 ** 2):.1f} MiB")
                if memory_bytes >= 1024 ** 3:
                    box.label(text="예상 Buffer가 1 GiB 이상입니다.", icon="ERROR")
        layout.prop(self, "file_format")
        if self.file_format == "PNG":
            layout.prop(self, "color_mode")
            layout.prop(self, "png_compression")
        else:
            layout.label(text="JPEG는 Alpha를 제외합니다.", icon="INFO")
            layout.prop(self, "jpeg_quality")
        if self.output_mode == "UDIM":
            layout.prop(self, "udim_naming")
        layout.prop(self, "overwrite")

    def execute(self, context):
        """Background는 동기 실행하고 GUI는 Native Bake 상태 머신으로 시작합니다."""
        progress = create_export_progress()
        try:
            reserve_texture_export_job()
        except Exception as exc:
            release_export_progress(progress)
            self.report({"ERROR"}, f"Texture Export 실패: {exc}")
            return {"CANCELLED"}
        options = {
            "output_mode": self.output_mode,
            "file_format": self.file_format,
            "color_mode": self.color_mode,
            "udim_naming": self.udim_naming,
            "atlas_max_size": int(self.atlas_max_size),
            "png_compression": self.png_compression,
            "jpeg_quality": self.jpeg_quality,
            "overwrite": self.overwrite,
            "progress": progress,
            "reserved": True,
        }
        if bpy.app.background or getattr(context, "window", None) is None:
            try:
                paths = export_texture_sync(context, self.filepath, **options)
                release_export_progress(progress)
                self.report({"INFO"}, f"Texture Export 완료: {len(paths)}개 파일 · {paths[0].parent}")
                return {"FINISHED"}
            except TextureExportCommittedCleanupError as exc:
                release_export_progress(progress)
                self.report({"WARNING"}, str(exc))
                return {"FINISHED"}
            except Exception as exc:
                release_export_progress(progress)
                self.report({"ERROR"}, f"Texture Export 실패: {exc}")
                return {"CANCELLED"}
        job = None
        try:
            job = start_texture_export_job(
                context,
                self.filepath,
                **options,
            )
            self._export_job = job
            self._export_progress = progress
            self._export_timer = context.window_manager.event_timer_add(0.05, window=context.window)
            _MODAL_EXPORT_OPERATORS[id(self)] = self
            self._export_cancel_requested = False
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            if job is not None and not job.cleanup_complete and not native_object_bake_running():
                cleanup_texture_export_job(context, job)
            _MODAL_EXPORT_OPERATORS.pop(id(self), None)
            release_export_progress(progress)
            release_texture_export_reservation()
            self.report({"ERROR"}, f"Texture Export Modal 시작 실패: {exc}")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _finish_modal(self, context: Any) -> None:
        """복원·임시 자원 정리 뒤 Event Timer·Overlay·단일 실행 예약을 해제합니다."""
        timer = getattr(self, "_export_timer", None)
        if timer is not None:
            try:
                context.window_manager.event_timer_remove(timer)
            except (ReferenceError, RuntimeError, ValueError):
                pass
            self._export_timer = None
        release_export_progress(getattr(self, "_export_progress", None))
        self._export_progress = None
        _MODAL_EXPORT_OPERATORS.pop(id(self), None)
        self._export_job = None
        release_texture_export_reservation()
        try:
            from .lifecycle import lifecycle_registry

            lifecycle_registry().schedule_pending_unregistration()
        except (ImportError, AttributeError, RuntimeError):
            pass

    def modal(self, context, event):
        """일반 입력을 차단하고 ESC만 Native Bake 취소로 전달한 뒤 TIMER에서 정리합니다."""
        job = getattr(self, "_export_job", None)
        if job is None:
            self._finish_modal(context)
            return {"CANCELLED"}
        lifecycle_cancel = bool(getattr(self, "_export_cancel_requested", False))
        if event.type == "ESC":
            job.cancel_requested = True
            if job.phase in {"COLOR_BAKING", "ALPHA_BAKING"}:
                if native_object_bake_running():
                    return {"PASS_THROUGH"}
                return {"RUNNING_MODAL"}
            job.phase = "CANCELLED"
        elif lifecycle_cancel:
            job.cancel_requested = True
            job.abort_after_bake = True
            if (
                job.phase in {"COLOR_BAKING", "ALPHA_BAKING"}
                and native_object_bake_running()
            ):
                return {"RUNNING_MODAL"}
            if job.phase not in {"COLOR_BAKING", "ALPHA_BAKING"}:
                job.phase = "CANCELLED"
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        if job.phase not in {"SUCCESS", "CANCELLED", "FAILED"}:
            if _pointer(getattr(context, "scene", None)) != _pointer(job.scene):
                if job.phase in {"COLOR_BAKING", "ALPHA_BAKING"} and native_object_bake_running():
                    job.abort_after_bake = True
                    return {"RUNNING_MODAL"}
                _set_texture_export_failed(
                    job,
                    TextureExportError("Texture Export 중 활성 Scene이 변경되었습니다."),
                )
            else:
                advance_texture_export_job(context, job)
        if job.phase in {"SUCCESS", "CANCELLED", "FAILED"}:
            terminal_phase = job.phase
            cleanup_errors = cleanup_texture_export_job(context, job)
            self._finish_modal(context)
            if terminal_phase == "SUCCESS":
                paths = job.plan.output_paths
                if cleanup_errors:
                    self.report(
                        {"WARNING"},
                        "새 Texture 출력은 확정되었지만 Texture Export 정리 실패: "
                        + "; ".join(cleanup_errors),
                    )
                else:
                    self.report(
                        {"INFO"},
                        f"Texture Export 완료: {len(paths)}개 파일 · {paths[0].parent}",
                    )
                return {"FINISHED"}
            if terminal_phase == "CANCELLED":
                message = "Texture Export를 취소했습니다."
                if cleanup_errors:
                    message += " 정리 경고: " + "; ".join(cleanup_errors)
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            message = job.error_message or "Texture Export 상태가 유효하지 않습니다."
            if cleanup_errors:
                message += " / 정리 실패: " + "; ".join(cleanup_errors)
            self.report({"ERROR"}, f"Texture Export 실패: {message}")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


CLASSES = (PP_OT_texture_export,)


__all__ = [
    "CLASSES",
    "PP_OT_texture_export",
    "AtomicCommitResult",
    "TextureExportJob",
    "TextureExportProgress",
    "TextureExportCommittedCleanupError",
    "TextureExportError",
    "atlas_output_attribute",
    "atlas_dimensions",
    "atlas_grid",
    "build_export_path_plan",
    "blender_png_compression_to_oiio",
    "clear_texture_export_jobs",
    "cleanup_texture_export_job",
    "commit_staged_files",
    "create_atlas",
    "create_export_progress",
    "advance_texture_export_job",
    "export_texture_sync",
    "normalize_export_filepath",
    "mark_texture_export_native_bake_outcome",
    "save_image_tiles",
    "save_single_image",
    "texture_export_running",
    "texture_export_native_bake_pending",
    "start_texture_export_job",
    "udim_to_uvtile",
]
