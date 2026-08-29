from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.modifier.blender.axis_display import (
    BLENDER_AXIS_PRESET,
    CUSTOM_AXIS_PRESET,
    DEFAULT_AXIS_PRESET,
    axes_for_preset,
    axis_preset_items,
    axis_summary,
    infer_legacy_axis_preset,
)


class DisplayAxisPresetTests(unittest.TestCase):
    """Display Axis 프리셋의 순수 데이터 계약을 검증합니다."""

    def test_preset_order_and_axes_match_ui_contract(self) -> None:
        """프리셋 순서와 축 값이 승인된 UI 계약과 일치합니다."""

        self.assertEqual(
            (
                DEFAULT_AXIS_PRESET,
                BLENDER_AXIS_PRESET,
                "MAYA",
                "HOUDINI",
                CUSTOM_AXIS_PRESET,
            ),
            tuple(item[0] for item in axis_preset_items()),
        )
        self.assertEqual(("Z", "-Y"), axes_for_preset(DEFAULT_AXIS_PRESET))
        self.assertEqual(("Y", "Z"), axes_for_preset(BLENDER_AXIS_PRESET))
        self.assertEqual(("-Z", "Y"), axes_for_preset("MAYA"))
        self.assertEqual(("-Z", "Y"), axes_for_preset("HOUDINI"))
        self.assertIsNone(axes_for_preset(CUSTOM_AXIS_PRESET))

    def test_unknown_preset_is_rejected(self) -> None:
        """정의되지 않은 프리셋을 조용히 Custom으로 처리하지 않습니다."""

        with self.assertRaises(ValueError):
            axes_for_preset("UNKNOWN")

    def test_legacy_inference_only_selects_unambiguous_presets(self) -> None:
        """구형 축은 RC와 Blender만 추론하고 나머지는 Custom으로 보존합니다."""

        self.assertEqual(
            BLENDER_AXIS_PRESET,
            infer_legacy_axis_preset("Y", "Z"),
        )
        self.assertEqual(
            DEFAULT_AXIS_PRESET,
            infer_legacy_axis_preset("Z", "-Y"),
        )
        self.assertEqual(
            CUSTOM_AXIS_PRESET,
            infer_legacy_axis_preset("-Z", "Y"),
        )
        self.assertEqual(
            CUSTOM_AXIS_PRESET,
            infer_legacy_axis_preset("X", "Z"),
        )

    def test_axis_summary_uses_resolved_axes(self) -> None:
        """UI 요약은 프리셋 이름이 아니라 현재 적용 축을 표시합니다."""

        self.assertEqual("Forward: Z / Up: -Y", axis_summary("Z", "-Y"))


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
