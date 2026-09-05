"""Blender 내장 OBJ 원본·프록시·복원·수명주기 계약을 검증합니다."""

from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np


def _add_source_path() -> None:
    """애드온 소스 루트를 테스트 모듈 탐색 경로에 추가합니다."""

    source = Path(__file__).resolve().parents[3]
    for candidate in (source, Path(__file__).resolve().parent):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


def _write_fixture(root: Path, name: str = "asymmetric.obj") -> Path:
    """Triangle·Quad·오목 N-gon과 Loop UV·재질을 가진 비대칭 OBJ를 만듭니다."""

    (root / "asymmetric.mtl").write_text(
        "newmtl red\nKd 1 0 0\nmap_Kd texture.png\nnewmtl blue\nKd 0 0 1\n"
        "newmtl unused\nKd 0 1 0\n",
        encoding="utf-8",
    )
    (root / "texture.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
            "WDCN3wAAAABJRU5ErkJggg=="
        )
    )
    path = root / name
    path.write_text(
        """mtllib asymmetric.mtl
v 2 3 5
v 4 3 5
v 2 6 5
v 10 0 0
v 12 0 0
v 12 2 0
v 10 2 0
v 20 0 0
v 24 0 0
v 24 4 0
v 22 1 0
v 20 4 0
vt 0 0
vt 1 0
vt 0 1
vt 0 0
vt 1 0
vt 1 1
vt 0 1
vt 0 0
vt 1 0
vt 1 1
vt .5 .25
vt 0 1
usemtl red
f 1/1 2/2 3/3
usemtl blue
f 4/4 5/5 6/6 7/7
usemtl red
f 8/8 9/9 10/10 11/11 12/12
""",
        encoding="utf-8",
    )
    return path


def _finish_stage_only(job) -> None:
    """준비 전용 OBJ 작업을 제한 횟수 안에 완료합니다."""

    for _ in range(100):
        result = job.step(time_budget_seconds=1.0)
        if result != "RUNNING":
            assert job.stage == "COMPLETED", job.error_message
            return
    raise AssertionError("OBJ 준비 전용 작업이 종료되지 않았습니다.")


def test_native_import_preserves_original_topology_and_proxy() -> None:
    """Native Import가 원본 좌표·가변 Polygon을 보존하고 비연결 프록시만 생성하는지 확인합니다."""

    from tools.align.mesh_io.embedded_obj_store import (
        EMBEDDED_OBJ_SUPPORT_OWNER,
        build_triangle_proxy_from_embedded_mesh,
        commit_embedded_obj_mesh,
        remove_embedded_obj_mesh,
    )
    from tools.align.mesh_io.obj_native_import import import_obj_to_staging

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        active_before = bpy.context.view_layer.objects.active
        if active_before is not None and getattr(active_before, "type", "") == "MESH":
            bpy.ops.object.mode_set(mode="EDIT")
        selected_before = tuple(obj.name for obj in bpy.context.selected_objects)
        mode_before = str(getattr(active_before, "mode", "OBJECT"))
        collection_before = bpy.context.view_layer.active_layer_collection
        object_count = len(bpy.data.objects)
        staging = import_obj_to_staging(path)
        mesh = staging.mesh
        assert len(bpy.data.objects) == object_count
        assert bpy.context.view_layer.objects.active == active_before
        assert tuple(obj.name for obj in bpy.context.selected_objects) == selected_before
        assert str(getattr(active_before, "mode", "OBJECT")) == mode_before
        assert bpy.context.view_layer.active_layer_collection == collection_before
        assert tuple(polygon.loop_total for polygon in mesh.polygons) == (3, 4, 5)
        assert np.allclose(mesh.vertices[0].co, (2.0, 3.0, 5.0))
        original_uv = np.asarray([tuple(item.uv) for item in mesh.uv_layers.active.data])
        original_materials = tuple(polygon.material_index for polygon in mesh.polygons)
        asset_uuid = commit_embedded_obj_mesh(mesh, model_id="native", source_path=path)
        proxy = build_triangle_proxy_from_embedded_mesh(
            mesh,
            model_id="native",
            display_name="native",
            source_path=path,
        )
        assert proxy.original_polygon_count == 3
        assert proxy.proxy_triangle_count == 6
        assert tuple(np.bincount(proxy.triangle_polygon_indices_uint32)) == (1, 2, 3)
        assert np.array_equal(
            original_uv,
            np.asarray([tuple(item.uv) for item in mesh.uv_layers.active.data]),
        )
        assert original_materials == tuple(polygon.material_index for polygon in mesh.polygons)
        assert mesh.use_fake_user is True
        assert not any(getattr(obj, "data", None) == mesh for obj in bpy.data.objects)
        staging.commit()
        assert remove_embedded_obj_mesh(asset_uuid, expected_model_id="native") is True
        assert not any(
            str(item.get("owner", "")) == EMBEDDED_OBJ_SUPPORT_OWNER
            for collection in (bpy.data.materials, bpy.data.images)
            for item in collection
        )
        if active_before is not None and str(getattr(active_before, "mode", "OBJECT")) != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")


