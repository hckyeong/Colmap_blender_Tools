"""A 그룹의 확정 폴리곤 레이어를 Project World OBJ로 증분 내보냅니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
from time import perf_counter
from typing import Any
from uuid import uuid4

import bpy
import numpy as np
from bpy.props import BoolProperty, StringProperty

from ..constants import (
    GEOMETRY_KIND_TRIANGLE_MESH,
    OBJ_EXPORT_OUTPUT_CANDIDATE_PREFIX,
    OBJ_EXPORT_OUTPUT_DIRECTORY_NAME,
    OBJ_EXPORT_PHASE_CANCELLED,
    OBJ_EXPORT_PHASE_CANCEL_PENDING,
    OBJ_EXPORT_PHASE_COMPLETED,
    OBJ_EXPORT_PHASE_FAILED,
    OBJ_EXPORT_PHASE_FINALIZING,
    OBJ_EXPORT_PHASE_IDLE,
    OBJ_EXPORT_PHASE_RUNNING,
    OBJ_EXPORT_PHASE_STALE,
    OBJ_EXPORT_PREVIOUS_OUTPUT_PREFIX,
    OBJ_EXPORT_STAGE_CANCELLED,
    OBJ_EXPORT_STAGE_COMPLETED,
    OBJ_EXPORT_STAGE_FAILED,
    OBJ_EXPORT_STAGE_FINALIZING,
    OBJ_EXPORT_STAGE_REPORT,
    OBJ_EXPORT_STAGE_SNAPSHOT_INPUTS,
    OBJ_EXPORT_STAGE_COPYING_TEXTURES,
    OBJ_EXPORT_STAGE_VALIDATING,
    OBJ_EXPORT_STAGE_WRITING_MATERIALS,
    OBJ_EXPORT_STAGE_WRITING_FACES,
    OBJ_EXPORT_STAGE_WRITING_TEXCOORDS,
    OBJ_EXPORT_STAGE_WRITING_VERTICES,
)
from ..geometry_access import is_triangle_mesh_model
from ..logging_utils import get_logger
from ..mesh_io.embedded_obj_store import extract_embedded_obj_export_geometry
from ..mesh_io.obj_reader import ObjReader
from ..mesh_io.obj_writer import (
    IncrementalObjWriter,
    ObjExportModelSnapshot,
    ObjRoundTripValidation,
    ObjTextureAssetSnapshot,
    build_obj_export_report,
    canonical_obj_filename,
    compose_project_world_matrix,
    transformed_polygon_corners,
    transformed_polygon_texcoord_indices,
    transformed_vertices,
    validate_obj_material_links,
    validate_windows_output_path,
    write_json_deterministic,
    write_material_library,
)
from ..project_persistence.runtime import restore_input_block_reason
from ..session import is_session_active_for_window, session_align_workspace
from ..state import RUNTIME
from ..status import set_workspace_status_text
from ..viewport.renderer import request_redraw


_OBJ_EXPORT_TIMER_INTERVAL_SECONDS = 0.01
_OBJ_EXPORT_TIME_BUDGET_SECONDS = 0.025
_OBJ_EXPORT_VERTEX_CHUNK_SIZE = 4096
_OBJ_EXPORT_TEXCOORD_CHUNK_SIZE = 4096
_OBJ_EXPORT_FACE_CHUNK_SIZE = 4096
_OBJ_EXPORT_VALIDATION_CHUNK_SIZE = 65_536
_OBJ_EXPORT_HASH_CHUNK_SIZE = 1024 * 1024
_LOGGER = get_logger("operators.export_obj")


@dataclass(frozen=True, slots=True)
class ObjExportProgress:
    """패널에 표시할 OBJ Export 단계와 정점·UV·면 누적 처리량을 보관합니다."""

    stage: str = OBJ_EXPORT_STAGE_SNAPSHOT_INPUTS
    fraction: float = 0.0
    model_name: str = ""
    written_vertex_count: int = 0
    written_texture_coordinate_count: int = 0
    written_face_count: int = 0
    total_vertex_count: int = 0
    total_texture_coordinate_count: int = 0
    total_face_count: int = 0


@dataclass(frozen=True, slots=True)
class ObjExportResult:
    """완료한 OBJ Export의 출력 경로와 전체 정점·UV·면 count를 보관합니다."""

    output_root: Path
    report_path: Path
    model_count: int
    vertex_count: int
    texture_coordinate_count: int
    face_count: int
    cleanup_warning: str = ""


def _ordered_obj_records(runtime: Any = RUNTIME) -> tuple[Any, ...]:
    """A 그룹의 삼각형 메시만 load order와 모델 ID 순서로 반환합니다."""

    repository = runtime.model_repository
    records = []
    for record in runtime.model_catalog.active_models():
        model = repository.models.get(record.model_id)
        if model is None or not is_triangle_mesh_model(model):
            continue
        if str(getattr(record, "geometry_kind", "")).strip().upper() != GEOMETRY_KIND_TRIANGLE_MESH:
            continue
        records.append(record)
    return tuple(sorted(records, key=lambda item: (int(item.load_order), str(item.model_id))))


def build_obj_export_snapshots(runtime: Any = RUNTIME) -> tuple[ObjExportModelSnapshot, ...]:
    """현재 A 그룹의 내장 원본 Polygon 또는 구버전 Triangle 형상을 snapshot으로 고정합니다."""

    repository = runtime.model_repository
    snapshots: list[ObjExportModelSnapshot] = []
    for sequence, record in enumerate(_ordered_obj_records(runtime), start=1):
        model = repository.models.get(record.model_id)
        if model is None or not is_triangle_mesh_model(model):
            raise RuntimeError(f"OBJ Export 모델 데이터를 찾을 수 없습니다: {record.display_name}")
        try:
            display = repository.display_transform_for_model_id(record.model_id)
            alignment = repository.alignment_transform_for_model_id(record.model_id)
        except Exception as error:
            raise RuntimeError(
                f"OBJ Export에 필요한 확정 변환을 찾을 수 없습니다: {record.display_name}"
            ) from error
        metadata = repository.source_metadata.get(record.model_id)
        source_fingerprint = str(getattr(metadata, "source_fingerprint", "") or "").strip()
        triangles = np.asarray(getattr(model, "triangles_uint32"), dtype=np.uint32)
        embedded_uuid = str(getattr(model, "embedded_asset_uuid", "") or "").strip()
        if not embedded_uuid and (runtime is RUNTIME or metadata is not None):
            raise RuntimeError(
                f"현재 프로젝트의 OBJ 모델에는 내장 Mesh 식별자가 필요합니다: {record.display_name}"
            )
        # 비전역 Runtime에서 Source metadata가 없는 직접 생성 legacy fixture만 Triangle 경로를 사용합니다.
        embedded_geometry = None
        if embedded_uuid:
            embedded_geometry = extract_embedded_obj_export_geometry(
                embedded_uuid,
                expected_model_id=str(record.model_id),
                expected_revision=int(getattr(model, "geometry_revision", 0)),
            )
            expected_schema = int(getattr(model, "embedded_asset_schema_version", 0))
            expected_polygon_count = int(getattr(model, "original_polygon_count", 0))
            if embedded_geometry.schema_version != expected_schema:
                raise RuntimeError(
                    f"내장 OBJ schema가 Runtime과 다릅니다: {record.display_name}"
                )
            if embedded_geometry.polygon_count != expected_polygon_count:
                raise RuntimeError(
                    f"내장 OBJ Polygon 수가 Runtime과 다릅니다: {record.display_name}"
                )
            vertices = embedded_geometry.vertices_float64
            texcoords = embedded_geometry.texcoords_float64
            component_counts = embedded_geometry.texcoord_component_counts_uint8
            materials = embedded_geometry.materials
            face_count = embedded_geometry.polygon_count
            polygon_starts = embedded_geometry.polygon_loop_starts_uint32
            polygon_totals = embedded_geometry.polygon_loop_totals_uint32
            loop_vertices = embedded_geometry.loop_vertex_indices_uint32
            loop_texcoords = embedded_geometry.loop_texcoord_indices_int32
            polygon_materials = embedded_geometry.polygon_material_indices_int32
        else:
            vertices = np.asarray(getattr(model, "vertices_float64"), dtype=np.float64)
            texcoords = np.asarray(getattr(model, "texcoords_float64"), dtype=np.float64)
            component_counts = np.asarray(
                getattr(model, "texcoord_component_counts_uint8"),
                dtype=np.uint8,
            )
            materials = tuple(getattr(model, "materials"))
            face_count = int(triangles.shape[0])
            polygon_starts = np.empty((0,), dtype=np.uint32)
            polygon_totals = np.empty((0,), dtype=np.uint32)
            loop_vertices = np.empty((0,), dtype=np.uint32)
            loop_texcoords = np.empty((0,), dtype=np.int32)
            polygon_materials = np.empty((0,), dtype=np.int32)
        face_texcoords = np.asarray(
            getattr(model, "triangle_texcoord_indices_int32"),
            dtype=np.int32,
        )
        face_materials = np.asarray(
            getattr(model, "triangle_material_indices_int32"),
            dtype=np.int32,
        )
        snapshots.append(
            ObjExportModelSnapshot(
                model_id=str(record.model_id),
                display_name=str(record.display_name),
                source_path=Path(getattr(model, "source_path")),
                output_filename=canonical_obj_filename(
                    sequence,
                    str(record.display_name),
                    str(record.model_id),
                ),
                load_order=int(record.load_order),
                geometry_revision=int(getattr(model, "geometry_revision", 0)),
                vertex_count=int(vertices.shape[0]),
                face_count=face_count,
                source_fingerprint=source_fingerprint,
                display_fingerprint=str(display.fingerprint),
                alignment_fingerprint=str(alignment.fingerprint),
                project_world_matrix_float64=compose_project_world_matrix(alignment, display),
                vertices_float64=vertices,
                triangles_uint32=triangles,
                texcoords_float64=texcoords,
                texcoord_component_counts_uint8=component_counts,
                triangle_texcoord_indices_int32=face_texcoords,
                materials=materials,
                triangle_material_indices_int32=face_materials,
                polygon_loop_starts_uint32=polygon_starts,
                polygon_loop_totals_uint32=polygon_totals,
                loop_vertex_indices_uint32=loop_vertices,
                loop_texcoord_indices_int32=loop_texcoords,
                polygon_material_indices_int32=polygon_materials,
                embedded_asset_uuid=(embedded_geometry.asset_uuid if embedded_geometry else ""),
                embedded_asset_schema_version=(
                    embedded_geometry.schema_version if embedded_geometry else 0
                ),
                embedded_data_name_hint=(
                    embedded_geometry.data_name_hint if embedded_geometry else ""
                ),
                polygon_topology_explicit=embedded_geometry is not None,
            )
        )
    if not snapshots:
        raise RuntimeError("A 그룹에 OBJ로 내보낼 폴리곤 레이어가 없습니다.")
    return tuple(snapshots)


def obj_export_snapshot_fingerprint(snapshots: tuple[ObjExportModelSnapshot, ...]) -> str:
    """완료 직전 stale 비교에 사용할 결정적 OBJ Export 입력 지문을 만듭니다."""

    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"colmap-align-obj-export-runtime-v3\0")
    for snapshot in snapshots:
        for value in (
            snapshot.model_id,
            snapshot.output_filename,
            snapshot.load_order,
            snapshot.geometry_revision,
            snapshot.vertex_count,
            snapshot.texture_coordinate_count,
            snapshot.face_count,
            snapshot.textured_face_count,
            snapshot.material_count,
            snapshot.diffuse_texture_count,
            snapshot.source_fingerprint,
            snapshot.display_fingerprint,
            snapshot.alignment_fingerprint,
            snapshot.embedded_asset_uuid,
            snapshot.embedded_asset_schema_version,
            snapshot.embedded_data_name_hint,
        ):
            digest.update(str(value).encode("utf-8"))
            digest.update(b"\0")
        digest.update(
            np.ascontiguousarray(
                snapshot.project_world_matrix_float64,
                dtype=np.dtype("<f8"),
            ).tobytes()
        )
        digest.update(b"\0")
        for array, dtype in (
            (snapshot.vertices_float64, np.dtype("<f8")),
            (snapshot.texcoords_float64, np.dtype("<f8")),
            (snapshot.texcoord_component_counts_uint8, np.dtype("u1")),
        ):
            digest.update(np.ascontiguousarray(array, dtype=dtype).tobytes())
            digest.update(b"\0")
        digest.update(
            np.ascontiguousarray(
                snapshot.polygon_material_indices_int32,
                dtype=np.dtype("<i4"),
            ).tobytes()
        )
        digest.update(b"\0")
        for array, dtype in (
            (snapshot.polygon_loop_starts_uint32, np.dtype("<u4")),
            (snapshot.polygon_loop_totals_uint32, np.dtype("<u4")),
            (snapshot.loop_vertex_indices_uint32, np.dtype("<u4")),
            (snapshot.loop_texcoord_indices_int32, np.dtype("<i4")),
        ):
            digest.update(np.ascontiguousarray(array, dtype=dtype).tobytes())
            digest.update(b"\0")
        for material in snapshot.materials:
            digest.update(material.name.encode("utf-8"))
            digest.update(np.asarray(material.diffuse_color, dtype=np.dtype("<f8")).tobytes())
            digest.update(b"\0")
        for asset in snapshot.texture_assets:
            for value in (
                asset.source_path,
                asset.output_relative_path,
                asset.source_size,
                asset.source_mtime_ns,
            ):
                digest.update(str(value).encode("utf-8"))
                digest.update(b"\0")
    return digest.hexdigest()


def _path_contains(parent: Path, child: Path) -> bool:
    """한 경로가 다른 경로와 같거나 그 하위인지 resolve 결과로 확인합니다."""

    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def normalized_obj_output_root(selected_parent: str | Path) -> Path:
    """파일 탐색기 부모를 검증하고 고정 `aligned_obj` 최종 경로를 반환합니다."""

    raw = str(selected_parent).strip()
    if not raw:
        raise ValueError("OBJ를 저장할 부모 폴더를 선택하십시오.")
    parent_candidate = Path(bpy.path.abspath(raw)).expanduser().absolute()
    if parent_candidate.is_symlink():
        raise ValueError("OBJ 출력 부모는 심볼릭 링크일 수 없습니다.")
    parent = parent_candidate.resolve(strict=False)
    if not parent.is_dir():
        raise ValueError("OBJ 출력 부모는 존재하는 일반 폴더여야 합니다.")
    output_root = parent / OBJ_EXPORT_OUTPUT_DIRECTORY_NAME
    if output_root.exists() and (output_root.is_symlink() or not output_root.is_dir()):
        raise ValueError("기존 aligned_obj 경로가 일반 폴더가 아닙니다.")
    return output_root


def _validate_output_against_sources(
    output_root: Path,
    snapshots: tuple[ObjExportModelSnapshot, ...],
) -> None:
    """최종 출력 안의 source를 덮어쓸 수 있는 위험한 경로를 거부합니다."""

    for snapshot in snapshots:
        if _path_contains(output_root, snapshot.source_path):
            raise ValueError(
                f"OBJ 원본이 출력 폴더 안에 있어 덮어쓸 수 없습니다: {snapshot.source_path}"
            )


def _validate_candidate_output_paths(
    candidate_root: Path,
    backup_root: Path,
    snapshots: tuple[ObjExportModelSnapshot, ...],
) -> None:
    """후보·백업과 모든 OBJ·MTL·텍스처 파일 경로를 생성 전에 검사합니다."""

    validate_windows_output_path(candidate_root, "OBJ Export 후보 폴더 경로")
    validate_windows_output_path(backup_root, "OBJ Export 백업 폴더 경로")
    validate_windows_output_path(candidate_root / "obj_export_report.json", "OBJ Export 보고서 경로")
    for snapshot in snapshots:
        validate_windows_output_path(
            candidate_root / snapshot.output_filename,
            "OBJ 출력 경로",
        )
        if snapshot.material_count:
            validate_windows_output_path(
                candidate_root / snapshot.material_library_filename,
                "MTL 출력 경로",
            )
        for asset in snapshot.texture_assets:
            validate_windows_output_path(
                candidate_root / asset.output_relative_path,
                "OBJ 텍스처 출력 경로",
            )


def _validate_flat_export_name_collisions(
    snapshots: tuple[ObjExportModelSnapshot, ...],
) -> None:
    """같은 Export 루트에서 모델·MTL·텍스처 파일명이 덮어쓰이지 않는지 검사합니다."""

    output_owners: dict[str, tuple[str, Path | None]] = {}
    for snapshot in snapshots:
        model_outputs = [("OBJ", snapshot.output_filename)]
        if snapshot.material_count:
            model_outputs.append(("MTL", snapshot.material_library_filename))
        for label, filename in model_outputs:
            key = Path(filename).name.casefold()
            previous = output_owners.get(key)
            if previous is not None:
                raise ValueError(
                    f"Export {label} 파일명이 중복됩니다: {filename}. "
                    "각 모델의 표시 이름을 고유하게 변경하십시오."
                )
            output_owners[key] = (label, None)
        for asset in snapshot.texture_assets:
            filename = asset.output_relative_path.name
            key = filename.casefold()
            previous = output_owners.get(key)
            if previous is None:
                output_owners[key] = ("텍스처", asset.source_path)
                continue
            previous_label, previous_source = previous
            if previous_label == "텍스처" and previous_source == asset.source_path:
                continue
            if previous_label == "텍스처":
                raise ValueError(
                    f"서로 다른 원본 텍스처가 같은 파일명을 사용합니다: {filename}. "
                    "원본 이미지 파일명을 고유하게 변경하십시오."
                )
            raise ValueError(
                f"텍스처 파일명이 Export {previous_label} 파일명과 충돌합니다: {filename}. "
                "원본 이미지 파일명을 변경하십시오."
            )


def _validated_job_directory(path: Path, parent: Path, prefix: str, job_id: str) -> Path:
    """후보·백업 삭제 전에 정확한 부모와 전용 이름을 검증합니다."""

    expected_name = f"{prefix}{job_id}"
    absolute = path.absolute()
    if absolute.parent != parent.absolute() or absolute.name != expected_name:
        raise RuntimeError(f"OBJ Export 전용 작업 경로가 아닙니다: {path}")
    if absolute.is_symlink():
        raise RuntimeError(f"OBJ Export 작업 경로가 심볼릭 링크입니다: {path}")
    return absolute


def _remove_job_directory(path: Path, parent: Path, prefix: str, job_id: str) -> None:
    """검증된 OBJ Export 후보 또는 백업 폴더만 재귀 정리합니다."""

    target = _validated_job_directory(path, parent, prefix, job_id)
    if target.exists():
        shutil.rmtree(target)


class ObjExportJob:
    """여러 메시를 제한 시간마다 기록·검증하고 후보 폴더를 소유합니다."""

    def __init__(
        self,
        output_root: Path,
        snapshots: tuple[ObjExportModelSnapshot, ...],
        *,
        overwrite_existing_output: bool,
        job_id: str | None = None,
    ) -> None:
        """고정 출력·snapshot·후보 경로를 저장하고 아직 파일을 만들지 않습니다."""

        self.job_id = str(job_id or uuid4().hex).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.job_id) is None:
            raise ValueError("OBJ Export job ID가 안전한 형식이 아닙니다.")
        self.output_root = Path(output_root).expanduser().absolute()
        if self.output_root.name != OBJ_EXPORT_OUTPUT_DIRECTORY_NAME:
            raise ValueError("OBJ Export 최종 폴더 이름은 aligned_obj여야 합니다.")
        self.parent = self.output_root.parent
        if not self.parent.is_dir() or self.parent.is_symlink():
            raise ValueError("OBJ Export 부모는 존재하는 일반 폴더여야 합니다.")
        self.candidate_root = self.parent / f"{OBJ_EXPORT_OUTPUT_CANDIDATE_PREFIX}{self.job_id}"
        self.backup_root = self.parent / f"{OBJ_EXPORT_PREVIOUS_OUTPUT_PREFIX}{self.job_id}"
        self.snapshots = tuple(snapshots)
        if not self.snapshots:
            raise ValueError("OBJ Export에는 하나 이상의 메시 snapshot이 필요합니다.")
        model_ids = tuple(item.model_id for item in self.snapshots)
        output_filenames = tuple(item.output_filename for item in self.snapshots)
        if len(set(model_ids)) != len(model_ids):
            raise ValueError("OBJ Export snapshot의 모델 ID가 중복되었습니다.")
        if len({filename.casefold() for filename in output_filenames}) != len(output_filenames):
            raise ValueError(
                "OBJ Export 모델 이름이 중복됩니다. 각 모델의 표시 이름을 고유하게 변경하십시오."
            )
        _validate_flat_export_name_collisions(self.snapshots)
        self.overwrite_existing_output = bool(overwrite_existing_output)
        self.input_fingerprint = obj_export_snapshot_fingerprint(self.snapshots)
        self.total_vertex_count = sum(item.vertex_count for item in self.snapshots)
        self.total_texture_coordinate_count = sum(
            item.texture_coordinate_count for item in self.snapshots
        )
        self.total_face_count = sum(item.face_count for item in self.snapshots)
        self.progress = ObjExportProgress(
            total_vertex_count=self.total_vertex_count,
            total_texture_coordinate_count=self.total_texture_coordinate_count,
            total_face_count=self.total_face_count,
        )
        self.status = "RUNNING"
        self.error_message = ""
        self.warning_message = ""
        self.cancel_requested = False
        self.model_index = 0
        self.vertex_offset = 0
        self.texture_coordinate_offset = 0
        self.face_offset = 0
        self.written_vertex_count = 0
        self.written_texture_coordinate_count = 0
        self.written_face_count = 0
        self.stage = OBJ_EXPORT_STAGE_SNAPSHOT_INPUTS
        self.writer: IncrementalObjWriter | None = None
        self.reader: ObjReader | None = None
        self.parsed: Any = None
        self.validation_vertex_offset = 0
        self.validation_texture_coordinate_offset = 0
        self.validation_face_offset = 0
        self.hash_stream: Any = None
        self.hash_digest: Any = None
        self.texture_asset_index = 0
        self.texture_source_stream: Any = None
        self.texture_target_stream: Any = None
        self.texture_validation_stream: Any = None
        self.texture_source_digest: Any = None
        self.texture_validation_digest: Any = None
        self.copied_texture_hashes: dict[str, str] = {}
        self.shared_texture_hashes: dict[str, str] = {}
        self.validations: dict[str, ObjRoundTripValidation] = {}
        self.report_path = self.candidate_root / "obj_export_report.json"

    @property
    def is_ready_to_finalize(self) -> bool:
        """모든 후보 파일과 보고서 검증이 끝났는지 반환합니다."""

        return self.status == "READY"

    def request_cancel(self) -> None:
        """다음 안전한 chunk 경계에서 작업을 취소하도록 표시합니다."""

        if self.status == "RUNNING":
            self.cancel_requested = True

    def _current_snapshot(self) -> ObjExportModelSnapshot:
        """현재 기록·검증 중인 모델 snapshot을 반환합니다."""

        return self.snapshots[self.model_index]

    def _update_progress(self, stage: str) -> None:
        """현재 stage와 전체 기록 레코드 기준 진행률을 갱신합니다."""

        total = max(
            1,
            self.total_vertex_count
            + self.total_texture_coordinate_count
            + self.total_face_count,
        )
        written = (
            self.written_vertex_count
            + self.written_texture_coordinate_count
            + self.written_face_count
        )
        fraction = min(0.95, max(0.0, written / total * 0.95))
        if stage == OBJ_EXPORT_STAGE_VALIDATING:
            fraction = max(fraction, 0.95)
        elif stage == OBJ_EXPORT_STAGE_REPORT:
            fraction = 0.99
        elif stage in {OBJ_EXPORT_STAGE_FINALIZING, OBJ_EXPORT_STAGE_COMPLETED}:
            fraction = 1.0
        model_name = ""
        if self.model_index < len(self.snapshots):
            model_name = self._current_snapshot().display_name
        self.progress = ObjExportProgress(
            stage=stage,
            fraction=fraction,
            model_name=model_name,
            written_vertex_count=self.written_vertex_count,
            written_texture_coordinate_count=self.written_texture_coordinate_count,
            written_face_count=self.written_face_count,
            total_vertex_count=self.total_vertex_count,
            total_texture_coordinate_count=self.total_texture_coordinate_count,
            total_face_count=self.total_face_count,
        )

    def _close_resources(self) -> None:
        """열린 writer·reader·OBJ hash·텍스처 복사 stream을 중복 없이 닫습니다."""

        if self.writer is not None:
            self.writer.close()
            self.writer = None
        if self.reader is not None and not self.reader.done:
            self.reader.cancel()
        self.reader = None
        if self.hash_stream is not None:
            self.hash_stream.close()
            self.hash_stream = None
        for attribute in (
            "texture_source_stream",
            "texture_target_stream",
            "texture_validation_stream",
        ):
            stream = getattr(self, attribute)
            if stream is not None:
                stream.close()
                setattr(self, attribute, None)

    def cleanup_candidate(self) -> None:
        """열린 자원을 닫고 검증된 후보 폴더만 제거합니다."""

        self._close_resources()
        _remove_job_directory(
            self.candidate_root,
            self.parent,
            OBJ_EXPORT_OUTPUT_CANDIDATE_PREFIX,
            self.job_id,
        )

    def _cancel(self) -> None:
        """취소 요청에서 후보만 정리하고 terminal 상태로 전환합니다."""

        self.cleanup_candidate()
        self.status = "CANCELLED"
        self.stage = OBJ_EXPORT_STAGE_CANCELLED
        self._update_progress(self.stage)

    def _fail(self, error: Exception) -> None:
        """실패 원인을 보존하고 후보·열린 자원을 정리합니다."""

        model_id = (
            self.snapshots[self.model_index].model_id
            if self.model_index < len(self.snapshots)
            else ""
        )
        _LOGGER.error(
            "event=obj_export_failed job_id=%s stage=%s model_id=%s error_type=%s reason=%s",
            self.job_id,
            self.stage,
            model_id,
            type(error).__name__,
            error,
        )
        try:
            self.cleanup_candidate()
        except Exception as cleanup_error:
            self.error_message = f"{error} 후보 정리도 실패했습니다: {cleanup_error}"
        else:
            self.error_message = str(error)
        self.status = "FAILED"
        self.stage = OBJ_EXPORT_STAGE_FAILED
        self._update_progress(self.stage)

    def _prepare_candidate(self) -> None:
        """충돌 없는 같은 부모 후보 폴더를 새로 만듭니다."""

        _validate_output_against_sources(self.output_root, self.snapshots)
        _validate_candidate_output_paths(self.candidate_root, self.backup_root, self.snapshots)
        if self.candidate_root.exists() or self.backup_root.exists():
            raise RuntimeError("같은 ID의 OBJ Export 작업 폴더가 이미 존재합니다.")
        self.candidate_root.mkdir()
        self.stage = "START_MODEL"

    def _start_model(self) -> None:
        """다음 모델의 후보 OBJ writer를 열고 정점 단계로 전환합니다."""

        snapshot = self._current_snapshot()
        self.writer = IncrementalObjWriter(
            self.candidate_root / snapshot.output_filename,
            snapshot,
        )
        self.vertex_offset = 0
        self.texture_coordinate_offset = 0
        self.face_offset = 0
        self.texture_asset_index = 0
        self.copied_texture_hashes = {}
        self.stage = OBJ_EXPORT_STAGE_WRITING_VERTICES
        self._update_progress(self.stage)

    def _write_vertex_chunk(self) -> None:
        """현재 모델의 다음 정점 chunk를 기록합니다."""

        snapshot = self._current_snapshot()
        stop = min(snapshot.vertex_count, self.vertex_offset + _OBJ_EXPORT_VERTEX_CHUNK_SIZE)
        written = self.writer.write_vertices(self.vertex_offset, stop)  # type: ignore[union-attr]
        self.vertex_offset = stop
        self.written_vertex_count += written
        if self.vertex_offset >= snapshot.vertex_count:
            self.stage = (
                OBJ_EXPORT_STAGE_WRITING_TEXCOORDS
                if snapshot.texture_coordinate_count > 0
                else OBJ_EXPORT_STAGE_WRITING_FACES
            )
        self._update_progress(self.stage)

    def _write_texture_coordinate_chunk(self) -> None:
        """현재 모델의 다음 UV 좌표 chunk를 원본 성분 수로 기록합니다."""

        snapshot = self._current_snapshot()
        stop = min(
            snapshot.texture_coordinate_count,
            self.texture_coordinate_offset + _OBJ_EXPORT_TEXCOORD_CHUNK_SIZE,
        )
        written = self.writer.write_texture_coordinates(  # type: ignore[union-attr]
            self.texture_coordinate_offset,
            stop,
        )
        self.texture_coordinate_offset = stop
        self.written_texture_coordinate_count += written
        if self.texture_coordinate_offset >= snapshot.texture_coordinate_count:
            self.stage = OBJ_EXPORT_STAGE_WRITING_FACES
        self._update_progress(self.stage)

    def _write_face_chunk(self) -> None:
        """현재 모델의 다음 Polygon chunk를 기록하고 완료 시 재질 단계로 전환합니다."""

        snapshot = self._current_snapshot()
        stop = min(snapshot.face_count, self.face_offset + _OBJ_EXPORT_FACE_CHUNK_SIZE)
        written = self.writer.write_faces(self.face_offset, stop)  # type: ignore[union-attr]
        self.face_offset = stop
        self.written_face_count += written
        if self.face_offset >= snapshot.face_count:
            self.writer.close()  # type: ignore[union-attr]
            self.writer = None
            self.stage = OBJ_EXPORT_STAGE_WRITING_MATERIALS
        self._update_progress(self.stage)

    def _write_material_library(self) -> None:
        """현재 모델의 생성 MTL을 기록하고 텍스처 복사 또는 OBJ 검증으로 전환합니다."""

        snapshot = self._current_snapshot()
        write_material_library(
            self.candidate_root / snapshot.material_library_filename,
            snapshot,
        )
        self.stage = OBJ_EXPORT_STAGE_COPYING_TEXTURES
        self._advance_past_shared_textures()
        self._update_progress(self.stage)

    def _advance_past_shared_textures(self) -> None:
        """앞선 모델이 이미 복사한 같은 원본 이미지는 해시를 공유하고 중복 복사를 건너뜁니다."""

        assets = self._current_snapshot().texture_assets
        while self.texture_asset_index < len(assets):
            asset = assets[self.texture_asset_index]
            key = asset.output_relative_path.as_posix().casefold()
            output_hash = self.shared_texture_hashes.get(key)
            if output_hash is None:
                self.stage = OBJ_EXPORT_STAGE_COPYING_TEXTURES
                return
            self.copied_texture_hashes[asset.output_relative_path.as_posix()] = output_hash
            self.texture_asset_index += 1
        self._start_validation_reader()

    def _current_texture_asset(self) -> ObjTextureAssetSnapshot:
        """현재 모델에서 복사·검증 중인 텍스처 asset을 반환합니다."""

        return self._current_snapshot().texture_assets[self.texture_asset_index]

    def _start_texture_copy(self) -> None:
        """원본 상태를 확인하고 현재 텍스처의 신규 복사 stream을 엽니다."""

        asset = self._current_texture_asset()
        stat = asset.source_path.stat()
        if int(stat.st_size) != asset.source_size or int(stat.st_mtime_ns) != asset.source_mtime_ns:
            raise RuntimeError(f"OBJ 텍스처 원본이 내보내기 중 변경되었습니다: {asset.source_path}")
        target = self.candidate_root / asset.output_relative_path
        validate_windows_output_path(target, "OBJ 텍스처 출력 경로")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.texture_source_stream = asset.source_path.open("rb")
        self.texture_target_stream = target.open("xb")
        self.texture_source_digest = hashlib.sha256()

    def _copy_texture_chunk(self) -> None:
        """현재 텍스처 원본 바이트를 후보 파일에 1MiB씩 그대로 복사합니다."""

        if self.texture_source_stream is None:
            self._start_texture_copy()
        chunk = self.texture_source_stream.read(_OBJ_EXPORT_HASH_CHUNK_SIZE)
        if chunk:
            self.texture_target_stream.write(chunk)
            self.texture_source_digest.update(chunk)
            return
        self.texture_source_stream.close()
        self.texture_source_stream = None
        self.texture_target_stream.flush()
        try:
            os.fsync(self.texture_target_stream.fileno())
        except OSError:
            pass
        self.texture_target_stream.close()
        self.texture_target_stream = None
        asset = self._current_texture_asset()
        stat = asset.source_path.stat()
        target = self.candidate_root / asset.output_relative_path
        if (
            int(stat.st_size) != asset.source_size
            or int(stat.st_mtime_ns) != asset.source_mtime_ns
            or target.stat().st_size != asset.source_size
        ):
            raise RuntimeError(f"OBJ 텍스처 원본 또는 복사본이 내보내기 중 변경되었습니다: {asset.source_path}")
        self.texture_validation_stream = target.open("rb")
        self.texture_validation_digest = hashlib.sha256()
        self.stage = "VALIDATE_TEXTURE_COPY"

    def _validate_texture_copy_chunk(self) -> None:
        """복사된 텍스처를 다시 읽어 원본과 SHA-256이 같은지 chunk 단위로 검증합니다."""

        chunk = self.texture_validation_stream.read(_OBJ_EXPORT_HASH_CHUNK_SIZE)
        if chunk:
            self.texture_validation_digest.update(chunk)
            return
        self.texture_validation_stream.close()
        self.texture_validation_stream = None
        source_hash = self.texture_source_digest.hexdigest()
        output_hash = self.texture_validation_digest.hexdigest()
        if source_hash != output_hash:
            raise RuntimeError("OBJ 텍스처 복사본이 원본 바이트와 다릅니다.")
        asset = self._current_texture_asset()
        relative_path = asset.output_relative_path.as_posix()
        self.copied_texture_hashes[relative_path] = output_hash
        self.shared_texture_hashes[relative_path.casefold()] = output_hash
        self.texture_source_digest = None
        self.texture_validation_digest = None
        self.texture_asset_index += 1
        self._advance_past_shared_textures()

    def _start_validation_reader(self) -> None:
        """생성 MTL과 복사 텍스처가 준비된 현재 OBJ의 증분 reader를 엽니다."""

        snapshot = self._current_snapshot()
        self.reader = ObjReader(
            self.candidate_root / snapshot.output_filename,
            preserve_polygons=True,
        )
        self.stage = OBJ_EXPORT_STAGE_VALIDATING

    def _read_validation_obj(self, remaining_seconds: float) -> None:
        """기존 OBJ parser를 시간 예산 안에서 구동해 후보 파일을 다시 읽습니다."""

        self.reader.step(max(0.0, remaining_seconds))  # type: ignore[union-attr]
        if self.reader.error is not None:  # type: ignore[union-attr]
            raise self.reader.error  # type: ignore[union-attr]
        if not self.reader.done:  # type: ignore[union-attr]
            return
        self.parsed = self.reader.model  # type: ignore[union-attr]
        self.reader = None
        snapshot = self._current_snapshot()
        if (
            self.parsed.vertex_count != snapshot.vertex_count
            or self.parsed.texture_coordinate_count != snapshot.texture_coordinate_count
            or self.parsed.polygon_count != snapshot.face_count
        ):
            raise RuntimeError("OBJ 출력 정점·UV·Polygon 수가 snapshot과 다릅니다.")
        validate_obj_material_links(self.parsed, snapshot, self.candidate_root)
        self.validation_vertex_offset = 0
        self.validation_texture_coordinate_offset = 0
        self.validation_face_offset = 0
        self.stage = "VALIDATE_VERTICES"

    def _validate_vertex_chunk(self) -> None:
        """왕복 파싱 좌표를 Project World 기대 좌표와 chunk 단위로 비교합니다."""

        snapshot = self._current_snapshot()
        stop = min(
            snapshot.vertex_count,
            self.validation_vertex_offset + _OBJ_EXPORT_VALIDATION_CHUNK_SIZE,
        )
        expected = transformed_vertices(snapshot, self.validation_vertex_offset, stop)
        actual = self.parsed.vertices_float64[self.validation_vertex_offset:stop]
        if not np.allclose(actual, expected, rtol=0.0, atol=1.0e-12):
            raise RuntimeError("OBJ 출력 좌표가 Project World 변환 결과와 다릅니다.")
        self.validation_vertex_offset = stop
        if stop >= snapshot.vertex_count:
            self.stage = (
                "VALIDATE_TEXCOORDS"
                if snapshot.texture_coordinate_count > 0
                else "VALIDATE_FACES"
            )

    def _validate_texture_coordinate_chunk(self) -> None:
        """왕복 파싱 UV 좌표와 성분 수를 원본 snapshot과 chunk 단위로 비교합니다."""

        snapshot = self._current_snapshot()
        stop = min(
            snapshot.texture_coordinate_count,
            self.validation_texture_coordinate_offset + _OBJ_EXPORT_VALIDATION_CHUNK_SIZE,
        )
        begin = self.validation_texture_coordinate_offset
        actual = self.parsed.texcoords_float64[begin:stop]
        expected = snapshot.texcoords_float64[begin:stop]
        if not np.allclose(actual, expected, rtol=0.0, atol=1.0e-12):
            raise RuntimeError("OBJ 출력 UV 좌표가 snapshot과 다릅니다.")
        if not np.array_equal(
            self.parsed.texcoord_component_counts_uint8[begin:stop],
            snapshot.texcoord_component_counts_uint8[begin:stop],
        ):
            raise RuntimeError("OBJ 출력 UV 성분 수가 snapshot과 다릅니다.")
        self.validation_texture_coordinate_offset = stop
        if stop >= snapshot.texture_coordinate_count:
            self.stage = "VALIDATE_FACES"

    def _validate_face_chunk(self) -> None:
        """왕복 파싱 Polygon Corner·UV·재질을 chunk 단위로 비교합니다."""

        snapshot = self._current_snapshot()
        stop = min(
            snapshot.face_count,
            self.validation_face_offset + _OBJ_EXPORT_VALIDATION_CHUNK_SIZE,
        )
        expected = transformed_polygon_corners(
            snapshot,
            self.validation_face_offset,
            stop,
        )
        expected_texcoords = transformed_polygon_texcoord_indices(
            snapshot,
            self.validation_face_offset,
            stop,
        )
        for relative_index, (expected_vertices, expected_uv) in enumerate(
            zip(expected, expected_texcoords, strict=True)
        ):
            polygon_index = self.validation_face_offset + relative_index
            loop_start = int(self.parsed.polygon_loop_starts_uint32[polygon_index])
            loop_total = int(self.parsed.polygon_loop_totals_uint32[polygon_index])
            actual_vertices = self.parsed.loop_vertex_indices_uint32[
                loop_start : loop_start + loop_total
            ]
            actual_uv = self.parsed.loop_texcoord_indices_int32[
                loop_start : loop_start + loop_total
            ]
            if loop_total != expected_vertices.shape[0] or not np.array_equal(
                actual_vertices,
                expected_vertices - 1,
            ):
                raise RuntimeError("OBJ 출력 Polygon Corner 연결 또는 winding이 snapshot과 다릅니다.")
            if loop_total != expected_uv.shape[0] or not np.array_equal(actual_uv, expected_uv):
                raise RuntimeError("OBJ 출력 Polygon UV 연결 또는 winding이 snapshot과 다릅니다.")
        if not np.array_equal(
            self.parsed.polygon_material_indices_int32[self.validation_face_offset:stop],
            snapshot.polygon_material_indices_int32[self.validation_face_offset:stop],
        ):
            raise RuntimeError("OBJ 출력 Polygon 재질 연결이 snapshot과 다릅니다.")
        self.validation_face_offset = stop
        if stop >= snapshot.face_count:
            path = self.candidate_root / snapshot.output_filename
            self.hash_stream = path.open("rb")
            self.hash_digest = hashlib.sha256()
            self.stage = "HASH_OUTPUT"

    def _hash_output_chunk(self) -> None:
        """후보 OBJ SHA-256을 한 번에 1MiB씩 계산합니다."""

        chunk = self.hash_stream.read(_OBJ_EXPORT_HASH_CHUNK_SIZE)
        if chunk:
            self.hash_digest.update(chunk)
            return
        self.hash_stream.close()
        self.hash_stream = None
        snapshot = self._current_snapshot()
        self.validations[snapshot.model_id] = ObjRoundTripValidation(
            vertex_count=self.parsed.vertex_count,
            face_count=self.parsed.polygon_count,
            texture_coordinate_count=self.parsed.texture_coordinate_count,
            textured_face_count=self.parsed.textured_polygon_count,
            uv_coverage=(self.parsed.textured_polygon_count / self.parsed.polygon_count),
            bounds_min=tuple(float(value) for value in self.parsed.bounds_min),
            bounds_max=tuple(float(value) for value in self.parsed.bounds_max),
            output_sha256=self.hash_digest.hexdigest(),
            material_count=self.parsed.material_count,
            diffuse_texture_count=self.parsed.diffuse_texture_count,
            copied_texture_sha256=tuple(sorted(self.copied_texture_hashes.items())),
        )
        self.parsed = None
        self.hash_digest = None
        self.model_index += 1
        self.stage = "START_MODEL" if self.model_index < len(self.snapshots) else OBJ_EXPORT_STAGE_REPORT

    def _write_report(self) -> None:
        """모든 검증 결과를 후보 폴더의 결정적 JSON 보고서로 기록합니다."""

        report = build_obj_export_report(
            job_id=self.job_id,
            snapshots=self.snapshots,
            validations=self.validations,
        )
        write_json_deterministic(self.report_path, report)
        self.status = "READY"
        self.stage = OBJ_EXPORT_STAGE_FINALIZING
        self._update_progress(self.stage)

    def step(self, time_budget_seconds: float = _OBJ_EXPORT_TIME_BUDGET_SECONDS) -> bool:
        """한 TIMER tick에서 제한 시간만 진행하고 terminal 또는 finalize 준비 여부를 반환합니다."""

        if self.status != "RUNNING":
            return True
        try:
            budget = float(time_budget_seconds)
            if not np.isfinite(budget) or budget < 0.0:
                raise ValueError("OBJ Export 시간 예산이 올바르지 않습니다.")
            deadline = perf_counter() + budget
            first_step = True
            while first_step or perf_counter() < deadline:
                first_step = False
                if self.cancel_requested:
                    self._cancel()
                    return True
                if self.stage == OBJ_EXPORT_STAGE_SNAPSHOT_INPUTS:
                    self._prepare_candidate()
                elif self.stage == "START_MODEL":
                    self._start_model()
                elif self.stage == OBJ_EXPORT_STAGE_WRITING_VERTICES:
                    self._write_vertex_chunk()
                elif self.stage == OBJ_EXPORT_STAGE_WRITING_TEXCOORDS:
                    self._write_texture_coordinate_chunk()
                elif self.stage == OBJ_EXPORT_STAGE_WRITING_FACES:
                    self._write_face_chunk()
                elif self.stage == OBJ_EXPORT_STAGE_WRITING_MATERIALS:
                    self._write_material_library()
                elif self.stage == OBJ_EXPORT_STAGE_COPYING_TEXTURES:
                    self._copy_texture_chunk()
                elif self.stage == "VALIDATE_TEXTURE_COPY":
                    self._validate_texture_copy_chunk()
                elif self.stage == OBJ_EXPORT_STAGE_VALIDATING:
                    self._read_validation_obj(deadline - perf_counter())
                elif self.stage == "VALIDATE_VERTICES":
                    self._validate_vertex_chunk()
                elif self.stage == "VALIDATE_TEXCOORDS":
                    self._validate_texture_coordinate_chunk()
                elif self.stage == "VALIDATE_FACES":
                    self._validate_face_chunk()
                elif self.stage == "HASH_OUTPUT":
                    self._hash_output_chunk()
                elif self.stage == OBJ_EXPORT_STAGE_REPORT:
                    self._write_report()
                    return True
                else:
                    raise RuntimeError(f"알 수 없는 OBJ Export 단계입니다: {self.stage}")
            return False
        except Exception as error:
            self._fail(error)
            return True

    def mark_stale(self, message: str) -> None:
        """Runtime 변경을 확인한 후보를 폐기하고 STALE terminal로 전환합니다."""

        self.cleanup_candidate()
        self.status = "STALE"
        self.error_message = str(message)

    def finalize(self) -> ObjExportResult:
        """검증된 후보를 최종 폴더로 원자 승격하고 기존 출력은 실패 시 복원합니다."""

        if not self.is_ready_to_finalize:
            raise RuntimeError("완전히 검증된 OBJ 후보만 최종 폴더로 확정할 수 있습니다.")
        if self.output_root.exists() and not self.overwrite_existing_output:
            raise RuntimeError("기존 aligned_obj 덮어쓰기가 승인되지 않았습니다.")
        if self.output_root.exists() and (
            self.output_root.is_symlink() or not self.output_root.is_dir()
        ):
            raise RuntimeError("기존 aligned_obj가 일반 폴더가 아닙니다.")
        backup_created = False
        try:
            if self.output_root.exists():
                self.output_root.replace(self.backup_root)
                backup_created = True
            self.candidate_root.replace(self.output_root)
        except Exception:
            if backup_created and self.backup_root.exists() and not self.output_root.exists():
                self.backup_root.replace(self.output_root)
            raise
        if backup_created:
            try:
                _remove_job_directory(
                    self.backup_root,
                    self.parent,
                    OBJ_EXPORT_PREVIOUS_OUTPUT_PREFIX,
                    self.job_id,
                )
            except Exception as cleanup_error:
                self.warning_message = (
                    "OBJ 내보내기는 완료됐지만 이전 출력 백업을 정리하지 못했습니다. "
                    f"후속 정리 경로: {self.backup_root}. 원인: {cleanup_error}"
                )
                _LOGGER.warning(
                    "event=obj_export_backup_cleanup_pending job_id=%s backup=%s reason=%s",
                    self.job_id,
                    self.backup_root,
                    cleanup_error,
                )
        self.status = "COMPLETED"
        self.stage = OBJ_EXPORT_STAGE_COMPLETED
        self._update_progress(self.stage)
        return ObjExportResult(
            output_root=self.output_root,
            report_path=self.output_root / "obj_export_report.json",
            model_count=len(self.snapshots),
            vertex_count=self.total_vertex_count,
            texture_coordinate_count=self.total_texture_coordinate_count,
            face_count=self.total_face_count,
            cleanup_warning=self.warning_message,
        )


def obj_export_phase() -> str:
    """현재 OBJ Export Runtime phase를 문자열로 반환합니다."""

    return str(getattr(RUNTIME.obj_export, "phase", OBJ_EXPORT_PHASE_IDLE))


def is_obj_export_running() -> bool:
    """OBJ Export가 입력을 잠가야 하는 실행 상태인지 반환합니다."""

    return bool(getattr(RUNTIME.obj_export, "is_running", False))


def is_obj_export_input_locked() -> bool:
    """다른 오퍼레이터가 OBJ Export 중 입력 변경을 막을지 반환합니다."""

    return is_obj_export_running()


def obj_export_start_block_reason() -> str | None:
    """OBJ Export 시작 전에 가장 먼저 만족하지 못한 조건을 설명합니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(RUNTIME)
    if axis_reason is not None:
        return axis_reason

    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        return restore_reason
    if is_obj_export_running():
        return "OBJ 내보내기 작업이 이미 실행 중입니다. 취소하거나 완료를 기다리십시오."
    if bool(getattr(RUNTIME.merge_export, "is_running", False)):
        return "COLMAP Export가 끝난 뒤 OBJ Export를 시작하십시오."
    if RUNTIME.import_in_progress:
        return "가져오기가 끝난 뒤 OBJ Export를 시작하십시오."
    if RUNTIME.workspace_setup_task is not None:
        return "작업공간 구성이 끝난 뒤 OBJ Export를 시작하십시오."
    from .layer_transform import layer_transform_active_block_reason

    if layer_transform_active_block_reason() is not None:
        return "데이터 레이어 변환을 확정하거나 취소한 뒤 OBJ Export를 시작하십시오."
    if RUNTIME.modal_active or RUNTIME.marker_selection.has_draft:
        return "열린 마커 입력을 확정하거나 취소한 뒤 OBJ Export를 시작하십시오."
    from .marker_alignment import is_icp_input_locked, is_marker_preview_active

    if is_marker_preview_active() or is_icp_input_locked():
        return "마커 또는 ICP 미리보기를 정리한 뒤 OBJ Export를 시작하십시오."
    if not _ordered_obj_records():
        return "A 그룹에 OBJ로 내보낼 폴리곤 레이어가 없습니다."
    return None


