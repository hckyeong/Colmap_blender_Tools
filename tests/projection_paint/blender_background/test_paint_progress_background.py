"""Blender Background에서 Paint 준비 진행 세션과 Overlay 값을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tools.projection_paint.blender.paint_progress_session import (  # noqa: E402
    create_paint_progress_session,
    paint_progress_session_count,
    release_paint_progress_session,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    PaintSessionProgress,
    _paint_progress_overlay_view,
)


class _AbortableProgressJob:
    """진행 세션의 중단 호출 여부를 기록하는 최소 테스트 작업입니다."""

    def __init__(self) -> None:
        """중간 Paint 준비 진행값과 미완료 상태를 준비합니다."""

        self.progress = PaintSessionProgress(
            "Creating PP_UDIM UV",
            7,
            16,
            "타일 4개 · 폴리곤 500,102개",
        )
        self.done = False
        self.aborted = False

    def abort(self) -> None:
        """진행 세션이 작업 중단을 전달했음을 기록합니다."""

        self.aborted = True
        self.done = True

    @property
    def progress_total_units(self) -> int:
        """최초 Paint 준비 Modal이 표시하는 마지막 단위를 반환합니다."""

        return 11

    @property
    def progress_title(self) -> str:
        """최초 Paint 준비 중앙 진행창 제목을 반환합니다."""

        return "Texture Paint Preparation"


class PaintProgressBackgroundTests(unittest.TestCase):
    """Paint 준비 Overlay 표시값과 Registry 정리를 검증합니다."""

    def test_overlay_uses_job_progress_and_release_aborts_job(self) -> None:
        """중앙 Overlay가 실제 작업값을 표시하고 해제가 미완료 Job을 중단합니다."""

        scene = bpy.data.scenes.new("PP_UDIM_PROGRESS_TEST")
        job = _AbortableProgressJob()
        session = create_paint_progress_session(scene, job)
        try:
            view = _paint_progress_overlay_view(session)
            self.assertEqual("Texture Paint Preparation", view.title)
            self.assertEqual("Creating PP_UDIM UV", view.phase)
            self.assertEqual(7 / 11, view.fraction)
            self.assertEqual((7, 11), (view.completed_units, view.total_units))
            self.assertEqual(1, paint_progress_session_count())
        finally:
            release_paint_progress_session(session.job_id)
            bpy.data.scenes.remove(scene)

        self.assertTrue(job.aborted)
        self.assertEqual(0, paint_progress_session_count())


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
