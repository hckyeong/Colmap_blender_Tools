"""Blender Background에서 Composite 작업 Layer Bake와 원본 복구를 검증합니다."""

from __future__ import annotations

from array import array
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import bpy


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import composite_layer as composite_module  # noqa: E402
from tools.projection_paint.blender import composite_progress as composite_progress_module  # noqa: E402
from tools.projection_paint.blender import layers as layers_module  # noqa: E402
from tools.projection_paint.blender import lifecycle as lifecycle_module  # noqa: E402
from tools.projection_paint.blender import progress_overlay as progress_overlay_module  # noqa: E402
from tools.projection_paint.blender import state_handlers as state_handlers_module  # noqa: E402
from tools.projection_paint.blender.composite_layer import (  # noqa: E402
    COMPOSITE_IMAGE_SESSION_KEY,
    CompositeLayerError,
    CompositeSession,
    create_composite_layer,
    release_composite_layer,
    validate_composite_session,
)
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    LAYER_MIX_NODE_ROLE,
    LAYER_UUID_KEY,
    ORIGINAL_GATE_NODE_ROLE,
    ORIGINAL_LAYER_SCHEMA_VERSION,
    TARGET_UUID_KEY,
    _graph_layers,
    reconcile_layer_graph,
    validate_layer_graph,
)
from tools.projection_paint.blender.layers import (  # noqa: E402
    PP_OT_composite_layer_create,
    _record_layer_ui_pointer_event,
)
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    pending_overlay_resolution_change,
    prepare_active_material,
    requires_udim_conversion,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    PaintSessionError,
    finish_paint_session,
    start_paint_session,
)
from tools.projection_paint.blender.paint_state import (  # noqa: E402
    commit_paint_setup,
)
from tools.projection_paint.blender.ownership import (  # noqa: E402
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from tools.projection_paint.blender.udim_setup import (  # noqa: E402
    new_tiled_paint_image,
)


def _new_target(
    name: str,
    *,
    multi_udim: bool = False,
    full_uv_coverage: bool = False,
):
    """하나 또는 두 UDIM 타일에 UV 면을 가진 작은 Composite Bake 대상을 만듭니다."""
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    if multi_udim and full_uv_coverage:
        mesh.from_pydata(
            (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (1.0, 1.0, 0.0),
                (0.0, 1.0, 0.0),
                (2.0, 0.0, 0.0),
                (3.0, 0.0, 0.0),
                (3.0, 1.0, 0.0),
                (2.0, 1.0, 0.0),
            ),
            (),
            ((0, 1, 2, 3), (4, 5, 6, 7)),
        )
        uv_values = (
            (0.0, 0.0),
            (1.0, 0.0),
            (1.0, 1.0),
            (0.0, 1.0),
            (1.0, 0.0),
            (2.0, 0.0),
            (2.0, 1.0),
            (1.0, 1.0),
        )
    elif multi_udim:
        mesh.from_pydata(
            (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (1.0, 1.0, 0.0),
                (2.0, 0.0, 0.0),
                (3.0, 0.0, 0.0),
                (3.0, 1.0, 0.0),
            ),
            (),
            ((0, 1, 2), (3, 4, 5)),
        )
        uv_values = (
            (0.0, 0.0),
            (1.0, 0.0),
            (1.0, 1.0),
            (1.0, 0.0),
            (2.0, 0.0),
            (2.0, 1.0),
        )
    else:
        mesh.from_pydata(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            (),
            ((0, 1, 2),),
        )
        uv_values = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop, uv in zip(uv_layer.data, uv_values):
        loop.uv = uv
    target = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new(f"{name}Material")
    material.use_nodes = True
    target.data.materials.append(material)
    return target


def _prepare_backend(
    name: str,
    *,
    multi_udim: bool = False,
    full_uv_coverage: bool = False,
):
    """요청한 UDIM 타일 구성을 가진 Paint Layer Graph와 Composite Bake 대상을 반환합니다."""
    scene = bpy.context.scene
    target = _new_target(
        name,
        multi_udim=multi_udim,
        full_uv_coverage=full_uv_coverage,
    )
    if multi_udim:
        source = new_tiled_paint_image(
            f"{name}Source",
            (32, 32),
            (1001, 1002),
            (0.0, 0.0, 0.0, 1.0),
        )
        material = target.active_material
        principled = next(
            node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        source_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        source_node.image = source
        material.node_tree.links.new(source_node.outputs["Color"], principled.inputs["Base Color"])
    setup = prepare_active_material(target, 32, 32, use_base_resolution=False)
    commit_paint_setup(scene, setup)
    from tools.projection_paint.blender.layer_shader import reconcile_layer_graph

    reconcile_layer_graph(scene, material=setup.material, overlay=setup.overlay)
    setup.commit()
    for layer in scene.projection_paint.layers:
        layer.ui_selected = True
    return target, setup


def _fill_image(image, color: tuple[float, float, float, float]) -> None:
    """테스트용 Paint Layer Image의 모든 픽셀을 식별 가능한 RGBA 색으로 채웁니다."""
    pixels = array("f", color) * (int(image.size[0]) * int(image.size[1]))
    image.pixels.foreach_set(pixels)
    image.update()


def _fill_image_tile(
    image,
    tile_number: int,
    color: tuple[float, float, float, float],
) -> None:
    """지정 UDIM 타일의 모든 픽셀을 식별 가능한 RGBA 색으로 채웁니다."""
    previous = image.tiles.active
    tile = image.tiles.get(int(tile_number))
    if tile is None:
        raise AssertionError(f"UDIM {tile_number} 타일을 찾을 수 없습니다.")
    try:
        image.tiles.active = tile
        _fill_image(image, color)
    finally:
        image.tiles.active = previous


def _tile_pixel_sum(image, tile_number: int, x: int, y: int) -> float:
    """지정 UDIM 타일의 한 픽셀 RGB 합계를 읽고 이전 활성 타일을 복구합니다."""
    previous = image.tiles.active
    tile = image.tiles.get(int(tile_number))
    if tile is None:
        raise AssertionError(f"UDIM {tile_number} 타일을 찾을 수 없습니다.")
    try:
        image.tiles.active = tile
        width = int(image.size[0])
        offset = ((int(y) * width) + int(x)) * 4
        return sum(image.pixels[offset : offset + 3])
    finally:
        image.tiles.active = previous


def _tile_pixel(image, tile_number: int, x: int, y: int) -> tuple[float, ...]:
    """지정 UDIM 타일의 한 픽셀 RGBA 값을 읽고 이전 활성 타일을 복구합니다."""
    previous = image.tiles.active
    tile = image.tiles.get(int(tile_number))
    if tile is None:
        raise AssertionError(f"UDIM {tile_number} 타일을 찾을 수 없습니다.")
    try:
        image.tiles.active = tile
        width = int(image.size[0])
        offset = ((int(y) * width) + int(x)) * 4
        return tuple(image.pixels[offset : offset + 4])
    finally:
        image.tiles.active = previous


def _tile_pixels(image, tile_number: int) -> tuple[float, ...]:
    """지정 UDIM 타일의 전체 RGBA Pixel을 읽고 이전 활성 타일을 복구합니다."""
    previous = image.tiles.active
    tile = image.tiles.get(int(tile_number))
    if tile is None:
        raise AssertionError(f"UDIM {tile_number} 타일을 찾을 수 없습니다.")
    try:
        image.tiles.active = tile
        return tuple(image.pixels[:])
    finally:
        image.tiles.active = previous


def _original_material_bake_target(target, name: str):
    """원본 슬롯에 활성 Source Image Texture와 다중 슬롯 경계 조건을 준비합니다."""
    original = target.material_slots[0].material
    tree = original.node_tree
    source = new_tiled_paint_image(
        f"{name}OriginalSource",
        (32, 32),
        (1001,),
        (0.0, 0.0, 0.0, 1.0),
    )
    _fill_image_tile(source, 1001, (0.15, 0.35, 0.65, 1.0))
    source_node = tree.nodes.new("ShaderNodeTexImage")
    source_node.name = "Original Source Bake Target"
    source_node.image = source
    for node in tree.nodes:
        node.select = False
    source_node.select = True
    tree.nodes.active = source_node
    target.data.materials.append(original)
    no_nodes_material = bpy.data.materials.new(f"{name}NoNodesMaterial")
    no_nodes_material.use_nodes = False
    target.data.materials.append(no_nodes_material)
    return original, source, source_node


def _node_selection_state(tree) -> tuple[tuple[int, bool], ...]:
    """Node Tree의 선택 상태 비교에 쓸 Snapshot을 반환합니다."""
    return tuple((node.as_pointer(), bool(node.select)) for node in tree.nodes)


def _mix_for(material, layer_uuid: str):
    """Layer UUID와 일치하는 유일한 관리 Mix Node를 반환합니다."""
    matches = tuple(
        node
        for node in material.node_tree.nodes
        if node.get("pp_record_kind") == LAYER_MIX_NODE_ROLE
        and str(node.get(LAYER_UUID_KEY, "") or "") == str(layer_uuid)
    )
    if len(matches) != 1:
        raise AssertionError("Layer Mix Node를 하나로 찾을 수 없습니다.")
    return matches[0]


class _FakeCyclesPreferences:
    """GPU 선택 정책을 Blender 장치 유무와 분리해 검사하는 간단한 환경설정 대역입니다."""

    def __init__(self, devices, compute_device_type: str = "NONE"):
        """지정한 장치 목록과 초기 Backend를 보관합니다."""
        self.devices = list(devices)
        self.compute_device_type = compute_device_type

    def get_devices(self) -> None:
        """실제 Cycles API의 장치 새로고침 호출을 부작용 없이 흉내 냅니다."""


class _FailingOptixPreferences(_FakeCyclesPreferences):
    """OPTIX Backend 설정 오류가 CPU 폴백을 깨지 않는지 검사하는 대역입니다."""

    @property
    def compute_device_type(self) -> str:
        """현재 대역 Backend 값을 반환합니다."""

        return self._compute_device_type

    @compute_device_type.setter
    def compute_device_type(self, value: str) -> None:
        """OPTIX 설정만 실패시키고 다른 복구 값은 허용합니다."""

        if value == "OPTIX":
            raise RuntimeError("OPTIX를 사용할 수 없습니다.")
        self._compute_device_type = value


def _cycles_runtime_snapshot(scene):
    """실제 Blender Cycles Scene·Samples·환경설정 장치 상태를 비교 가능한 값으로 읽습니다."""
    preferences = composite_module._cycles_preferences()
    if preferences is None:
        return (
            str(scene.render.engine),
            str(scene.cycles.device),
            int(scene.cycles.samples),
            None,
            (),
        )
    devices = composite_module._cycles_devices(preferences)
    return (
        str(scene.render.engine),
        str(scene.cycles.device),
        int(scene.cycles.samples),
        str(preferences.compute_device_type),
        tuple(
            (str(device.name), str(device.type), bool(device.use))
            for device in devices
        ),
    )


def _layer_for_uuid(settings, layer_uuid: str):
    """CollectionProperty 이동 뒤 UUID가 같은 현재 Layer 항목을 다시 찾습니다."""
    layer = next(
        (
            candidate
            for candidate in settings.layers
            if str(candidate.layer_uuid) == str(layer_uuid)
        ),
        None,
    )
    if layer is None:
        raise AssertionError(f"Layer UUID {layer_uuid}를 찾을 수 없습니다.")
    return layer


class _FakeCompositeWindowManager:
    """GUI Composite 실행 순서를 검사하는 최소 Window Manager 대역입니다."""

    def __init__(self):
        """등록된 Timer·Modal·상태 막대 호출을 빈 상태로 시작합니다."""
        self.timers = []
        self.modal_operators = []
        self.progress_calls = []
        self.confirm_calls = []

    def event_timer_add(self, _interval, *, window):
        """후처리 Modal이 소유할 식별 가능한 Timer를 등록합니다."""
        timer = SimpleNamespace(window=window)
        self.timers.append(timer)
        return timer

    def event_timer_remove(self, timer):
        """정리 단계가 등록한 Timer를 정확히 제거합니다."""
        if timer in self.timers:
            self.timers.remove(timer)

    def modal_handler_add(self, operator):
        """Bake 뒤에 등록되는 Composite 후처리 Modal을 기록합니다."""
        self.modal_operators.append(operator)

    def invoke_props_dialog(self, operator, **kwargs):
        """재합성 확인창의 사용자 안내 문구를 기록합니다."""
        self.confirm_calls.append((operator, None, kwargs))
        return {"RUNNING_MODAL"}

    def progress_begin(self, minimum, maximum):
        """진행 상태 막대 시작 호출을 기록합니다."""
        self.progress_calls.append(("begin", minimum, maximum))

    def progress_update(self, value):
        """진행 상태 막대 갱신 호출을 기록합니다."""
        self.progress_calls.append(("update", value))

    def progress_end(self):
        """진행 상태 막대 종료 호출을 기록합니다."""
        self.progress_calls.append(("end",))


class _FakeCompositeWindow:
    """GUI 입력 추적기가 요구하는 Window Pointer 계약만 제공합니다."""

    def as_pointer(self):
        """테스트 인스턴스를 구분하는 안정적인 비영점 Pointer 값을 반환합니다."""
        return id(self)


class CompositeLayerBackgroundTests(unittest.TestCase):
    """Cycles Bake와 Composite 활성·해제의 데이터 계약을 실제 Blender에서 검사합니다."""

    @classmethod
    def setUpClass(cls):
        """Composite Operator를 포함한 Extension 클래스를 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls):
        """테스트가 등록한 Extension 클래스를 해제합니다."""
        addon.unregister()

    def setUp(self):
        """각 Composite 테스트를 빈 Blender Factory Scene으로 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_native_progress_uses_common_activity_details_without_percent(self):
        """Native 진행창은 백분율 없이 Process와 장치·Image·Bake 정보를 표시합니다."""
        session = composite_progress_module.CompositeProgressSession(
            session_id="native-progress-test",
            started_at=0.0,
            total_steps=7,
            memory_bytes=256 * 1024 * 1024,
        )
        image = SimpleNamespace(source="FILE", size=(8192, 8192), tiles=(object(),))

        composite_progress_module.begin_composite_native_bake(
            session,
            phase="Native Processing 1/1",
            detail="새 Composite를 Cycles로 계산하고 Blender Image로 변환 중",
            device_detail="Cycles OPTIX GPU",
            image=image,
            sample_count=32,
        )
        view = composite_progress_module.composite_progress_view(session)

        self.assertTrue(view.indeterminate)
        self.assertEqual(view.phase, "Native Processing 1/1")
        self.assertIn(view.activity_text, {"Process.", "Process..", "Process...", "Process...."})
        self.assertEqual(
            progress_overlay_module.progress_overlay_height(view),
            progress_overlay_module.ACTIVITY_OVERLAY_HEIGHT,
        )
        self.assertLess(
            progress_overlay_module.ACTIVITY_OVERLAY_HEIGHT,
            progress_overlay_module.OVERLAY_HEIGHT,
        )
        self.assertIn("Cycles", view.detail_lines[1])
        self.assertNotIn("1/1", view.detail_lines[0])
        self.assertNotIn("100%", " ".join(view.detail_lines))
        self.assertIn("Image:", view.detail_lines[2])
        self.assertIn("Bake: EMIT", view.detail_lines[2])
        self.assertIn("Samples 32", view.detail_lines[2])

    def test_composite_process_label_repeats_one_to_four_dots(self):
        """Process 문구는 0.4초마다 점 한 개부터 네 개까지 늘어난 뒤 반복합니다."""
        labels = tuple(
            composite_progress_module.composite_process_label(elapsed)
            for elapsed in (0.0, 0.4, 0.8, 1.2, 1.6, 2.0)
        )

        self.assertEqual(
            labels,
            (
                "Process.",
                "Process..",
                "Process...",
                "Process....",
                "Process.",
                "Process..",
            ),
        )

    def test_native_job_stop_marks_image_conversion_complete_before_commit(self):
        """Native Job 종료 틱은 결과 적용 전에 이미지 변환 완료 단계를 표시합니다."""
        session = composite_progress_module.CompositeProgressSession(
            session_id="native-stop-test",
            started_at=0.0,
            total_steps=7,
            indeterminate=True,
        )
        job = SimpleNamespace(
            phase="NATIVE_BAKING",
            native_bake_stage="COMPOSITE",
            native_bake_job_stopped=False,
            native_bake_outcome="COMPLETE",
            progress=session,
            kind="CREATE",
            reuse_old_composite_image=False,
        )

        composite_module.mark_native_bake_job_stopped(job)

        self.assertTrue(job.native_bake_job_stopped)
        self.assertFalse(session.indeterminate)
        self.assertEqual(session.completed_steps, 3)
        self.assertEqual(session.phase, "Image Ready 1/1")
        self.assertIn("이미지 변환이 완료", session.detail)

    def test_cycles_gpu_selection_prefers_optix_and_disables_cpu_hybrid(self):
        """GPU 자동 선택은 OPTIX를 우선하고 CPU 장치 혼합 사용을 끕니다."""
        cpu = SimpleNamespace(name="CPU", type="CPU", use=True)
        cuda = SimpleNamespace(name="RTX CUDA", type="CUDA", use=True)
        optix = SimpleNamespace(name="RTX OPTIX", type="OPTIX", use=False)
        preferences = _FakeCyclesPreferences((cpu, cuda, optix), "CUDA")
        scene = SimpleNamespace(cycles=SimpleNamespace(device="CPU"))
        state = composite_module._capture_cycles_bake_device_state(
            scene,
            preferences=preferences,
        )

        detail = composite_module._configure_cycles_bake_device(scene, state)

        self.assertIn("OPTIX", detail)
        self.assertEqual(scene.cycles.device, "GPU")
        self.assertEqual(preferences.compute_device_type, "OPTIX")
        self.assertFalse(cpu.use)
        self.assertFalse(cuda.use)
        self.assertTrue(optix.use)
        composite_module._restore_cycles_bake_device_state(scene, state)
        composite_module._restore_cycles_bake_device_state(scene, state)
        self.assertEqual(scene.cycles.device, "CPU")
        self.assertEqual(preferences.compute_device_type, "CUDA")
        self.assertTrue(cpu.use)
        self.assertTrue(cuda.use)
        self.assertFalse(optix.use)

    def test_source_partition_uses_selected_span_and_visible_bake_snapshot(self):
        """선택 경계 사이 Layer는 Source에 남기고 선택·눈 ON만 Bake 대상으로 확정합니다."""
        layers = tuple(
            SimpleNamespace(
                layer_uuid=name,
                ui_selected=selected,
                visible=visible,
            )
            for name, selected, visible in (
                ("4", True, True),
                ("3", False, True),
                ("2", True, False),
                ("1", False, True),
            )
        )

        partition = composite_module.composite_source_partition(layers)

        self.assertEqual(partition.normal_layer_uuids, ("4", "3", "2", "1"))
        self.assertEqual(partition.source_layer_uuids, ("4", "3", "2"))
        self.assertEqual(partition.bake_included_layer_uuids, ("4",))
        self.assertEqual(partition.source_insert_index, 0)

    def test_composite_session_contains_only_current_internal_values(self):
        """CompositeSession은 세션 UUID·Composite·Source 세 값만 보관합니다."""
        _target, setup = _prepare_backend("CompositeCompatibilityShimTarget")
        settings = bpy.context.scene.projection_paint
        session = CompositeSession("session", settings.layers[0], (settings.layers[0],))
        self.assertEqual(session.session_uuid, "session")
        self.assertEqual(session.composite.as_pointer(), settings.layers[0].as_pointer())
        self.assertEqual(
            tuple(source.as_pointer() for source in session.sources),
            (settings.layers[0].as_pointer(),),
        )
        self.assertEqual(tuple(session.__slots__), ("session_uuid", "composite", "sources"))
        with self.assertRaisesRegex(CompositeLayerError, "눈이 켜진"):
            composite_module.composite_source_partition(
                tuple(
                    SimpleNamespace(
                        layer_uuid=name,
                        ui_selected=True,
                        visible=False,
                    )
                    for name in ("A", "B")
                )
            )

    def test_composite_bake_margin_scales_with_actual_tile_resolution(self):
        """Composite Margin은 UDIM 수가 아니라 각 타일의 실제 해상도에 비례합니다."""
        def fake_image(*sizes):
            """Image Pointer와 Paint Image 타일 크기 계약을 가진 테스트 대역을 만듭니다."""
            return SimpleNamespace(
                as_pointer=lambda: 1,
                source="TILED",
                size=sizes[0],
                tiles=tuple(
                    SimpleNamespace(number=1001 + index, size=size)
                    for index, size in enumerate(sizes)
                ),
            )

        for size, expected in (
            ((512, 512), 2),
            ((1024, 1024), 4),
            ((2048, 2048), 8),
            ((4096, 4096), 16),
            ((8192, 8192), 32),
            ((32768, 32768), 64),
        ):
            with self.subTest(size=size):
                self.assertEqual(
                    composite_module._composite_bake_margin(fake_image(size)),
                    expected,
                )
        self.assertEqual(
            composite_module._composite_bake_margin(fake_image((4096, 2048))),
            16,
        )
        self.assertEqual(
            composite_module._composite_bake_margin(
                fake_image((8192, 8192), (8192, 8192))
            ),
            32,
        )
        with self.assertRaisesRegex(CompositeLayerError, "서로 다릅니다"):
            composite_module._composite_bake_margin(
                fake_image((2048, 2048), (4096, 4096))
            )
        with self.assertRaisesRegex(CompositeLayerError, "유효하지 않습니다"):
            composite_module._composite_bake_margin(fake_image((0, 2048)))

    def test_partial_source_composite_preserves_external_graph_and_release_state(self):
        """부분 Source만 Bake·치환하고 중간·외부 Layer의 상태와 순서를 보존합니다."""
        _target, setup = _prepare_backend("CompositePartialSourceTarget")
        settings = bpy.context.scene.projection_paint
        for _unused in range(3):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        normal_before = tuple(
            layer
            for layer in settings.layers
            if str(layer.layer_role) == "NORMAL"
        )
        self.assertEqual(len(normal_before), 5)
        for layer in normal_before:
            layer.ui_selected = False
            layer.visible = True
        external_top, source_top, source_middle, source_bottom, external_bottom = normal_before
        source_top.ui_selected = True
        source_bottom.ui_selected = True
        source_bottom.visible = False
        _fill_image(source_top.image, (0.9, 0.1, 0.0, 1.0))
        _fill_image(source_middle.image, (0.0, 0.9, 0.1, 1.0))
        _fill_image(source_bottom.image, (0.0, 0.1, 0.9, 1.0))
        expected_normal_order = tuple(str(layer.layer_uuid) for layer in normal_before)
        expected_source_uuids = expected_normal_order[1:4]
        source_middle_uuid = str(source_middle.layer_uuid)
        source_bottom_uuid = str(source_bottom.layer_uuid)
        external_uuids = (
            str(external_top.layer_uuid),
            str(external_bottom.layer_uuid),
        )

        composite_image = create_composite_layer(bpy.context)

        session_uuid = str(settings.active_composite_session_uuid)
        source_after = tuple(
            layer
            for layer in settings.layers
            if str(getattr(layer, "composite_source_session_uuid", "") or "")
            == session_uuid
        )
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in source_after),
            expected_source_uuids,
        )
        self.assertEqual(
            tuple(bool(layer.composite_bake_included) for layer in source_after),
            (True, False, False),
        )
        self.assertEqual(
            tuple(bool(layer.composite_saved_visible) for layer in source_after),
            (True, True, False),
        )
        self.assertTrue(all(not bool(layer.visible) for layer in source_after))
        external_top_after = _layer_for_uuid(settings, external_uuids[0])
        external_bottom_after = _layer_for_uuid(settings, external_uuids[1])
        self.assertTrue(external_top_after.visible)
        self.assertTrue(external_bottom_after.visible)
        graph_layers = _graph_layers(settings, tuple(settings.layers))
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in graph_layers),
            (
                external_uuids[0],
                str(settings.composite_layer_uuid),
                external_uuids[1],
            ),
        )
        red, green, blue, alpha = _tile_pixel(composite_image, 1001, 24, 8)
        self.assertGreater(red, 0.5)
        self.assertLess(green, 0.2)
        self.assertLess(blue, 0.2)
        self.assertGreater(alpha, 0.9)
        external_bottom_after.visible = False
        self.assertEqual(_mix_for(setup.material, external_uuids[1]).inputs[0].default_value, 0.0)
        external_bottom_after.visible = True

        release_composite_layer(bpy.context)

        normal_after = tuple(
            layer
            for layer in settings.layers
            if str(layer.layer_role) == "NORMAL"
        )
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in normal_after),
            expected_normal_order,
        )
        restored_middle = _layer_for_uuid(settings, source_middle_uuid)
        restored_bottom = _layer_for_uuid(settings, source_bottom_uuid)
        self.assertTrue(restored_middle.visible)
        self.assertFalse(restored_bottom.visible)
        self.assertTrue(all(not str(layer.composite_source_session_uuid) for layer in normal_after))
        self.assertTrue(all(not bool(layer.composite_bake_included) for layer in normal_after))
        self.assertTrue(
            all(
                not bool(_layer_for_uuid(settings, layer_uuid).composite_saved_visible)
                for layer_uuid in expected_source_uuids
            )
        )
        validate_layer_graph(setup.material, expected_normal_order, expected_original_base=False)

    def test_composite_bake_margin_fills_uv_gutter_with_source_color(self):
        """Composite Bake는 UV 경계 바깥 여백을 투명 검정 대신 Source 색으로 채웁니다."""
        _target, _setup = _prepare_backend("CompositeMarginPixelTarget")
        settings = bpy.context.scene.projection_paint
        settings.composite_include_alpha = True
        for layer in settings.layers:
            _fill_image(layer.image, (0.8, 0.2, 0.1, 1.0))
            layer.ui_selected = True
            layer.visible = True

        composite_image = create_composite_layer(bpy.context)

        inside = _tile_pixel(composite_image, 1001, 16, 16)
        gutter = _tile_pixel(composite_image, 1001, 15, 16)
        outside = _tile_pixel(composite_image, 1001, 14, 16)
        self.assertGreater(inside[3], 0.9)
        self.assertGreater(gutter[0], 0.5)
        self.assertGreater(gutter[3], 0.9)
        self.assertLess(outside[3], 0.1)

    def test_cycles_device_restore_uses_id_when_same_gpu_order_changes(self):
        """동일 모델 GPU 순서가 바뀌어도 Cycles 장치 ID별 사용 상태를 복구합니다."""
        cpu = SimpleNamespace(name="CPU", type="CPU", use=True)
        first_gpu = SimpleNamespace(
            name="동일 모델 GPU",
            type="CUDA",
            id="PCI:0000:01:00",
            use=True,
        )
        second_gpu = SimpleNamespace(
            name="동일 모델 GPU",
            type="CUDA",
            id="PCI:0000:02:00",
            use=False,
        )
        preferences = _FakeCyclesPreferences((cpu, first_gpu, second_gpu), "CUDA")
        scene = SimpleNamespace(cycles=SimpleNamespace(device="CPU"))
        state = composite_module._capture_cycles_bake_device_state(
            scene,
            preferences=preferences,
        )

        preferences.devices = [cpu, second_gpu, first_gpu]
        first_gpu.use = False
        second_gpu.use = True
        composite_module._restore_cycles_bake_device_state(scene, state)

        self.assertTrue(first_gpu.use)
        self.assertFalse(second_gpu.use)

    def test_cycles_gpu_absence_or_backend_failure_falls_back_to_cpu(self):
        """GPU가 없거나 Backend 설정이 실패해도 Composite Bake는 CPU 폴백을 선택합니다."""
        cpu = SimpleNamespace(name="CPU", type="CPU", use=True)
        scene = SimpleNamespace(cycles=SimpleNamespace(device="GPU"))
        preferences = _FakeCyclesPreferences((cpu,))
        state = composite_module._capture_cycles_bake_device_state(
            scene,
            preferences=preferences,
        )
        detail = composite_module._configure_cycles_bake_device(scene, state)
        self.assertIn("CPU", detail)
        self.assertEqual(scene.cycles.device, "CPU")
        self.assertTrue(cpu.use)
        composite_module._restore_cycles_bake_device_state(scene, state)
        self.assertEqual(scene.cycles.device, "GPU")

        optix = SimpleNamespace(name="RTX OPTIX", type="OPTIX", use=False)
        failed_preferences = _FailingOptixPreferences((cpu, optix))
        failed_scene = SimpleNamespace(cycles=SimpleNamespace(device="CPU"))
        failed_state = composite_module._capture_cycles_bake_device_state(
            failed_scene,
            preferences=failed_preferences,
        )
        detail = composite_module._configure_cycles_bake_device(
            failed_scene,
            failed_state,
        )
        self.assertIn("CPU", detail)
        self.assertEqual(failed_scene.cycles.device, "CPU")
        composite_module._restore_cycles_bake_device_state(
            failed_scene,
            failed_state,
        )
        self.assertEqual(failed_preferences.compute_device_type, "NONE")

    def test_cycles_bake_preparation_failure_restores_actual_runtime_state(self):
        """GPU 설정 뒤 Bake 준비가 실패해도 실제 Cycles 환경설정이 원래대로 복구됩니다."""
        _target, setup = _prepare_backend("CompositeCyclesPreparationFailureTarget")
        bpy.context.scene.render.bake.margin_type = "EXTEND"
        runtime_before = _cycles_runtime_snapshot(bpy.context.scene)
        image = composite_module.create_composite_layer_image(
            bpy.context.scene,
            "cycles-preparation-failure",
        )
        with patch.object(
            composite_module,
            "_activate_bake_target",
            side_effect=CompositeLayerError("강제 Bake 준비 실패"),
        ):
            with self.assertRaisesRegex(CompositeLayerError, "강제 Bake 준비 실패"):
                composite_module._prepare_final_base_color_bake(
                    bpy.context,
                    image,
                )

        self.assertEqual(_cycles_runtime_snapshot(bpy.context.scene), runtime_before)
        self.assertEqual(bpy.context.scene.render.bake.margin_type, "EXTEND")
        self.assertIsNotNone(setup.material)

    def test_bake_preparation_failure_restores_original_material_node_state(self):
        """Bake 준비 실패도 원본 Material의 활성 Source Image 선택 상태를 되돌립니다."""
        target, _setup = _prepare_backend("CompositeOriginalPreparationFailureTarget")
        original, source, source_node = _original_material_bake_target(
            target,
            "CompositeOriginalPreparationFailureTarget",
        )
        tree = original.node_tree
        expected_selection = _node_selection_state(tree)
        expected_pixel = _tile_pixel(source, 1001, 8, 8)
        composite_image = composite_module.create_composite_layer_image(
            bpy.context.scene,
            "original-preparation-failure",
        )

        with patch.object(
            composite_module,
            "_activate_bake_target",
            side_effect=CompositeLayerError("강제 Bake 준비 실패"),
        ):
            with self.assertRaisesRegex(CompositeLayerError, "강제 Bake 준비 실패"):
                composite_module._prepare_final_base_color_bake(
                    bpy.context,
                    composite_image,
                )

        self.assertEqual(tree.nodes.active.as_pointer(), source_node.as_pointer())
        self.assertEqual(_node_selection_state(tree), expected_selection)
        self.assertEqual(_tile_pixel(source, 1001, 8, 8), expected_pixel)

    def test_composite_bake_excludes_original_material_active_source_image(self):
        """Composite Bake가 원본 슬롯의 활성 Source Image 픽셀을 지우지 않는지 검증합니다."""
        target, setup = _prepare_backend("CompositeOriginalSourcePreserveTarget")
        original, source, source_node = _original_material_bake_target(
            target,
            "CompositeOriginalSourcePreserveTarget",
        )
        tree = original.node_tree
        expected_selection = _node_selection_state(tree)
        expected_pixel = _tile_pixel(source, 1001, 8, 8)

        create_composite_layer(bpy.context)

        self.assertEqual(tree.nodes.active.as_pointer(), source_node.as_pointer())
        self.assertEqual(_node_selection_state(tree), expected_selection)
        self.assertEqual(_tile_pixel(source, 1001, 8, 8), expected_pixel)

        release_composite_layer(bpy.context)

        self.assertEqual(tree.nodes.active.as_pointer(), source_node.as_pointer())
        self.assertEqual(_node_selection_state(tree), expected_selection)
        self.assertEqual(_tile_pixel(source, 1001, 8, 8), expected_pixel)
        validate_layer_graph(
            setup.material,
            tuple(
                str(layer.layer_uuid)
                for layer in bpy.context.scene.projection_paint.layers
            ),
        )

    def test_create_bakes_udim_and_bypasses_source_chain(self):
        """Composite 생성이 실제 Bake Image와 원본에서 분리된 최종 Chain을 만듭니다."""
        target, setup = _prepare_backend("CompositeCreateTarget")
        settings = bpy.context.scene.projection_paint
        source = settings.layers[0]
        source_uuid = str(source.layer_uuid)
        source_image = source.image
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        for candidate in settings.layers:
            if str(candidate.layer_role) == "NORMAL":
                candidate.ui_selected = True
        source_uuids = tuple(
            str(layer.layer_uuid)
            for layer in settings.layers
            if str(layer.layer_role) == "NORMAL"
        )
        _fill_image(
            _layer_for_uuid(settings, source_uuid).image,
            (0.25, 0.5, 0.75, 1.0),
        )
        cycles_before = _cycles_runtime_snapshot(bpy.context.scene)

        composite_image = create_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertEqual(len(settings.layers), 1 + len(source_uuids))
        composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        source_after_create = _layer_for_uuid(settings, source_uuid)
        normal_sources = tuple(
            layer
            for layer in settings.layers
            if str(layer.layer_role) == "NORMAL"
        )
        self.assertEqual(composite.layer_role, "COMPOSITE")
        self.assertIs(composite.image, composite_image)
        self.assertTrue(composite.visible)
        self.assertEqual(source_after_create.layer_role, "NORMAL")
        self.assertFalse(source_after_create.visible)
        self.assertIs(source_after_create.image, source_image)
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in normal_sources),
            source_uuids,
        )
        self.assertTrue(all(not layer.visible for layer in normal_sources))
        self.assertEqual(
            str(composite.image.get(COMPOSITE_IMAGE_SESSION_KEY, "")),
            str(settings.active_composite_session_uuid),
        )
        self.assertEqual(tuple(tile.number for tile in composite_image.tiles), (1001,))
        sample_index = ((8 * int(composite_image.size[0])) + 24) * 4
        self.assertGreater(
            sum(composite_image.pixels[sample_index : sample_index + 3]),
            0.1,
        )
        validate_composite_session(bpy.context.scene)
        base = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        ).inputs["Base Color"]
        self.assertEqual(
            base.links[0].from_node.as_pointer(),
            _mix_for(setup.material, composite.layer_uuid).as_pointer(),
        )
        self.assertNotEqual(
            _mix_for(setup.material, source_uuid).outputs["Color"].as_pointer(),
            base.links[0].from_socket.as_pointer(),
        )
        self.assertEqual(_cycles_runtime_snapshot(bpy.context.scene), cycles_before)

    def test_active_composite_reentry_uses_only_active_output_chain(self):
        """Composite Source 보관 Chain이 분리돼도 재진입 계약 검증은 활성 Chain만 사용합니다."""
        target, setup = _prepare_backend("CompositeReentryValidationTarget")
        settings = bpy.context.scene.projection_paint
        source_uuid = str(settings.layers[0].layer_uuid)
        create_composite_layer(bpy.context)
        composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        base = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        ).inputs["Base Color"]
        source_mix = _mix_for(setup.material, source_uuid)

        self.assertFalse(requires_udim_conversion(target))
        self.assertIsNone(
            pending_overlay_resolution_change(
                target,
                32,
                32,
                use_base_resolution=False,
            )
        )
        session = start_paint_session(
            bpy.context,
            target,
            width=32,
            height=32,
            use_base_resolution=False,
        )

        self.assertIs(session.setup.material, setup.material)
        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertEqual(
            base.links[0].from_node.as_pointer(),
            _mix_for(setup.material, composite.layer_uuid).as_pointer(),
        )
        self.assertNotEqual(
            source_mix.outputs["Color"].as_pointer(),
            base.links[0].from_socket.as_pointer(),
        )
        finish_paint_session(bpy.context)

    def test_composite_transition_state_blocks_paint_reentry_without_reconciliation(self):
        """처리 중·손상 Composite 상태는 Source Chain을 바꾸지 않고 Paint 진입을 차단합니다."""
        target, setup = _prepare_backend("CompositeTransitionEntryTarget")
        settings = bpy.context.scene.projection_paint
        source_uuid = str(settings.layers[0].layer_uuid)
        create_composite_layer(bpy.context)
        base = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        ).inputs["Base Color"]
        source_mix = _mix_for(setup.material, source_uuid)
        source_output = source_mix.outputs["Color"].as_pointer()
        base_input = base.links[0].from_socket.as_pointer()

        for state in ("CREATING", "RELEASING", "STALE"):
            settings.composite_state = state
            with self.assertRaisesRegex(PaintSessionError, "Composite 처리 중이거나 손상된 상태"):
                start_paint_session(
                    bpy.context,
                    target,
                    width=32,
                    height=32,
                    use_base_resolution=False,
                )
            self.assertEqual(source_mix.outputs["Color"].as_pointer(), source_output)
            self.assertEqual(base.links[0].from_socket.as_pointer(), base_input)

        settings.composite_state = "ACTIVE"

    def test_direct_release_preserves_independent_normal_layer_and_clone_settings(self):
        """직접 해제는 독립 일반 Layer의 UUID·Image·Pixel과 외부 Clone 설정을 보존합니다."""
        _target, setup = _prepare_backend("CompositeReleaseTarget")
        settings = bpy.context.scene.projection_paint
        initial_source_uuids = tuple(str(layer.layer_uuid) for layer in settings.layers)
        source_uuid = str(settings.layers[0].layer_uuid)
        source_image = settings.layers[0].image
        image_paint = bpy.context.scene.tool_settings.image_paint
        external_clone = bpy.data.images.new("ExternalClone", width=2, height=2)
        image_paint.clone_image = external_clone
        image_paint.use_clone_layer = True
        clone_slot_before = int(setup.material.paint_clone_slot)
        create_composite_layer(bpy.context)
        composite_image = settings.layers[0].image
        composite_image_name = str(composite_image.name)

        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        independent = settings.layers[0]
        independent_uuid = str(independent.layer_uuid)
        independent_image = independent.image
        _fill_image(independent_image, (0.125, 0.25, 0.5, 1.0))
        independent_pixels = tuple(independent_image.pixels[:])
        self.assertEqual(independent.layer_role, "NORMAL")
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=independent_uuid
            ),
            {"FINISHED"},
        )
        independent_mask = independent.mask.image

        release_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(len(settings.layers), len(initial_source_uuids) + 1)
        self.assertTrue(
            set(initial_source_uuids).issubset(
                {str(layer.layer_uuid) for layer in settings.layers}
            )
        )
        source_after_release = _layer_for_uuid(settings, source_uuid)
        self.assertIs(source_after_release.image, source_image)
        self.assertEqual(source_after_release.layer_role, "NORMAL")
        self.assertTrue(source_after_release.visible)
        self.assertIsNone(bpy.data.images.get(composite_image_name))
        independent_after = _layer_for_uuid(settings, independent_uuid)
        self.assertIs(independent_after.image, independent_image)
        self.assertIs(independent_after.mask.image, independent_mask)
        self.assertEqual(tuple(independent_after.image.pixels[:]), independent_pixels)
        self.assertIs(image_paint.clone_image, external_clone)
        self.assertTrue(image_paint.use_clone_layer)
        self.assertEqual(int(setup.material.paint_clone_slot), clone_slot_before)
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )

    def test_release_refuses_mismatched_composite_image_session(self):
        """소유 Image의 Composite UUID가 달라지면 파생 Layer를 삭제하지 않고 중단합니다."""
        _target, _setup = _prepare_backend("CompositeFailClosedTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        composite = settings.layers[0]
        composite.image[COMPOSITE_IMAGE_SESSION_KEY] = "wrong-session"

        with self.assertRaises(CompositeLayerError):
            release_composite_layer(bpy.context)

        self.assertEqual(len(settings.layers), 3)
        self.assertEqual(settings.composite_state, "STALE")

    def test_failed_create_restores_existing_clone_settings(self):
        """베이크 실패가 Composite 전 Clone 설정과 원본 가시성을 정확히 되돌리는지 검증합니다."""
        _target, setup = _prepare_backend("CompositeRollbackTarget")
        settings = bpy.context.scene.projection_paint
        source = settings.layers[0]
        original_clone = bpy.data.images.new("ExistingClone", width=1, height=1)
        image_paint = bpy.context.scene.tool_settings.image_paint
        image_paint.clone_image = original_clone
        image_paint.use_clone_layer = True
        original_clone_slot = int(setup.material.paint_clone_slot)

        with patch.object(
            composite_module,
            "_bake_source_rgba",
            side_effect=CompositeLayerError("강제 Bake 실패"),
        ):
            with self.assertRaises(CompositeLayerError):
                create_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(len(settings.layers), 2)
        self.assertTrue(source.visible)
        self.assertEqual(image_paint.clone_image.as_pointer(), original_clone.as_pointer())
        self.assertTrue(image_paint.use_clone_layer)
        self.assertEqual(int(setup.material.paint_clone_slot), original_clone_slot)

    def test_sync_bake_uses_exec_default_without_progress_overlay(self):
        """동기 GPU Bake 호출 전에는 Composite 진행 Overlay와 Timer를 만들지 않는지 검증합니다."""
        _target, _setup = _prepare_backend("CompositeSyncBakeTarget")
        bpy.context.scene.cycles.samples = 4096
        bpy.context.scene.render.bake.margin_type = "EXTEND"
        job = composite_module.start_composite_layer_creation(bpy.context)
        complete_handlers_before = tuple(bpy.app.handlers.object_bake_complete)
        cancel_handlers_before = tuple(bpy.app.handlers.object_bake_cancel)
        calls = []
        try:
            composite_module.prepare_composite_image(job)

            def fake_bake(*args, **kwargs):
                """Bake 호출 형식과 호출 시점의 Samples를 기록하고 즉시 성공합니다."""
                calls.append(
                    (
                        args,
                        kwargs,
                        int(bpy.context.scene.cycles.samples),
                        str(bpy.context.scene.render.bake.margin_type),
                    )
                )
                return {"FINISHED"}

            fake_bpy = SimpleNamespace(
                context=bpy.context,
                ops=SimpleNamespace(
                    object=SimpleNamespace(
                        bake=fake_bake,
                        mode_set=lambda **_kwargs: {"FINISHED"},
                    )
                ),
            )
            with patch.object(composite_module, "bpy", fake_bpy):
                composite_module._bake_final_base_color(
                    bpy.context,
                    job.composite_image,
                    job=job,
                )

            self.assertEqual(calls[0][0], ("EXEC_DEFAULT",))
            self.assertEqual(calls[0][2], 1)
            self.assertEqual(calls[0][1]["margin"], 2)
            self.assertEqual(calls[0][3], "ADJACENT_FACES")
            self.assertEqual(bpy.context.scene.cycles.samples, 4096)
            self.assertEqual(bpy.context.scene.render.bake.margin_type, "EXTEND")
            self.assertIsNone(job.progress)
            self.assertEqual(job.phase, "BAKED")
            self.assertEqual(composite_progress_module.composite_progress_session_count(), 0)
            self.assertEqual(tuple(bpy.app.handlers.object_bake_complete), complete_handlers_before)
            self.assertEqual(tuple(bpy.app.handlers.object_bake_cancel), cancel_handlers_before)
        finally:
            if not job.committed and not job.rolled_back:
                composite_module._fail_composite_bake(
                    bpy.context,
                    job,
                    CompositeLayerError("테스트 동기 Bake 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_sync_bake_exception_restores_render_samples(self):
        """Bake 예외 뒤에도 기존 Cycles Render Samples를 정확히 복구하는지 검증합니다."""
        _target, _setup = _prepare_backend("CompositeSampleRollbackTarget")
        bpy.context.scene.cycles.samples = 4096
        bpy.context.scene.render.bake.margin_type = "EXTEND"
        job = composite_module.start_composite_layer_creation(bpy.context)
        observed_bake_state = []
        try:
            composite_module.prepare_composite_image(job)

            def failing_bake(*_args, **_kwargs):
                """Bake 순간 Samples를 기록한 뒤 강제 예외를 발생시킵니다."""
                observed_bake_state.append(
                    (
                        int(bpy.context.scene.cycles.samples),
                        str(bpy.context.scene.render.bake.margin_type),
                        int(_kwargs["margin"]),
                    )
                )
                raise RuntimeError("강제 Bake 예외")

            fake_bpy = SimpleNamespace(
                context=bpy.context,
                ops=SimpleNamespace(
                    object=SimpleNamespace(
                        bake=failing_bake,
                        mode_set=lambda **_kwargs: {"FINISHED"},
                    )
                ),
            )
            with patch.object(composite_module, "bpy", fake_bpy):
                with self.assertRaisesRegex(RuntimeError, "강제 Bake 예외"):
                    composite_module._bake_final_base_color(
                        bpy.context,
                        job.composite_image,
                        job=job,
                    )

            self.assertEqual(observed_bake_state, [(1, "ADJACENT_FACES", 2)])
            self.assertEqual(bpy.context.scene.cycles.samples, 4096)
            self.assertEqual(bpy.context.scene.render.bake.margin_type, "EXTEND")
        finally:
            if not job.committed and not job.rolled_back:
                composite_module._fail_composite_bake(
                    bpy.context,
                    job,
                    CompositeLayerError("테스트 Samples 롤백 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_native_bake_uses_margin_fixed_during_preparation(self):
        """Native Bake는 비동기 시작 전에 준비 상태에 고정한 Margin을 그대로 사용합니다."""
        calls = []

        def fake_bake(*args, **kwargs):
            """Native Bake 호출 인자를 기록하고 Modal 시작 성공을 반환합니다."""
            calls.append((args, kwargs))
            return {"RUNNING_MODAL"}

        job = SimpleNamespace(
            phase="NATIVE_BAKING",
            native_bake_stage="COMPOSITE",
            native_bake_started=False,
            bake_preparation=SimpleNamespace(bake_margin=32),
        )
        fake_bpy = SimpleNamespace(
            ops=SimpleNamespace(object=SimpleNamespace(bake=fake_bake)),
        )

        with patch.object(composite_module, "bpy", fake_bpy):
            composite_module.invoke_native_bake(SimpleNamespace(), job)

        self.assertEqual(calls[0][0], ("INVOKE_DEFAULT",))
        self.assertEqual(calls[0][1]["margin"], 32)
        self.assertTrue(job.native_bake_started)

    def test_gui_execute_starts_native_overlay_and_modal_before_bake_completion(self):
        """GUI는 Native Bake 시작 전에 Overlay·Timer·PASS_THROUGH Modal을 등록합니다."""
        _target, _setup = _prepare_backend("CompositeGuiPostBakeTarget")
        settings = bpy.context.scene.projection_paint
        window_manager = _FakeCompositeWindowManager()
        context = SimpleNamespace(
            scene=bpy.context.scene,
            view_layer=bpy.context.view_layer,
            window=_FakeCompositeWindow(),
            window_manager=window_manager,
        )
        operator = SimpleNamespace(_job=None, report=lambda *_args: None)
        observed = {}

        def fake_prepare_native(_context, job, stage):
            """실제 GPU 작업 없이 Native Bake 시작 직전 Runtime 상태만 기록합니다."""
            observed["progress"] = job.progress
            observed["timers"] = len(window_manager.timers)
            observed["modals"] = len(window_manager.modal_operators)
            observed["sessions"] = composite_progress_module.composite_progress_session_count()
            job.phase = "NATIVE_BAKING"
            job.native_bake_stage = str(stage)
            job.native_bake_outcome = "PENDING"

        def fake_invoke_native(_context, job):
            """Native Job 시작 성공과 동일한 RUNNING 신호만 기록합니다."""
            job.native_bake_started = True
            job.native_bake_outcome = "RUNNING"

        with patch.object(
            layers_module,
            "bpy",
            SimpleNamespace(app=SimpleNamespace(background=False)),
        ), patch.object(
            composite_module,
            "prepare_native_bake",
            side_effect=fake_prepare_native,
        ), patch.object(
            composite_module,
            "invoke_native_bake",
            side_effect=fake_invoke_native,
        ):
            self.assertEqual(
                PP_OT_composite_layer_create.execute(operator, context),
                {"RUNNING_MODAL"},
            )
            self.assertIs(observed["progress"], operator._job.progress)
            self.assertEqual(observed["timers"], 1)
            self.assertEqual(observed["modals"], 0)
            self.assertEqual(observed["sessions"], 1)
            self.assertEqual(len(window_manager.timers), 1)
            self.assertEqual(len(window_manager.modal_operators), 1)
            self.assertEqual(composite_progress_module.composite_progress_session_count(), 1)
            self.assertEqual(operator._job.phase, "NATIVE_BAKING")
            self.assertEqual(operator._job.native_bake_stage, "COLOR")
            self.assertEqual(
                PP_OT_composite_layer_create.modal(
                    operator,
                    context,
                    SimpleNamespace(type="ESC", value="PRESS"),
                ),
                {"PASS_THROUGH"},
            )
            operator._job.native_bake_outcome = "COMPLETE"
            observed["finalize_calls"] = 0

            def fake_finalize(_context, job):
                """Job 종료 다음 Timer에서만 결과 Image를 확정한 것처럼 기록합니다."""
                observed["finalize_calls"] += 1
                job.phase = "BAKED"
                return "COLOR"

            def fake_complete(_context, job):
                """실제 Graph 변경 없이 Modal 완료 상태만 기록합니다."""
                job.committed = True
                job.phase = "RESULT"

            with patch.object(
                composite_module,
                "finalize_native_bake",
                side_effect=fake_finalize,
            ), patch.object(
                composite_module,
                "_complete_composite_bake",
                side_effect=fake_complete,
            ):
                self.assertEqual(
                    PP_OT_composite_layer_create.modal(
                        operator,
                        context,
                        SimpleNamespace(type="TIMER", value="NOTHING"),
                    ),
                    {"RUNNING_MODAL"},
                )
                self.assertEqual(observed["finalize_calls"], 0)
                self.assertEqual(
                    PP_OT_composite_layer_create.modal(
                        operator,
                        context,
                        SimpleNamespace(type="TIMER", value="NOTHING"),
                    ),
                    {"FINISHED"},
                )
            self.assertEqual(observed["finalize_calls"], 1)
            self.assertEqual(window_manager.progress_calls, [])
            self.assertEqual(len(window_manager.timers), 0)
            self.assertEqual(composite_progress_module.composite_progress_session_count(), 0)

    def test_gui_bake_failure_rolls_back_without_post_bake_runtime_resources(self):
        """Bake 실패는 Modal·Overlay·Timer 없이 임시 Image·Node와 원본 상태를 복구합니다."""
        _target, setup = _prepare_backend("CompositeGuiBakeFailureTarget")
        settings = bpy.context.scene.projection_paint
        source = settings.layers[0]
        before_images = {image.as_pointer() for image in bpy.data.images}
        complete_handlers_before = tuple(bpy.app.handlers.object_bake_complete)
        cancel_handlers_before = tuple(bpy.app.handlers.object_bake_cancel)
        window_manager = _FakeCompositeWindowManager()
        context = SimpleNamespace(
            scene=bpy.context.scene,
            view_layer=bpy.context.view_layer,
            window=_FakeCompositeWindow(),
            window_manager=window_manager,
        )
        operator = SimpleNamespace(_job=None, report=lambda *_args: None)

        fake_bpy = SimpleNamespace(
            context=bpy.context,
            ops=SimpleNamespace(
                object=SimpleNamespace(
                    bake=lambda *_args, **_kwargs: {"CANCELLED"},
                    mode_set=lambda **_kwargs: {"FINISHED"},
                )
            ),
        )
        with patch.object(
            layers_module,
            "bpy",
            SimpleNamespace(app=SimpleNamespace(background=False)),
        ), patch.object(composite_module, "bpy", fake_bpy):
            self.assertEqual(
                PP_OT_composite_layer_create.execute(operator, context),
                {"CANCELLED"},
            )

        self.assertIsNone(operator._job)
        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(len(settings.layers), 2)
        self.assertTrue(source.visible)
        self.assertEqual({image.as_pointer() for image in bpy.data.images}, before_images)
        self.assertFalse(
            any(
                node.name.startswith("PP Composite Bake")
                for node in setup.material.node_tree.nodes
            )
        )
        self.assertEqual(len(window_manager.timers), 0)
        self.assertEqual(len(window_manager.modal_operators), 0)
        self.assertEqual(composite_progress_module.composite_progress_session_count(), 0)
        self.assertEqual(tuple(bpy.app.handlers.object_bake_complete), complete_handlers_before)
        self.assertEqual(tuple(bpy.app.handlers.object_bake_cancel), cancel_handlers_before)

    def test_recompose_confirmation_uses_neutral_action_label(self):
        """재사용 여부가 아직 고정되지 않은 확인창은 평면화한다고 단정하지 않습니다."""
        _target, _setup = _prepare_backend("CompositeNeutralRecomposeConfirmTarget")
        create_composite_layer(bpy.context)
        window_manager = _FakeCompositeWindowManager()
        context = SimpleNamespace(
            scene=bpy.context.scene,
            window_manager=window_manager,
        )
        operator = SimpleNamespace(report=lambda *_args: None)

        try:
            self.assertEqual(
                PP_OT_composite_layer_create.invoke(operator, context, object()),
                {"RUNNING_MODAL"},
            )
            self.assertEqual(len(window_manager.confirm_calls), 1)
            self.assertEqual(
                window_manager.confirm_calls[0][2]["confirm_text"],
                "Composite 다시 생성",
            )
            message = " ".join(operator._wrapped_confirm_lines)
            self.assertIn("기존 Source", message)
            self.assertIn("관련 리소스", message)
            self.assertIn("Blender Undo 기록", message)
            self.assertIn("예상 추가 메모리", message)
            self.assertNotIn("숨김 백업", message)
            self.assertNotIn("임시 파일", message)
        finally:
            release_composite_layer(bpy.context)

    def test_lifecycle_boundaries_keep_native_resources_until_modal_rollback(self):
        """Load·Undo·Redo 전에는 Native Bake 자원을 유지하고 종료 다음 틱에만 롤백합니다."""
        boundaries = (
            ("load", state_handlers_module.load_pre_handler),
            ("undo", state_handlers_module.undo_pre_handler),
            ("redo", state_handlers_module.redo_pre_handler),
        )
        for name, boundary in boundaries:
            with self.subTest(boundary=name):
                bpy.ops.wm.read_factory_settings(use_empty=True)
                lifecycle_module.lifecycle_registry().rebuild(
                    bpy.context.scene,
                    f"test_{name}_native_boundary_setup",
                )
                _target, setup = _prepare_backend(f"CompositeNative{name.title()}BoundaryTarget")
                settings = bpy.context.scene.projection_paint
                window_manager = _FakeCompositeWindowManager()
                context = SimpleNamespace(
                    scene=bpy.context.scene,
                    view_layer=bpy.context.view_layer,
                    window=_FakeCompositeWindow(),
                    window_manager=window_manager,
                )
                job = composite_module.start_composite_layer_creation(context)
                composite_module.start_composite_native_progress(context, job)
                timer = window_manager.event_timer_add(0.1, window=context.window)
                composite_progress_module.attach_composite_progress_timer(job.progress, timer)
                composite_module.prepare_composite_image(job)
                composite_module.prepare_native_bake(bpy.context, job, "COMPOSITE")
                composite_image_pointer = job.composite_image.as_pointer()
                temporary_nodes_before = tuple(
                    node.as_pointer()
                    for node in setup.material.node_tree.nodes
                    if node.name.startswith("PP Composite Bake")
                )
                complete_handlers_before = tuple(bpy.app.handlers.object_bake_complete)
                cancel_handlers_before = tuple(bpy.app.handlers.object_bake_cancel)
                operator = SimpleNamespace(_job=job, report=lambda *_args: None)

                boundary()

                self.assertTrue(job.abort_after_bake)
                self.assertFalse(job.rolled_back)
                self.assertEqual(job.phase, "NATIVE_BAKING")
                self.assertTrue(
                    any(image.as_pointer() == composite_image_pointer for image in bpy.data.images)
                )
                self.assertEqual(
                    tuple(
                        node.as_pointer()
                        for node in setup.material.node_tree.nodes
                        if node.name.startswith("PP Composite Bake")
                    ),
                    temporary_nodes_before,
                )
                self.assertEqual(tuple(bpy.app.handlers.object_bake_complete), complete_handlers_before)
                self.assertEqual(tuple(bpy.app.handlers.object_bake_cancel), cancel_handlers_before)
                self.assertEqual(len(window_manager.timers), 1)
                self.assertEqual(composite_progress_module.composite_progress_session_count(), 1)

                self.assertEqual(
                    PP_OT_composite_layer_create.modal(
                        operator,
                        context,
                        SimpleNamespace(type="TIMER", value="NOTHING"),
                    ),
                    {"RUNNING_MODAL"},
                )
                self.assertEqual(
                    PP_OT_composite_layer_create.modal(
                        operator,
                        context,
                        SimpleNamespace(type="TIMER", value="NOTHING"),
                    ),
                    {"CANCELLED"},
                )
                self.assertTrue(job.rolled_back)
                self.assertEqual(settings.composite_state, "NONE")
                self.assertFalse(
                    any(image.as_pointer() == composite_image_pointer for image in bpy.data.images)
                )
                self.assertFalse(
                    any(
                        node.name.startswith("PP Composite Bake")
                        for node in setup.material.node_tree.nodes
                    )
                )
                self.assertEqual(len(window_manager.timers), 0)
                self.assertEqual(composite_progress_module.composite_progress_session_count(), 0)
                lifecycle_module.lifecycle_registry().rebuild(
                    bpy.context.scene,
                    f"test_{name}_native_rollback",
                )

    def test_extension_unregistration_waits_for_native_modal_rollback(self):
        """Extension 해제 요청은 Native Job을 유지하고 Modal 해제 뒤 한 번만 실제 해제합니다."""
        target, setup = _prepare_backend("CompositeNativeExtensionUnregisterTarget")
        window_manager = _FakeCompositeWindowManager()
        context = SimpleNamespace(
            scene=bpy.context.scene,
            view_layer=bpy.context.view_layer,
            window=_FakeCompositeWindow(),
            window_manager=window_manager,
        )
        job = composite_module.start_composite_layer_creation(context)
        composite_module.start_composite_native_progress(context, job)
        timer = window_manager.event_timer_add(0.1, window=context.window)
        composite_progress_module.attach_composite_progress_timer(job.progress, timer)
        composite_module.prepare_composite_image(job)
        composite_module.prepare_native_bake(bpy.context, job, "COMPOSITE")
        composite_image_pointer = job.composite_image.as_pointer()
        temporary_nodes_before = tuple(
            node.as_pointer()
            for node in setup.material.node_tree.nodes
            if node.name.startswith("PP Composite Bake")
        )
        operator = SimpleNamespace(_job=job, report=lambda *_args: None)
        scheduled_callbacks = []

        def record_timer(callback, *, first_interval):
            """Deferred Extension 해제 Timer를 실행하지 않고 정확히 하나만 기록합니다."""
            self.assertEqual(first_interval, 0.0)
            scheduled_callbacks.append(callback)

        with patch.object(
            bpy.app.timers,
            "register",
            side_effect=record_timer,
        ), patch.object(
            bpy.app.timers,
            "is_registered",
            return_value=False,
        ), patch.object(
            lifecycle_module,
            "_unregister_now",
        ) as unregister_now:
            lifecycle_module.unregister()

            self.assertTrue(job.abort_after_bake)
            self.assertFalse(job.rolled_back)
            self.assertTrue(lifecycle_module.lifecycle_registry()._unregister_after_native_bake)
            self.assertTrue(
                any(image.as_pointer() == composite_image_pointer for image in bpy.data.images)
            )
            self.assertEqual(
                tuple(
                    node.as_pointer()
                    for node in setup.material.node_tree.nodes
                    if node.name.startswith("PP Composite Bake")
                ),
                temporary_nodes_before,
            )
            self.assertEqual(len(window_manager.timers), 1)
            self.assertEqual(composite_progress_module.composite_progress_session_count(), 1)

            self.assertEqual(
                PP_OT_composite_layer_create.modal(
                    operator,
                    context,
                    SimpleNamespace(type="TIMER", value="NOTHING"),
                ),
                {"RUNNING_MODAL"},
            )
            self.assertEqual(
                PP_OT_composite_layer_create.modal(
                    operator,
                    context,
                    SimpleNamespace(type="TIMER", value="NOTHING"),
                ),
                {"CANCELLED"},
            )
            self.assertTrue(job.rolled_back)
            self.assertEqual(len(window_manager.timers), 0)
            self.assertEqual(composite_progress_module.composite_progress_session_count(), 0)
            self.assertEqual(len(scheduled_callbacks), 1)
            lifecycle_module.lifecycle_registry().schedule_pending_unregistration()
            self.assertEqual(len(scheduled_callbacks), 1)

            scheduled_callbacks[0]()
            unregister_now.assert_called_once_with()

        lifecycle_module.lifecycle_registry().cancel_pending_unregistration()

    def test_reenable_cancels_pending_extension_unregistration_timer(self):
        """재활성화는 아직 실행하지 않은 Extension 해제 Timer를 취소합니다."""
        registry = lifecycle_module.lifecycle_registry()

        def pending_timer():
            """등록 대기 상태를 식별하는 최소 Timer Callback입니다."""
            return None

        registry._unregister_after_native_bake = True
        registry._unregister_timer_registered = True
        registry._unregister_timer_callback = pending_timer
        try:
            with patch.object(
                bpy.app.timers,
                "is_registered",
                return_value=True,
            ), patch.object(bpy.app.timers, "unregister") as unregister_timer:
                lifecycle_module.register()

            self.assertFalse(registry._unregister_after_native_bake)
            self.assertFalse(registry._unregister_timer_registered)
            self.assertIsNone(registry._unregister_timer_callback)
            unregister_timer.assert_called_once_with(pending_timer)
        finally:
            registry.cancel_pending_unregistration()

    def test_lifecycle_clear_releases_post_bake_progress_and_rolls_back(self):
        """수명주기 중단은 Bake 뒤 후처리 Timer·Overlay와 미확정 Image를 함께 정리합니다."""
        _target, _setup = _prepare_backend("CompositeLifecycleClearTarget")
        settings = bpy.context.scene.projection_paint
        source = settings.layers[0]
        window_manager = _FakeCompositeWindowManager()
        context = SimpleNamespace(
            scene=bpy.context.scene,
            view_layer=bpy.context.view_layer,
            window=_FakeCompositeWindow(),
            window_manager=window_manager,
        )
        job = composite_module.start_composite_layer_creation(context)
        composite_module.prepare_composite_image(job)
        job.phase = "BAKED"
        composite_module.start_composite_post_bake_progress(context, job)
        timer = window_manager.event_timer_add(0.1, window=context.window)
        composite_progress_module.attach_composite_progress_timer(job.progress, timer)

        composite_module.clear_composite_create_jobs()

        self.assertTrue(job.rolled_back)
        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(len(settings.layers), 2)
        self.assertTrue(source.visible)
        self.assertEqual(len(window_manager.timers), 0)
        self.assertEqual(composite_progress_module.composite_progress_session_count(), 0)

    def test_cancel_rollback_ignores_ui_restore_failure(self):
        """취소 Rollback의 선택·Mode 복구 실패는 원본 Graph를 STALE로 만들지 않는지 검증합니다."""
        _target, setup = _prepare_backend("CompositeCancelUiRestoreTarget")
        settings = bpy.context.scene.projection_paint
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            job.bake_preparation = composite_module._prepare_final_base_color_bake(
                bpy.context,
                job.composite_image,
            )
            with patch.object(
                composite_module,
                "_restore_selection",
                side_effect=RuntimeError("강제 선택 복구 실패"),
            ):
                composite_module._fail_composite_bake(
                    bpy.context,
                    job,
                    CompositeLayerError("강제 Bake 취소"),
                )

            self.assertTrue(job.rolled_back)
            self.assertEqual(settings.composite_state, "NONE")
            self.assertIn("UI 복구 경고", job.ui_restore_warning)
            self.assertFalse(
                any(node.name.startswith("PP Composite Bake") for node in setup.material.node_tree.nodes)
            )
        finally:
            composite_module.release_composite_create_job(job)

    def test_persistent_bake_restore_failure_marks_transaction_stale(self):
        """Surface·Node·렌더 복구 실패는 기존처럼 영구 Rollback 오류와 STALE로 남는지 검증합니다."""
        _target, _setup = _prepare_backend("CompositePersistentRestoreFailureTarget")
        settings = bpy.context.scene.projection_paint
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            job.bake_preparation = composite_module._prepare_final_base_color_bake(
                bpy.context,
                job.composite_image,
            )
            with patch.object(
                composite_module,
                "_restore_bake_persistent_state",
                side_effect=RuntimeError("강제 Surface 복구 실패"),
            ):
                composite_module._complete_composite_bake(bpy.context, job)

            self.assertTrue(job.rolled_back)
            self.assertEqual(settings.composite_state, "STALE")
            self.assertIn("주 오류", job.error_message)
            self.assertIn("영구 Rollback 오류", job.error_message)
            self.assertIn("강제 Surface 복구 실패", job.persistent_rollback_error)
        finally:
            composite_module.release_composite_create_job(job)

    def test_ui_restore_failure_keeps_committed_composite_active(self):
        """선택·Mode 복구 실패만으로는 검증된 Composite를 STALE로 만들지 않는지 검증합니다."""
        _target, _setup = _prepare_backend("CompositeRestoreFailureTarget")
        settings = bpy.context.scene.projection_paint
        job = composite_module.start_composite_layer_creation(bpy.context)
        cycles_before = _cycles_runtime_snapshot(bpy.context.scene)
        try:
            composite_module.prepare_composite_image(job)
            job.bake_preparation = composite_module._prepare_final_base_color_bake(
                bpy.context,
                job.composite_image,
            )
            with patch.object(
                composite_module,
                "_restore_selection",
                side_effect=RuntimeError("강제 선택 복구 실패"),
            ):
                composite_module._complete_composite_bake(bpy.context, job)

            self.assertTrue(job.committed)
            self.assertFalse(job.rolled_back)
            self.assertEqual(settings.composite_state, "ACTIVE")
            self.assertIn("UI 복구 경고", job.ui_restore_warning)
            self.assertIn("강제 선택 복구 실패", job.ui_restore_warning)
            self.assertFalse(
                any(node.name.startswith("PP Composite Bake") for node in settings.paint_material.node_tree.nodes)
            )
            self.assertEqual(_cycles_runtime_snapshot(bpy.context.scene), cycles_before)
        finally:
            composite_module.release_composite_create_job(job)

    def test_multi_udim_composite_bakes_every_tile_and_releases(self):
        """1001·1002 UV 면의 Composite Bake 결과와 타일 구성이 해제까지 유지되는지 검증합니다."""
        _target, setup = _prepare_backend(
            "CompositeMultiUdimTarget",
            multi_udim=True,
        )
        settings = bpy.context.scene.projection_paint
        source = settings.layers[0]
        source_uuid = str(source.layer_uuid)
        source_image = source.image
        self.assertEqual(tuple(tile.number for tile in source.image.tiles), (1001, 1002))
        _fill_image_tile(source.image, 1001, (0.25, 0.5, 0.75, 1.0))
        _fill_image_tile(source.image, 1002, (0.75, 0.25, 0.5, 1.0))

        composite_image = create_composite_layer(bpy.context)

        self.assertEqual(tuple(tile.number for tile in composite_image.tiles), (1001, 1002))
        self.assertGreater(_tile_pixel_sum(composite_image, 1001, 24, 8), 0.1)
        self.assertGreater(_tile_pixel_sum(composite_image, 1002, 24, 8), 0.1)

        release_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(len(settings.layers), 2)
        source_after_release = _layer_for_uuid(settings, source_uuid)
        self.assertIs(source_after_release.image, source_image)
        self.assertEqual(tuple(tile.number for tile in source_after_release.image.tiles), (1001, 1002))
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )

    def test_release_validation_failure_restores_composite_active_state(self):
        """원본 그래프 검증 실패가 삭제 없이 Composite 활성 선택·Mask·Clone 상태를 복구하는지 검증합니다."""
        _target, setup = _prepare_backend("CompositeReleaseRollbackTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        derived = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=str(derived.layer_uuid)
            ),
            {"FINISHED"},
        )
        material = setup.material
        tree = material.node_tree
        image_paint = bpy.context.scene.tool_settings.image_paint
        snapshot = {
            "layers": tuple(str(layer.layer_uuid) for layer in settings.layers),
            "visible": tuple(bool(layer.visible) for layer in settings.layers),
            "active_index": int(settings.active_layer_index),
            "overlay": settings.overlay_image.as_pointer(),
            "active_image": settings.active_paint_image.as_pointer(),
            "mask_mode": bool(settings.paint_mask_mode),
            "mask_uuid": str(settings.active_mask_uuid),
            "clone": (
                image_paint.clone_image.as_pointer()
                if image_paint.clone_image is not None
                else 0
            ),
            "clone_layer": bool(image_paint.use_clone_layer),
            "clone_slot": int(material.paint_clone_slot),
            "active_node": tree.nodes.active.as_pointer(),
            "node_selection": tuple(
                (node.as_pointer(), bool(node.select)) for node in tree.nodes
            ),
            "paint_slot": int(material.paint_active_slot),
        }
        original_validate = composite_module.validate_layer_graph
        calls = 0

        def fail_second_validate(*args, **kwargs):
            """Composite 해제의 원본 체인 검증 호출만 강제로 실패시킵니다."""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CompositeLayerError("강제 원본 Chain 검증 실패")
            return original_validate(*args, **kwargs)

        with patch.object(
            composite_module,
            "validate_layer_graph",
            side_effect=fail_second_validate,
        ):
            with self.assertRaises(CompositeLayerError):
                release_composite_layer(bpy.context)

        self.assertEqual(calls, 1)
        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in settings.layers),
            snapshot["layers"],
        )
        self.assertEqual(
            tuple(bool(layer.visible) for layer in settings.layers),
            snapshot["visible"],
        )
        self.assertEqual(int(settings.active_layer_index), snapshot["active_index"])
        self.assertEqual(settings.overlay_image.as_pointer(), snapshot["overlay"])
        self.assertEqual(settings.active_paint_image.as_pointer(), snapshot["active_image"])
        self.assertEqual(bool(settings.paint_mask_mode), snapshot["mask_mode"])
        self.assertEqual(str(settings.active_mask_uuid), snapshot["mask_uuid"])
        self.assertEqual(
            image_paint.clone_image.as_pointer() if image_paint.clone_image is not None else 0,
            snapshot["clone"],
        )
        self.assertEqual(bool(image_paint.use_clone_layer), snapshot["clone_layer"])
        self.assertEqual(int(material.paint_clone_slot), snapshot["clone_slot"])
        self.assertEqual(tree.nodes.active.as_pointer(), snapshot["active_node"])
        self.assertEqual(
            tuple((node.as_pointer(), bool(node.select)) for node in tree.nodes),
            snapshot["node_selection"],
        )
        self.assertEqual(int(material.paint_active_slot), snapshot["paint_slot"])

    def test_release_cleanup_failure_keeps_committed_normal_graph_with_warning(self):
        """논리 Commit 뒤 고아 정리 실패는 정상 일반 Graph와 명확한 경고를 유지합니다."""
        _target, setup = _prepare_backend("CompositeReleaseCleanupWarningTarget")
        settings = bpy.context.scene.projection_paint
        source_uuid = str(settings.layers[0].layer_uuid)
        create_composite_layer(bpy.context)
        composite_uuid = str(settings.composite_layer_uuid)

        with patch.object(
            composite_module,
            "remove_owned_layer_bundles",
            side_effect=RuntimeError("강제 고아 정리 실패"),
        ):
            release_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "NONE")
        self.assertNotIn(
            composite_uuid,
            {str(layer.layer_uuid) for layer in settings.layers},
        )
        self.assertIsNotNone(_layer_for_uuid(settings, source_uuid))
        self.assertEqual(settings.status_kind, "WARNING")
        self.assertIn("고아 자원 정리 경고", settings.status)
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )

    def test_stale_composite_blocks_layer_operations_without_data_changes(self):
        """STALE 세션이 자동 재구성과 Layer·Mask·선택·이동·삭제를 모두 차단하는지 검증합니다."""
        _target, setup = _prepare_backend("CompositeStaleTarget")
        settings = bpy.context.scene.projection_paint
        source_before_composite = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=str(source_before_composite.layer_uuid),
                adjustment_type="BRIGHTNESS",
            ),
            {"FINISHED"},
        )
        adjustment_uuid = str(source_before_composite.adjustments[0].adjustment_uuid)
        source_before_composite.adjustments[0].ui_selected = False
        source_before_composite.ui_selected = True
        create_composite_layer(bpy.context)
        composite = settings.layers[0]
        source = settings.layers[1]
        composite.image[COMPOSITE_IMAGE_SESSION_KEY] = "wrong-session"
        with self.assertRaises(CompositeLayerError):
            release_composite_layer(bpy.context)
        self.assertEqual(settings.composite_state, "STALE")
        snapshot = (
            len(settings.layers),
            int(settings.active_layer_index),
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )
        with self.assertRaises(RuntimeError):
            bpy.ops.projection_paint.layer_add()
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=str(source.layer_uuid)),
            {"CANCELLED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=str(source.layer_uuid)),
            {"CANCELLED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_select(
                layer_uuid=str(source.layer_uuid),
                adjustment_uuid=adjustment_uuid,
            ),
            {"CANCELLED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=str(source.layer_uuid),
                adjustment_uuid=adjustment_uuid,
            ),
            {"CANCELLED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_remove(
                layer_uuid=str(source.layer_uuid),
                adjustment_uuid=adjustment_uuid,
            ),
            {"CANCELLED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="DOWN"),
            {"CANCELLED"},
        )
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"CANCELLED"})
        with self.assertRaises(composite_module.LayerShaderError):
            composite_module.reconcile_layer_graph(bpy.context.scene)
        self.assertEqual(
            (
                len(settings.layers),
                int(settings.active_layer_index),
                tuple(str(layer.layer_uuid) for layer in settings.layers),
            ),
            snapshot,
        )
        self.assertEqual(setup.material.node_tree.nodes.active.image, composite.image)

    def test_original_layer_is_composite_source_and_release_restores_it(self):
        """실제 Original Layer는 Composite Source에 포함되고 해제 뒤 원래 리소스를 그대로 사용합니다."""
        _target, setup = _prepare_backend("CompositeOriginalSourceTarget")
        settings = bpy.context.scene.projection_paint
        material = setup.material
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        original_uuid = str(original.layer_uuid)
        original_image = original.image

        create_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "ACTIVE")
        composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        self.assertFalse(composite.expanded)
        source = _layer_for_uuid(settings, original_uuid)
        self.assertFalse(source.visible)
        self.assertTrue(source.composite_saved_visible)
        self.assertTrue(settings.layers[0].composite_source_expanded)

        release_composite_layer(bpy.context)

        restored = _layer_for_uuid(settings, original_uuid)
        self.assertTrue(restored.is_original_layer)
        self.assertTrue(restored.visible)
        self.assertEqual(restored.image.as_pointer(), original_image.as_pointer())
        validate_layer_graph(
            material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )

    def test_composite_execute_preserves_candidates_during_confirm_click(self):
        """Composite 확인 PRESS는 Source Snapshot 전 후보 선택을 변경하지 않습니다."""
        _target, _setup = _prepare_backend("CompositeConfirmSelectionTarget")
        settings = bpy.context.scene.projection_paint
        for layer in settings.layers:
            layer.ui_selected = True
        settings.layer_selection_anchor_uuid = str(settings.layers[-1].layer_uuid)
        expected_source_uuids = tuple(str(layer.layer_uuid) for layer in settings.layers)
        _record_layer_ui_pointer_event(
            bpy.context.window,
            SimpleNamespace(
                type="LEFTMOUSE",
                value="PRESS",
                ctrl=False,
                shift=False,
                mouse_x=520,
                mouse_y=320,
            ),
            context=bpy.context,
        )
        self.assertTrue(all(bool(layer.ui_selected) for layer in settings.layers))

        operator = SimpleNamespace(report=lambda _types, _message: None)
        self.assertEqual(
            PP_OT_composite_layer_create.execute(operator, bpy.context),
            {"FINISHED"},
        )

        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertEqual(
            tuple(
                str(layer.layer_uuid)
                for layer in composite_module.composite_source_layers(settings)
            ),
            expected_source_uuids,
        )
        release_composite_layer(bpy.context)

    def test_active_composite_migrates_legacy_original_without_rebake(self):
        """활성 Composite의 분리된 구형 Gate는 Bake 결과를 바꾸지 않고 실제 Original로 이관합니다."""
        _target, setup = _prepare_backend("CompositeActiveLegacyOriginalTarget")
        settings = bpy.context.scene.projection_paint
        material = setup.material
        tree = material.node_tree
        create_composite_layer(bpy.context)
        composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        composite_image = composite.image
        composite_pixels = tuple(composite_image.pixels[:])
        session_uuid = str(settings.active_composite_session_uuid)

        legacy_original = next(layer for layer in settings.layers if layer.is_original_layer)
        legacy_uuid = str(legacy_original.layer_uuid)
        legacy_index = next(
            index
            for index, layer in enumerate(settings.layers)
            if str(layer.layer_uuid) == legacy_uuid
        )
        for node in tuple(tree.nodes):
            if str(node.get(LAYER_UUID_KEY, "")) == legacy_uuid:
                tree.nodes.remove(node)
        legacy_image = legacy_original.image
        settings.layers.remove(legacy_index)
        if legacy_image is not None and int(legacy_image.users) == 0:
            bpy.data.images.remove(legacy_image)

        source_layer = next(
            layer for layer in settings.layers if layer.layer_role == "NORMAL"
        )
        source_mix = _mix_for(material, source_layer.layer_uuid)
        for link in tuple(source_mix.inputs[1].links):
            tree.links.remove(link)
        raw = tree.nodes.new("ShaderNodeRGB")
        raw.outputs["Color"].default_value = (0.1, 0.3, 0.7, 1.0)
        gate = tree.nodes.new("ShaderNodeMixRGB")
        gate[OWNER_KEY] = OWNER_TOKEN
        gate[RECORD_KIND_KEY] = ORIGINAL_GATE_NODE_ROLE
        gate.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)
        tree.links.new(raw.outputs["Color"], gate.inputs[2])
        tree.links.new(gate.outputs["Color"], source_mix.inputs[1])
        settings.original_layer_schema_version = 0
        settings.original_source_image = None
        settings.original_source_node_uuid = ""
        settings.original_source_socket_identifier = ""

        with patch.object(
            composite_module,
            "_bake_final_base_color",
            side_effect=AssertionError("활성 Composite 이전에서 재베이크하면 안 됩니다."),
        ):
            reconcile_layer_graph(bpy.context.scene)

        migrated = next(layer for layer in settings.layers if layer.is_original_layer)
        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertEqual(str(settings.active_composite_session_uuid), session_uuid)
        self.assertEqual(composite.image.as_pointer(), composite_image.as_pointer())
        self.assertEqual(tuple(composite_image.pixels[:]), composite_pixels)
        self.assertEqual(
            int(settings.original_layer_schema_version),
            ORIGINAL_LAYER_SCHEMA_VERSION,
        )
        self.assertTrue(migrated.is_original_layer)
        self.assertFalse(
            any(
                node.get(RECORD_KIND_KEY) == ORIGINAL_GATE_NODE_ROLE
                for node in tree.nodes
            )
        )
        validate_composite_session(bpy.context.scene)

    def test_composite_source_folder_toggle_does_not_change_graph_or_images(self):
        """Source 폴더 접기·펼치기는 표시 상태만 바꾸고 Composite 그래프와 Image를 보존합니다."""
        _target, setup = _prepare_backend("CompositeSourceFolderToggle")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        tree = setup.material.node_tree
        graph_before = frozenset(
            (
                link.from_node.as_pointer(),
                str(link.from_socket.identifier),
                link.to_node.as_pointer(),
                str(link.to_socket.identifier),
            )
            for link in tree.links
        )
        images_before = tuple(layer.image.as_pointer() for layer in settings.layers)
        session_uuid = str(settings.active_composite_session_uuid)
        composite.composite_source_expanded = False
        self.assertFalse(composite.composite_source_expanded)
        composite.composite_source_expanded = True
        self.assertTrue(composite.composite_source_expanded)
        self.assertEqual(str(settings.active_composite_session_uuid), session_uuid)
        self.assertEqual(
            tuple(layer.image.as_pointer() for layer in settings.layers),
            images_before,
        )
        self.assertEqual(
            frozenset(
                (
                    link.from_node.as_pointer(),
                    str(link.from_socket.identifier),
                    link.to_node.as_pointer(),
                    str(link.to_socket.identifier),
                )
                for link in tree.links
            ),
            graph_before,
        )

    def test_original_off_is_excluded_from_composite_bake_and_restored(self):
        """Original 눈 OFF는 검정 Base로 Bake되고 Composite 해제 뒤에도 그대로 유지됩니다."""
        _target, setup = _prepare_backend("CompositeOriginalOffTarget")
        settings = bpy.context.scene.projection_paint
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        source_uuid = str(original.layer_uuid)
        original.visible = False
        self.assertFalse(original.visible)

        create_composite_layer(bpy.context)

        composite = settings.layers[0]
        self.assertLess(_tile_pixel_sum(composite.image, 1001, 8, 8), 0.01)
        source = _layer_for_uuid(settings, source_uuid)
        self.assertFalse(source.visible)
        self.assertFalse(source.composite_saved_visible)

        release_composite_layer(bpy.context)

        restored = _layer_for_uuid(settings, source_uuid)
        self.assertEqual(settings.composite_state, "NONE")
        self.assertFalse(restored.visible)
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )

    def test_composite_creation_rejects_non_black_original_base(self):
        """실제 Original 최하단을 Raw Socket에 연결한 변조는 Composite 생성을 중단합니다."""
        _target, setup = _prepare_backend("CompositeOriginalBypassTarget")
        settings = bpy.context.scene.projection_paint
        material = setup.material
        source = material.node_tree.nodes.new("ShaderNodeRGB")
        bottom = _mix_for(material, settings.layers[-1].layer_uuid)
        for link in tuple(bottom.inputs[1].links):
            material.node_tree.links.remove(link)
        material.node_tree.links.new(source.outputs["Color"], bottom.inputs[1])

        with self.assertRaisesRegex(CompositeLayerError, "검정 Base"):
            create_composite_layer(bpy.context)
        self.assertEqual(settings.composite_state, "NONE")

    def test_active_composite_rejects_non_black_base_reconnection(self):
        """활성 Composite 최하단의 Raw Socket 재연결은 fail-closed로 거부합니다."""
        _target, setup = _prepare_backend("CompositeOriginalReconnectionTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        bottom = _mix_for(setup.material, composite.layer_uuid)
        raw = setup.material.node_tree.nodes.new("ShaderNodeRGB")
        setup.material.node_tree.links.new(raw.outputs["Color"], bottom.inputs[1])

        with self.assertRaisesRegex(CompositeLayerError, "검정 Base"):
            validate_composite_session(bpy.context.scene)
        self.assertEqual(settings.composite_state, "STALE")

    def test_original_layer_preview_blocks_composite_creation(self):
        """실제 Original Layer 미리 보기 활성 상태에서는 Composite 생성을 시작하지 않습니다."""
        _target, _setup = _prepare_backend("CompositeOriginalPreviewTarget")
        settings = bpy.context.scene.projection_paint
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        self.assertEqual(
            bpy.ops.projection_paint.layer_preview_toggle(
                layer_uuid=str(original.layer_uuid),
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layer_preview_uuid, original.layer_uuid)
        with self.assertRaises(CompositeLayerError):
            create_composite_layer(bpy.context)
        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(settings.layer_preview_uuid, original.layer_uuid)

    def test_recomposite_flattens_active_composite_and_deletes_previous_sources(self):
        """재합성은 기존 Source를 삭제하고 직전 Composite 평면 Layer 하나만 새 Source로 남깁니다."""
        _target, setup = _prepare_backend("CompositeRecomposeTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_session_uuid = str(settings.active_composite_session_uuid)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        old_composite_uuid = str(old_composite.layer_uuid)
        old_source_uuids = tuple(
            str(layer.layer_uuid)
            for layer in composite_module.composite_source_layers(settings)
        )
        old_source_image_names = tuple(
            str(layer.image.name)
            for layer in composite_module.composite_source_layers(settings)
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=str(old_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        old_mask_name = str(old_composite.mask.image.name)
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=str(old_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        dependent = settings.layers[0]
        dependent_uuid = str(dependent.layer_uuid)
        dependent_image_name = str(dependent.image.name)
        old_composite = _layer_for_uuid(settings, old_composite_uuid)
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        dependent.ui_selected = True

        new_image = create_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertNotEqual(settings.active_composite_session_uuid, old_session_uuid)
        self.assertEqual(len([layer for layer in settings.layers if layer.layer_role == "COMPOSITE"]), 1)
        new_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        self.assertIs(new_composite.image, new_image)
        self.assertIsNone(bpy.context.scene.tool_settings.image_paint.clone_image)
        self.assertFalse(bpy.context.scene.tool_settings.image_paint.use_clone_layer)
        self.assertEqual(new_composite.composite_generation, 2)
        self.assertFalse(new_composite.expanded)
        self.assertFalse(
            new_composite.image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY)
        )
        flat = _layer_for_uuid(settings, old_composite_uuid)
        self.assertIsNotNone(flat)
        self.assertEqual(flat.layer_role, "NORMAL")
        self.assertEqual(flat.composite_generation, 1)
        self.assertEqual(flat.composite_source_session_uuid, settings.active_composite_session_uuid)
        self.assertEqual(flat.composite_session_uuid, "")
        self.assertFalse(
            flat.image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY)
        )
        self.assertEqual(
            set(str(layer.layer_uuid) for layer in settings.layers),
            {str(new_composite.layer_uuid), old_composite_uuid},
        )
        self.assertNotIn(
            dependent_uuid,
            {str(layer.layer_uuid) for layer in settings.layers},
        )
        self.assertIsNone(bpy.data.images.get(dependent_image_name))
        self.assertTrue(
            all(
                layer_uuid not in {str(layer.layer_uuid) for layer in settings.layers}
                for layer_uuid in old_source_uuids
            )
        )
        self.assertTrue(all(bpy.data.images.get(name) is None for name in old_source_image_names))
        self.assertIsNone(bpy.data.images.get(old_mask_name))
        validate_composite_session(bpy.context.scene)

        release_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in settings.layers),
            (old_composite_uuid,),
        )
        self.assertTrue(
            all(
                layer_uuid not in {str(layer.layer_uuid) for layer in settings.layers}
                for layer_uuid in old_source_uuids
            )
        )
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
            expected_original_base=False,
        )

    def test_recomposite_reuse_cancel_preserves_existing_composite_image_and_session(self):
        """재사용 경로 취소는 기존 Composite Image·계층을 바꾸지 않고 새 Image만 정리합니다."""
        _target, _setup = _prepare_backend("CompositeRecomposeCancelTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_session_uuid = str(settings.active_composite_session_uuid)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        old_image = old_composite.image
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            composite_module.prepare_recomposite_flatten_image(bpy.context, job)
            new_image = job.composite_image
            flatten_image = job.flatten_image
            new_image_name = str(new_image.name)
            old_image_name = str(old_image.name)
            self.assertTrue(job.reuse_old_composite_image)
            self.assertIs(flatten_image, old_image)
            self.assertEqual(
                new_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY),
                job.session_uuid,
            )
            self.assertFalse(flatten_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY))

            composite_module._fail_composite_bake(
                None,
                job,
                CompositeLayerError("강제 재합성 취소"),
            )

            self.assertTrue(job.rolled_back)
            self.assertEqual(settings.composite_state, "ACTIVE")
            self.assertEqual(settings.active_composite_session_uuid, old_session_uuid)
            restored = _layer_for_uuid(settings, str(old_composite.layer_uuid))
            self.assertIs(restored.image, old_image)
            self.assertIsNone(bpy.data.images.get(new_image_name))
            self.assertIs(bpy.data.images.get(old_image_name), old_image)
            self.assertFalse(old_image.get("pp_recomposite_pending_delete"))
            validate_composite_session(bpy.context.scene)
        finally:
            composite_module.release_composite_create_job(job)

    def test_recomposite_reuses_dirty_owned_composite_image(self):
        """직접 Paint로 Dirty가 된 유효 Composite Image도 읽기 전용 Source로 재사용합니다."""
        _target, _setup = _prepare_backend(
            "CompositeRecomposeDirtyReuseTarget",
            multi_udim=True,
        )
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        old_image = old_composite.image
        _fill_image_tile(old_image, 1001, (0.2, 0.4, 0.6, 1.0))
        _fill_image_tile(old_image, 1002, (0.6, 0.4, 0.2, 1.0))
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            composite_module.prepare_recomposite_native_source(job)

            self.assertTrue(job.reuse_old_composite_image)
            self.assertIs(job.flatten_image, old_image)
            self.assertFalse(old_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY))

            composite_module._fail_composite_bake(
                None,
                job,
                CompositeLayerError("Dirty 재사용 롤백 검사"),
            )
            self.assertIs(
                _layer_for_uuid(settings, str(old_composite.layer_uuid)).image,
                old_image,
            )
            validate_composite_session(bpy.context.scene)
        finally:
            composite_module.release_composite_create_job(job)

    def test_recomposite_mask_or_adjustment_requires_flatten_source(self):
        """Composite 자체 Mask 또는 Adjustment가 있으면 평면 Source Bake 경로를 고정합니다."""
        _target, _setup = _prepare_backend("CompositeRecomposeMaskFallbackTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=str(old_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=str(old_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            composite_module.prepare_recomposite_native_source(job)

            self.assertFalse(job.reuse_old_composite_image)
            self.assertIsNot(job.flatten_image, old_composite.image)
            self.assertEqual(
                job.flatten_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY),
                job.session_uuid,
            )
        finally:
            if not job.rolled_back:
                composite_module._fail_composite_bake(
                    None,
                    job,
                    CompositeLayerError("Mask 평면 Source 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_recomposite_udim_contract_mismatch_requires_flatten_source(self):
        """새 Composite UDIM 계약이 다르면 이전 Image를 직접 재사용하지 않습니다."""
        _target, _setup = _prepare_backend(
            "CompositeRecomposeContractFallbackTarget",
            multi_udim=True,
        )
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            job.composite_image.tiles.new(1003)
            composite_module.prepare_recomposite_native_source(job)

            self.assertFalse(job.reuse_old_composite_image)
            self.assertIsNot(job.flatten_image, old_composite.image)
        finally:
            if not job.rolled_back:
                composite_module._fail_composite_bake(
                    None,
                    job,
                    CompositeLayerError("UDIM 계약 불일치 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_recomposite_direct_reuse_and_flatten_rgba_udim_results_match(self):
        """직접 재사용과 Mask 강제 EMIT 평면화의 RGBA·UDIM 결과가 8-bit 오차 안에서 같습니다."""
        _target, _setup = _prepare_backend(
            "CompositeRecomposeRgbaEquivalenceTarget",
            multi_udim=True,
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        source = settings.layers[0].image
        _fill_image_tile(source, 1001, (0.15, 0.35, 0.65, 1.0))
        _fill_image_tile(source, 1002, (0.15, 0.35, 0.65, 1.0))
        create_composite_layer(bpy.context)
        first_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        for layer in settings.layers:
            layer.ui_selected = False
        first_composite.ui_selected = True

        with patch.object(
            composite_module,
            "_prepare_source_emit_bake",
            wraps=composite_module._prepare_source_emit_bake,
        ) as direct_prepare:
            direct_image = create_composite_layer(bpy.context)
        self.assertEqual(direct_prepare.call_count, 1)
        direct_pixels = {
            tile_number: _tile_pixels(direct_image, tile_number)
            for tile_number in (1001, 1002)
        }

        direct_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=str(direct_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        for tile_number in (1001, 1002):
            _fill_image_tile(direct_composite.mask.image, tile_number, (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=str(direct_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        for layer in settings.layers:
            layer.ui_selected = False
        direct_composite.ui_selected = True

        with patch.object(
            composite_module,
            "_prepare_source_emit_bake",
            wraps=composite_module._prepare_source_emit_bake,
        ) as flattened_prepare:
            flattened_image = create_composite_layer(bpy.context)
        self.assertEqual(flattened_prepare.call_count, 3)
        for tile_number, expected in direct_pixels.items():
            actual = _tile_pixels(flattened_image, tile_number)
            self.assertEqual(len(actual), len(expected))
            difference_index = max(
                range(len(actual)),
                key=lambda index: abs(actual[index] - expected[index]),
            )
            self.assertLessEqual(
                max(abs(left - right) for left, right in zip(actual, expected)),
                0.05,
                (
                    f"UDIM {tile_number} pixel index {difference_index}: "
                    f"direct={expected[difference_index]!r}, "
                    f"flatten={actual[difference_index]!r}"
                ),
            )

    def test_native_bake_handlers_only_record_matching_job_signal(self):
        """Native Bake Handler는 일치 Job의 신호만 바꾸고 Image·Node·Layer를 수정하지 않습니다."""
        target, setup = _prepare_backend("CompositeNativeHandlerSignalTarget")
        settings = bpy.context.scene.projection_paint
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            job.phase = "NATIVE_BAKING"
            job.native_bake_stage = "COMPOSITE"
            nodes_before = tuple(node.as_pointer() for node in setup.material.node_tree.nodes)
            images_before = tuple(image.as_pointer() for image in bpy.data.images)
            layers_before = tuple(layer.as_pointer() for layer in settings.layers)

            state_handlers_module.object_bake_pre_handler(target, None)
            self.assertEqual(job.native_bake_outcome, "RUNNING")
            state_handlers_module.object_bake_complete_handler(target, None)
            self.assertEqual(job.native_bake_outcome, "COMPLETE")
            state_handlers_module.object_bake_cancel_handler(target, None)
            self.assertEqual(job.native_bake_outcome, "COMPLETE")
            self.assertEqual(
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                nodes_before,
            )
            self.assertEqual(tuple(image.as_pointer() for image in bpy.data.images), images_before)
            self.assertEqual(tuple(layer.as_pointer() for layer in settings.layers), layers_before)
        finally:
            if not job.rolled_back:
                composite_module._fail_composite_bake(
                    None,
                    job,
                    CompositeLayerError("Native Handler 신호 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_composite_creation_allows_only_one_live_job(self):
        """같은 Blender Runtime에서는 Native 또는 동기 Composite Job을 하나만 시작할 수 있습니다."""
        _target, _setup = _prepare_backend("CompositeSingleLiveJobTarget")
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            with self.assertRaisesRegex(CompositeLayerError, "하나뿐"):
                composite_module.start_composite_layer_creation(bpy.context)
        finally:
            if not job.rolled_back:
                composite_module._fail_composite_bake(
                    None,
                    job,
                    CompositeLayerError("단일 Job 검사 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_recomposite_graph_failure_rolls_back_without_pending_marker(self):
        """새 Graph 검증 실패는 삭제 전 Transaction을 되돌리고 기존 Composite를 보존합니다."""
        _target, _setup = _prepare_backend("CompositeRecomposeGraphFailureTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_session_uuid = str(settings.active_composite_session_uuid)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        old_image = old_composite.image
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            composite_module.prepare_recomposite_flatten_image(bpy.context, job)
            new_image_name = str(job.composite_image.name)
            self.assertTrue(job.reuse_old_composite_image)
            self.assertIs(job.flatten_image, old_image)
            composite_module._bake_source_rgba(
                bpy.context,
                job.composite_image,
                job=job,
            )
            with patch.object(
                composite_module,
                "validate_composite_session",
                side_effect=CompositeLayerError("강제 새 Graph 검증 실패"),
            ):
                composite_module._complete_composite_bake(bpy.context, job)

            self.assertTrue(job.rolled_back)
            self.assertFalse(job.deletion_started)
            self.assertEqual(settings.composite_state, "ACTIVE")
            self.assertEqual(settings.active_composite_session_uuid, old_session_uuid)
            restored = _layer_for_uuid(settings, str(old_composite.layer_uuid))
            self.assertIs(restored.image, old_image)
            self.assertIsNone(bpy.data.images.get(new_image_name))
            self.assertIs(bpy.data.images.get(str(old_image.name)), old_image)
            self.assertFalse(old_image.get("pp_recomposite_pending_delete"))
            validate_composite_session(bpy.context.scene)
        finally:
            composite_module.release_composite_create_job(job)

    def test_recomposite_reload_recovers_unshared_images_and_preserves_shared_new_image(self):
        """Load 복구는 미참조 임시 Image를 지우고 외부 공유 새 Image의 표식만 해제합니다."""
        _target, _setup = _prepare_backend("CompositeRecomposeReloadTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_session_uuid = str(settings.active_composite_session_uuid)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=str(old_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=str(old_composite.layer_uuid)
            ),
            {"FINISHED"},
        )
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            composite_module.prepare_recomposite_flatten_image(bpy.context, job)
            shared_new_image = job.composite_image
            flatten_image_name = str(job.flatten_image.name)
            external_material = bpy.data.materials.new("CompositeRecomposeReloadExternal")
            external_material.use_nodes = True
            external_node = external_material.node_tree.nodes.new("ShaderNodeTexImage")
            external_node.image = shared_new_image

            composite_module.recover_interrupted_recomposite(bpy.context.scene)

            self.assertEqual(settings.composite_state, "ACTIVE")
            self.assertEqual(settings.active_composite_session_uuid, old_session_uuid)
            self.assertIs(external_node.image, shared_new_image)
            self.assertIs(bpy.data.images.get(str(shared_new_image.name)), shared_new_image)
            self.assertIsNone(bpy.data.images.get(flatten_image_name))
            self.assertFalse(
                shared_new_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY)
            )
            self.assertFalse(
                shared_new_image.get(composite_module.RECOMPOSITE_PARENT_SESSION_KEY)
            )
            self.assertFalse(shared_new_image.get("pp_recomposite_pending_delete"))
            validate_composite_session(bpy.context.scene)
        finally:
            composite_module.release_composite_create_job(job)

    def test_recomposite_cleanup_failure_marks_stale_without_restoring_deleted_transaction(self):
        """실제 이전 자원 삭제 시작 뒤 오류는 롤백 대신 STALE로 남아 손실을 숨기지 않습니다."""
        _target, _setup = _prepare_backend("CompositeRecomposeCleanupFailureTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=str(old_composite.layer_uuid),
                adjustment_type="BRIGHTNESS",
            ),
            {"FINISHED"},
        )
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True
        job = composite_module.start_composite_layer_creation(bpy.context)
        try:
            composite_module.prepare_composite_image(job)
            composite_module.prepare_recomposite_flatten_image(bpy.context, job)
            self.assertFalse(job.reuse_old_composite_image)
            composite_module._bake_source_rgba(
                bpy.context,
                job.composite_image,
                job=job,
            )
            with patch.object(
                composite_module,
                "remove_adjustment_resources",
                side_effect=CompositeLayerError("강제 이전 자원 삭제 실패"),
            ):
                composite_module._complete_composite_bake(bpy.context, job)

            self.assertTrue(job.deletion_started)
            self.assertTrue(job.rolled_back)
            self.assertEqual(settings.composite_state, "STALE")
            self.assertIn("삭제 중 오류", settings.status)
            self.assertFalse(
                job.composite_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY)
            )
            self.assertFalse(
                job.flatten_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY)
            )
        finally:
            composite_module.release_composite_create_job(job)

    def test_recomposite_preserves_externally_shared_previous_composite_image(self):
        """재합성 삭제는 외부 Material이 참조한 이전 Composite Image를 제거하지 않습니다."""
        _target, _setup = _prepare_backend("CompositeRecomposeSharedImageTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        old_composite = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
        old_image = old_composite.image
        external_material = bpy.data.materials.new("CompositeRecomposeSharedImageExternal")
        external_material.use_nodes = True
        external_node = external_material.node_tree.nodes.new("ShaderNodeTexImage")
        external_node.image = old_image
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=str(old_composite.layer_uuid),
                adjustment_type="BRIGHTNESS",
            ),
            {"FINISHED"},
        )
        for layer in settings.layers:
            layer.ui_selected = False
        old_composite.ui_selected = True

        create_composite_layer(bpy.context)

        self.assertIs(external_node.image, old_image)
        self.assertIs(bpy.data.images.get(str(old_image.name)), old_image)
        self.assertIsNone(old_image.get(OWNER_KEY))
        self.assertIsNone(old_image.get(RECORD_KIND_KEY))
        self.assertIsNone(old_image.get(TARGET_UUID_KEY))
        self.assertIsNone(old_image.get(LAYER_UUID_KEY))
        self.assertFalse(old_image.get("pp_recomposite_pending_delete"))
        self.assertFalse(old_image.get(composite_module.RECOMPOSITE_TRANSACTION_KEY))
        validate_composite_session(bpy.context.scene)

    def test_three_recomposites_keep_only_immediate_flattened_composite_and_release_it(self):
        """세 번 재합성해도 직전 평면 Composite 하나만 남고 해제 뒤 그 Layer만 복구합니다."""
        _target, setup = _prepare_backend("CompositeRecomposeRepeatTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        initial_source_uuids = {
            str(layer.layer_uuid)
            for layer in composite_module.composite_source_layers(settings)
        }
        created_source_uuids = []
        for generation in (2, 3, 4):
            current_composite_uuid = str(settings.composite_layer_uuid)
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
            source = settings.layers[0]
            created_source_uuids.append(str(source.layer_uuid))
            current_composite = _layer_for_uuid(
                settings,
                current_composite_uuid,
            )
            for layer in settings.layers:
                layer.ui_selected = False
            source.ui_selected = True
            current_composite.ui_selected = True

            create_composite_layer(bpy.context)

            current = _layer_for_uuid(settings, str(settings.composite_layer_uuid))
            flattened = tuple(
                layer
                for layer in settings.layers
                if layer.layer_role == "NORMAL" and int(layer.composite_generation) > 0
            )
            self.assertEqual(current.composite_generation, generation)
            self.assertEqual(len(flattened), 1)
            self.assertEqual(flattened[0].composite_generation, generation - 1)
            self.assertEqual(
                flattened[0].composite_source_session_uuid,
                settings.active_composite_session_uuid,
            )
            validate_composite_session(bpy.context.scene)

        final_composite_uuid = str(settings.composite_layer_uuid)
        final_flattened_uuid = str(
            next(
                layer.layer_uuid
                for layer in settings.layers
                if layer.layer_role == "NORMAL" and int(layer.composite_generation) == 3
            )
        )
        remaining_uuids = {str(layer.layer_uuid) for layer in settings.layers}
        self.assertTrue(initial_source_uuids.isdisjoint(remaining_uuids))
        self.assertNotIn(created_source_uuids[0], remaining_uuids)
        self.assertNotIn(created_source_uuids[1], remaining_uuids)
        self.assertNotIn(created_source_uuids[2], remaining_uuids)

        release_composite_layer(bpy.context)

        self.assertEqual(settings.composite_state, "NONE")
        self.assertEqual(
            {str(layer.layer_uuid) for layer in settings.layers},
            {final_flattened_uuid},
        )
        self.assertNotIn(final_composite_uuid, {str(layer.layer_uuid) for layer in settings.layers})
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
            expected_original_base=False,
        )

    def test_active_composite_survives_blend_reload(self):
        """저장한 활성 Composite 세션이 파일 재로드 뒤에도 같은 Shader Chain을 복구합니다."""
        _target, _setup = _prepare_backend("CompositeReloadTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        session_uuid = str(settings.active_composite_session_uuid)
        blend_path = Path(bpy.app.tempdir) / f"projection_paint_{uuid4().hex}.blend"

        self.assertEqual(
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False),
            {"FINISHED"},
        )
        self.assertEqual(bpy.ops.wm.open_mainfile(filepath=str(blend_path)), {"FINISHED"})

        settings = bpy.context.scene.projection_paint
        self.assertEqual(settings.composite_state, "ACTIVE")
        self.assertEqual(str(settings.active_composite_session_uuid), session_uuid)
        validate_composite_session(bpy.context.scene)
        composite = next(
            layer for layer in settings.layers if layer.layer_role == "COMPOSITE"
        )
        material = settings.paint_material
        base = next(
            node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        ).inputs["Base Color"]
        self.assertEqual(
            base.links[0].from_node.as_pointer(),
            _mix_for(material, composite.layer_uuid).as_pointer(),
        )

    def test_emit_split_defaults_to_one_color_bake_without_combined(self):
        """기본 Composite는 Samples 1의 Color EMIT를 한 번만 실행합니다."""
        _target, _setup = _prepare_backend("CompositeEmitDefaultTarget")
        settings = bpy.context.scene.projection_paint
        self.assertFalse(settings.composite_include_alpha)
        job = composite_module.start_composite_layer_creation(bpy.context)
        calls = []
        try:
            composite_module.prepare_composite_image(job)

            def fake_bake(*args, **kwargs):
                """동기 Bake 종류와 Samples를 기록합니다."""
                calls.append((args, kwargs, int(bpy.context.scene.cycles.samples)))
                return {"FINISHED"}

            fake_bpy = SimpleNamespace(
                context=bpy.context,
                app=SimpleNamespace(is_job_running=lambda _kind: False),
                ops=SimpleNamespace(
                    object=SimpleNamespace(
                        bake=fake_bake,
                        mode_set=lambda **_kwargs: {"FINISHED"},
                    )
                ),
            )
            with patch.object(composite_module, "bpy", fake_bpy):
                composite_module._bake_source_rgba(
                    bpy.context,
                    job.composite_image,
                    job=job,
                )
            self.assertEqual([call[1]["type"] for call in calls], ["EMIT"])
            self.assertEqual([call[2] for call in calls], [1])
            self.assertFalse(job.include_alpha)
        finally:
            if not job.committed and not job.rolled_back:
                composite_module._fail_composite_bake(
                    bpy.context,
                    job,
                    CompositeLayerError("테스트 Color EMIT 정리"),
                )
            composite_module.release_composite_create_job(job)

    def test_alpha_emit_numpy_keeps_premultiplied_rgb_and_half_alpha(self):
        """반투명 Source의 EMIT 결과가 기존 premultiplied Shader 계약과 일치합니다."""
        _target, _setup = _prepare_backend("CompositeEmitAlphaTarget")
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        for layer in settings.layers:
            layer.ui_selected = False
        source = settings.layers[0]
        source.ui_selected = True
        _fill_image(source.image, (0.8, 0.2, 0.1, 0.5))
        settings.composite_include_alpha = True

        image = create_composite_layer(bpy.context)

        composite = next(layer for layer in settings.layers if layer.layer_role == "COMPOSITE")
        red, green, blue, alpha = _tile_pixel(image, 1001, 24, 8)
        self.assertTrue(composite.composite_has_baked_alpha)
        self.assertAlmostEqual(alpha, 0.5, delta=1.0 / 255.0)
        self.assertAlmostEqual(red, 0.5843, delta=1.0 / 255.0)
        self.assertLess(green, 0.2)
        self.assertLess(blue, 0.1)
        self.assertTrue(bool(image.get("pp_combined_rgba_premultiplied", False)))


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
