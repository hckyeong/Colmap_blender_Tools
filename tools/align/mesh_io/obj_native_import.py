"""공통 OBJ 가져오기에 Align 내장 자산의 소유권과 폐기 정책을 연결합니다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bpy

try:
    from ....shared.blender_runtime.obj_import import (
        NativeObjStaging as SharedObjStaging,
        ObjNativeImportError,
        _contains_id,
        import_obj_to_staging as _import_obj_to_staging,
    )
except ImportError:  # 소스 트리의 최상위 도구 패키지 실행도 지원합니다.
    from shared.blender_runtime.obj_import import (
        NativeObjStaging as SharedObjStaging,
        ObjNativeImportError,
        _contains_id,
        import_obj_to_staging as _import_obj_to_staging,
    )
from .embedded_obj_store import EMBEDDED_OBJ_RETIRED_OWNER, EMBEDDED_OBJ_SUPPORT_OWNER


class NativeObjStaging(SharedObjStaging):
    """공통 임시 자원에 Align 내장 자산 소유권 규칙만 추가합니다."""

    def commit(self) -> Any:
        """Align 자산의 부속 소유 태그를 기록한 뒤 원본 메시를 인계합니다."""

        if self._discarded:
            raise ObjNativeImportError("이미 폐기한 OBJ Staging을 확정할 수 없습니다.")
        self._remove_unused_support_data()
        self._tag_owned_support_data()
        self.committed = True
        return self.mesh

    def _remove_mesh(self, mesh: Any) -> None:
        """Align 폐기 검증에서 사용하는 메시 제거 경계를 호출합니다."""

        _remove_mesh_data_block(mesh)

    def _handle_mesh_removal_failure(self, mesh: Any, error: Exception) -> None:
        """제거하지 못한 내장 메시를 Align 후속 정리 대상으로 격리합니다."""

        try:
            mesh["owner"] = EMBEDDED_OBJ_RETIRED_OWNER
            mesh.use_fake_user = False
        except (ReferenceError, RuntimeError, TypeError, ValueError) as failure:
            raise ObjNativeImportError("OBJ Staging Mesh를 제거하거나 격리하지 못했습니다.") from failure

    def _discard_support_data(self) -> None:
        """폐기한 Import의 부속 ID를 지우고 제거 실패 항목에는 후속 정리 태그를 남깁니다."""

        for collection, items in (
            (bpy.data.materials, self.materials),
            (bpy.data.images, self.images),
        ):
            for item in items:
                try:
                    if not _contains_id(collection, item):
                        continue
                    item["owner"] = EMBEDDED_OBJ_SUPPORT_OWNER
                    item["asset_uuid"] = "staging"
                    if item.users == 0:
                        collection.remove(item)
                except (ReferenceError, RuntimeError, TypeError, ValueError):
                    continue

    def _tag_owned_support_data(self) -> None:
        """확정 Mesh가 참조하는 이번 Import 생성 Material·Image에 소유 태그를 기록합니다."""

        asset_uuid = str(self.mesh.get("asset_uuid", "")).strip() if self.mesh is not None else ""
        if not asset_uuid:
            raise ObjNativeImportError("OBJ support Data Block에 연결할 asset UUID가 없습니다.")
        for material in self.materials:
            try:
                exists = _contains_id(bpy.data.materials, material)
            except ReferenceError:
                exists = False
            if not exists:
                continue
            material["owner"] = EMBEDDED_OBJ_SUPPORT_OWNER
            material["asset_uuid"] = asset_uuid
            material["colmap_align_source_name"] = str(material.name)
        for image in self.images:
            try:
                exists = _contains_id(bpy.data.images, image)
            except ReferenceError:
                exists = False
            if not exists:
                continue
            image["owner"] = EMBEDDED_OBJ_SUPPORT_OWNER
            image["asset_uuid"] = asset_uuid


def _remove_mesh_data_block(mesh: Any) -> None:
    """Align 폐기 검증이 주입할 수 있는 메시 제거 경계를 제공합니다."""

    bpy.data.meshes.remove(mesh)


def import_obj_to_staging(path: str | Path) -> NativeObjStaging:
    """공통 가져오기 결과에 Align 자산의 소유권 정책을 연결합니다."""

    staging = _import_obj_to_staging(path)
    return NativeObjStaging(mesh=staging.mesh, materials=staging.materials, images=staging.images)


__all__ = ("NativeObjStaging", "ObjNativeImportError", "import_obj_to_staging")