def obj_export_status_entries() -> tuple[str, ...]:
    """패널에서 사용할 OBJ Export 진행·오류 문구를 구성합니다."""

    state = RUNTIME.obj_export
    progress = getattr(state, "progress", None)
    entries = [f"상태 · {obj_export_phase()}"]
    stage = str(getattr(progress, "stage", "") or "").strip()
    if stage:
        entries.append(f"단계 · {stage}")
    model_name = str(getattr(progress, "model_name", "") or "").strip()
    if model_name:
        entries.append(f"레이어 · {model_name}")
    if progress is not None:
        entries.append(
            f"정점 · {int(progress.written_vertex_count):,} / {int(progress.total_vertex_count):,}"
        )
        entries.append(
            "UV · "
            f"{int(progress.written_texture_coordinate_count):,} / "
            f"{int(progress.total_texture_coordinate_count):,}"
        )
        entries.append(
            f"Polygon · {int(progress.written_face_count):,} / {int(progress.total_face_count):,}"
        )
    if state.last_error:
        entries.append(f"오류 · {state.last_error}")
    return tuple(entries)


def _clear_obj_export_timer(*, unregister: bool) -> None:
    """OBJ Export app timer 등록과 Runtime 참조를 정리합니다."""

    callback = RUNTIME.obj_export_timer
    if unregister and callable(callback):
        try:
            if bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    RUNTIME.clear_obj_export_timer()


