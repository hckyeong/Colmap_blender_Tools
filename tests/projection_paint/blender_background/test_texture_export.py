"""Blender Background에서 최종 Texture Export 경로·저장·Atlas 불변성을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import texture_export as export_module  # noqa: E402
from tools.projection_paint.blender import composite_layer as composite_module  # noqa: E402
from tools.projection_paint.blender import progress_overlay as progress_overlay_module  # noqa: E402
from tools.projection_paint.blender import state_handlers as state_handlers_module  # noqa: E402
from tools.projection_paint.blender.texture_export import (  # noqa: E402
    TextureExportCommittedCleanupError,
    TextureExportError,
    atlas_output_attribute,
    atlas_dimensions,
    blender_png_compression_to_oiio,
    build_export_path_plan,
    clear_texture_export_jobs,
    commit_staged_files,
    create_atlas,
    export_texture_sync,
    normalize_export_filepath,
    reserve_texture_export_job,
    save_image_tiles,
    save_single_image,
    texture_export_steps,
    texture_export_running,
    udim_to_uvtile,
)
from tools.projection_paint.blender.composite_layer import (  # noqa: E402
    CompositeLayerError,
    create_composite_layer,
    validate_composite_session,
)
from tools.projection_paint.blender.udim_setup import (  # noqa: E402
    new_tiled_paint_image,
)
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    active_base_color_graph_layers,
)
from tests.projection_paint.blender_background.test_composite_layer import (  # noqa: E402
    _fill_image_tile,
    _prepare_backend,
)


class TextureExportBackgroundTests(unittest.TestCase):
    """작은 UDIM Fixture로 파일 결과와 Blender Image 불변성을 검사합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """Texture Export Operator를 포함한 Extension 클래스를 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """테스트가 등록한 Extension 클래스와 Runtime을 해제합니다."""
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트를 빈 Factory Scene과 고유 임시 폴더로 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.directory = Path(tempfile.mkdtemp(prefix="pp_texture_export_test_"))

    def tearDown(self) -> None:
        """테스트가 만든 외부 파일 묶음을 모두 제거합니다."""
        shutil.rmtree(self.directory, ignore_errors=True)

    def _output_pixel(self, path: Path) -> tuple[float, ...]:
        """Fixture 삼각형 내부의 출력 Pixel을 OpenImageIO 실수 채널 값으로 읽습니다."""
        import OpenImageIO as oiio

        image = oiio.ImageBuf(str(path))
        return tuple(
            float(value)
            for value in image.getpixel(
                image.spec().width * 3 // 4,
                image.spec().height // 4,
            )
        )

    def _assert_pixel_close(
        self,
        actual: tuple[float, ...],
        expected: tuple[float, ...],
        *,
        delta: float = 0.04,
    ) -> None:
        """출력 Pixel의 기대 RGBA 채널을 지정 허용 오차로 비교합니다."""
        self.assertGreaterEqual(len(actual), len(expected))
        for value, target in zip(actual, expected):
            self.assertAlmostEqual(value, target, delta=delta)

    def _fill_tile_by_index(
        self,
        image,
        tile_number: int,
        color: tuple[float, float, float, float],
    ) -> None:
        """Blender 5.2에서 외부 UDIM 파일로 지정 타일의 Pixel만 교체합니다."""
        import OpenImageIO as oiio

        self.assertIsNotNone(image.tiles.get(int(tile_number)))
        if len(image.packed_files):
            image.unpack(method="REMOVE")
        tile_path = Path(
            bpy.path.abspath(str(image.filepath).replace("<UDIM>", str(tile_number)))
        )
        buffer = oiio.ImageBuf(
            oiio.ImageSpec(int(image.size[0]), int(image.size[1]), 4, oiio.UINT8)
        )
        oiio.ImageBufAlgo.fill(buffer, color)
        self.assertTrue(buffer.write(str(tile_path)))
        image.reload()

    def test_path_plan_normalizes_tokens_and_expands_both_udim_names(self) -> None:
        """파일 탐색기 기준 이름은 토큰을 정리하고 UDIM·UVTILE 파일을 결정적으로 확장합니다."""
        filepath = self.directory / "한글 texture.<UDIM>.jpeg"
        self.assertEqual(
            Path(normalize_export_filepath(str(filepath), "PNG")).name,
            "한글 texture.png",
        )
        udim = build_export_path_plan(
            str(filepath),
            file_format="PNG",
            output_mode="UDIM",
            udim_naming="UDIM",
            tile_numbers=(1011, 1001, 1002),
        )
        self.assertEqual(
            tuple(path.name for path in udim.output_paths),
            (
                "한글 texture.1001.png",
                "한글 texture.1002.png",
                "한글 texture.1011.png",
            ),
        )
        uvtile = build_export_path_plan(
            str(filepath),
            file_format="JPEG",
            output_mode="UDIM",
            udim_naming="UVTILE",
            tile_numbers=(1001, 1002, 1011),
        )
        self.assertEqual(
            tuple(path.name for path in uvtile.output_paths),
            (
                "한글 texture.u1_v1.jpg",
                "한글 texture.u2_v1.jpg",
                "한글 texture.u1_v2.jpg",
            ),
        )
        self.assertEqual(udim_to_uvtile(1012), "u2_v2")

    def test_export_operator_uses_only_addon_overwrite_authority(self) -> None:
        """단일 덮어쓰기 권한과 실행 중 입력 차단 Operator 계약을 확인합니다."""
        operator_rna = bpy.ops.projection_paint.texture_export.get_rna_type()
        check_existing = operator_rna.properties["check_existing"]
        overwrite = operator_rna.properties["overwrite"]

        self.assertFalse(check_existing.default)
        self.assertTrue(check_existing.is_hidden)
        self.assertFalse(overwrite.default)
        self.assertFalse(overwrite.is_hidden)
        self.assertIn("BLOCKING", export_module.PP_OT_texture_export.bl_options)

        operator = SimpleNamespace(
            _export_job=SimpleNamespace(phase="PREPARING"),
            _export_timer=object(),
            _export_cancel_requested=False,
        )
        self.assertEqual(
            export_module.PP_OT_texture_export.modal(
                operator,
                bpy.context,
                SimpleNamespace(type="LEFTMOUSE", timer=None),
            ),
            {"RUNNING_MODAL"},
        )

    def test_gui_native_export_runs_color_alpha_and_post_steps_in_order(self) -> None:
        """GUI Job은 Color·Alpha INVOKE Bake 뒤 결합·기록·Staging·Commit을 순서대로 진행합니다."""
        target, _setup = _prepare_backend("TextureExportNativeSequenceTarget")
        progress = export_module.create_export_progress()
        invoke_modes = []
        job = None

        def fake_invoke(_preparation, *, execution_mode):
            """실제 GPU Job 대신 Native 호출 모드만 기록합니다."""
            invoke_modes.append(str(execution_mode))
            return {"RUNNING_MODAL"}

        def fake_write(_context, target_job):
            """파일 기록 완료 상태를 한 개의 가상 타일 경로로 표시합니다."""
            target_job.tile_paths = (self.directory / "tile.png",)

        def fake_stage(target_job):
            """Staging 완료 상태를 최종 경로와 같은 가상 경로로 표시합니다."""
            target_job.staged_paths = target_job.plan.output_paths

        def fake_commit(target_job):
            """원자 확정 완료 상태를 실제 Commit 결과 형식으로 표시합니다."""
            target_job.commit_result = export_module.AtomicCommitResult(
                target_job.plan.output_paths,
                (),
                (),
            )
            target_job.committed_paths = target_job.plan.output_paths

        try:
            with patch.object(
                export_module,
                "invoke_prepared_emit_bake",
                side_effect=fake_invoke,
            ), patch.object(
                export_module,
                "native_object_bake_running",
                return_value=False,
            ), patch.object(
                export_module,
                "_write_texture_export_job",
                side_effect=fake_write,
            ), patch.object(
                export_module,
                "_stage_texture_export_job",
                side_effect=fake_stage,
            ), patch.object(
                export_module,
                "_commit_texture_export_job",
                side_effect=fake_commit,
            ):
                job = export_module.start_texture_export_job(
                    bpy.context,
                    str(self.directory / "native_sequence.png"),
                    output_mode="SINGLE",
                    file_format="PNG",
                    color_mode="RGBA",
                    udim_naming="UDIM",
                    atlas_max_size=64,
                    png_compression=15,
                    jpeg_quality=90,
                    overwrite=False,
                    progress=progress,
                )
                self.assertEqual(job.phase, "COLOR_BAKING")
                self.assertEqual(invoke_modes, ["INVOKE_DEFAULT"])

                state_handlers_module.object_bake_complete_handler(target)
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "COLOR_BAKING")
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "ALPHA_BAKING")
                self.assertEqual(invoke_modes, ["INVOKE_DEFAULT", "INVOKE_DEFAULT"])

                state_handlers_module.object_bake_complete_handler(target)
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "ALPHA_BAKING")
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "COMBINING")
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "WRITING")
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "STAGING")
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "COMMITTING")
                self.assertEqual(export_module.advance_texture_export_job(bpy.context, job), "SUCCESS")
                self.assertEqual(progress.phase, "Complete")
        finally:
            if job is not None and not job.cleanup_complete:
                with patch.object(
                    export_module,
                    "native_object_bake_running",
                    return_value=False,
                ):
                    export_module.cleanup_texture_export_job(bpy.context, job)
            export_module.release_export_progress(progress)
            export_module.release_texture_export_reservation()

    def test_gui_modal_blocks_input_and_passes_only_escape_to_native_bake(self) -> None:
        """Native Bake 중 일반 입력은 소비하고 ESC만 취소 플래그와 PASS_THROUGH로 전달합니다."""
        job = SimpleNamespace(
            phase="COLOR_BAKING",
            cancel_requested=False,
            abort_after_bake=False,
        )
        operator = SimpleNamespace(
            _export_job=job,
            _export_cancel_requested=False,
        )
        with patch.object(
            export_module,
            "native_object_bake_running",
            return_value=True,
        ):
            self.assertEqual(
                export_module.PP_OT_texture_export.modal(
                    operator,
                    bpy.context,
                    SimpleNamespace(type="LEFTMOUSE"),
                ),
                {"RUNNING_MODAL"},
            )
            self.assertEqual(
                export_module.PP_OT_texture_export.modal(
                    operator,
                    bpy.context,
                    SimpleNamespace(type="ESC"),
                ),
                {"PASS_THROUGH"},
            )
        self.assertTrue(job.cancel_requested)
        self.assertFalse(job.abort_after_bake)

    def test_lifecycle_cancel_advances_after_native_bake_stops(self) -> None:
        """수명주기 취소는 Native Job 종료 뒤 TIMER 상태 전이와 정리를 계속 진행합니다."""
        job = SimpleNamespace(
            phase="COLOR_BAKING",
            cancel_requested=False,
            abort_after_bake=False,
            scene=bpy.context.scene,
        )
        reports = []
        operator = SimpleNamespace(
            _export_job=job,
            _export_cancel_requested=True,
            _finish_modal=lambda _context: None,
            report=lambda level, message: reports.append((level, message)),
        )

        def fake_advance(_context, target_job):
            """종료된 Native Bake를 취소 종단 상태로 전환합니다."""
            target_job.phase = "CANCELLED"
            return target_job.phase

        with patch.object(
            export_module,
            "native_object_bake_running",
            return_value=False,
        ), patch.object(
            export_module,
            "advance_texture_export_job",
            side_effect=fake_advance,
        ) as advance_mock, patch.object(
            export_module,
            "cleanup_texture_export_job",
            return_value=(),
        ):
            result = export_module.PP_OT_texture_export.modal(
                operator,
                bpy.context,
                SimpleNamespace(type="TIMER"),
            )

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(advance_mock.call_count, 1)
        self.assertTrue(job.cancel_requested)
        self.assertTrue(job.abort_after_bake)
        self.assertTrue(reports)

    def test_export_native_progress_uses_common_activity_contract(self) -> None:
        """Export Bake Overlay는 Process·단계·장치·해상도·EMIT·Samples를 백분율 없이 표시합니다."""
        _target, _setup = _prepare_backend("TextureExportNativeProgressTarget")
        image = bpy.context.scene.projection_paint.layers[0].image
        progress = export_module.TextureExportProgress(
            session_id="native-export-progress",
            started_at=export_module.monotonic(),
        )
        export_module.begin_export_native_bake(
            progress,
            stage="COLOR",
            detail="Color 계산 중",
            device_detail="Cycles OPTIX GPU",
            image=image,
        )

        view = export_module._progress_view(progress)

        self.assertTrue(view.indeterminate)
        self.assertEqual(view.phase, "Color Bake")
        self.assertIn(view.activity_text, {"Process.", "Process..", "Process...", "Process...."})
        self.assertIn("Device:", view.detail_lines[1])
        self.assertIn("Image:", view.detail_lines[2])
        self.assertIn("Bake: EMIT", view.detail_lines[2])
        self.assertIn("Samples 1", view.detail_lines[2])
        self.assertNotIn("%", " ".join(view.detail_lines))
        self.assertNotIn("최종 결과를 검증하는 중입니다", " ".join(view.detail_lines))

    def test_progress_overlay_long_text_is_fitted_to_pixel_width(self) -> None:
        """긴 Device 안내 문구는 지정한 픽셀 폭 안에서 말줄임표로 축약됩니다."""
        maximum_width = 180.0
        fitted = progress_overlay_module._fit_text_to_width(
            "Device: Cycles OPTIX GPU로 베이크합니다: NVIDIA GeForce RTX 4090",
            11,
            maximum_width,
        )
        progress_overlay_module.blf.size(0, 11)

        self.assertTrue(fitted.endswith("…"))
        self.assertLessEqual(
            progress_overlay_module.blf.dimensions(0, fitted)[0],
            maximum_width,
        )
        self.assertEqual(
            progress_overlay_module._fit_text_to_width("짧은 안내", 11, maximum_width),
            "짧은 안내",
        )

    def test_native_handler_ignores_external_bake_and_records_matching_export(self) -> None:
        """Export Handler는 Scene 소속 Paint Target과 활성 Stage가 일치할 때만 결과를 기록합니다."""
        target, _setup = _prepare_backend("TextureExportNativeHandlerTarget")
        external_mesh = bpy.data.meshes.new("TextureExportExternalBakeMesh")
        external_target = bpy.data.objects.new("TextureExportExternalBake", external_mesh)
        bpy.context.scene.collection.objects.link(external_target)
        job = SimpleNamespace(
            phase="COLOR_BAKING",
            native_bake_stage="COLOR",
            native_bake_outcome="PENDING",
            scene=bpy.context.scene,
            paint_target_pointer=target.as_pointer(),
        )
        export_module._EXPORT_JOBS["handler-test"] = job
        try:
            state_handlers_module.object_bake_pre_handler(external_target)
            self.assertEqual(job.native_bake_outcome, "PENDING")
            state_handlers_module.object_bake_pre_handler(target)
            self.assertEqual(job.native_bake_outcome, "RUNNING")
            state_handlers_module.object_bake_complete_handler(target)
            self.assertEqual(job.native_bake_outcome, "COMPLETE")
            state_handlers_module.object_bake_cancel_handler(target)
            self.assertEqual(job.native_bake_outcome, "COMPLETE")
        finally:
            export_module._EXPORT_JOBS.pop("handler-test", None)
            bpy.data.objects.remove(external_target, do_unlink=True)
            bpy.data.meshes.remove(external_mesh)

    def test_sync_texture_export_bake_uses_exec_default(self) -> None:
        """Background용 최종 Texture Image 생성은 Color·Alpha 모두 EXEC_DEFAULT를 유지합니다."""
        _target, _setup = _prepare_backend("TextureExportSyncBoundaryTarget")
        execution_modes = []

        def fake_sync_invoke(_preparation, *, execution_mode):
            """동기 Bake 호출 모드만 기록하고 즉시 완료를 반환합니다."""
            execution_modes.append(str(execution_mode))
            return {"FINISHED"}

        with patch.object(
            composite_module,
            "invoke_prepared_emit_bake",
            side_effect=fake_sync_invoke,
        ):
            image = composite_module.create_final_texture_export_image(bpy.context)
        try:
            self.assertEqual(execution_modes, ["EXEC_DEFAULT", "EXEC_DEFAULT"])
        finally:
            composite_module.remove_final_texture_export_image(image)

    def test_single_path_plan_uses_exact_name_without_tile_token(self) -> None:
        """SINGLE 경로 계획은 논리 1001을 파일명에 붙이지 않고 한 장만 생성합니다."""
        plan = build_export_path_plan(
            str(self.directory / "single.<UDIM>.png"),
            file_format="PNG",
            output_mode="SINGLE",
            udim_naming="UDIM",
            tile_numbers=(1001,),
        )
        self.assertEqual(tuple(path.name for path in plan.output_paths), ("single.png",))

    def test_atlas_dimensions_preserve_grid_ratio(self) -> None:
        """Atlas Max Size는 4×1과 1×4 배치의 긴 변만 지정 크기에 맞춥니다."""
        self.assertEqual(atlas_dimensions((1001, 1002, 1003, 1004), 8192), (8192, 2048, 2048))
        self.assertEqual(atlas_dimensions((1001, 1011, 1021, 1031), 8192), (2048, 8192, 2048))

    def test_atlas_png_compression_maps_blender_percent_to_oiio_level(self) -> None:
        """Atlas PNG는 Blender 0~100 압축 의미를 OIIO png Level 0~9에 같은 방향으로 전달합니다."""
        self.assertEqual(blender_png_compression_to_oiio(0), 0)
        self.assertEqual(blender_png_compression_to_oiio(50), 5)
        self.assertEqual(blender_png_compression_to_oiio(100), 9)
        self.assertEqual(
            atlas_output_attribute(
                Path("atlas.png"),
                png_compression=67,
                jpeg_quality=21,
            ),
            ("png:compressionLevel", 6),
        )
        self.assertEqual(
            atlas_output_attribute(
                Path("atlas.jpg"),
                png_compression=67,
                jpeg_quality=82,
            ),
            ("CompressionQuality", 82),
        )

    def test_lifecycle_clear_releases_reserved_export_job(self) -> None:
        """Load·Undo·Redo Barrier 정리는 시작 전 예약된 Export Job과 Overlay 상태를 해제합니다."""
        reserve_texture_export_job()
        self.assertTrue(texture_export_running())
        clear_texture_export_jobs("test", 1)
        self.assertFalse(texture_export_running())

    def test_lifecycle_clear_ignores_removed_modal_operator(self) -> None:
        """Blender가 먼저 제거한 Modal Operator 참조는 해제 중 오류 없이 폐기합니다."""
        class RemovedOperator:
            """삭제된 StructRNA처럼 속성 쓰기에서 ReferenceError를 발생시킵니다."""

            def __setattr__(self, _name, _value) -> None:
                """Blender가 제거한 Operator의 속성 쓰기 실패를 재현합니다."""
                raise ReferenceError("StructRNA has been removed")

        operator = RemovedOperator()
        reserve_texture_export_job()
        export_module._MODAL_EXPORT_OPERATORS[id(operator)] = operator

        clear_texture_export_jobs("unregister", 1)

        self.assertNotIn(id(operator), export_module._MODAL_EXPORT_OPERATORS)
        self.assertFalse(texture_export_running())

    def test_native_job_stop_keeps_activity_view_until_next_phase(self) -> None:
        """Bake 종료 확인 전용 틱은 가짜 백분율로 바꾸지 않고 활동형 화면을 유지합니다."""
        progress = export_module.TextureExportProgress(
            session_id="native-stop-progress",
            started_at=export_module.monotonic(),
            indeterminate=True,
            phase="Color Bake",
        )
        job = SimpleNamespace(
            phase="COLOR_BAKING",
            native_bake_job_stopped=False,
            native_bake_outcome="COMPLETE",
            native_bake_stage="COLOR",
            cancel_requested=False,
            abort_after_bake=False,
            pending_error="",
            progress=progress,
        )

        with patch.object(
            export_module,
            "native_object_bake_running",
            return_value=False,
        ):
            phase = export_module.advance_texture_export_job(bpy.context, job)

        self.assertEqual(phase, "COLOR_BAKING")
        self.assertTrue(job.native_bake_job_stopped)
        self.assertTrue(progress.indeterminate)
        self.assertTrue(export_module._progress_view(progress).activity_text)

    def test_lifecycle_clear_waits_for_native_export_restore(self) -> None:
        """수명주기 중단은 Native Job 종료 전 Image·Node·예약을 유지하고 취소 복구를 표시합니다."""
        _target, setup = _prepare_backend("TextureExportNativeLifecycleTarget")
        progress = export_module.create_export_progress()
        operator = SimpleNamespace(_export_cancel_requested=False)
        job = None
        try:
            with patch.object(
                export_module,
                "invoke_prepared_emit_bake",
                return_value={"RUNNING_MODAL"},
            ), patch.object(
                export_module,
                "native_object_bake_running",
                return_value=False,
            ):
                job = export_module.start_texture_export_job(
                    bpy.context,
                    str(self.directory / "native_lifecycle.png"),
                    output_mode="SINGLE",
                    file_format="PNG",
                    color_mode="RGBA",
                    udim_naming="UDIM",
                    atlas_max_size=64,
                    png_compression=15,
                    jpeg_quality=90,
                    overwrite=False,
                    progress=progress,
                )
            export_module._MODAL_EXPORT_OPERATORS[id(operator)] = operator
            image_pointers = (
                job.color_image.as_pointer(),
                job.alpha_image.as_pointer(),
            )
            node_pointers = tuple(
                node.as_pointer() for node in setup.material.node_tree.nodes
            )

            clear_texture_export_jobs("undo_pre", 1)

            self.assertTrue(operator._export_cancel_requested)
            self.assertTrue(job.cancel_requested)
            self.assertTrue(job.abort_after_bake)
            self.assertFalse(job.cleanup_complete)
            self.assertTrue(texture_export_running())
            self.assertEqual(
                tuple(image.as_pointer() for image in (job.color_image, job.alpha_image)),
                image_pointers,
            )
            self.assertEqual(
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                node_pointers,
            )
            self.assertIn(progress.session_id, export_module._EXPORT_PROGRESS)

            with patch.object(
                export_module,
                "native_object_bake_running",
                return_value=False,
            ):
                self.assertEqual(
                    export_module.advance_texture_export_job(bpy.context, job),
                    "COLOR_BAKING",
                )
                self.assertEqual(
                    export_module.advance_texture_export_job(bpy.context, job),
                    "CANCELLED",
                )
                export_module.cleanup_texture_export_job(bpy.context, job)
            self.assertTrue(job.cleanup_complete)
        finally:
            export_module._MODAL_EXPORT_OPERATORS.pop(id(operator), None)
            if job is not None and not job.cleanup_complete:
                with patch.object(
                    export_module,
                    "native_object_bake_running",
                    return_value=False,
                ):
                    export_module.cleanup_texture_export_job(bpy.context, job)
            export_module.release_export_progress(progress)
            export_module.release_texture_export_reservation()

    def test_blender_udim_save_restores_path_format_pack_and_dirty(self) -> None:
        """Blender Save a Copy 뒤 Source의 경로·포맷·Pack·Dirty 계약이 그대로 유지됩니다."""
        image = new_tiled_paint_image(
            "TextureExportSaveSource",
            (8, 8),
            (1001, 1002),
            (0.2, 0.4, 0.6, 0.8),
        )
        image.filepath = "//원본 경로/source.<UDIM>.png"
        image.filepath_raw = "//원본 경로/source.<UDIM>.png"
        image.pack()
        value = float(image.pixels[0])
        image.pixels[0] = value
        image.update()
        before = (
            image.filepath,
            image.filepath_raw,
            image.file_format,
            image.is_dirty,
            tuple(item.as_pointer() for item in image.packed_files),
        )
        paths = save_image_tiles(
            bpy.context,
            image,
            self.directory / "tiles" / "paint.<UDIM>.png",
            file_format="PNG",
            color_mode="RGBA",
            png_compression=15,
            jpeg_quality=90,
        )
        self.assertEqual(tuple(path.name for path in paths), ("paint.1001.png", "paint.1002.png"))
        self.assertTrue(all(path.stat().st_size > 0 for path in paths))
        self.assertEqual(
            (
                image.filepath,
                image.filepath_raw,
                image.file_format,
                image.is_dirty,
                tuple(item.as_pointer() for item in image.packed_files),
            ),
            before,
        )

    def test_openimageio_atlas_places_high_v_row_at_top(self) -> None:
        """Atlas는 높은 V 타일을 위 행에 두고 빈 Cell을 투명하게 유지합니다."""
        image = new_tiled_paint_image(
            "TextureExportAtlasSource",
            (8, 8),
            (1001, 1002, 1011),
            (0.25, 0.5, 0.75, 0.5),
        )
        tile_paths = save_image_tiles(
            bpy.context,
            image,
            self.directory / "atlas_tiles" / "paint.<UDIM>.png",
            file_format="PNG",
            color_mode="RGBA",
            png_compression=15,
            jpeg_quality=90,
        )
        output = self.directory / "atlas.png"
        with patch.object(
            export_module,
            "atlas_output_attribute",
            wraps=atlas_output_attribute,
        ) as output_attribute:
            self.assertEqual(
                create_atlas(
                    tile_paths,
                    (1001, 1002, 1011),
                    output,
                    max_size=64,
                    color_mode="RGBA",
                    png_compression=63,
                    jpeg_quality=90,
                ),
                (64, 64),
            )
        output_attribute.assert_called_once_with(
            output,
            png_compression=63,
            jpeg_quality=90,
        )
        import OpenImageIO as oiio

        atlas = oiio.ImageBuf(str(output))
        self.assertEqual((atlas.spec().width, atlas.spec().height, atlas.spec().nchannels), (64, 64, 4))
        source_pixel = oiio.ImageBuf(str(tile_paths[2])).getpixel(4, 4)
        filled = atlas.getpixel(16, 16)
        self.assertLessEqual(
            max(abs(float(left) - float(right)) for left, right in zip(filled, source_pixel)),
            2.0 / 255.0,
        )
        self.assertLessEqual(abs(float(filled[3]) - 0.5), 2.0 / 255.0)
        empty = atlas.getpixel(48, 16)
        self.assertLessEqual(max(abs(float(value)) for value in empty), 1.0 / 255.0)

    def test_atomic_commit_replaces_whole_existing_set(self) -> None:
        """덮어쓰기 허용 시 기존 UDIM 묶음을 모두 새 Staging 파일로 교체합니다."""
        staging = self.directory / "staging"
        staging.mkdir()
        staged = (staging / "a.png", staging / "b.png")
        outputs = (self.directory / "paint.1001.png", self.directory / "paint.1002.png")
        for path, content in zip(staged, (b"new-a", b"new-b")):
            path.write_bytes(content)
        for path, content in zip(outputs, (b"old-a", b"old-b")):
            path.write_bytes(content)
        commit_staged_files(staged, outputs, overwrite=True)
        self.assertEqual(tuple(path.read_bytes() for path in outputs), (b"new-a", b"new-b"))

    def test_atomic_commit_failure_restores_whole_existing_set(self) -> None:
        """UDIM 중간 확정 실패는 이미 옮긴 새 파일을 제거하고 기존 묶음을 모두 복원합니다."""
        staging = self.directory / "rollback_staging"
        staging.mkdir()
        staged = (staging / "a.png", staging / "b.png")
        outputs = (self.directory / "rollback.1001.png", self.directory / "rollback.1002.png")
        for path, content in zip(staged, (b"new-a", b"new-b")):
            path.write_bytes(content)
        for path, content in zip(outputs, (b"old-a", b"old-b")):
            path.write_bytes(content)
        original_replace = export_module.os.replace

        def fail_second_staged(source, destination):
            """두 번째 새 타일의 목적 경로 확정만 강제로 실패시킵니다."""
            if Path(source) == staged[1] and Path(destination) == outputs[1]:
                raise OSError("강제 중간 확정 실패")
            return original_replace(source, destination)

        with patch.object(export_module.os, "replace", side_effect=fail_second_staged):
            with self.assertRaisesRegex(Exception, "확정에 실패"):
                commit_staged_files(staged, outputs, overwrite=True)
        self.assertEqual(tuple(path.read_bytes() for path in outputs), (b"old-a", b"old-b"))

    def test_backup_cleanup_failure_keeps_committed_outputs_and_old_backups(self) -> None:
        """확정 후 Backup 삭제 실패는 새 출력 성공과 보존된 이전 Backup을 명시적으로 반환합니다."""
        staging = self.directory / "cleanup_failure_staging"
        staging.mkdir()
        staged = (staging / "a.png", staging / "b.png")
        outputs = (self.directory / "cleanup.1001.png", self.directory / "cleanup.1002.png")
        for path, content in zip(staged, (b"new-a", b"new-b")):
            path.write_bytes(content)
        for path, content in zip(outputs, (b"old-a", b"old-b")):
            path.write_bytes(content)
        original_unlink = Path.unlink

        def fail_backup_unlink(path, *args, **kwargs):
            """확정된 이전 Backup 파일의 개별 정리만 강제로 실패시킵니다."""
            if path.parent.name == "backups":
                raise PermissionError("강제 Backup 정리 실패")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_backup_unlink):
            result = commit_staged_files(staged, outputs, overwrite=True)
        self.assertEqual(result.output_paths, outputs)
        self.assertEqual(tuple(path.read_bytes() for path in outputs), (b"new-a", b"new-b"))
        self.assertEqual(
            tuple(path.read_bytes() for path in result.retained_backup_paths),
            (b"old-a", b"old-b"),
        )
        self.assertEqual(len(result.cleanup_errors), 2)

    def test_blender_udim_jpeg_is_rgb_and_uses_quality_setting(self) -> None:
        """JPEG UDIM은 Alpha 없는 3채널 파일로 기록하고 Source Image를 변경하지 않습니다."""
        image = new_tiled_paint_image(
            "TextureExportJpegSource",
            (8, 8),
            (1001,),
            (0.3, 0.6, 0.9, 0.25),
        )
        before = (image.filepath, image.filepath_raw, image.file_format, image.is_dirty)
        paths = save_image_tiles(
            bpy.context,
            image,
            self.directory / "jpeg_tiles" / "paint.<UDIM>.jpg",
            file_format="JPEG",
            color_mode="RGB",
            png_compression=15,
            jpeg_quality=72,
        )
        import OpenImageIO as oiio

        loaded = oiio.ImageBuf(str(paths[0]))
        self.assertEqual(loaded.spec().nchannels, 3)
        self.assertEqual((image.filepath, image.filepath_raw, image.file_format, image.is_dirty), before)

    def test_active_composite_exports_udim_without_changing_layer_graph(self) -> None:
        """활성 Composite 직접 경로는 실제 PNG를 만들고 Layer·Image·Shader Pointer를 보존합니다."""
        _target, setup = _prepare_backend(
            "TextureExportCompositeTarget",
            multi_udim=True,
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        for tile_number in (1001, 1002):
            _fill_image_tile(settings.layers[0].image, tile_number, (0.2, 0.4, 0.7, 1.0))
        create_composite_layer(bpy.context)
        session = validate_composite_session(bpy.context.scene)
        source = session.composite.image
        before = (
            tuple(layer.as_pointer() for layer in settings.layers),
            tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
            source.as_pointer(),
            source.filepath,
            source.filepath_raw,
            source.file_format,
            source.is_dirty,
            tuple(item.as_pointer() for item in source.packed_files),
        )
        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "final.png"),
            output_mode="UDIM",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=2048,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        self.assertEqual(tuple(path.name for path in paths), ("final.1001.png", "final.1002.png"))
        self.assertTrue(all(path.stat().st_size > 0 for path in paths))
        self.assertEqual(
            (
                tuple(layer.as_pointer() for layer in settings.layers),
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                source.as_pointer(),
                source.filepath,
                source.filepath_raw,
                source.file_format,
                source.is_dirty,
                tuple(item.as_pointer() for item in source.packed_files),
            ),
            before,
        )
        validate_composite_session(bpy.context.scene)

    def test_composite_none_poll_and_single_export_visible_graph_pixel(self) -> None:
        """Composite NONE의 일반 Layer Graph도 버튼이 활성화되고 SINGLE Pixel을 내보냅니다."""
        _target, _setup = _prepare_backend(
            "TextureExportNoCompositeSingleTarget",
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        exported = settings.layers[0]
        for layer in settings.layers:
            layer.visible = layer.as_pointer() == exported.as_pointer()
        _fill_image_tile(exported.image, 1001, (1.0, 0.0, 0.0, 1.0))
        visibility_before = tuple(bool(layer.visible) for layer in settings.layers)
        self.assertEqual(settings.composite_state, "NONE")
        self.assertTrue(export_module.PP_OT_texture_export.poll(bpy.context))
        for state in ("CREATING", "RECOMPOSITING", "RELEASING", "STALE"):
            settings.composite_state = state
            self.assertFalse(export_module.PP_OT_texture_export.poll(bpy.context))
        settings.composite_state = "NONE"
        reserve_texture_export_job()
        try:
            self.assertFalse(export_module.PP_OT_texture_export.poll(bpy.context))
        finally:
            export_module.release_texture_export_reservation()

        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "no_composite.png"),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )

        self.assertEqual(tuple(path.name for path in paths), ("no_composite.png",))
        pixel = self._output_pixel(paths[0])
        self.assertGreater(pixel[0], 0.7)
        self.assertLess(max(pixel[1], pixel[2]), 0.05)
        self.assertAlmostEqual(pixel[3], 1.0, delta=0.03)
        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(
            tuple(bool(layer.visible) for layer in settings.layers),
            visibility_before,
        )

    def test_composite_none_udim_exports_each_visible_graph_tile(self) -> None:
        """Composite NONE의 UDIM 일반 Layer가 타일별 실제 Pixel을 그대로 내보냅니다."""
        _target, _setup = _prepare_backend(
            "TextureExportNoCompositeUdimTarget",
            multi_udim=True,
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        exported = settings.layers[0]
        for layer in settings.layers:
            layer.visible = layer.as_pointer() == exported.as_pointer()
        self._fill_tile_by_index(exported.image, 1001, (1.0, 0.0, 0.0, 1.0))
        self._fill_tile_by_index(exported.image, 1002, (0.0, 1.0, 0.0, 1.0))

        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "no_composite_udim.png"),
            output_mode="UDIM",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )

        self.assertEqual(tuple(path.name for path in paths), ("no_composite_udim.1001.png", "no_composite_udim.1002.png"))
        first = self._output_pixel(paths[0])
        second = self._output_pixel(paths[1])
        self.assertGreater(first[0], 0.7)
        self.assertLess(max(first[1], first[2]), 0.05)
        self.assertGreater(second[1], 0.7)
        self.assertLess(max(second[0], second[2]), 0.05)

    def test_visibility_filter_and_all_hidden_error_without_output(self) -> None:
        """눈이 꺼진 Layer는 제외하고 전부 숨기면 명확한 오류와 무출력을 보장합니다."""
        _target, _setup = _prepare_backend(
            "TextureExportVisibilityTarget",
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        top, bottom = settings.layers[0], settings.layers[1]
        for layer in tuple(settings.layers)[2:]:
            layer.visible = False
        _fill_image_tile(top.image, 1001, (1.0, 0.0, 0.0, 1.0))
        _fill_image_tile(bottom.image, 1001, (0.0, 0.0, 1.0, 1.0))
        top.visible = False

        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "visible_only.png"),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        pixel = self._output_pixel(paths[0])
        self.assertGreater(pixel[2], 0.6)
        self.assertLess(max(pixel[0], pixel[1]), 0.05)

        bottom.visible = False
        hidden_output = self.directory / "all_hidden.png"
        with self.assertRaisesRegex(
            TextureExportError,
            "내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.",
        ):
            export_texture_sync(
                bpy.context,
                str(hidden_output),
                output_mode="SINGLE",
                file_format="PNG",
                color_mode="RGBA",
                udim_naming="UDIM",
                atlas_max_size=64,
                png_compression=15,
                jpeg_quality=90,
                overwrite=False,
            )
        self.assertFalse(hidden_output.exists())
        self.assertEqual(tuple(self.directory.glob(".pp_texture_export_*")), ())

    def test_export_applies_order_alpha_adjustment_and_masks(self) -> None:
        """임시 Graph 합성은 Layer 순서·Alpha·Adjustment Mask·Layer Mask를 실제 Pixel에 적용합니다."""
        _target, _setup = _prepare_backend(
            "TextureExportProcessedGraphTarget",
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        top, bottom = settings.layers[0], settings.layers[1]
        for layer in tuple(settings.layers)[2:]:
            layer.visible = False
        _fill_image_tile(bottom.image, 1001, (0.0, 0.0, 1.0, 1.0))
        _fill_image_tile(top.image, 1001, (0.25, 0.0, 0.0, 0.5))
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=str(top.layer_uuid),
                adjustment_type="EXPOSURE",
            ),
            {"FINISHED"},
        )
        adjustment = top.adjustments[0]
        adjustment.exposure = 1.0
        self.assertEqual(
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=str(top.layer_uuid),
                adjustment_uuid=str(adjustment.adjustment_uuid),
            ),
            {"FINISHED"},
        )
        _fill_image_tile(adjustment.mask.image, 1001, (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=str(top.layer_uuid)),
            {"FINISHED"},
        )
        _fill_image_tile(top.mask.image, 1001, (0.5, 0.5, 0.5, 1.0))

        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "processed_graph.png"),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )

        pixel = self._output_pixel(paths[0])
        self.assertGreater(pixel[0], 0.1)
        self.assertLess(pixel[0], 0.3)
        self.assertLess(pixel[1], 0.05)
        self.assertGreater(pixel[2], 0.7)
        self.assertAlmostEqual(pixel[3], 1.0, delta=0.03)

    def test_solo_preview_exports_full_visible_graph_without_changing_preview(self) -> None:
        """Layer 단독 보기는 Export Source를 바꾸지 않고 가시 Graph 전체 결과와 태그를 유지합니다."""
        _target, _setup = _prepare_backend(
            "TextureExportSoloPreviewTarget",
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        top, bottom = settings.layers[0], settings.layers[1]
        for layer in tuple(settings.layers)[2:]:
            layer.visible = False
        _fill_image_tile(bottom.image, 1001, (0.0, 0.0, 1.0, 1.0))
        _fill_image_tile(top.image, 1001, (1.0, 0.0, 0.0, 0.5))
        self.assertEqual(
            bpy.ops.projection_paint.layer_preview_toggle(layer_uuid=str(top.layer_uuid)),
            {"FINISHED"},
        )
        preview_before = (
            str(settings.layer_preview_uuid),
            str(settings.mask_preview_adjustment_uuid),
            bool(settings.original_layer_preview),
        )

        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "solo_preview.png"),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )

        pixel = self._output_pixel(paths[0])
        self.assertGreater(pixel[0], 0.25)
        self.assertGreater(pixel[2], 0.25)
        self.assertLess(pixel[1], 0.05)
        self.assertEqual(
            (
                str(settings.layer_preview_uuid),
                str(settings.mask_preview_adjustment_uuid),
                bool(settings.original_layer_preview),
            ),
            preview_before,
        )

    def test_export_source_has_no_full_pixel_hash_guard(self) -> None:
        """입력 차단 Export는 시작·확정 경계에서 Image 전체 Pixel Hash를 만들지 않습니다."""
        source = Path(export_module.__file__).read_text(encoding="utf-8")

        self.assertNotIn("sha256", source)
        self.assertNotIn("pixels.foreach_get", source)
        self.assertNotIn("capture_export_snapshot", source)
        self.assertNotIn("validate_export_snapshot", source)

    def test_active_composite_graph_includes_external_layer_without_source_duplicate(self) -> None:
        """활성 Composite와 외부 일반 Layer만 Export Graph에 남고 Source 자식은 중복되지 않습니다."""
        _target, _setup = _prepare_backend(
            "TextureExportCompositeGraphTarget",
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        settings.composite_include_alpha = True
        source = settings.layers[0]
        source_uuid = str(source.layer_uuid)
        for layer in settings.layers:
            layer.ui_selected = layer.as_pointer() == source.as_pointer()
            layer.visible = layer.as_pointer() == source.as_pointer()
        _fill_image_tile(source.image, 1001, (0.0, 1.0, 0.0, 0.5))
        create_composite_layer(bpy.context)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        external = settings.layers[0]
        _fill_image_tile(external.image, 1001, (1.0, 0.0, 0.0, 0.5))
        source_after = next(
            layer for layer in settings.layers if str(layer.layer_uuid) == source_uuid
        )
        source_after.visible = True
        graph = active_base_color_graph_layers(settings)

        graph_uuids = tuple(str(layer.layer_uuid) for layer in graph)
        self.assertEqual(
            graph_uuids[:2],
            (str(external.layer_uuid), str(settings.composite_layer_uuid)),
        )
        self.assertNotIn(source_uuid, graph_uuids)
        composite = next(
            layer
            for layer in graph
            if str(layer.layer_uuid) == str(settings.composite_layer_uuid)
        )
        width = int(composite.image.size[0])
        composite_alpha = float(composite.image.pixels[((24 * width) + 24) * 4 + 3])
        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "composite_graph.png"),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        pixel = self._output_pixel(paths[0])
        expected_alpha = 0.5 + composite_alpha * 0.5
        self.assertAlmostEqual(pixel[3], expected_alpha, delta=0.04)
        self.assertGreater(pixel[0], pixel[1])

    def test_cancel_after_compositing_preserves_visibility_and_removes_temporary_image(self) -> None:
        """준비 단계 뒤 취소해도 Layer 눈 상태와 출력·임시 Image를 그대로 보존합니다."""
        _target, _setup = _prepare_backend(
            "TextureExportCancelTarget",
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        settings.layers[0].visible = False
        before_visibility = tuple(bool(layer.visible) for layer in settings.layers)
        before_images = tuple(image.as_pointer() for image in bpy.data.images)
        output = self.directory / "cancelled.png"
        steps = texture_export_steps(
            bpy.context,
            str(output),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )

        self.assertEqual(next(steps), "PREPARED")
        steps.close()

        self.assertEqual(
            tuple(bool(layer.visible) for layer in settings.layers),
            before_visibility,
        )
        self.assertEqual(tuple(image.as_pointer() for image in bpy.data.images), before_images)
        self.assertFalse(output.exists())
        self.assertFalse(texture_export_running())

    def test_post_commit_temp_cleanup_failure_reports_committed_output_and_retained_backup(self) -> None:
        """임시 폴더 정리까지 실패하면 새 출력 확정과 이전 Backup 보존 상태를 구분해 보고합니다."""
        _target, _setup = _prepare_backend("TextureExportCommittedCleanupTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        output = self.directory / "committed_cleanup.png"
        output.write_bytes(b"old-texture")
        original_unlink = Path.unlink
        original_rmtree = shutil.rmtree

        def fail_backup_unlink(path, *args, **kwargs):
            """이전 Texture Backup의 개별 삭제만 실패시켜 복구본을 유지합니다."""
            if path.parent.name == "backups":
                raise PermissionError("강제 Backup 보존")
            return original_unlink(path, *args, **kwargs)

        def fail_export_temp_rmtree(path, *args, **kwargs):
            """Texture Export Transaction 폴더 정리만 실패시키고 Image Staging은 정리합니다."""
            if Path(path).name.startswith(".pp_texture_export_"):
                raise PermissionError("강제 임시 폴더 정리 실패")
            return original_rmtree(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_backup_unlink), patch.object(
            export_module.shutil,
            "rmtree",
            side_effect=fail_export_temp_rmtree,
        ):
            with self.assertRaises(TextureExportCommittedCleanupError) as raised:
                export_texture_sync(
                    bpy.context,
                    str(self.directory / "committed_cleanup.png"),
                    output_mode="SINGLE",
                    file_format="PNG",
                    color_mode="RGBA",
                    udim_naming="UDIM",
                    atlas_max_size=2048,
                    png_compression=15,
                    jpeg_quality=90,
                    overwrite=True,
                )
        self.assertEqual(raised.exception.output_paths, (output,))
        self.assertNotEqual(output.read_bytes(), b"old-texture")
        backups = tuple(self.directory.glob(".pp_texture_export_*/staged/backups/*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old-texture")
        validate_composite_session(bpy.context.scene)

    def test_visible_layer_above_composite_uses_temporary_bake_and_cleans_it(self) -> None:
        """Composite 위 가시 Layer는 임시 최종 RGBA Bake를 쓰고 Image·Node를 남기지 않습니다."""
        _target, setup = _prepare_backend(
            "TextureExportFinalBakeTarget",
            multi_udim=True,
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        _fill_image_tile(settings.layers[0].image, 1001, (0.1, 0.2, 0.4, 1.0))
        create_composite_layer(bpy.context)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        _fill_image_tile(settings.layers[0].image, 1001, (0.8, 0.2, 0.1, 0.5))
        before = (
            tuple(layer.as_pointer() for layer in settings.layers),
            tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
            tuple(image.as_pointer() for image in bpy.data.images),
        )
        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "final_atlas.png"),
            output_mode="ATLAS",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        self.assertEqual(tuple(path.name for path in paths), ("final_atlas.png",))
        self.assertTrue(paths[0].stat().st_size > 0)
        self.assertEqual(
            (
                tuple(layer.as_pointer() for layer in settings.layers),
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                tuple(image.as_pointer() for image in bpy.data.images),
            ),
            before,
        )
        validate_composite_session(bpy.context.scene)

    def test_temporary_final_bake_failure_restores_nodes_render_and_image_set(self) -> None:
        """최종 Bake 준비 뒤 오류가 나도 임시 Node·Image와 렌더 변경을 모두 정리합니다."""
        _target, setup = _prepare_backend("TextureExportBakeFailureTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        before = (
            tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
            tuple(image.as_pointer() for image in bpy.data.images),
            tuple(bool(layer.visible) for layer in settings.layers),
            bpy.context.scene.render.engine,
            bpy.context.scene.render.film_transparent,
            bpy.context.scene.render.bake.margin_type,
        )
        with patch.object(
            composite_module,
            "_set_composite_bake_samples",
            side_effect=CompositeLayerError("강제 Export Bake 준비 실패"),
        ):
            with self.assertRaisesRegex(CompositeLayerError, "강제 Export Bake"):
                composite_module.create_final_texture_export_image(bpy.context)
        self.assertEqual(
            (
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                tuple(image.as_pointer() for image in bpy.data.images),
                tuple(bool(layer.visible) for layer in settings.layers),
                bpy.context.scene.render.engine,
                bpy.context.scene.render.film_transparent,
                bpy.context.scene.render.bake.margin_type,
            ),
            before,
        )
        validate_composite_session(bpy.context.scene)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