def test_stage_only_discard_and_geometry_only_proxy() -> None:
    """연결 OBJ 후보는 원본 UV·재질을 유지하되 프록시만 비우고 취소 때 제거하는지 확인합니다."""

    from tools.align.mesh_importer import create_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        job = create_mesh_import_job(
            path,
            model_id_factory=lambda: "companion",
            geometry_only=True,
            stage_only=True,
            prepare_gpu=False,
        )
        _finish_stage_only(job)
        proxy = job.staged_model
        assert proxy is not None
        assert proxy.materials == () and proxy.texture_coordinate_count == 0
        mesh = find_embedded_obj_mesh(proxy.embedded_asset_uuid)
        assert len(mesh.materials) == 2 and len(mesh.uv_layers.active.data) == 12
        job.discard_staging()
        assert find_embedded_obj_mesh(proxy.embedded_asset_uuid, allow_missing=True) is None


def test_embedded_restore_ignores_deleted_source_and_legacy_rollback() -> None:
    """내장 복원은 삭제된 외부 OBJ를 읽지 않고 구버전 이전 실패 자산은 회수하는지 확인합니다."""

    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh, remove_embedded_obj_mesh
    from tools.align.project_persistence.runtime import (
        _preflight_restore_sources,
        _rollback_restore_runtime,
        _start_restore_import,
        build_project_state_snapshot,
    )
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        runtime = RuntimeState()
        job = run_mesh_import_job(
            runtime,
            path,
            model_id_factory=lambda: "restore",
            prepare_gpu=False,
        )
        asset_uuid = job.staged_model.embedded_asset_uuid
        snapshot = build_project_state_snapshot(runtime)
        assert snapshot.models[0].source.source_fingerprint == ""
        path.unlink()
        restored = RuntimeState()
        _preflight_restore_sources(restored, snapshot)
        assert _start_restore_import(restored, snapshot.models[0]) is True
        restored_proxy = restored.model_repository.models["restore"]
        assert restored_proxy.embedded_asset_uuid == asset_uuid
        assert restored_proxy.original_polygon_count == 3

        controller = RuntimeState()
        controller.project_restore.staging_runtime = restored
        restored.project_restore = controller.project_restore
        controller.project_restore.created_embedded_assets[asset_uuid] = "restore"
        _rollback_restore_runtime(controller)
        assert find_embedded_obj_mesh(asset_uuid, allow_missing=True) is None
        runtime.model_repository.clear()


def test_v8_rejects_external_obj_fingerprint() -> None:
    """v8 저장은 내장 UUID와 함께 외부 OBJ SHA를 권위로 기록하지 않는지 확인합니다."""

    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.project_persistence.codec import ProjectPersistenceCodecError, encode_project_state
    from tools.align.project_persistence.runtime import build_project_state_snapshot
    from tools.align.mesh_io.embedded_obj_store import remove_embedded_obj_mesh
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        runtime = RuntimeState()
        job = run_mesh_import_job(
            runtime,
            path,
            model_id_factory=lambda: "schema",
            prepare_gpu=False,
        )
        snapshot = build_project_state_snapshot(runtime)
        model = snapshot.models[0]
        malformed = replace(
            snapshot,
            models=(replace(model, source=replace(model.source, source_fingerprint="a" * 64)),),
        )
        try:
            encode_project_state(malformed)
        except ProjectPersistenceCodecError as error:
            assert "fingerprint" in str(error)
        else:
            raise AssertionError("v8 OBJ 외부 source fingerprint를 거부하지 않았습니다.")
        remove_embedded_obj_mesh(job.staged_model.embedded_asset_uuid, expected_model_id="schema")