def _set_obj_export_terminal_from_job(job: ObjExportJob) -> None:
    """job terminal 상태를 Runtime phase와 사용자 오류에 반영합니다."""

    state = RUNTIME.obj_export
    state.job = None
    state.progress = job.progress
    if job.status == "CANCELLED":
        state.phase = OBJ_EXPORT_PHASE_CANCELLED
        state.last_error = "OBJ 내보내기를 취소했습니다. 기존 출력은 변경하지 않았습니다."
    elif job.status == "STALE":
        state.phase = OBJ_EXPORT_PHASE_STALE
        state.last_error = job.error_message
    else:
        state.phase = OBJ_EXPORT_PHASE_FAILED
        state.last_error = job.error_message or "OBJ 내보내기가 실패했습니다."


def _poll_obj_export_timer() -> float | None:
    """한 tick의 OBJ 기록을 진행하고 검증·stale·최종 승격을 처리합니다."""

    state = RUNTIME.obj_export
    job = state.job
    if job is None:
        _clear_obj_export_timer(unregister=False)
        return None
    finished_step = job.step(_OBJ_EXPORT_TIME_BUDGET_SECONDS)
    state.progress = job.progress
    if not finished_step:
        request_redraw()
        return _OBJ_EXPORT_TIMER_INTERVAL_SECONDS
    if job.is_ready_to_finalize:
        state.phase = OBJ_EXPORT_PHASE_FINALIZING
        try:
            current = build_obj_export_snapshots()
            if obj_export_snapshot_fingerprint(current) != job.input_fingerprint:
                job.mark_stale("OBJ Export 실행 중 A 그룹 또는 확정 변환이 변경되어 결과를 폐기했습니다.")
                _set_obj_export_terminal_from_job(job)
            else:
                result = job.finalize()
                state.phase = OBJ_EXPORT_PHASE_COMPLETED
                state.job = None
                state.result = result
                state.progress = job.progress
                state.last_error = ""
                state.last_success_output_root = str(result.output_root)
                state.last_success_report_path = str(result.report_path)
                state.last_vertex_count = result.vertex_count
                state.last_face_count = result.face_count
                status_text = f"OBJ 내보내기를 완료했습니다: {result.output_root}"
                if result.cleanup_warning:
                    status_text = f"{status_text} · {result.cleanup_warning}"
                set_workspace_status_text(session_align_workspace(), status_text)
        except Exception as error:
            try:
                job.cleanup_candidate()
            except Exception:
                pass
            job.status = "FAILED"
            job.error_message = str(error)
            _set_obj_export_terminal_from_job(job)
    else:
        _set_obj_export_terminal_from_job(job)
    _clear_obj_export_timer(unregister=False)
    request_redraw()
    return None


