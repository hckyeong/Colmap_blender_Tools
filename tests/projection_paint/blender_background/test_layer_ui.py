"""Blender Background에서 논리 Paint Layer 목록 UI 데이터를 검증합니다."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import layers as layers_module  # noqa: E402
from tools.projection_paint.blender import paint_session as paint_session_module  # noqa: E402
from tools.projection_paint.blender.composite_layer import (  # noqa: E402
    composite_source_partition,
)
from tools.projection_paint.blender.layers import (  # noqa: E402
    PP_OT_layer_adjustment_remove_hovered,
    PP_OT_layer_select,
    _finalize_pending_layer_ui_selection_clear,
    _layer_ui_pointer_events,
    _layer_ui_pointer_releases,
    _layer_ui_tracked_windows,
    _last_layer_reset_message,
    _record_layer_ui_pointer_event,
    _restore_layer_ui_pointer_selection,
    active_layer_tree_item,
    adjustment_mask_row_selected,
    adjustment_row_selected,
    layer_mask_row_selected,
    layer_preview_active,
    layer_row_selected,
    mask_preview_active,
)
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    composite_layer_is_editable,
)
from tools.projection_paint.blender.panels import (  # noqa: E402
    PP_PT_layers,
    _draw_composite_source_rows,
    _layer_panel_adjustment_mask_indent,
    _layer_panel_child_indent,
    _layer_panel_display_layers,
    _layer_button_text,
    _panel_warning_lines,
    _preview_check_icon,
    _layer_panel_source_insertions,
    _layer_panel_source_rows,
    _source_child_status_icon,
)
from tools.projection_paint.blender.ui_messages import (  # noqa: E402
    CONFIRM_DIALOG_WIDTH,
    CONFIRM_MESSAGE_MAX_COLUMNS,
    invoke_wrapped_confirmation,
    justify_ui_line,
    ui_text_columns,
    wrap_ui_message,
)


class LayerUITests(unittest.TestCase):
    """Paint Layer 추가·삭제·순서 변경과 선택 Index 계약을 검증합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """Layer PropertyGroup과 Operator를 포함한 Extension을 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """테스트 Process에서 Extension 등록을 해제합니다."""
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트를 빈 Factory Scene과 빈 Layer 목록으로 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)
        layers_module._layer_ui_click_history.clear()
        _layer_ui_pointer_events.clear()
        _layer_ui_pointer_releases.clear()
        _layer_ui_tracked_windows.clear()

    def test_add_creates_named_layers_with_unique_uuids(self) -> None:
        """더하기 버튼이 연속 이름과 고유 UUID를 가진 Layer를 추가합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            self.assertEqual(
                bpy.ops.projection_paint.layer_add(),
                {"FINISHED"},
            )
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 3", "Layer 2", "Layer 1"],
        )
        uuids = [str(layer.layer_uuid) for layer in settings.layers]
        self.assertTrue(all(uuids))
        self.assertEqual(len(set(uuids)), 3)
        self.assertEqual(settings.active_layer_index, 0)
        self.assertTrue(all(layer.expanded for layer in settings.layers))
        self.assertTrue(all(layer.visible for layer in settings.layers))

    def test_export_texture_is_single_button_below_composite_actions(self) -> None:
        """Layers 패널은 Composite 작업 아래에 File Browser Export 버튼 하나만 표시합니다."""
        draw_source = inspect.getsource(PP_PT_layers.draw)
        self.assertEqual(
            draw_source.count('"projection_paint.texture_export"'),
            1,
        )
        self.assertGreater(
            draw_source.index('"projection_paint.texture_export"'),
            draw_source.index('"projection_paint.composite_layer_create"'),
        )
        self.assertIn('text="Export Texture"', draw_source)

    def test_all_confirmation_messages_use_bounded_sentence_wrapping(self) -> None:
        """공통 확인창은 한글 폭과 문장 경계를 반영해 제한 폭 안에서 표시합니다."""
        message = (
            "기존 Source 2개와 관련 리소스를 새 결과 검증 후 삭제합니다. "
            "현재 Composite 출력만 평면 Layer로 남으며, 삭제된 Source는 Composite 해제로 복구되지 않습니다. "
            "되돌리기는 Blender Undo 기록이 있을 때만 가능합니다. "
            "예상 추가 메모리: 최소 512.0 MiB"
        )
        lines = wrap_ui_message(message)

        self.assertGreater(len(lines), 3)
        self.assertTrue(
            all(
                ui_text_columns(line) <= CONFIRM_MESSAGE_MAX_COLUMNS
                for line in lines
            )
        )
        self.assertEqual(" ".join(lines), message)
        self.assertTrue(any(line.endswith("삭제합니다.") for line in lines))
        self.assertTrue(any(line.endswith("가능합니다.") for line in lines))

        intermediate = next(
            line
            for line in lines[:-1]
            if line and not line.endswith((".", "!", "?"))
        )
        justified = justify_ui_line(intermediate)
        self.assertEqual(ui_text_columns(justified), CONFIRM_MESSAGE_MAX_COLUMNS)
        self.assertEqual(justified.split(), intermediate.split())

        calls = []
        window_manager = SimpleNamespace(
            invoke_props_dialog=lambda operator, **kwargs: (
                calls.append((operator, kwargs)) or {"RUNNING_MODAL"}
            )
        )
        operator = SimpleNamespace()
        result = invoke_wrapped_confirmation(
            operator,
            SimpleNamespace(window_manager=window_manager),
            title="Composite 다시 생성",
            message=message,
            confirm_text="Composite 다시 생성",
            icon="ERROR",
        )

        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(calls[0][1]["width"], CONFIRM_DIALOG_WIDTH)
        self.assertEqual(calls[0][1]["confirm_text"], "Composite 다시 생성")
        self.assertEqual(operator._wrapped_confirm_lines, lines)
        self.assertEqual(operator._wrapped_confirm_icon, "ERROR")

    def test_alpha_warning_wraps_into_scrollable_panel_rows(self) -> None:
        """긴 Alpha 경고는 말줄임표 대신 패널 세로 스크롤에 포함될 여러 행이 됩니다."""
        message = "Alpha를 제외하면 빈 영역이 불투명한 색상으로 합쳐질 수 있습니다."
        lines = _panel_warning_lines(message)

        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), message)
        self.assertTrue(all(ui_text_columns(line) <= 37 for line in lines))
        draw_source = inspect.getsource(PP_PT_layers.draw)
        self.assertIn("_draw_panel_warning(composite_options, alpha_warning)", draw_source)

    def test_confirmation_warning_icons_use_blender_52_enum(self) -> None:
        """공용 확인창은 Blender 5.2에서 유효한 상태 경고 아이콘만 사용합니다."""
        icon_items = bpy.types.UILayout.bl_rna.functions["label"].parameters[
            "icon"
        ].enum_items
        self.assertIn("STATUS_WARNING", icon_items.keys())
        self.assertNotIn("WARNING", icon_items.keys())
        for module in (layers_module, paint_session_module):
            source = inspect.getsource(module)
            self.assertNotIn('icon="WARNING"', source)

    def test_layer_and_adjustment_masks_use_one_to_one_child_schema(self) -> None:
        """Layer와 Adjustment Mask가 각각 하나의 자식 상태로 저장됩니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=layer.layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertTrue(layer.mask.mask_uuid)
        self.assertEqual(len(layer.adjustments), 0)
        self.assertTrue(layer_mask_row_selected(settings, 0))
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=layer.layer_uuid,
                adjustment_type="BRIGHTNESS",
            ),
            {"FINISHED"},
        )
        adjustment = layer.adjustments[0]
        self.assertEqual(
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=adjustment.adjustment_uuid,
            ),
            {"FINISHED"},
        )
        self.assertTrue(adjustment.mask.mask_uuid)
        self.assertNotEqual(adjustment.mask.mask_uuid, layer.mask.mask_uuid)
        self.assertTrue(adjustment_mask_row_selected(settings, 0, 0))
        self.assertFalse(layer_mask_row_selected(settings, 0))
        self.assertEqual(
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=adjustment.adjustment_uuid,
            ),
            {"CANCELLED"},
        )

    def test_collapsing_adjustment_mask_selects_parent_exclusively(self) -> None:
        """Adjustment Mask를 접으면 자식 선택을 지우고 부모 하나만 강조합니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = layer.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        self.assertTrue(adjustment_mask_row_selected(settings, 0, 0))

        self.assertEqual(
            bpy.ops.projection_paint.adjustment_toggle(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=adjustment.adjustment_uuid,
            ),
            {"FINISHED"},
        )

        self.assertFalse(adjustment.expanded)
        self.assertEqual(settings.active_mask_uuid, "")
        self.assertTrue(adjustment_row_selected(settings, 0, 0))
        self.assertFalse(adjustment_mask_row_selected(settings, 0, 0))
        self.assertFalse(layer_row_selected(settings, 0))

    def test_tree_parent_select_and_expand_state(self) -> None:
        """스크립트 직접 활성 선택과 자식 행 접기·펼치기가 Layer UUID를 따라갑니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        bpy.ops.projection_paint.layer_add()
        first_uuid = str(settings.layers[0].layer_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=first_uuid),
            {"FINISHED"},
        )
        self.assertEqual(settings.active_layer_index, 0)
        self.assertEqual(
            bpy.ops.projection_paint.layer_toggle(layer_uuid=first_uuid),
            {"FINISHED"},
        )
        self.assertFalse(settings.layers[0].expanded)
        self.assertEqual(
            bpy.ops.projection_paint.layer_toggle(layer_uuid=first_uuid),
            {"FINISHED"},
        )
        self.assertTrue(settings.layers[0].expanded)
        self.assertEqual(
            bpy.ops.projection_paint.layer_rename(
                layer_uuid=first_uuid,
                new_name="Renamed Layer",
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layers[0].name, "Renamed Layer")

    def test_parent_and_child_selection_are_mutually_exclusive(self) -> None:
        """활성 Paint Layer 변경은 Composite 후보 선택과 독립임을 검증합니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        self.assertFalse(layer_row_selected(settings, 0))
        self.assertTrue(layer_mask_row_selected(settings, 0))
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=layer.layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertFalse(layer_row_selected(settings, 0))
        self.assertFalse(layer_mask_row_selected(settings, 0))
        self.assertEqual(layer.active_adjustment_index, -1)

    def test_click_selection_matrix_and_composite_snapshot(self) -> None:
        """일반·Ctrl·Shift 선택 행렬과 Composite Snapshot이 ui_selected만 읽는지 검증합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(4):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        layer_0, layer_1, layer_2, layer_3 = settings.layers

        def invoke_layer_click(
            layer_uuid: str,
            value: str = "CLICK",
            *,
            ctrl: bool = False,
            shift: bool = False,
        ):
            """실제 Operator의 이벤트 분기만 작은 Blender 문맥에서 호출합니다."""
            operator = SimpleNamespace(
                layer_uuid=layer_uuid,
                report=lambda _types, _message: None,
            )
            operator._activate_paint_layer = lambda context: PP_OT_layer_select._activate_paint_layer(
                operator,
                context,
            )
            return PP_OT_layer_select.invoke(
                operator,
                bpy.context,
                SimpleNamespace(value=value, ctrl=ctrl, shift=shift),
            )

        self.assertEqual(
            invoke_layer_click(str(layer_0.layer_uuid)),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (True, False, False, False),
        )
        self.assertEqual(
            invoke_layer_click(str(layer_2.layer_uuid)),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (False, False, True, False),
        )
        self.assertEqual(settings.active_layer_index, 0)
        self.assertEqual(
            invoke_layer_click(str(layer_0.layer_uuid), ctrl=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (True, False, True, False),
        )
        self.assertEqual(
            invoke_layer_click(str(layer_2.layer_uuid), ctrl=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (True, False, False, False),
        )
        self.assertEqual(
            invoke_layer_click(str(layer_3.layer_uuid)),
            {"FINISHED"},
        )
        self.assertEqual(
            invoke_layer_click(str(layer_1.layer_uuid), shift=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (False, True, True, True),
        )
        self.assertEqual(
            invoke_layer_click(str(layer_0.layer_uuid)),
            {"FINISHED"},
        )
        self.assertEqual(
            invoke_layer_click(str(layer_3.layer_uuid), ctrl=True),
            {"FINISHED"},
        )
        self.assertEqual(
            invoke_layer_click(str(layer_1.layer_uuid), ctrl=True, shift=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (True, True, True, True),
        )

        self.assertEqual(
            invoke_layer_click(str(layer_0.layer_uuid)),
            {"FINISHED"},
        )
        self.assertEqual(
            invoke_layer_click(str(layer_2.layer_uuid), ctrl=True),
            {"FINISHED"},
        )
        partition = composite_source_partition(tuple(settings.layers))
        self.assertEqual(
            partition.source_layer_uuids,
            (str(layer_0.layer_uuid), str(layer_1.layer_uuid), str(layer_2.layer_uuid)),
        )
        self.assertEqual(
            partition.bake_included_layer_uuids,
            (str(layer_0.layer_uuid), str(layer_2.layer_uuid)),
        )

        settings.layer_selection_anchor_uuid = "missing-anchor"
        for layer in settings.layers:
            layer.ui_selected = False
        layer_0.ui_selected = True
        layer_2.ui_selected = True
        self.assertEqual(
            invoke_layer_click(str(layer_3.layer_uuid), shift=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (False, False, False, True),
        )
        self.assertEqual(settings.layer_selection_anchor_uuid, str(layer_3.layer_uuid))

        settings.layer_selection_anchor_uuid = "missing-anchor"
        layer_0.ui_selected = True
        self.assertEqual(
            invoke_layer_click(str(layer_2.layer_uuid), ctrl=True, shift=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (True, False, True, True),
        )
        self.assertEqual(settings.layer_selection_anchor_uuid, str(layer_2.layer_uuid))

    def test_pointer_tracker_snapshot_recovers_stripped_ui_modifiers(self) -> None:
        """실제 UI 버튼이 modifier를 잃어도 모달 입력 Snapshot으로 선택 규칙을 복원합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(4):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        layer_0, layer_1, layer_2, layer_3 = settings.layers

        def invoke_with_snapshot(
            layer,
            *,
            ctrl: bool = False,
            shift: bool = False,
            duplicate_press: bool = False,
        ):
            """후보를 비운 모달 PRESS와 modifier가 제거된 UI RELEASE를 재현합니다."""
            press_event = SimpleNamespace(
                type="LEFTMOUSE",
                value="PRESS",
                ctrl=ctrl,
                shift=shift,
                mouse_x=640,
                mouse_y=360,
            )
            _record_layer_ui_pointer_event(
                bpy.context.window,
                press_event,
                context=bpy.context,
            )
            if duplicate_press:
                _record_layer_ui_pointer_event(
                    bpy.context.window,
                    press_event,
                    context=bpy.context,
                )
            operator = SimpleNamespace(
                layer_uuid=str(layer.layer_uuid),
                report=lambda _types, _message: None,
            )
            operator._activate_paint_layer = lambda context: PP_OT_layer_select._activate_paint_layer(
                operator,
                context,
            )
            return PP_OT_layer_select.invoke(
                operator,
                bpy.context,
                SimpleNamespace(
                    type="LEFTMOUSE",
                    value="RELEASE",
                    ctrl=False,
                    shift=False,
                    mouse_x=640,
                    mouse_y=360,
                ),
            )

        self.assertEqual(invoke_with_snapshot(layer_0), {"FINISHED"})
        self.assertEqual(invoke_with_snapshot(layer_2, ctrl=True), {"FINISHED"})
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (True, False, True, False),
        )
        self.assertEqual(invoke_with_snapshot(layer_3, shift=True), {"FINISHED"})
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (False, False, True, True),
        )
        self.assertEqual(
            invoke_with_snapshot(layer_1, ctrl=True, shift=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (False, True, True, True),
        )
        self.assertEqual(invoke_with_snapshot(layer_1), {"FINISHED"})
        self.assertEqual(
            invoke_with_snapshot(layer_1, duplicate_press=True),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(4)),
            (False, True, False, False),
        )
        self.assertEqual(
            settings.active_layer_index,
            next(
                index
                for index, layer in enumerate(settings.layers)
                if str(layer.layer_uuid) == str(layer_1.layer_uuid)
            ),
        )
        self.assertFalse(_layer_ui_pointer_events)

    def test_pointer_tracker_accepts_small_press_release_coordinate_drift(self) -> None:
        """작은 손떨림이 있어도 같은 버튼 클릭의 modifier Snapshot을 소비합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        first_layer, _middle_layer, target_layer = settings.layers
        first_layer.ui_selected = True
        settings.layer_selection_anchor_uuid = str(first_layer.layer_uuid)

        _record_layer_ui_pointer_event(
            bpy.context.window,
            SimpleNamespace(
                type="LEFTMOUSE",
                value="PRESS",
                ctrl=True,
                shift=False,
                mouse_x=640,
                mouse_y=360,
            ),
            context=bpy.context,
        )
        operator = SimpleNamespace(
            layer_uuid=str(target_layer.layer_uuid),
            report=lambda _types, _message: None,
        )
        operator._activate_paint_layer = (
            lambda context: PP_OT_layer_select._activate_paint_layer(
                operator,
                context,
            )
        )

        self.assertEqual(
            PP_OT_layer_select.invoke(
                operator,
                bpy.context,
                SimpleNamespace(
                    type="LEFTMOUSE",
                    value="RELEASE",
                    ctrl=False,
                    shift=False,
                    mouse_x=644,
                    mouse_y=356,
                ),
            ),
            {"FINISHED"},
        )
        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(3)),
            (True, False, True),
        )
        self.assertFalse(_layer_ui_pointer_events)

    def test_click_outside_layer_name_clears_candidate_selection_and_anchor(self) -> None:
        """작은 좌표 이동이 있는 외부 클릭도 후보와 앵커를 해제합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        settings.layers[0].ui_selected = True
        settings.layers[2].ui_selected = True
        settings.layer_selection_anchor_uuid = str(settings.layers[2].layer_uuid)
        history_key = layers_module._layer_ui_click_history_key(bpy.context.scene)
        layers_module._layer_ui_click_history[history_key] = (
            str(settings.layers[2].layer_uuid),
            1.0,
        )

        _record_layer_ui_pointer_event(
            bpy.context.window,
            event := SimpleNamespace(
                type="LEFTMOUSE",
                value="PRESS",
                ctrl=False,
                shift=False,
                mouse_x=320,
                mouse_y=240,
            ),
            context=bpy.context,
        )

        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(3)),
            (True, False, True),
        )
        self.assertEqual(
            settings.layer_selection_anchor_uuid,
            str(settings.layers[2].layer_uuid),
        )
        self.assertIn(history_key, layers_module._layer_ui_click_history)
        event.value = "RELEASE"
        event.mouse_x += 4
        event.mouse_y -= 4
        _record_layer_ui_pointer_event(
            bpy.context.window,
            event,
            context=bpy.context,
        )
        self.assertIsNone(_finalize_pending_layer_ui_selection_clear())

        self.assertEqual(
            tuple(layer_row_selected(settings, index) for index in range(3)),
            (False, False, False),
        )
        self.assertEqual(settings.layer_selection_anchor_uuid, "")
        self.assertNotIn(history_key, layers_module._layer_ui_click_history)

    def test_unconfirmed_pointer_timeout_discards_input_without_deselecting(self) -> None:
        """좌표가 크게 달라 RELEASE가 미확정이면 만료 후에도 후보를 유지합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        selected_uuids = (
            str(settings.layers[0].layer_uuid),
            str(settings.layers[2].layer_uuid),
        )
        settings.layers[0].ui_selected = True
        settings.layers[2].ui_selected = True
        settings.layer_selection_anchor_uuid = selected_uuids[-1]
        history_key = layers_module._layer_ui_click_history_key(bpy.context.scene)
        expected_click_history = (selected_uuids[-1], 1.0)
        layers_module._layer_ui_click_history[history_key] = expected_click_history

        event = SimpleNamespace(
            type="LEFTMOUSE",
            value="PRESS",
            ctrl=False,
            shift=False,
            mouse_x=320,
            mouse_y=240,
        )
        with patch.object(layers_module.time, "monotonic", return_value=10.0):
            _record_layer_ui_pointer_event(
                bpy.context.window,
                event,
                context=bpy.context,
            )
        event.value = "RELEASE"
        event.mouse_x = 340
        with patch.object(layers_module.time, "monotonic", return_value=10.1):
            _record_layer_ui_pointer_event(
                bpy.context.window,
                event,
                context=bpy.context,
            )
        self.assertFalse(_layer_ui_pointer_releases)

        with patch.object(layers_module.time, "monotonic", return_value=10.6):
            self.assertIsNone(_finalize_pending_layer_ui_selection_clear())

        self.assertEqual(
            tuple(
                str(layer.layer_uuid)
                for layer in settings.layers
                if bool(layer.ui_selected)
            ),
            selected_uuids,
        )
        self.assertEqual(settings.layer_selection_anchor_uuid, selected_uuids[-1])
        self.assertEqual(
            layers_module._layer_ui_click_history.get(history_key),
            expected_click_history,
        )
        self.assertFalse(_layer_ui_pointer_events)

    def test_layer_related_button_can_restore_pointer_candidate_snapshot(self) -> None:
        """Composite 같은 선택 의존 버튼은 같은 PRESS의 후보와 앵커를 복원할 수 있습니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        selected_uuids = (
            str(settings.layers[0].layer_uuid),
            str(settings.layers[2].layer_uuid),
        )
        settings.layers[0].ui_selected = True
        settings.layers[2].ui_selected = True
        settings.layer_selection_anchor_uuid = selected_uuids[-1]
        history_key = layers_module._layer_ui_click_history_key(bpy.context.scene)
        expected_click_history = (selected_uuids[-1], 1.0)
        layers_module._layer_ui_click_history[history_key] = expected_click_history
        event = SimpleNamespace(
            type="LEFTMOUSE",
            value="PRESS",
            ctrl=False,
            shift=False,
            mouse_x=400,
            mouse_y=280,
        )

        _record_layer_ui_pointer_event(
            bpy.context.window,
            event,
            context=bpy.context,
        )
        self.assertIsNotNone(_restore_layer_ui_pointer_selection(bpy.context, event))

        self.assertEqual(
            tuple(
                str(layer.layer_uuid)
                for layer in settings.layers
                if bool(layer.ui_selected)
            ),
            selected_uuids,
        )
        self.assertEqual(settings.layer_selection_anchor_uuid, selected_uuids[-1])
        self.assertEqual(
            layers_module._layer_ui_click_history.get(history_key),
            expected_click_history,
        )
        self.assertFalse(_layer_ui_pointer_events)

    def test_double_click_activates_paint_image_without_reselecting_candidate(self) -> None:
        """더블클릭은 활성 Image만 바꾸고 첫 일반 클릭의 후보 선택을 유지하는지 검증합니다."""
        from tools.projection_paint.blender.layer_shader import (
            add_layer_transaction,
        )
        from tools.projection_paint.blender.material_setup import (
            prepare_active_material,
        )
        from tools.projection_paint.blender.paint_state import (
            commit_paint_setup,
        )
        from tools.projection_paint.blender.layer_shader import (
            reconcile_layer_graph,
        )

        mesh = bpy.data.meshes.new("LayerSelectionTargetMesh")
        mesh.from_pydata(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            (),
            ((0, 1, 2),),
        )
        mesh.uv_layers.new(name="UVMap")
        target = bpy.data.objects.new("LayerSelectionTarget", mesh)
        bpy.context.scene.collection.objects.link(target)
        material = bpy.data.materials.new("LayerSelectionMaterial")
        material.use_nodes = True
        target.data.materials.append(material)
        setup = prepare_active_material(target, 64, 32, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)
        setup.commit()
        settings = bpy.context.scene.projection_paint
        first_layer = settings.layers[0]
        add_layer_transaction(bpy.context, "Layer 2", "selection-layer-2")
        target_layer = settings.layers[0]
        self.assertIs(settings.active_paint_image, target_layer.image)

        settings.active_layer_index = next(
            index
            for index, layer in enumerate(settings.layers)
            if str(layer.layer_uuid) == str(first_layer.layer_uuid)
        )
        settings.active_paint_image = first_layer.image
        settings.overlay_image = first_layer.image

        def invoke_layer_click(value: str):
            """실제 Operator의 이벤트 분기만 필요한 Layer로 호출합니다."""
            operator = SimpleNamespace(
                layer_uuid=str(target_layer.layer_uuid),
                report=lambda _types, _message: None,
            )
            operator._activate_paint_layer = lambda context: PP_OT_layer_select._activate_paint_layer(
                operator,
                context,
            )
            return PP_OT_layer_select.invoke(
                operator,
                bpy.context,
                SimpleNamespace(value=value, ctrl=False, shift=False),
            )

        self.assertEqual(invoke_layer_click("RELEASE"), {"FINISHED"})
        self.assertTrue(target_layer.ui_selected)
        self.assertIs(settings.active_paint_image, first_layer.image)
        self.assertEqual(invoke_layer_click("RELEASE"), {"FINISHED"})
        self.assertIs(settings.active_paint_image, target_layer.image)
        self.assertEqual(settings.active_layer_index, 0)
        self.assertTrue(target_layer.ui_selected)

        settings.active_layer_index = next(
            index
            for index, layer in enumerate(settings.layers)
            if str(layer.layer_uuid) == str(first_layer.layer_uuid)
        )
        settings.active_paint_image = first_layer.image
        settings.overlay_image = first_layer.image
        target_layer.ui_selected = False
        self.assertEqual(invoke_layer_click("DOUBLE_CLICK"), {"FINISHED"})
        self.assertIs(settings.active_paint_image, target_layer.image)
        self.assertFalse(target_layer.ui_selected)

    def test_mask_preview_highlight_is_exclusive(self) -> None:
        """저장된 단일 UUID와 일치하는 Mask만 미리보기 활성 상태가 됩니다."""
        settings = bpy.context.scene.projection_paint
        settings.mask_preview_adjustment_uuid = "mask-a"
        self.assertTrue(mask_preview_active(settings, "mask-a"))
        self.assertFalse(mask_preview_active(settings, "mask-b"))
        settings.mask_preview_adjustment_uuid = ""
        self.assertFalse(mask_preview_active(settings, "mask-a"))

    def test_layer_and_mask_preview_helpers_share_one_state_group(self) -> None:
        """전역 단독 보기에서 저장된 한 Layer 또는 Mask만 활성 표시됩니다."""
        settings = bpy.context.scene.projection_paint
        settings.layer_preview_uuid = "layer-a"
        settings.mask_preview_adjustment_uuid = ""
        self.assertTrue(layer_preview_active(settings, "layer-a"))
        self.assertFalse(layer_preview_active(settings, "layer-b"))
        self.assertFalse(mask_preview_active(settings, "mask-a"))
        settings.layer_preview_uuid = ""
        settings.mask_preview_adjustment_uuid = "mask-a"
        self.assertFalse(layer_preview_active(settings, "layer-a"))
        self.assertTrue(mask_preview_active(settings, "mask-a"))

    def test_composite_source_expand_state_is_stored_per_composite_layer(self) -> None:
        """Composite 가상 Source 폴더의 펼침 상태는 해당 Composite Layer에 저장됩니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        first_uuid = str(settings.layers[0].layer_uuid)
        bpy.ops.projection_paint.layer_add()
        second_uuid = str(settings.layers[0].layer_uuid)
        first = next(
            layer for layer in settings.layers if str(layer.layer_uuid) == first_uuid
        )
        second = next(
            layer for layer in settings.layers if str(layer.layer_uuid) == second_uuid
        )
        first.composite_source_expanded = False
        self.assertFalse(first.composite_source_expanded)
        self.assertTrue(second.composite_source_expanded)

    def test_composite_ui_keeps_dependent_siblings_at_top_level(self) -> None:
        """부분 Source만 숨기고 구형 파생 항목도 최상위 형제로 표시합니다."""
        composite = SimpleNamespace(
            name="Composite Layer",
            layer_role="COMPOSITE",
            composite_session_uuid="session-a",
        )
        derived = SimpleNamespace(
            name="Layer 4",
            layer_role="COMPOSITE_DERIVED",
            composite_session_uuid="session-a",
        )
        unrelated_derived = SimpleNamespace(
            name="Unrelated Derived",
            layer_role="COMPOSITE_DERIVED",
            composite_session_uuid="session-b",
        )
        external = SimpleNamespace(
            name="Layer 5",
            layer_role="NORMAL",
            composite_session_uuid="",
            composite_source_session_uuid="",
        )
        source = SimpleNamespace(
            name="Layer 3",
            layer_role="NORMAL",
            composite_session_uuid="",
            composite_source_session_uuid="session-a",
        )
        settings = SimpleNamespace(
            composite_state="ACTIVE",
            active_composite_session_uuid="session-a",
            composite_source_schema_version=1,
            layers=(derived, external, source, composite, unrelated_derived),
        )

        display_layers = _layer_panel_display_layers(settings)

        self.assertEqual(
            [(layer.name, composite_child) for _index, layer, composite_child in display_layers],
            [
                ("Layer 4", False),
                ("Layer 5", False),
                ("Composite Layer", False),
                ("Unrelated Derived", False),
            ],
        )
        self.assertFalse(composite_layer_is_editable(settings, derived))
        self.assertFalse(composite_layer_is_editable(settings, source))
        self.assertTrue(composite_layer_is_editable(settings, external))
        self.assertEqual(
            [
                (display_index, item.name)
                for display_index, item in _layer_panel_source_insertions(display_layers)
            ],
            [(2, "Composite Layer")],
        )

    def test_composite_children_share_source_depth_and_adjustment_mask_nests(self) -> None:
        """Composite의 Mask·Adjustment는 Source와 같은 깊이이며 Mask는 한 단계 더 들어갑니다."""
        composite = SimpleNamespace(layer_role="COMPOSITE")
        derived = SimpleNamespace(layer_role="COMPOSITE_DERIVED")
        normal = SimpleNamespace(layer_role="NORMAL")

        self.assertEqual(_layer_panel_child_indent(composite), 0.12)
        self.assertEqual(_layer_panel_child_indent(derived), 0.22)
        self.assertEqual(_layer_panel_child_indent(normal), 0.22)
        self.assertEqual(_layer_panel_adjustment_mask_indent(composite), 0.22)
        self.assertEqual(_layer_panel_adjustment_mask_indent(derived), 0.34)

    def test_composite_source_rows_are_always_last_and_nested(self) -> None:
        """Source 가상 행은 부분 Source를 순서대로 최하단 깊이에 표시합니다."""
        composite = SimpleNamespace(composite_source_expanded=True)
        source_one = SimpleNamespace(name="Layer 4", composite_bake_included=True)
        source_two = SimpleNamespace(name="Layer 3", composite_bake_included=False)
        source_three = SimpleNamespace(name="Layer 2", composite_bake_included=True)

        source_rows = _layer_panel_source_rows(
            composite,
            (source_one, source_two, source_three),
        )

        self.assertEqual(
            [(row_type, getattr(source, "name", None), indent) for row_type, source, indent in source_rows],
            [
                ("SOURCE", None, 0.12),
                ("SOURCE_CHILD", "Layer 4", 0.22),
                ("SOURCE_CHILD", "Layer 3", 0.22),
                ("SOURCE_CHILD", "Layer 2", 0.22),
            ],
        )
        self.assertEqual(_source_child_status_icon(source_one), None)
        self.assertEqual(_source_child_status_icon(source_two), "HIDE_ON")
        self.assertEqual(_source_child_status_icon(source_three), None)
        legacy_visible = SimpleNamespace(composite_saved_visible=True)
        legacy_hidden = SimpleNamespace(composite_saved_visible=False)
        self.assertEqual(
            _source_child_status_icon(legacy_visible, source_schema=0),
            None,
        )
        self.assertEqual(
            _source_child_status_icon(legacy_hidden, source_schema=0),
            "HIDE_ON",
        )
        composite.composite_source_expanded = False
        self.assertEqual(
            _layer_panel_source_rows(
                composite,
                (source_one, source_two, source_three),
            ),
            (("SOURCE", None, 0.12),),
        )

    def test_composite_source_ui_marks_virtual_row_and_children_inactive(self) -> None:
        """Source UI는 가상 행과 자식을 어둡게 표시하고 숨긴 자식만 눈 상태를 표시합니다."""
        draw_source = inspect.getsource(PP_PT_layers.draw)
        draw_source_rows = inspect.getsource(_draw_composite_source_rows)

        self.assertIn("source_branch.enabled = False", draw_source_rows)
        self.assertIn("source_label.enabled = False", draw_source_rows)
        self.assertIn('source_label.label(text="Source")', draw_source_rows)
        self.assertIn('if row_type == "SOURCE_CHILD":', draw_source_rows)
        self.assertIn("source_container.enabled = False", draw_source_rows)
        self.assertIn("source_insertions = {", draw_source)
        self.assertIn("source_layer = source_insertions.get(display_index)", draw_source)
        self.assertIn("if composite_child:", draw_source)
        self.assertIn("parent.enabled = layer_editable", draw_source)
        self.assertIn("source_row.label(text=str(source.name))", draw_source_rows)
        self.assertIn("source_container.enabled = False", draw_source_rows)
        self.assertIn("_source_child_status_icon(source, source_schema=source_schema)", draw_source_rows)
        self.assertIn('source_row.label(text="", icon=status_icon)', draw_source_rows)
        self.assertIn('return "HIDE_ON"', inspect.getsource(_source_child_status_icon))
        self.assertNotIn("FILE_FOLDER", draw_source_rows)
        self.assertNotIn('"포함"', draw_source_rows)
        self.assertNotIn('"제외"', draw_source_rows)

    def test_mask_preview_icon_is_visible_only_when_active(self) -> None:
        """Mask 미리보기 체크가 활성일 때만 보이고 비활성일 때 자리를 유지합니다."""
        self.assertEqual(_preview_check_icon(True), "CHECKMARK")
        self.assertEqual(_preview_check_icon(False), "BLANK1")

    def test_active_layer_check_uses_button_text_without_separate_icon_column(self) -> None:
        """활성 Layer 체크는 이름 버튼 안에만 표시하고 별도 열을 만들지 않는지 검증합니다."""
        self.assertEqual(_layer_button_text("Layer 1", True), "Layer 1  ✓")
        self.assertEqual(_layer_button_text("Layer 1", False), "Layer 1")
        draw_source = inspect.getsource(PP_PT_layers.draw)
        self.assertIn("text=_layer_button_text(", draw_source)
        self.assertNotIn('parent.label(text="", icon="CHECKMARK")', draw_source)

    def test_last_layer_reset_warning_names_new_resolution(self) -> None:
        """마지막 Layer 경고가 전체 작업 삭제와 새 해상도를 명시합니다."""
        message = _last_layer_reset_message("Layer 1", "8K")
        self.assertIn("Layer 1과 모든 Mask·보정 작업을 삭제", message)
        self.assertIn("새 8K Layer 1", message)
        self.assertIn("Blender Undo 기록", message)

    def test_remove_updates_active_index_and_reuses_available_name(self) -> None:
        """빼기 버튼이 선택 Layer를 지우고 활성 Index와 기본 이름을 보정합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            bpy.ops.projection_paint.layer_add()
        settings.active_layer_index = 1
        self.assertEqual(
            bpy.ops.projection_paint.layer_remove(),
            {"FINISHED"},
        )
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 3", "Layer 1"],
        )
        self.assertEqual(settings.active_layer_index, 1)
        bpy.ops.projection_paint.layer_add()
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 2", "Layer 3", "Layer 1"],
        )
        self.assertEqual(settings.active_layer_index, 0)

    def test_remove_button_deletes_only_selected_child_item(self) -> None:
        """빼기 버튼이 선택된 Mask·Adjustment 자식만 제거하도록 검증합니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        self.assertEqual(active_layer_tree_item(settings)[0], "LAYER_MASK")
        self.assertEqual(
            bpy.ops.projection_paint.layer_remove("INVOKE_DEFAULT"),
            {"FINISHED"},
        )
        self.assertEqual(len(settings.layers), 1)
        self.assertFalse(str(layer.mask.mask_uuid or ""))

        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = layer.adjustments[0]
        self.assertEqual(active_layer_tree_item(settings)[0], "ADJUSTMENT")
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        self.assertEqual(len(settings.layers), 1)
        self.assertEqual(len(layer.adjustments), 0)

        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="EXPOSURE",
        )
        adjustment = layer.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        self.assertEqual(active_layer_tree_item(settings)[0], "ADJUSTMENT_MASK")
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        self.assertEqual(len(settings.layers), 1)
        self.assertEqual(len(layer.adjustments), 1)
        self.assertFalse(str(adjustment.mask.mask_uuid or ""))

    def test_move_reorders_collection_and_preserves_layer_identity(self) -> None:
        """위·아래 버튼이 UUID를 유지하며 실제 Collection 순서를 변경합니다."""
        settings = bpy.context.scene.projection_paint
        for _index in range(3):
            bpy.ops.projection_paint.layer_add()
        original_uuids = [layer.layer_uuid for layer in settings.layers]
        settings.active_layer_index = 1
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="UP"),
            {"FINISHED"},
        )
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 2", "Layer 3", "Layer 1"],
        )
        self.assertEqual(
            [layer.layer_uuid for layer in settings.layers],
            [original_uuids[1], original_uuids[0], original_uuids[2]],
        )
        self.assertEqual(settings.active_layer_index, 0)
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="DOWN"),
            {"FINISHED"},
        )
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 3", "Layer 2", "Layer 1"],
        )
        self.assertEqual(settings.active_layer_index, 1)

    def test_move_refuses_to_cross_collection_boundaries(self) -> None:
        """첫 Layer의 위 이동과 마지막 Layer의 아래 이동을 안전하게 거부합니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        bpy.ops.projection_paint.layer_add()
        settings.active_layer_index = 0
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="UP"),
            {"CANCELLED"},
        )
        settings.active_layer_index = 1
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="DOWN"),
            {"CANCELLED"},
        )
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 2", "Layer 1"],
        )

    def test_layer_items_are_unique_and_do_not_create_shader_data(self) -> None:
        """Mask와 세 보정 UI가 UUID와 함께 저장되고 Shader Data는 만들지 않습니다."""
        settings = bpy.context.scene.projection_paint
        material_count = len(bpy.data.materials)
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=layer.layer_uuid,
            ),
            {"FINISHED"},
        )
        for adjustment_type in ("BRIGHTNESS", "EXPOSURE", "SATURATION"):
            self.assertEqual(
                bpy.ops.projection_paint.layer_adjustment_add(
                    layer_uuid=layer.layer_uuid,
                    adjustment_type=adjustment_type,
                ),
                {"FINISHED"},
            )
        self.assertEqual(
            [item.adjustment_type for item in layer.adjustments],
            ["BRIGHTNESS", "EXPOSURE", "SATURATION"],
        )
        adjustment_uuids = [
            str(item.adjustment_uuid)
            for item in layer.adjustments
        ]
        self.assertTrue(all(adjustment_uuids))
        self.assertEqual(len(set(adjustment_uuids)), 3)
        self.assertEqual(layer.active_adjustment_index, 2)
        self.assertTrue(layer.mask.enabled)
        layer.mask.enabled = False
        self.assertFalse(layer.mask.enabled)
        self.assertEqual(layer.adjustments[0].brightness, 0.0)
        self.assertEqual(layer.adjustments[1].exposure, 0.0)
        self.assertEqual(layer.adjustments[2].hue, 0.5)
        self.assertEqual(layer.adjustments[2].saturation, 1.0)
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_select(
                layer_uuid=layer.layer_uuid,
                mask_uuid=layer.mask.mask_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(layer.active_adjustment_index, -1)
        layer.adjustments[0].brightness = 25.0
        self.assertEqual(layer.adjustments[0].brightness, 25.0)
        self.assertEqual(len(bpy.data.materials), material_count)
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=layer.layer_uuid,
            ),
            {"CANCELLED"},
        )
        self.assertEqual(len(layer.adjustments), 3)

    def test_remove_layer_item_preserves_nearest_selection(self) -> None:
        """아이콘 삭제가 항목을 제거하고 활성 Index를 가까운 항목으로 보정합니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        for adjustment_type in ("BRIGHTNESS", "EXPOSURE", "SATURATION"):
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=layer.layer_uuid,
                adjustment_type=adjustment_type,
            )
        layer.active_adjustment_index = 1
        brightness_uuid = str(layer.adjustments[1].adjustment_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_remove(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=brightness_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(
            [item.adjustment_type for item in layer.adjustments],
            ["BRIGHTNESS", "SATURATION"],
        )
        self.assertEqual(layer.active_adjustment_index, 1)
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_remove(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=layer.adjustments[0].adjustment_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(layer.active_adjustment_index, 0)
        self.assertEqual(layer.adjustments[0].adjustment_type, "SATURATION")
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_remove(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=layer.adjustments[0].adjustment_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(layer.active_adjustment_index, -1)

    def test_hovered_delete_is_scoped_to_layer_item_icon(self) -> None:
        """Delete 연산이 Layer 보조 아이콘 Context에서만 항목을 제거합니다."""
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        button_operator = SimpleNamespace(
            bl_rna=SimpleNamespace(
                identifier="PROJECTION_PAINT_OT_layer_adjustment_select",
            ),
            layer_uuid=str(layer.layer_uuid),
            adjustment_uuid=str(layer.adjustments[0].adjustment_uuid),
        )
        context = SimpleNamespace(
            scene=bpy.context.scene,
            button_operator=button_operator,
        )
        self.assertTrue(PP_OT_layer_adjustment_remove_hovered.poll(context))
        self.assertEqual(
            PP_OT_layer_adjustment_remove_hovered.execute(
                SimpleNamespace(),
                context,
            ),
            {"FINISHED"},
        )
        self.assertEqual(len(layer.adjustments), 0)
        unrelated_context = SimpleNamespace(
            scene=bpy.context.scene,
            button_operator=SimpleNamespace(
                bl_rna=SimpleNamespace(identifier="OBJECT_OT_delete"),
            ),
        )
        self.assertFalse(
            PP_OT_layer_adjustment_remove_hovered.poll(unrelated_context)
        )


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
