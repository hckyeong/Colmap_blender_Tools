from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import bmesh
import bpy


PACKAGE_PARENT = Path(__file__).resolve().parents[3]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tools import modifier as blender_colmap
from tools.modifier.blender.ownership import (
    DATASET_UUID_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from tools.modifier.blender.operators_export import (
    _execution_inputs,
    _prepare_export,
)
from tools.modifier.blender.runtime_state import ensure_runtime
from tools.modifier.blender.modifier_runtime_domain import domain_for_scene
from tools.modifier.core.binary_io import read_model_binary
from tools.modifier.core.text_io import read_model_text
from tests.modifier.fixture_utils import make_legacy_text_multi_point


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _managed_objects(kind: str, dataset_uuid: str):
    return [
        obj
        for obj in bpy.data.objects
        if obj.get(OWNER_KEY) == OWNER_TOKEN
        and obj.get(RECORD_KIND_KEY) == kind
        and obj.get(DATASET_UUID_KEY) == dataset_uuid
        and obj.users_collection
    ]


def _remove_first_point(settings) -> None:
    assert_true(
        bpy.ops.colmap.toggle_edit_point_mode() == {"FINISHED"},
        settings.point_selection_status,
    )
    point_object = _managed_objects("POINT_MESH", settings.dataset_uuid)[0]
    bm = bmesh.from_edit_mesh(point_object.data)
    bm.verts.ensure_lookup_table()
    bm.select_history.clear()
    for vertex in bm.verts:
        vertex.select = False
    bm.verts[0].select = True
    bm.select_history.add(bm.verts[0])
    bmesh.update_edit_mesh(point_object.data)
    assert_true(
        bpy.ops.colmap.remove() == {"FINISHED"},
        settings.point_selection_status,
    )


blender_colmap.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        dataset_root = Path(directory)
        model_directory, _images_directory = make_legacy_text_multi_point(
            dataset_root
        )
        settings = bpy.context.scene.colmap_import
        settings.dataset_root = str(dataset_root)
        settings.input_format = "TEXT"

        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        domain_after_import = domain_for_scene(bpy.context.scene, required=True)
        assert_true(
            domain_after_import.dataset_state is ensure_runtime(bpy.context.scene)
            and domain_after_import.journal.revision == 0,
            "Import가 독립 Modifier Runtime Domain과 빈 Journal을 설치하지 않았습니다.",
        )
        assert_true(
            settings.export_backup_enabled,
            "Backup 기본값은 활성화여야 합니다.",
        )
        settings.export_backup_enabled = False

        _remove_first_point(settings)
        domain_before_export = domain_for_scene(bpy.context.scene, required=True)
        assert_true(
            domain_before_export is domain_after_import
            and domain_before_export.journal.revision == 1
            and domain_before_export.journal.poll_undo(),
            "Point 편집이 독립 Modifier Journal에 기록되지 않았습니다.",
        )
        state_before_export = ensure_runtime(bpy.context.scene)
        assert_true(
            state_before_export.edit_revision == 1
            and len(state_before_export.current_reconstruction.points3d) == 2,
            "첫 번째 Point 편집 결과가 Export 입력에 반영되지 않았습니다.",
        )
        with patch(
            "tools.modifier.blender.runtime_state.source_fingerprint",
            side_effect=AssertionError("Export 준비 단계에서 source 전체 hash를 호출했습니다."),
        ):
            prepared_state, prepared_plan = _prepare_export(bpy.context)
        assert_true(
            prepared_plan.output_format == "text"
            and tuple(
                path.suffix for path in prepared_plan.target_paths
            )
            == (".txt", ".txt", ".txt"),
            "Same as Source Export 형식이 원본 TXT를 따르지 않았습니다.",
        )
        reused_state, reused_plan = _execution_inputs(
            bpy.context,
            prepared_plan,
        )
        assert_true(
            prepared_state is reused_state
            and prepared_state is state_before_export
            and reused_plan is prepared_plan,
            "확인창에서 준비한 Export 계획과 Runtime token이 실행 시 재사용되지 않았습니다.",
        )
        stale_plan = replace(
            prepared_plan,
            runtime_token=replace(
                prepared_plan.runtime_token,
                history_hash="stale",
            ),
        )
        try:
            _execution_inputs(bpy.context, stale_plan)
        except ValueError:
            pass
        else:
            raise AssertionError("확인창 이후 변경된 Runtime token을 거부하지 않았습니다.")
        indexes_before_export = state_before_export.indexes
        reconstruction_before_export = state_before_export.current_reconstruction
        point_object = _managed_objects(
            "POINT_MESH",
            settings.dataset_uuid,
        )[0]
        point_identity = (
            point_object.as_pointer(),
            point_object.data.as_pointer(),
        )
        camera_identity = tuple(
            sorted(
                obj.as_pointer()
                for obj in _managed_objects("CAMERA", settings.dataset_uuid)
            )
        )

        assert_true(
            bpy.ops.colmap.export() == {"FINISHED"},
            settings.export_status,
        )
        domain_after_export = domain_for_scene(bpy.context.scene, required=True)
        assert_true(
            domain_after_export is domain_before_export
            and domain_after_export.domain_uuid == settings.domain_uuid
            and domain_after_export.journal.revision == 0
            and not domain_after_export.journal.poll_undo()
            and not domain_after_export.journal.poll_redo(),
            "Export가 같은 Modifier Domain을 새 기준선과 빈 Journal로 전환하지 않았습니다.",
        )
        exported_model = read_model_text(model_directory, modern=False)
        assert_true(
            len(exported_model.points3d) == 2,
            "Backup 비활성 Export가 편집된 Point3D를 원본에 기록하지 못했습니다.",
        )
        assert_true(
            not (dataset_root / "sparse_backup").exists(),
            "Backup 비활성 Export가 영구 sparse_backup을 만들었습니다.",
        )
        exported_state = ensure_runtime(bpy.context.scene)
        assert_true(
            exported_state.edit_revision == 0
            and exported_state.index_revision == 0
            and not exported_state.operations
            and settings.edit_revision == 0,
            "Export 성공 뒤 Runtime 기준선과 편집 이력이 초기화되지 않았습니다.",
        )
        assert_true(
            exported_state.current_reconstruction is reconstruction_before_export
            and exported_state.baseline_reconstruction is reconstruction_before_export
            and exported_state.indexes is indexes_before_export,
            "Export 성공 뒤 검증된 Reconstruction 또는 관계 인덱스를 불필요하게 재생성했습니다.",
        )
        point_after_export = _managed_objects(
            "POINT_MESH",
            settings.dataset_uuid,
        )[0]
        assert_true(
            (
                point_after_export.as_pointer(),
                point_after_export.data.as_pointer(),
            )
            == point_identity
            and tuple(
                sorted(
                    obj.as_pointer()
                    for obj in _managed_objects(
                        "CAMERA",
                        settings.dataset_uuid,
                    )
                )
            )
            == camera_identity,
            "Export가 기존 Point/Camera Blender 데이터 블록을 재생성했습니다.",
        )

        _remove_first_point(settings)
        pre_backup_model = read_model_text(model_directory, modern=False)
        settings.export_backup_enabled = True
        assert_true(
            bpy.ops.colmap.export() == {"FINISHED"},
            settings.export_status,
        )
        final_model = read_model_text(model_directory, modern=False)
        assert_true(
            len(final_model.points3d) == 1,
            "Backup 활성 Export가 최종 Point3D를 원본에 기록하지 못했습니다.",
        )

        backup_root = dataset_root / "sparse_backup"
        backup_models = list(backup_root.glob("*/0/points3D.txt"))
        assert_true(
            len(backup_models) == 1,
            "Backup 활성 Export가 고유 snapshot을 만들지 못했습니다.",
        )
        backup_model = read_model_text(backup_models[0].parent, modern=False)
        assert_true(
            backup_model == pre_backup_model,
            "sparse_backup snapshot이 Export 직전 원본과 일치하지 않습니다.",
        )
        assert_true(
            not list(backup_root.glob("*/backup_manifest.json")),
            "단순 복사 Backup에 manifest가 생성되었습니다.",
        )

        assert_true(
            settings.edit_revision == 0
            and settings.source_fingerprint
            == ensure_runtime(bpy.context.scene).source.source_fingerprint,
            "Backup 활성 Export 뒤 Scene과 Runtime token이 일치하지 않습니다.",
        )

        _remove_first_point(settings)
        settings.export_backup_enabled = False
        settings.export_output_format = "BINARY"
        _state, conversion_plan = _prepare_export(bpy.context)
        assert_true(
            conversion_plan.output_format == "binary"
            and tuple(
                path.name for path in conversion_plan.target_paths
            )
            == ("cameras.bin", "images.bin", "points3D.bin"),
            "Blender Export BIN 선택이 Core 계획에 반영되지 않았습니다.",
        )
        settings.export_output_format = "TEXT"
        try:
            _execution_inputs(bpy.context, conversion_plan)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "확인창 이후 변경된 Export 형식을 거부하지 않았습니다."
            )
        settings.export_output_format = "BINARY"
        assert_true(
            bpy.ops.colmap.export() == {"FINISHED"},
            settings.export_status,
        )
        binary_model = read_model_binary(model_directory, modern=False)
        converted_state = ensure_runtime(bpy.context.scene)
        assert_true(
            not binary_model.points3d
            and converted_state.source.input_format == "binary"
            and settings.active_input_format == "binary"
            and "BIN" in settings.export_status,
            "TXT→BIN Export 뒤 모델 또는 Runtime source가 갱신되지 않았습니다.",
        )
        assert_true(
            not list(model_directory.glob("*.txt")),
            "TXT→BIN Export 성공 뒤 반대 형식 TXT 파일이 남았습니다.",
        )
finally:
    blender_colmap.unregister()

print("Blender COLMAP Export background test passed.")