def _register_obj_export_timer() -> None:
    """OBJ Export timer callback을 중복 없이 등록합니다."""

    callback = RUNTIME.obj_export_timer
    if callable(callback):
        try:
            if bpy.app.timers.is_registered(callback):
                return
        except (ReferenceError, RuntimeError, ValueError):
            pass
    RUNTIME.obj_export_timer = _poll_obj_export_timer
    bpy.app.timers.register(_poll_obj_export_timer, first_interval=0.0)


def start_obj_export_job(
    output_root: Path,
    *,
    overwrite_existing_output: bool,
    runtime: Any = RUNTIME,
) -> ObjExportJob:
    """현재 A 그룹 snapshot으로 새 증분 OBJ Export job을 시작합니다."""

    snapshots = build_obj_export_snapshots(runtime)
    _validate_output_against_sources(Path(output_root), snapshots)
    job = ObjExportJob(
        Path(output_root),
        snapshots,
        overwrite_existing_output=overwrite_existing_output,
    )
    state = runtime.obj_export
    state.clear()
    state.phase = OBJ_EXPORT_PHASE_RUNNING
    state.job = job
    state.snapshot = snapshots
    state.progress = job.progress
    if runtime is RUNTIME:
        _register_obj_export_timer()
    return job


def invalidate_obj_export_for_lifecycle(reason: str) -> None:
    """파일 교체·세션 종료·애드온 해제에서 OBJ job과 후보를 정리합니다."""

    state = RUNTIME.obj_export
    job = state.job
    _clear_obj_export_timer(unregister=True)
    if job is not None:
        try:
            job.cleanup_candidate()
        except Exception:
            pass
    state.clear(error_message=reason if job is not None else "")