def test_legacy_import_rehashes_after_native_read() -> None:
    """구버전 이전은 크기·mtime이 같은 파일 교체도 Native Import 뒤 SHA 재검사로 거부합니다."""

    from tools.align.mesh_importer import (
        MESH_IMPORT_STAGE_READ_OBJ,
        create_mesh_import_job,
    )

    original = "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    changed = "v 0 0 0\nv 2 0 0\nv 0 1 0\nf 1 2 3\n"
    assert len(original.encode("utf-8")) == len(changed.encode("utf-8"))
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "legacy.obj"
        path.write_text(original, encoding="utf-8")
        original_stat = path.stat()
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        mesh_names_before = tuple(mesh.name for mesh in bpy.data.meshes)
        job = create_mesh_import_job(
            path,
            expected_source_fingerprint=expected,
            stage_only=True,
            prepare_gpu=False,
        )
        while job.stage != MESH_IMPORT_STAGE_READ_OBJ:
            assert job.step(time_budget_seconds=1.0) == "RUNNING"
        path.write_text(changed, encoding="utf-8")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        while not job.is_terminal:
            job.step(time_budget_seconds=1.0)
        assert job.stage == "FAILED"
        assert "변경" in job.error_message
        assert tuple(mesh.name for mesh in bpy.data.meshes) == mesh_names_before


def test_blend_reopen_restores_without_external_obj() -> None:
    """ACTIVE Text와 Fake User 원본을 재열어 외부 OBJ 없이 자동 복원·Export합니다."""

    from test_obj_export import _finish_job
    from test_project_restore import _drain_restore_timer, _remove_owned_project_texts
    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh, remove_embedded_obj_mesh
    from tools.align.project_persistence.runtime import (
        build_project_state_snapshot,
        schedule_project_restore_if_available,
    )
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.operators.export_obj import ObjExportJob, build_obj_export_snapshots
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = _write_fixture(root)
        blend_path = root / "embedded.blend"
        runtime = RuntimeState()
        job = run_mesh_import_job(
            runtime,
            path,
            model_id_factory=lambda: "reopen",
            prepare_gpu=False,
        )
        snapshot = build_project_state_snapshot(runtime)
        asset_uuid = job.staged_model.embedded_asset_uuid
        write_active_project_state(snapshot)
        assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        path.unlink()
        runtime.model_repository.clear()
        assert "FINISHED" in bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False)
        mesh = find_embedded_obj_mesh(asset_uuid)
        assert mesh.use_fake_user and tuple(polygon.loop_total for polygon in mesh.polygons) == (3, 4, 5)
        restored = RuntimeState()
        assert schedule_project_restore_if_available(restored) is True
        _drain_restore_timer(restored)
        assert restored.project_restore.phase == "IDLE", restored.project_restore.last_error
        assert restored.model_repository.models["reopen"].proxy_triangle_count == 6
        export_snapshots = build_obj_export_snapshots(restored)
        export_job = ObjExportJob(
            root / "aligned_obj",
            export_snapshots,
            overwrite_existing_output=False,
            job_id="blend-reopen",
        )
        _finish_job(export_job)
        result = export_job.finalize()
        exported = read_obj(
            result.output_root / export_snapshots[0].output_filename,
            preserve_polygons=True,
        )
        assert tuple(exported.polygon_loop_totals_uint32) == (3, 4, 5)
        remove_embedded_obj_mesh(asset_uuid, expected_model_id="reopen")
        _remove_owned_project_texts()


