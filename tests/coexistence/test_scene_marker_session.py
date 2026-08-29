"""공용 Scene Timeline Marker 단일 소유권 계약을 검증합니다."""

from __future__ import annotations

import gc
import unittest
import weakref

from shared.blender_runtime import (
    SceneMarkerSessionError,
    acquire_scene_marker_session,
    clear_scene_marker_sessions,
    release_scene_marker_session,
    scene_marker_session_owned_by,
    scene_marker_session_owner,
)


class _FakeRna:
    """Blender 없이 RNA Pointer와 삭제 상태를 재현합니다."""

    def __init__(self, pointer: int, name: str) -> None:
        """테스트용 Pointer와 표시 이름을 저장합니다."""

        self._pointer = pointer
        self.name = name
        self.deleted = False

    def as_pointer(self) -> int:
        """삭제된 RNA 접근은 Blender와 같이 ReferenceError를 발생시킵니다."""

        if self.deleted:
            raise ReferenceError("RNA가 삭제되었습니다.")
        return self._pointer


class _DeletedRnaProxy:
    """속성 접근 자체가 실패하는 삭제된 Blender RNA Proxy를 재현합니다."""

    def __getattribute__(self, _name: str) -> object:
        """모든 RNA 속성 접근에서 ReferenceError를 발생시킵니다."""

        raise ReferenceError("RNA가 삭제되었습니다.")


class SceneMarkerSessionTests(unittest.TestCase):
    """Scene Pointer별 Marker Lease의 획득·차단·해제를 검사합니다."""

    def setUp(self) -> None:
        """각 테스트 전에 전역 Runtime Lease를 비웁니다."""

        clear_scene_marker_sessions()

    def tearDown(self) -> None:
        """각 테스트 뒤 전역 Runtime Lease를 비웁니다."""

        clear_scene_marker_sessions()

    def test_acquire_and_same_tool_window_reacquire_are_idempotent(self) -> None:
        """같은 Scene·도구·Window 재획득은 동일 Lease를 반환합니다."""

        scene = _FakeRna(101, "공용 Scene")
        window = _FakeRna(201, "주 Window")

        first = acquire_scene_marker_session(scene, "modifier", window)
        second = acquire_scene_marker_session(scene, "modifier", window)

        self.assertIs(first, second)
        self.assertTrue(scene_marker_session_owned_by(scene, "modifier"))
        self.assertEqual("modifier", scene_marker_session_owner(scene))

    def test_other_tool_cannot_acquire_owned_scene(self) -> None:
        """다른 도구는 이미 소유된 Scene의 Marker를 동시에 획득할 수 없습니다."""

        scene = _FakeRna(102, "교차 도구 Scene")
        window = _FakeRna(202, "주 Window")
        acquire_scene_marker_session(scene, "modifier", window)

        with self.assertRaisesRegex(SceneMarkerSessionError, "이미.*modifier.*projection_paint"):
            acquire_scene_marker_session(scene, "projection_paint", window)

    def test_same_tool_cannot_acquire_from_other_window(self) -> None:
        """같은 도구도 다른 Window에서는 같은 Scene Marker를 획득할 수 없습니다."""

        scene = _FakeRna(103, "다중 Window Scene")
        acquire_scene_marker_session(scene, "modifier", _FakeRna(203, "첫 Window"))

        with self.assertRaisesRegex(SceneMarkerSessionError, "다른 Blender Window"):
            acquire_scene_marker_session(scene, "modifier", _FakeRna(204, "둘째 Window"))

    def test_release_requires_exact_owner_and_unowned_release_is_idempotent(self) -> None:
        """타 도구 해제는 차단하고 소유자 및 미소유 해제는 안전하게 처리합니다."""

        scene = _FakeRna(104, "해제 Scene")
        acquire_scene_marker_session(scene, "modifier")

        with self.assertRaisesRegex(SceneMarkerSessionError, "modifier.*projection_paint"):
            release_scene_marker_session(scene, "projection_paint")
        self.assertEqual("modifier", scene_marker_session_owner(scene))

        release_scene_marker_session(scene, "modifier")
        release_scene_marker_session(scene, "modifier")
        self.assertEqual("", scene_marker_session_owner(scene))

    def test_clear_releases_every_scene(self) -> None:
        """전체 Clear는 서로 다른 Scene의 모든 Lease를 폐기합니다."""

        first_scene = _FakeRna(105, "첫 Scene")
        second_scene = _FakeRna(106, "둘째 Scene")
        acquire_scene_marker_session(first_scene, "modifier")
        acquire_scene_marker_session(second_scene, "projection_paint")

        clear_scene_marker_sessions()

        self.assertEqual("", scene_marker_session_owner(first_scene))
        self.assertEqual("", scene_marker_session_owner(second_scene))

    def test_deleted_rna_is_safe_and_runtime_does_not_retain_blender_ids(self) -> None:
        """삭제된 RNA 조회는 안전하며 Lease는 Scene과 Window 객체를 강하게 참조하지 않습니다."""

        scene = _FakeRna(107, "삭제 Scene")
        window = _FakeRna(207, "삭제 Window")
        scene_reference = weakref.ref(scene)
        window_reference = weakref.ref(window)
        lease = acquire_scene_marker_session(scene, "modifier", window)
        scene.deleted = True

        self.assertEqual("", scene_marker_session_owner(scene))
        self.assertFalse(scene_marker_session_owned_by(scene, "modifier"))
        release_scene_marker_session(scene, "modifier")

        del scene
        del window
        gc.collect()
        self.assertIsNone(scene_reference())
        self.assertIsNone(window_reference())
        self.assertEqual(107, lease.scene_pointer)
        self.assertEqual(207, lease.window_pointer)

    def test_fully_invalid_deleted_rna_proxy_is_safe(self) -> None:
        """속성 접근부터 실패하는 삭제 RNA Proxy도 조회와 해제에서 예외를 내지 않습니다."""

        deleted_scene = _DeletedRnaProxy()

        self.assertEqual("", scene_marker_session_owner(deleted_scene))
        self.assertFalse(scene_marker_session_owned_by(deleted_scene, "modifier"))
        release_scene_marker_session(deleted_scene, "modifier")


if __name__ == "__main__":
    unittest.main()