def _obj_export_browser_start_directory(context: Any) -> Path:
    """마지막 가져오기 경로 또는 현재 작업 폴더를 파일 탐색기 시작 위치로 반환합니다."""

    settings = context.scene.colmap_align_settings
    raw = str(getattr(settings, "import_last_directory", "") or "").strip()
    if raw:
        candidate = Path(bpy.path.abspath(raw)).expanduser().resolve(strict=False)
        if candidate.is_file():
            candidate = candidate.parent
        if candidate.is_dir():
            return candidate
    return Path.cwd().resolve()


def _report_status(operator: Any, level: set[str], message: str) -> None:
    """오퍼레이터 report와 COLMAP Align 상태줄에 같은 문구를 표시합니다."""

    operator.report(level, message)
    set_workspace_status_text(session_align_workspace(), message)


class COLMAPALIGN_OT_obj_export(bpy.types.Operator):
    """부모 폴더를 선택받아 A 그룹 폴리곤 OBJ Export를 시작합니다."""

    bl_idname = "colmap_align.obj_export"
    bl_label = "OBJ Export"
    bl_description = "A 그룹의 폴리곤 레이어를 Project World 좌표의 OBJ로 내보냅니다"
    bl_options = {"INTERNAL"}

    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    overwrite_confirmed: BoolProperty(default=False, options={"HIDDEN"})
    pending_output_root: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """활성 세션과 OBJ Export 시작 조건이 충족될 때만 버튼을 허용합니다."""

        return bool(
            is_session_active_for_window(context.window)
            and obj_export_start_block_reason() is None
        )

    def invoke(self, context, _event):
        """마지막 가져오기 위치에서 부모 폴더 선택기를 엽니다."""

        reason = obj_export_start_block_reason()
        if reason is not None:
            _report_status(self, {"WARNING"}, reason)
            return {"CANCELLED"}
        if not self.properties.is_property_set("directory"):
            self.directory = str(_obj_export_browser_start_directory(context))
        self.overwrite_confirmed = False
        self.pending_output_root = ""
        if bpy.app.background:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        """덮어쓰기 확인에서 기존 출력 보존·복원 계약을 안내합니다."""

        if not self.overwrite_confirmed:
            self.layout.label(text="A 그룹 폴리곤을 레이어별 OBJ로 내보냅니다.")
            self.layout.label(text="좌표 · Project World (Alignment × Display Axis)")
            return
        self.layout.label(text=f"기존 폴더: {self.pending_output_root}", icon="ERROR")
        self.layout.label(text="새 OBJ가 검증된 뒤 기존 aligned_obj를 교체합니다.")
        self.layout.label(text="실패하거나 취소하면 기존 폴더를 유지합니다.")

    def execute(self, context):
        """고정 aligned_obj 경로를 확인하고 증분 job을 시작합니다."""

        reason = obj_export_start_block_reason()
        if reason is not None:
            _report_status(self, {"WARNING"}, reason)
            return {"CANCELLED"}
        try:
            if self.overwrite_confirmed:
                output_root = Path(self.pending_output_root).expanduser().resolve(strict=False)
                overwrite = output_root.exists()
            else:
                output_root = normalized_obj_output_root(self.directory)
                if output_root.exists():
                    self.pending_output_root = str(output_root)
                    self.overwrite_confirmed = True
                    return context.window_manager.invoke_props_dialog(
                        self,
                        width=560,
                        title="기존 aligned_obj를 덮어쓰시겠습니까?",
                        confirm_text="덮어쓰기",
                    )
                overwrite = False
            start_obj_export_job(
                output_root,
                overwrite_existing_output=overwrite,
            )
        except Exception as error:
            RUNTIME.obj_export.phase = OBJ_EXPORT_PHASE_FAILED
            RUNTIME.obj_export.last_error = str(error)
            _report_status(self, {"ERROR"}, str(error))
            request_redraw()
            return {"CANCELLED"}
        _report_status(self, {"INFO"}, f"OBJ 내보내기를 시작했습니다: {output_root}")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_cancel_obj_export(bpy.types.Operator):
    """현재 증분 OBJ Export에 취소를 요청합니다."""

    bl_idname = "colmap_align.cancel_obj_export"
    bl_label = "작업 취소"
    bl_description = "OBJ 후보 기록을 취소하고 기존 출력은 유지합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 세션의 실행 중 OBJ Export에서만 허용합니다."""

        return bool(is_session_active_for_window(context.window) and is_obj_export_running())

    def execute(self, _context):
        """job의 다음 chunk 경계 취소 플래그를 설정합니다."""

        job = RUNTIME.obj_export.job
        if job is None:
            return {"CANCELLED"}
        job.request_cancel()
        RUNTIME.obj_export.phase = OBJ_EXPORT_PHASE_CANCEL_PENDING
        _report_status(self, {"INFO"}, "OBJ 내보내기 취소를 요청했습니다.")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_open_obj_export_output(bpy.types.Operator):
    """마지막 성공 OBJ 출력 폴더를 운영체제 파일 탐색기로 엽니다."""

    bl_idname = "colmap_align.open_obj_export_output"
    bl_label = "폴더 열기"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """마지막 성공 폴더가 존재할 때만 허용합니다."""

        path = str(RUNTIME.obj_export.last_success_output_root).strip()
        return bool(path and Path(path).is_dir())

    def execute(self, _context):
        """검증된 마지막 출력 경로를 Blender 경로 열기에 전달합니다."""

        result = bpy.ops.wm.path_open(filepath=RUNTIME.obj_export.last_success_output_root)
        return result if "FINISHED" in result else {"CANCELLED"}