def test_new_project_after_restore_error_retires_text_and_cleans_owned_data() -> None:
    """새 프로젝트 전체 경계가 ACTIVE Text와 복원 staging 및 ACTIVE·RETIRED Mesh를 정리합니다."""

    from test_project_restore import _remove_owned_project_texts
    from tools.align.constants import (
        PROJECT_RESTORE_PHASE_FAILED,
        PROJECT_TEXT_OWNER_KEY,
        PROJECT_TEXT_OWNER_VALUE,
        PROJECT_TEXT_STATE_KEY,
        PROJECT_TEXT_STATE_RETIRED,
    )
    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import (
        EMBEDDED_OBJ_OWNER,
        EMBEDDED_OBJ_RETIRED_OWNER,
    )
    from tools.align.project_persistence.runtime import (
        build_project_state_snapshot,
        start_new_project_after_restore_error,
    )
    from tools.align.project_persistence.store import active_project_text, write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        origin = RuntimeState()
        run_mesh_import_job(
            origin,
            _write_fixture(root, "active.obj"),
            model_id_factory=lambda: "new-project-active",
            prepare_gpu=False,
        )
        write_active_project_state(build_project_state_snapshot(origin))
        retired = bpy.data.meshes.new("preexisting-retired")
        retired["owner"] = EMBEDDED_OBJ_RETIRED_OWNER
        retired["asset_uuid"] = "preexisting-retired"
        staging = RuntimeState()
        staged_job = run_mesh_import_job(
            staging,
            _write_fixture(root, "staged.obj"),
            model_id_factory=lambda: "new-project-staged",
            prepare_gpu=False,
        )
        staged_uuid = staged_job.staged_model.embedded_asset_uuid
        controller = RuntimeState()
        controller.project_restore.phase = PROJECT_RESTORE_PHASE_FAILED
        controller.project_restore.staging_runtime = staging
        staging.project_restore = controller.project_restore
        controller.project_restore.created_embedded_assets[staged_uuid] = "new-project-staged"
        assert start_new_project_after_restore_error(controller) is True
        assert controller.project_restore.phase == "IDLE"
        assert controller.project_restore.staging_runtime is None
        assert active_project_text() is None
        owners = {EMBEDDED_OBJ_OWNER, EMBEDDED_OBJ_RETIRED_OWNER}
        assert not any(str(mesh.get("owner", "")) in owners for mesh in bpy.data.meshes)
        retired_texts = tuple(
            text
            for text in bpy.data.texts
            if text.get(PROJECT_TEXT_OWNER_KEY) == PROJECT_TEXT_OWNER_VALUE
            and text.get(PROJECT_TEXT_STATE_KEY) == PROJECT_TEXT_STATE_RETIRED
        )
        assert len(retired_texts) == 1
    _remove_owned_project_texts()


def test_companion_parent_commit_failure_discards_candidate() -> None:
    """COLMAP 상위 commit 실패가 아직 넘기지 않은 companion 내장 후보를 제거하는지 확인합니다."""

    from test_companion_import import write_dataset
    from tools.align.importer import IMPORT_STAGE_COMMIT, create_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_dataset(root)
        runtime = RuntimeState()
        job = create_import_job(
            root,
            input_format="TEXT",
            model_id_factory=lambda: "parent-failure",
            prepare_gpu=False,
        )
        for _ in range(200):
            if job.stage == IMPORT_STAGE_COMMIT:
                break
            assert job.step(runtime) == "RUNNING", job.error_message
        else:
            raise AssertionError("COLMAP 연결 OBJ 작업이 commit 경계에 도달하지 않았습니다.")
        asset_uuid = job.staged_companion_mesh.embedded_asset_uuid
        assert find_embedded_obj_mesh(asset_uuid) is not None
        with patch.object(
            runtime.model_catalog,
            "add_imported_model",
            side_effect=RuntimeError("상위 commit 실패 검사"),
        ):
            assert job.step(runtime) == "CANCELLED"
        assert find_embedded_obj_mesh(asset_uuid, allow_missing=True) is None
        assert runtime.model_repository.models == {}
        assert runtime.model_repository.companion_meshes == {}


