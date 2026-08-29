"""실제 COLMAP sparse model 가져오기와 듀얼 뷰포트 점 표시 애드온입니다."""

bl_info = {
    "name": "COLMAP Align",
    "author": "COLMAP Align Team",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "Window > COLMAP Tools > COLMAP Align 열기",
    "description": "COLMAP sparse model 가져오기, 점 표시와 듀얼 뷰포트 A/P 마커 선택",
    "category": "3D View",
}


def register() -> None:
    """Blender가 호출하는 애드온 등록 진입점입니다."""
    from .registration import register as register_addon

    register_addon()


def unregister() -> None:
    """Blender가 호출하는 애드온 해제 진입점입니다."""
    from .registration import unregister as unregister_addon

    unregister_addon()


__all__ = ("register", "unregister", "bl_info")