class COLMAPALIGN_OT_open_obj_export_report(bpy.types.Operator):
    """마지막 성공 OBJ 보고서를 운영체제 기본 앱으로 엽니다."""

    bl_idname = "colmap_align.open_obj_export_report"
    bl_label = "보고서 보기"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """마지막 성공 보고서가 존재할 때만 허용합니다."""

        path = str(RUNTIME.obj_export.last_success_report_path).strip()
        return bool(path and Path(path).is_file())

    def execute(self, _context):
        """검증된 마지막 보고서 경로를 Blender 경로 열기에 전달합니다."""

        result = bpy.ops.wm.path_open(filepath=RUNTIME.obj_export.last_success_report_path)
        return result if "FINISHED" in result else {"CANCELLED"}


CLASSES = (
    COLMAPALIGN_OT_obj_export,
    COLMAPALIGN_OT_cancel_obj_export,
    COLMAPALIGN_OT_open_obj_export_output,
    COLMAPALIGN_OT_open_obj_export_report,
)


__all__ = (
    "CLASSES",
    "ObjExportJob",
    "ObjExportProgress",
    "ObjExportResult",
    "build_obj_export_snapshots",
    "invalidate_obj_export_for_lifecycle",
    "is_obj_export_input_locked",
    "is_obj_export_running",
    "normalized_obj_output_root",
    "obj_export_phase",
    "obj_export_snapshot_fingerprint",
    "obj_export_start_block_reason",
    "obj_export_status_entries",
    "start_obj_export_job",
)