def test_native_commit_cleanup_failure_can_rollback_mesh() -> None:
    """부속 정리 예외 뒤 committed 상태를 세우지 않아 후보 Mesh를 회수하는지 확인합니다."""

    from tools.align.mesh_io.embedded_obj_store import (
        commit_embedded_obj_mesh,
        find_embedded_obj_mesh,
    )
    from tools.align.mesh_io.obj_native_import import NativeObjStaging, import_obj_to_staging

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        staging = import_obj_to_staging(path)
        asset_uuid = commit_embedded_obj_mesh(
            staging.mesh,
            model_id="cleanup-failure",
            source_path=path,
        )
        with patch.object(
            NativeObjStaging,
            "_remove_unused_support_data",
            side_effect=(RuntimeError("정리 실패 검사"), None),
        ):
            try:
                staging.commit()
            except RuntimeError as error:
                assert str(error) == "정리 실패 검사"
            else:
                raise AssertionError("Native staging 정리 실패를 전달하지 않았습니다.")
            assert staging.committed is False
            staging.discard()
        assert find_embedded_obj_mesh(asset_uuid, allow_missing=True) is None


def test_native_discard_remove_failure_retires_candidate() -> None:
    """Staging Mesh 물리 제거 실패가 ACTIVE 후보를 비저장 tombstone으로 격리하는지 확인합니다."""

    from tools.align.mesh_io.embedded_obj_store import (
        EMBEDDED_OBJ_RETIRED_OWNER,
        commit_embedded_obj_mesh,
        find_embedded_obj_mesh,
    )
    from tools.align.mesh_io import obj_native_import

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        staging = obj_native_import.import_obj_to_staging(path)
        asset_uuid = commit_embedded_obj_mesh(
            staging.mesh,
            model_id="discard-failure",
            source_path=path,
        )
        with patch.object(
            obj_native_import,
            "_remove_mesh_data_block",
            side_effect=RuntimeError("Mesh 제거 실패 검사"),
        ):
            staging.discard()
        assert staging._discarded is True and staging.mesh is None
        assert find_embedded_obj_mesh(asset_uuid, allow_missing=True) is None
        retired = tuple(
            mesh
            for mesh in bpy.data.meshes
            if str(mesh.get("owner", "")) == EMBEDDED_OBJ_RETIRED_OWNER
            and str(mesh.get("asset_uuid", "")) == asset_uuid
        )
        assert len(retired) == 1 and retired[0].use_fake_user is False
        bpy.data.meshes.remove(retired[0])


def test_layer_delete_persistence_failure_preserves_runtime_and_mesh() -> None:
    """삭제 persistence 경계 실패가 Runtime과 내장 Mesh를 모두 원래 상태로 복원하는지 확인합니다."""

    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh, remove_embedded_obj_mesh
    from tools.align.operators import model_group
    from tools.align.state import RUNTIME

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        job = run_mesh_import_job(
            RUNTIME,
            path,
            model_id_factory=lambda: "delete-rollback",
            prepare_gpu=False,
        )
        assert RUNTIME.model_catalog.select_model(
            "delete-rollback",
            has_marker_draft=False,
            completed_pair_count=0,
        )
        from test_geometry_axes_ui import _Layout
        from tools.align.ui import panels

        layout = _Layout()
        panels._draw_data_layer_row(
            layout,
            RUNTIME.model_catalog.models["delete-rollback"],
            RUNTIME.model_catalog,
            importing=False,
            selection_locked=False,
        )
        select_button = next(
            item
            for item in layout.all_entries()
            if item[0] == "operator" and item[1] == "colmap_align.select_data_layer"
        )
        assert "OBJ 내장됨 · 원본 면 3 / 삼각면 6" in select_button[2]["text"]
        asset_uuid = job.staged_model.embedded_asset_uuid
        with patch.object(
            model_group,
            "clear_project_state_after_last_model",
            side_effect=RuntimeError("persistence 실패 검사"),
        ):
            try:
                model_group.delete_model_from_runtime("delete-rollback")
            except RuntimeError as error:
                assert str(error) == "persistence 실패 검사"
            else:
                raise AssertionError("레이어 삭제 persistence 실패를 전달하지 않았습니다.")
        assert "delete-rollback" in RUNTIME.model_repository.models
        assert "delete-rollback" in RUNTIME.model_catalog.models
        assert find_embedded_obj_mesh(asset_uuid) is not None
        remove_embedded_obj_mesh(asset_uuid, expected_model_id="delete-rollback")
    RUNTIME.clear_transient()


