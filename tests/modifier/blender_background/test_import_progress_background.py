from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import bpy


PACKAGE_PARENT = Path(__file__).resolve().parents[3]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tools import modifier as blender_colmap
from tools.modifier.blender import import_scene_job, scene_elements
from tools.modifier.blender.import_progress_session import (
    active_import_progress_session,
    clear_import_progress_sessions,
    create_import_progress_session,
    get_import_progress_session,
    import_progress_session_count,
    release_import_progress_session,
)
from tools.modifier.blender.import_scene_job import (
    ImportSceneBuildJob,
    ImportScenePhase,
)
from tools.modifier.blender.operation_guard import export_mutation_allowed
from tools.modifier.blender.operators_import import (
    COLMAP_OT_import,
    _import_progress_overlay_view,
)
from tools.modifier.blender.ownership import (
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from tools.modifier.core.import_types import ImportPlan
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.model import Reconstruction
from tests.modifier.fixture_utils import make_legacy_text


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


class _Event:
    """제품 Modal 코드가 사용하는 Blender Event의 최소 계약입니다."""

    __slots__ = ("type",)

    def __init__(self, event_type):
        self.type = event_type


class _FakeImportOperator:
    def __init__(self, job_id):
        self.job_id = job_id
        self.reports = []

    def report(self, levels, message):
        self.reports.append((set(levels), message))

    def _fail(self, context, session, exc):
        return COLMAP_OT_import._fail(self, context, session, exc)

    def _finish(self, context, session):
        return COLMAP_OT_import._finish(self, context, session)


def _managed_data_counts():
    def count(items):
        return sum(item.get(OWNER_KEY) == OWNER_TOKEN for item in items)

    return (
        count(bpy.data.collections),
        count(bpy.data.objects),
        count(bpy.data.meshes),
        count(bpy.data.cameras),
        count(bpy.data.materials),
        count(bpy.data.node_groups),
    )


blender_colmap.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        model_directory, images_directory = make_legacy_text(root)
        loaded = load_reconstruction(
            root,
            model_directory,
            images_directory,
            "TEXT",
        )

        # Camera 16개 batch 경계와 IMAGE_ID 오름차순을 Scene Job에서 직접 검증합니다.
        base_image = next(iter(loaded.reconstruction.images.values()))
        images = {
            image_id: replace(
                base_image,
                image_id=image_id,
                name=base_image.name,
            )
            for image_id in range(20, 0, -1)
        }
        twenty_camera_model = Reconstruction(
            cameras=dict(loaded.reconstruction.cameras),
            images=images,
            points3d=dict(loaded.reconstruction.points3d),
            rigs=dict(loaded.reconstruction.rigs),
            frames=dict(loaded.reconstruction.frames),
        )
        scene_loaded = replace(loaded, reconstruction=twenty_camera_model)
        baseline_counts = _managed_data_counts()
        scene_job = ImportSceneBuildJob(
            scene_loaded,
            point_size=0.05,
            forward_axis="Y",
            up_axis="Z",
            point_id_order=tuple(sorted(twenty_camera_model.points3d)),
        )
        previous_camera_count = 0
        observed_batch_sizes = []
        fractions = []
        while not scene_job.done:
            scene_job.advance(work_budget_ms=25.0)
            snapshot = scene_job.progress
            fractions.append(snapshot.overall_fraction)
            if scene_job._cameras_collection is not None:
                current_camera_count = len(scene_job._cameras_collection.objects)
                delta = current_camera_count - previous_camera_count
                if delta:
                    observed_batch_sizes.append(delta)
                previous_camera_count = current_camera_count
        assert_true(
            observed_batch_sizes == [16, 4],
            f"Camera batch가 16개 경계를 지키지 않았습니다: {observed_batch_sizes}",
        )
        assert_true(
            all(first <= second for first, second in zip(fractions, fractions[1:])),
            "Scene Job 진행률이 단조 증가하지 않았습니다.",
        )
        camera_ids = tuple(
            int(obj["colmap_image_id_text"])
            for obj in scene_job._cameras_collection.objects
            if obj.get(RECORD_KIND_KEY) == "CAMERA"
        )
        assert_true(
            camera_ids == tuple(range(1, 21)),
            f"Camera가 IMAGE_ID 오름차순으로 생성되지 않았습니다: {camera_ids}",
        )
        scene_job.abort()
        assert_true(
            _managed_data_counts() == baseline_counts,
            "완료 전 Scene Job abort 뒤 관리 staging datablock이 남았습니다.",
        )

        # Camera 중간 실패도 Object와 Camera Data orphan을 남기지 않아야 합니다.
        failing_job = ImportSceneBuildJob(
            scene_loaded,
            point_size=0.05,
            forward_axis="Y",
            up_axis="Z",
            point_id_order=tuple(sorted(twenty_camera_model.points3d)),
        )
        original_create_camera = import_scene_job._create_camera
        camera_call_count = [0]

        def fail_fifth_camera(*args, **kwargs):
            camera_call_count[0] += 1
            if camera_call_count[0] == 5:
                raise RuntimeError("의도한 Camera staging 실패")
            return original_create_camera(*args, **kwargs)

        import_scene_job._create_camera = fail_fifth_camera
        try:
            try:
                while not failing_job.done:
                    failing_job.advance(work_budget_ms=25.0)
            except RuntimeError as exc:
                assert_true(
                    "의도한 Camera staging 실패" in str(exc),
                    f"예상하지 않은 Scene Job 오류입니다: {exc}",
                )
            else:
                raise AssertionError("의도한 Camera staging 실패가 발생하지 않았습니다.")
        finally:
            import_scene_job._create_camera = original_create_camera
            failing_job.abort()
        assert_true(
            _managed_data_counts() == baseline_counts,
            "Scene Job 실패 정리 뒤 관리 datablock orphan이 남았습니다.",
        )

        # Collection Tree 생성 중 실패하면 아직 StagedScene이 없어도 모두 회수합니다.
        tree_failure_job = ImportSceneBuildJob(
            loaded,
            point_size=0.05,
            forward_axis="Y",
            up_axis="Z",
            point_id_order=tuple(sorted(loaded.reconstruction.points3d)),
        )
        original_mark_managed = scene_elements.mark_managed

        def fail_points_collection_mark(data_block, loaded_value, kind):
            if kind == "POINTS_COLLECTION":
                raise RuntimeError("의도한 Collection Tree 실패")
            return original_mark_managed(data_block, loaded_value, kind)

        scene_elements.mark_managed = fail_points_collection_mark
        try:
            try:
                tree_failure_job.advance(work_budget_ms=25.0)
            except RuntimeError as exc:
                assert_true(
                    "의도한 Collection Tree 실패" in str(exc),
                    f"예상하지 않은 Collection Tree 오류입니다: {exc}",
                )
            else:
                raise AssertionError("의도한 Collection Tree 실패가 발생하지 않았습니다.")
        finally:
            scene_elements.mark_managed = original_mark_managed
            tree_failure_job.abort()
        assert_true(
            _managed_data_counts() == baseline_counts,
            "Collection Tree 중간 실패 뒤 관리 Collection이 남았습니다.",
        )

        # 공개 Import Operator의 Modal 경계를 실제 Core/Scene Job으로 진행합니다.
        settings = bpy.context.scene.colmap_import
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        plan = ImportPlan(
            dataset_root=root,
            input_format="TEXT",
            point_size=0.05,
            axis_forward="Y",
            axis_up="Z",
        )
        modal_session = create_import_progress_session(bpy.context.scene, plan)
        settings.status_kind = "IMPORTING"
        settings.status = "Importing: modal test."
        operator = _FakeImportOperator(modal_session.job_id)
        assert_true(
            not export_mutation_allowed(bpy.context),
            "Import Session 중 공통 장면 변경 잠금이 활성화되지 않았습니다.",
        )
        created_phase = modal_session.job.progress.phase
        for event_type in ("ESC", "RIGHTMOUSE", "LEFTMOUSE"):
            result = COLMAP_OT_import.modal(
                operator,
                bpy.context,
                _Event(event_type),
            )
            assert_true(
                result == {"RUNNING_MODAL"}
                and modal_session.job.progress.phase is created_phase,
                f"{event_type} 입력이 Import Job을 진행하거나 취소했습니다.",
            )

        fractions = []
        final_result = None
        for _index in range(1000):
            active = get_import_progress_session(modal_session.job_id)
            if active is not None:
                view = _import_progress_overlay_view(active)
                fractions.append(view.fraction)
            final_result = COLMAP_OT_import.modal(
                operator,
                bpy.context,
                _Event("TIMER"),
            )
            if final_result != {"RUNNING_MODAL"}:
                break
        assert_true(
            final_result == {"FINISHED"},
            f"Modal Import가 완료되지 않았습니다: {final_result}",
        )
        assert_true(
            all(first <= second for first, second in zip(fractions, fractions[1:])),
            "Import 전체 Overlay 진행률이 단조 증가하지 않았습니다.",
        )
        assert_true(
            import_progress_session_count() == 0
            and export_mutation_allowed(bpy.context),
            "Modal Import 완료 뒤 Session 또는 작업 잠금이 남았습니다.",
        )
        assert_true(
            settings.status_kind == "SUCCESS"
            and settings.camera_count == "1"
            and settings.point3d_count == "1",
            f"Modal Import 결과가 동기 Import 계약과 다릅니다: {settings.status}",
        )

        # 파일 load·Undo·unregister가 사용하는 공통 강제 정리 경계도 검증합니다.
        interrupted = create_import_progress_session(bpy.context.scene, plan)
        settings.status_kind = "IMPORTING"
        clear_import_progress_sessions()
        assert_true(
            import_progress_session_count() == 0
            and interrupted.job.failed
            and settings.status_kind == "ERROR"
            and export_mutation_allowed(bpy.context),
            "강제 상태 변경 뒤 Import stream, Session 또는 잠금이 남았습니다.",
        )
finally:
    active = active_import_progress_session()
    if active is not None:
        release_import_progress_session(active.job_id)
    blender_colmap.unregister()

print("BLENDER_COLMAP_BACKGROUND_IMPORT_PROGRESS_OK")
