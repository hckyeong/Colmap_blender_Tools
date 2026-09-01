"""격리된 Blender Repository에 설치한 Extension ZIP의 등록 계약을 검증합니다."""

from __future__ import annotations

import addon_utils
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import traceback

import bpy
import numpy as np


REPOSITORY_ID = os.environ.get("COLMAP_TOOLS_TEST_REPOSITORY", "colmap_test")
MODULE_NAME = f"bl_ext.{REPOSITORY_ID}.colmap_tools"


def _operator_is_registered(operator_id: str) -> bool:
    """지정한 Blender Operator가 현재 RNA Registry에 존재하는지 반환합니다."""

    category, name = operator_id.split(".", 1)
    try:
        getattr(getattr(bpy.ops, category), name).get_rna_type()
    except (AttributeError, KeyError, RuntimeError):
        return False
    return True


def _modifier_f7_keymap_items() -> tuple[object, ...]:
    """설치본이 등록한 Modifier Background F7 키맵 항목을 반환합니다."""

    keyconfig = getattr(bpy.context.window_manager.keyconfigs, "addon", None)
    if keyconfig is None:
        return ()
    return tuple(
        item
        for keymap in keyconfig.keymaps
        for item in keymap.keymap_items
        if keymap.name == "3D View"
        and keymap.space_type == "VIEW_3D"
        and keymap.region_type == "WINDOW"
        and item.idname == "colmap.review_background_toggle"
        and item.type == "F7"
        and item.value == "PRESS"
    )


def _projection_paint_multiselect_block_keymap_items() -> tuple[object, ...]:
    """설치본이 등록한 Projection Paint 복수 선택 입력 차단 항목을 반환합니다."""

    keyconfig = getattr(bpy.context.window_manager.keyconfigs, "addon", None)
    if keyconfig is None:
        return ()
    return tuple(
        item
        for keymap in keyconfig.keymaps
        for item in keymap.keymap_items
        if keymap.name == "Image Paint"
        and keymap.region_type == "WINDOW"
        and item.idname == "projection_paint.block_multiselect_paint_stroke"
        and item.type in {"LEFTMOUSE", "PEN", "ERASER"}
        and item.value == "PRESS"
    )


def _assert_installed_location(module) -> None:
    """불러온 Root Module이 격리 설치 Repository 아래에 있는지 확인합니다."""

    expected_root = os.environ.get("COLMAP_TOOLS_EXPECTED_INSTALL_ROOT", "")
    if not expected_root:
        raise AssertionError("격리 설치 Repository 경로가 전달되지 않았습니다.")
    module_path = Path(module.__file__).resolve()
    repository_root = Path(expected_root).resolve()
    if repository_root not in module_path.parents:
        raise AssertionError(f"격리 설치본이 아닌 Module이 로드되었습니다: {module_path}")


def _assert_manifest_version(module) -> None:
    """설치본 Python 버전과 같은 패키지의 Manifest 버전이 일치하는지 확인합니다."""

    manifest_path = Path(module.__file__).resolve().parent / "blender_manifest.toml"
    with manifest_path.open("rb") as manifest_file:
        manifest_version = str(tomllib.load(manifest_file).get("version", ""))
    if not manifest_version or module.__version__ != manifest_version:
        raise AssertionError(
            "설치본 Python·Manifest 버전이 일치하지 않습니다: "
            f"python={module.__version__}, manifest={manifest_version}"
        )