def test_layer_delete_missing_embedded_uuid_fails_before_mutation() -> None:
    """독립 OBJ의 내장 UUID 손상은 삭제 전에 차단해 Runtime과 실제 Mesh를 그대로 보존합니다."""

    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh, remove_embedded_obj_mesh
    from tools.align.operators import model_group
    from tools.align.state import RUNTIME

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        job = run_mesh_import_job(
            RUNTIME,
            path,
            model_id_factory=lambda: "delete-missing-uuid",
            prepare_gpu=False,
        )
        assert RUNTIME.model_catalog.select_model(
            "delete-missing-uuid",
            has_marker_draft=False,
            completed_pair_count=0,
        )
        model = RUNTIME.model_repository.models["delete-missing-uuid"]
        asset_uuid = job.staged_model.embedded_asset_uuid
        object.__setattr__(model, "embedded_asset_uuid", "")
        try:
            model_group.delete_model_from_runtime("delete-missing-uuid")
        except RuntimeError as error:
            assert "UUID" in str(error)
        else:
            raise AssertionError("내장 Mesh UUID가 없는 OBJ 레이어 삭제를 허용했습니다.")
        assert "delete-missing-uuid" in RUNTIME.model_repository.models
        assert "delete-missing-uuid" in RUNTIME.model_catalog.models
        assert find_embedded_obj_mesh(asset_uuid) is not None
        object.__setattr__(model, "embedded_asset_uuid", asset_uuid)
        remove_embedded_obj_mesh(asset_uuid, expected_model_id="delete-missing-uuid")
    RUNTIME.clear_transient()


def test_layer_delete_multi_model_write_failure_preserves_runtime_and_mesh() -> None:
    """여러 레이어의 강제 Text 동기화 실패가 삭제 Runtime과 내장 Mesh를 함께 되돌리는지 확인합니다."""

    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh, remove_embedded_obj_mesh
    from tools.align.operators import model_group
    from tools.align.state import RUNTIME

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        first_path = _write_fixture(root, "first.obj")
        second_path = _write_fixture(root, "second.obj")
        first = run_mesh_import_job(
            RUNTIME,
            first_path,
            model_id_factory=lambda: "delete-first",
            prepare_gpu=False,
        )
        second = run_mesh_import_job(
            RUNTIME,
            second_path,
            model_id_factory=lambda: "delete-second",
            prepare_gpu=False,
        )
        assert RUNTIME.model_catalog.select_model(
            "delete-first",
            has_marker_draft=False,
            completed_pair_count=0,
        )
        first_uuid = first.staged_model.embedded_asset_uuid
        second_uuid = second.staged_model.embedded_asset_uuid
        with patch.object(
            model_group,
            "sync_project_state_if_dirty",
            side_effect=RuntimeError("Text 동기화 실패 검사"),
        ):
            try:
                model_group.delete_model_from_runtime("delete-first")
            except RuntimeError as error:
                assert str(error) == "Text 동기화 실패 검사"
            else:
                raise AssertionError("다중 레이어 Text 동기화 실패를 전달하지 않았습니다.")
        assert set(RUNTIME.model_repository.models) == {"delete-first", "delete-second"}
        assert set(RUNTIME.model_catalog.models) == {"delete-first", "delete-second"}
        assert find_embedded_obj_mesh(first_uuid) is not None
        assert find_embedded_obj_mesh(second_uuid) is not None
        remove_embedded_obj_mesh(first_uuid, expected_model_id="delete-first")
        remove_embedded_obj_mesh(second_uuid, expected_model_id="delete-second")
    RUNTIME.clear_transient()


def test_layer_delete_remove_failure_retires_orphan() -> None:
    """확정 삭제 뒤 물리 제거 예외는 Runtime을 되살리지 않고 비저장 tombstone으로 격리합니다."""

    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import (
        EMBEDDED_OBJ_RETIRED_OWNER,
        find_embedded_obj_mesh,
    )
    from tools.align.operators import model_group
    from tools.align.state import RUNTIME

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        job = run_mesh_import_job(
            RUNTIME,
            path,
            model_id_factory=lambda: "delete-retire",
            prepare_gpu=False,
        )
        assert RUNTIME.model_catalog.select_model(
            "delete-retire",
            has_marker_draft=False,
            completed_pair_count=0,
        )
        asset_uuid = job.staged_model.embedded_asset_uuid
        with patch.object(
            model_group,
            "remove_embedded_obj_mesh",
            side_effect=RuntimeError("제거 실패 검사"),
        ):
            result = model_group.delete_model_from_runtime("delete-retire")
        assert result.model_id == "delete-retire"
        assert RUNTIME.model_repository.models == {} and RUNTIME.model_catalog.models == {}
        assert find_embedded_obj_mesh(asset_uuid, allow_missing=True) is None
        retired = tuple(
            mesh
            for mesh in bpy.data.meshes
            if str(mesh.get("owner", "")) == EMBEDDED_OBJ_RETIRED_OWNER
            and str(mesh.get("asset_uuid", "")) == asset_uuid
        )
        assert len(retired) == 1 and retired[0].use_fake_user is False
        bpy.data.meshes.remove(retired[0])
    RUNTIME.clear_transient()


def test_new_project_cleanup_removes_corrupt_and_retired_unlinked_meshes() -> None:
    """명시적 새 프로젝트 정리가 중복·손상 ACTIVE와 RETIRED 비연결 Mesh를 모두 회수하는지 확인합니다."""

    from tools.align.mesh_io.embedded_obj_store import (
        EMBEDDED_OBJ_OWNER,
        EMBEDDED_OBJ_RETIRED_OWNER,
        remove_all_embedded_obj_meshes,
    )

    corrupt = bpy.data.meshes.new("corrupt-embedded")
    corrupt["owner"] = EMBEDDED_OBJ_OWNER
    corrupt["asset_uuid"] = "duplicate-or-invalid"
    retired = bpy.data.meshes.new("retired-embedded")
    retired["owner"] = EMBEDDED_OBJ_RETIRED_OWNER
    retired["asset_uuid"] = "duplicate-or-invalid"
    names = (corrupt.name, retired.name)
    assert remove_all_embedded_obj_meshes() >= 2
    assert all(name not in bpy.data.meshes for name in names)


def test_support_sweep_handles_shared_creator_and_preserves_user_ids() -> None:
    """다른 자산이 재사용한 소유 Image를 마지막 삭제 때 정리하고 사용자 ID는 보존합니다."""

    from tools.align.mesh_io.embedded_obj_store import (
        EMBEDDED_OBJ_SUPPORT_OWNER,
        commit_embedded_obj_mesh,
        remove_embedded_obj_mesh,
    )

    user_material = bpy.data.materials.new("user-material")
    user_image = bpy.data.images.new("user-image", 1, 1)
    image = bpy.data.images.new("shared-owned-image", 1, 1)
    image["owner"] = EMBEDDED_OBJ_SUPPORT_OWNER
    image["asset_uuid"] = "first-creator"
    material = bpy.data.materials.new("second-owned-material")
    material.use_nodes = True
    material["owner"] = EMBEDDED_OBJ_SUPPORT_OWNER
    material["asset_uuid"] = "second-creator"
    texture = material.node_tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    material_name = material.name
    image_name = image.name
    mesh = bpy.data.meshes.new("support-owner-mesh")
    mesh.from_pydata(((0, 0, 0), (1, 0, 0), (0, 1, 0)), (), ((0, 1, 2),))
    mesh.materials.append(material)
    asset_uuid = commit_embedded_obj_mesh(
        mesh,
        model_id="support-owner",
        source_path=Path.cwd() / "support-owner.obj",
    )
    assert remove_embedded_obj_mesh(asset_uuid, expected_model_id="support-owner") is True
    assert material_name not in bpy.data.materials and image_name not in bpy.data.images
    assert user_material.name in bpy.data.materials and user_image.name in bpy.data.images
    bpy.data.materials.remove(user_material)
    bpy.data.images.remove(user_image)