def _assert_small_gicp_runtime(module) -> None:
    """설치된 내장 CPython으로 고정 버전과 합성 Small GICP 연산을 검증합니다."""

    module_root = Path(module.__file__).resolve().parent
    align_properties = import_module(f"{MODULE_NAME}.tools.align.properties")
    distribution_profile = import_module(f"{MODULE_NAME}.tools.align.distribution_profile")
    if distribution_profile.DISTRIBUTION_PROFILE != "SMALL_GICP":
        raise AssertionError("설치본의 Align 배포 프로필이 SMALL_GICP가 아닙니다.")

    runtime_python = Path(align_properties.bundled_small_gicp_python_path()).resolve()
    expected_runtime_root = module_root / "tools" / "align" / "runtime" / "python"
    if runtime_python != expected_runtime_root / "python.exe":
        raise AssertionError(f"설치본의 Small GICP 실행 경로가 잘못되었습니다: {runtime_python}")
    required_paths = (
        runtime_python,
        expected_runtime_root / "python312.dll",
        expected_runtime_root / "python312._pth",
        expected_runtime_root / "runtime_manifest.json",
        expected_runtime_root / "LICENSE.txt",
        expected_runtime_root / "Lib" / "site-packages" / "numpy" / "__init__.py",
        expected_runtime_root
        / "Lib"
        / "site-packages"
        / "small_gicp.cp312-win_amd64.pyd",
    )
    missing_paths = [str(path) for path in required_paths if not path.is_file()]
    if missing_paths:
        raise AssertionError(f"설치본의 Small GICP 런타임 파일이 없습니다: {missing_paths}")
    if (expected_runtime_root / "pyvenv.cfg").exists():
        raise AssertionError("설치본에 이동할 수 없는 pyvenv.cfg가 포함되었습니다.")
    if (expected_runtime_root / "Lib" / "site-packages" / "open3d").exists():
        raise AssertionError("설치본의 Small GICP 런타임에 Open3D가 포함되었습니다.")

    worker_path = module_root / "tools" / "align" / "alignment" / "icp_small_gicp_worker.py"
    completed = subprocess.run(
        [
            str(runtime_python),
            "-I",
            str(worker_path),
            "--check-environment",
            "--backend",
            "SMALL_GICP",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "설치본의 Small GICP 합성 연산이 실패했습니다: "
            f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
        )
    payload = json.loads(completed.stdout.strip())
    expected_values = {
        "worker_python_version": "3.12.10",
        "worker_numpy_version": "1.26.4",
        "small_gicp_version": "1.0.1",
    }
    mismatches = {
        name: payload.get(name)
        for name, expected in expected_values.items()
        if payload.get(name) != expected
    }
    if not payload.get("ok") or mismatches:
        raise AssertionError(f"설치본의 Small GICP 버전 계약이 다릅니다: {payload}")
    if not payload.get("synthetic_gicp_smoke", {}).get("passed"):
        raise AssertionError(f"설치본의 Small GICP 합성 연산 결과가 올바르지 않습니다: {payload}")


def _assert_workspace_seeds(module) -> None:
    """설치 ZIP의 세 Workspace Seed 내부 ID와 중립 Viewport 계약을 검증합니다."""

    module_root = Path(module.__file__).resolve().parent
    seeds = (
        (
            module_root / "tools" / "align" / "resources" / "workspaces" / "colmap_align_seed_5_2.blend",
            "__COLMAP_ALIGN_SEED_5_2__",
            "align",
        ),
        (
            module_root / "tools" / "modifier" / "resources" / "workspaces" / "colmap_modifier_seed_5_2.blend",
            "__COLMAP_MODIFIER_SEED_5_2__",
            "modifier",
        ),
        (
            module_root
            / "tools"
            / "projection_paint"
            / "resources"
            / "workspaces"
            / "projection_paint_seed_5_2.blend",
            "__PROJECTION_PAINT_SEED_5_2__",
            "projection_paint",
        ),
    )
    for seed_path, workspace_name, tool_id in seeds:
        if not seed_path.is_file():
            raise AssertionError(f"설치본 Workspace Seed가 없습니다: {seed_path}")
        before = {workspace.as_pointer() for workspace in bpy.data.workspaces}
        with bpy.data.libraries.load(str(seed_path), link=False) as (data_from, data_to):
            if tuple(data_from.workspaces) != (workspace_name,):
                raise AssertionError(
                    f"설치본 Seed 내부 Workspace ID가 다릅니다: {seed_path}"
                )
            runtime_ids = {
                name: tuple(getattr(data_from, name))
                for name in (
                    "scenes",
                    "objects",
                    "collections",
                    "meshes",
                    "curves",
                    "cameras",
                    "lights",
                    "materials",
                    "images",
                )
                if getattr(data_from, name)
            }
            if runtime_ids:
                raise AssertionError(f"설치본 Seed에 Runtime ID가 포함되었습니다: {runtime_ids}")
            data_to.workspaces = [workspace_name]
        appended = [
            workspace
            for workspace in bpy.data.workspaces
            if workspace.as_pointer() not in before
        ]
        if len(appended) != 1:
            raise AssertionError(f"설치본 Seed Append 결과가 정확히 하나가 아닙니다: {seed_path}")
        workspace = appended[0]
        if workspace.get("colmap_tools_workspace_tool_id") != tool_id:
            raise AssertionError(f"설치본 Seed Tool ID가 다릅니다: {seed_path}")
        if workspace.get("colmap_tools_workspace_seed_schema") != 1:
            raise AssertionError(f"설치본 Seed Schema가 다릅니다: {seed_path}")
        if not str(workspace.get("colmap_tools_seed_blender_version", "")).startswith("5.2."):
            raise AssertionError(f"설치본 Seed 생성 Blender Version이 다릅니다: {seed_path}")
        if len(workspace.screens) != 1:
            raise AssertionError(f"설치본 Seed Screen 수가 하나가 아닙니다: {seed_path}")
        view_spaces = tuple(
            area.spaces.active
            for screen in workspace.screens
            for area in screen.areas
            if area.type == "VIEW_3D"
        )
        if not view_spaces:
            raise AssertionError(f"설치본 Seed에 3D Viewport가 없습니다: {seed_path}")
        for space in view_spaces:
            disabled = [
                prop.identifier
                for prop in space.bl_rna.properties
                if prop.identifier.startswith("show_object_viewport_")
                and not getattr(space, prop.identifier)
            ]
            if disabled:
                raise AssertionError(f"설치본 Seed Viewport 필터가 비활성입니다: {disabled}")


def _assert_obj_uv_roundtrip() -> None:
    """설치본 Import·Align·Export와 Blender 재수입에서 UV·diffuse 텍스처를 검증합니다."""

    mesh_io = import_module(f"{MODULE_NAME}.tools.align.mesh_io")
    obj_writer = import_module(f"{MODULE_NAME}.tools.align.mesh_io.obj_writer")
    mesh_importer = import_module(f"{MODULE_NAME}.tools.align.mesh_importer")
    export_obj = import_module(f"{MODULE_NAME}.tools.align.operators.export_obj")
    alignment_types = import_module(f"{MODULE_NAME}.tools.align.alignment.types")
    state_module = import_module(f"{MODULE_NAME}.tools.align.state")
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source_texture = root / "source texture.png"
        fixture_image = bpy.data.images.new(
            "설치본 OBJ 텍스처 테스트",
            width=2,
            height=2,
            alpha=True,
        )
        fixture_image.generated_color = (0.15, 0.55, 0.85, 1.0)
        fixture_image.filepath_raw = str(source_texture)
        fixture_image.file_format = "PNG"
        fixture_image.save()
        bpy.data.images.remove(fixture_image)
        (root / "source.mtl").write_text(
            'newmtl Surface Material\nKd 0.2 0.4 0.8\nmap_Kd "source texture.png"\n',
            encoding="utf-8",
        )
        source_path = root / "source.obj"
        source_path.write_text(
            (
                "mtllib source.mtl\nusemtl Surface Material\n"
                "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                "vt 0.25\nvt 1 0.5\nvt 0.1 0.2 0.3\nvt 0.9 0.8\n"
                "f 1/1 2/2 3/3\nf 1/4 3/3 4/2\n"
            ),
            encoding="utf-8",
        )
        runtime = state_module.RuntimeState()
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            mesh_importer.run_mesh_import_job(
                runtime,
                source_path,
                model_id_factory=lambda: "installed-uv",
                display_name_hint="installed-uv",
            )
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard
        imported_model = runtime.model_repository.models["installed-uv"]
        if imported_model.material_count != 1 or imported_model.diffuse_texture_count != 1:
            raise AssertionError("설치본 OBJ Import가 MTL diffuse 텍스처를 보존하지 않았습니다.")
        original_uv = imported_model.texcoords_float64.copy()
        alignment_matrix = np.eye(4, dtype=np.float64)
        alignment_matrix[:3, 3] = (2.0, 3.0, 4.0)
        runtime.model_repository.set_alignment_transform(
            "installed-uv",
            alignment_types.Sim3Transform(matrix_float64=alignment_matrix),
        )
        snapshots = export_obj.build_obj_export_snapshots(runtime)
        if len(snapshots) != 1:
            raise AssertionError("설치본 Import 결과가 OBJ Export A 그룹에 하나만 등록되지 않았습니다.")
        snapshot = snapshots[0]
        if not np.array_equal(snapshot.texcoords_float64, original_uv):
            raise AssertionError("설치본 Align 변환 뒤 UV 값이 변경되었습니다.")
        output_path = root / snapshot.output_filename
        validation = obj_writer.write_obj_incremental(output_path, snapshot)
        parsed = mesh_io.read_obj(output_path)
        if validation.texture_coordinate_count != 4 or validation.textured_face_count != 2:
            raise AssertionError("설치본 OBJ UV 왕복 count가 다릅니다.")
        if validation.material_count != 1 or validation.diffuse_texture_count != 1:
            raise AssertionError("설치본 OBJ 재질·텍스처 왕복 count가 다릅니다.")
        copied_texture = root / snapshot.texture_assets[0].output_relative_path
        if copied_texture.read_bytes() != source_texture.read_bytes():
            raise AssertionError("설치본 OBJ Export 텍스처가 원본 바이트와 다릅니다.")
        if snapshot.output_filename != "installed-uv.obj":
            raise AssertionError("설치본 OBJ Export 파일명이 모델 표시 이름만 사용하지 않습니다.")
        if snapshot.texture_assets[0].output_relative_path != Path(source_texture.name):
            raise AssertionError("설치본 OBJ Export가 텍스처를 하위 폴더에 배치했습니다.")
        if not np.array_equal(
            parsed.triangle_texcoord_indices_int32,
            snapshot.triangle_texcoord_indices_int32,
        ):
            raise AssertionError("설치본 OBJ UV face-corner 연결이 다릅니다.")
        if not np.allclose(parsed.vertices_float64[0], (2.0, 3.0, 4.0)):
            raise AssertionError("설치본 OBJ Export가 확정 Align 변환을 적용하지 않았습니다.")
        imported_before = set(bpy.data.objects)
        result = bpy.ops.wm.obj_import(filepath=str(output_path))
        if "FINISHED" not in result:
            raise AssertionError("설치본 UV OBJ를 Blender로 다시 가져오지 못했습니다.")
        imported = tuple(obj for obj in bpy.data.objects if obj not in imported_before)
        if len(imported) != 1 or imported[0].type != "MESH":
            raise AssertionError("설치본 UV OBJ 재수입 결과가 단일 메시가 아닙니다.")
        mesh_object = imported[0]
        uv_layer = mesh_object.data.uv_layers.active
        if uv_layer is None or len(mesh_object.data.polygons) != 2:
            raise AssertionError("설치본 UV OBJ 재수입에서 UV Layer가 생성되지 않았습니다.")
        first_uv = np.asarray(uv_layer.uv[mesh_object.data.polygons[0].loop_start].vector)
        second_uv = np.asarray(uv_layer.uv[mesh_object.data.polygons[1].loop_start].vector)
        if np.allclose(first_uv, second_uv):
            raise AssertionError("설치본 UV OBJ 재수입에서 seam이 합쳐졌습니다.")
        if len(mesh_object.data.materials) != 1:
            raise AssertionError("설치본 OBJ 재수입에서 Material이 생성되지 않았습니다.")
        material = mesh_object.data.materials[0]
        image_nodes = tuple(
            node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeTexImage"
        )
        if len(image_nodes) != 1:
            raise AssertionError("설치본 OBJ 재수입에서 diffuse 이미지 노드가 생성되지 않았습니다.")
        loaded_texture = Path(bpy.path.abspath(image_nodes[0].image.filepath)).resolve()
        if loaded_texture != copied_texture.resolve():
            raise AssertionError("설치본 OBJ 재수입이 복사 텍스처를 참조하지 않습니다.")
        bpy.data.objects.remove(mesh_object, do_unlink=True)


def main() -> int:
    """설치본 활성화, 공개 계약 확인과 비활성화를 순서대로 실행합니다."""

    try:
        _loaded, enabled = addon_utils.check(MODULE_NAME)
        if not enabled:
            module = addon_utils.enable(MODULE_NAME, default_set=False, persistent=False)
            if module is None:
                raise AssertionError("설치한 COLMAP Tools Extension을 활성화하지 못했습니다.")
        module = import_module(MODULE_NAME)
        _assert_installed_location(module)
        _assert_manifest_version(module)
        if not hasattr(bpy.types, "COLMAP_MT_tools"):
            raise AssertionError("설치본의 공통 COLMAP Tools 메뉴가 등록되지 않았습니다.")
        missing_operators = [
            operator_id
            for operator_id in (
                "colmap_align.open_workspace",
                "colmap.open_modifier_workspace",
                "colmap.review_background_toggle",
                "projection_paint.open_workspace",
                "projection_paint.update_colmap_snapshot",
                "projection_paint.block_multiselect_paint_stroke",
            )
            if not _operator_is_registered(operator_id)
        ]
        if missing_operators:
            raise AssertionError(f"설치본 Operator가 등록되지 않았습니다: {missing_operators}")
        missing_properties = [
            name
            for name in (
                "colmap_dataset",
                "colmap_align_settings",
                "colmap_import",
                "projection_paint",
            )
            if not hasattr(bpy.types.Scene, name)
        ]
        if missing_properties:
            raise AssertionError(f"설치본 Scene 속성이 등록되지 않았습니다: {missing_properties}")
        if _operator_is_registered("projection_paint.import_colmap"):
            raise AssertionError("설치본에 Projection Painting 중복 Import Operator가 등록되었습니다.")
        projection_settings = bpy.context.scene.projection_paint
        missing_snapshot_properties = [
            name
            for name in (
                "snapshot_uuid",
                "snapshot_revision",
                "source_modifier_dataset_uuid",
                "source_modifier_revision",
                "source_modifier_fingerprint",
                "snapshot_state",
            )
            if not hasattr(projection_settings, name)
        ]
        if missing_snapshot_properties:
            raise AssertionError(
                f"설치본 Snapshot 속성이 등록되지 않았습니다: {missing_snapshot_properties}"
            )
        if len(_modifier_f7_keymap_items()) != 1:
            raise AssertionError("설치본 Modifier F7 키맵이 정확히 하나가 아닙니다.")
        if len(_projection_paint_multiselect_block_keymap_items()) != 3:
            raise AssertionError("설치본 Projection Paint 입력 차단 키맵이 정확히 세 개가 아닙니다.")
        _assert_workspace_seeds(module)
        _assert_small_gicp_runtime(module)
        _assert_obj_uv_roundtrip()
        addon_utils.disable(MODULE_NAME, default_set=False)
        if hasattr(bpy.types, "COLMAP_MT_tools"):
            raise AssertionError("설치본 비활성화 뒤 공통 메뉴가 남았습니다.")
        if hasattr(bpy.types.Scene, "colmap_dataset"):
            raise AssertionError("설치본 비활성화 뒤 공용 Dataset 속성이 남았습니다.")
        if _modifier_f7_keymap_items():
            raise AssertionError("설치본 비활성화 뒤 Modifier F7 키맵이 남았습니다.")
        if _projection_paint_multiselect_block_keymap_items():
            raise AssertionError("설치본 비활성화 뒤 Projection Paint 입력 차단 키맵이 남았습니다.")
    except Exception:
        traceback.print_exc()
        try:
            _loaded, enabled = addon_utils.check(MODULE_NAME)
            if enabled:
                addon_utils.disable(MODULE_NAME, default_set=False)
        except Exception:
            traceback.print_exc()
        return 1
    print("COLMAP Tools 격리 설치본 검사 통과")
    return 0


def _exit_process(status: int) -> None:
    """검사 결과가 Blender Process 종료 코드에 반영되도록 즉시 종료합니다."""

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)


if __name__ == "__main__":
    _exit_process(main())