def test_session_undo_redo_preserves_embedded_mesh_data() -> None:
    """전용 Undo·Redo가 내장 UUID·revision·원본 Polygon·Loop·UV·재질과 Mesh 수를 바꾸지 않는지 확인합니다."""

    from tools.align.constants import (
        MODEL_ACTIVATION_SOURCE_MANUAL,
        MODEL_COMMAND_TOGGLE_LAYER_GROUP,
    )
    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh, remove_embedded_obj_mesh
    from tools.align import session
    from tools.align.state import RUNTIME

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = run_mesh_import_job(
            RUNTIME,
            _write_fixture(root, "undo-first.obj"),
            model_id_factory=lambda: "undo-first",
            prepare_gpu=False,
        )
        second = run_mesh_import_job(
            RUNTIME,
            _write_fixture(root, "undo-second.obj"),
            model_id_factory=lambda: "undo-second",
            prepare_gpu=False,
        )
        uuids = (first.staged_model.embedded_asset_uuid, second.staged_model.embedded_asset_uuid)
        mesh_count = len(bpy.data.meshes)
        signatures = {}
        for asset_uuid in uuids:
            mesh = find_embedded_obj_mesh(asset_uuid)
            signatures[asset_uuid] = (
                str(mesh.get("asset_uuid", "")),
                int(mesh.get("geometry_revision", 0)),
                tuple((item.loop_start, item.loop_total, item.material_index) for item in mesh.polygons),
                tuple(item.vertex_index for item in mesh.loops),
                tuple(tuple(item.uv) for item in mesh.uv_layers.active.data),
                tuple(item.name for item in mesh.materials),
            )
        before = RUNTIME.model_catalog.snapshot()
        assert RUNTIME.model_catalog.transfer_model_group(
            "undo-second",
            source=MODEL_ACTIVATION_SOURCE_MANUAL,
            has_marker_draft=False,
            completed_pair_count=0,
        )
        after = RUNTIME.model_catalog.snapshot()
        assert RUNTIME.undo_journal.record_committed(
            before,
            after,
            "내장 OBJ Undo 검사",
            MODEL_COMMAND_TOGGLE_LAYER_GROUP,
        )
        assert session.undo_session_state() is True
        assert session.redo_session_state() is True
        assert len(bpy.data.meshes) == mesh_count
        for asset_uuid in uuids:
            mesh = find_embedded_obj_mesh(asset_uuid)
            assert signatures[asset_uuid] == (
                str(mesh.get("asset_uuid", "")),
                int(mesh.get("geometry_revision", 0)),
                tuple((item.loop_start, item.loop_total, item.material_index) for item in mesh.polygons),
                tuple(item.vertex_index for item in mesh.loops),
                tuple(tuple(item.uv) for item in mesh.uv_layers.active.data),
                tuple(item.name for item in mesh.materials),
            )
        remove_embedded_obj_mesh(uuids[0], expected_model_id="undo-first")
        remove_embedded_obj_mesh(uuids[1], expected_model_id="undo-second")
    RUNTIME.clear_transient()


def run_all() -> None:
    """내장 OBJ 핵심 계약을 Blender 5.2에서 순서대로 검증합니다."""

    _add_source_path()
    test_native_import_preserves_original_topology_and_proxy()
    test_stage_only_discard_and_geometry_only_proxy()
    test_embedded_restore_ignores_deleted_source_and_legacy_rollback()
    test_v8_rejects_external_obj_fingerprint()
    test_legacy_import_rehashes_after_native_read()
    test_blend_reopen_restores_without_external_obj()
    test_new_project_after_restore_error_retires_text_and_cleans_owned_data()
    test_companion_parent_commit_failure_discards_candidate()
    test_native_commit_cleanup_failure_can_rollback_mesh()
    test_native_discard_remove_failure_retires_candidate()
    test_layer_delete_persistence_failure_preserves_runtime_and_mesh()
    test_layer_delete_missing_embedded_uuid_fails_before_mutation()
    test_layer_delete_multi_model_write_failure_preserves_runtime_and_mesh()
    test_layer_delete_remove_failure_retires_orphan()
    test_new_project_cleanup_removes_corrupt_and_retired_unlinked_meshes()
    test_support_sweep_handles_shared_creator_and_preserves_user_ids()
    test_session_undo_redo_preserves_embedded_mesh_data()
    print("test_embedded_obj: 통과")


if __name__ == "__main__":
    run_all()
