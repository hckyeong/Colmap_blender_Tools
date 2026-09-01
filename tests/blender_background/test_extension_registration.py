"""Blender Background에서 통합 Extension의 등록과 해제를 검증합니다."""

from __future__ import annotations

import importlib.util
from importlib import import_module
import os
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace

import bpy


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "colmap_tools_registration_test"


def _load_extension_package():
    """작업 Tree의 Root를 임시 Blender Extension 패키지로 불러옵니다."""

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("통합 Extension Root를 불러올 수 없습니다.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _assert_registered_contract() -> None:
    """공통 메뉴, 도구 진입점과 기존 Scene 속성이 모두 등록되었는지 확인합니다."""

    if not hasattr(bpy.types, "COLMAP_MT_tools"):
        raise AssertionError("공통 COLMAP Tools 메뉴가 등록되지 않았습니다.")
    required_operator_ids = (
        "colmap_align.open_workspace",
        "colmap.open_modifier_workspace",
        "projection_paint.open_workspace",
    )
    missing_operators: list[str] = []
    for operator_id in required_operator_ids:
        category, name = operator_id.split(".", 1)
        try:
            getattr(getattr(bpy.ops, category), name).get_rna_type()
        except (AttributeError, RuntimeError):
            missing_operators.append(operator_id)
    if missing_operators:
        raise AssertionError(f"등록되지 않은 Blender Operator: {missing_operators}")
    try:
        bpy.ops.projection_paint.import_colmap.get_rna_type()
    except (AttributeError, KeyError, RuntimeError):
        pass
    else:
        raise AssertionError("Legacy projection_paint.import_colmap Operator가 등록되었습니다.")
    required_scene_properties = (
        "colmap_dataset",
        "colmap_align_settings",
        "colmap_import",
        "projection_paint",
    )
    missing_properties = [
        name for name in required_scene_properties if not hasattr(bpy.types.Scene, name)
    ]
    if missing_properties:
        raise AssertionError(f"등록되지 않은 Scene 속성: {missing_properties}")


def _assert_unregistered_contract() -> None:
    """통합 Extension이 소유한 공통 메뉴와 Scene 속성이 모두 해제되었는지 확인합니다."""

    if hasattr(bpy.types, "COLMAP_MT_tools"):
        raise AssertionError("공통 COLMAP Tools 메뉴가 해제되지 않았습니다.")
    leaked_properties = [
        name
        for name in (
            "colmap_dataset",
            "colmap_align_settings",
            "colmap_import",
            "projection_paint",
        )
        if hasattr(bpy.types.Scene, name)
    ]
    if leaked_properties:
        raise AssertionError(f"해제되지 않은 Scene 속성: {leaked_properties}")


def _assert_atomic_registration_rollback() -> None:
    """도구 등록 실패 시 먼저 등록된 도구와 Root Class가 역순 정리되는지 확인합니다."""

    lifecycle = import_module(f"{PACKAGE_NAME}.shell.lifecycle")
    calls: list[str] = []

    def register_first() -> None:
        """첫 번째 가상 도구의 등록 완료를 기록합니다."""

        calls.append("register_first")

    def unregister_first() -> None:
        """Rollback이 첫 번째 가상 도구를 해제했는지 기록합니다."""

        calls.append("unregister_first")

    def register_failure() -> None:
        """두 번째 가상 도구 등록 실패를 재현합니다."""

        calls.append("register_failure")
        raise RuntimeError("통합 등록 Rollback 검사")

    def unregister_failure() -> None:
        """실패 도구는 등록 완료되지 않아 호출되지 않는 해제 Callback입니다."""

    def load_modules():
        """등록 Rollback 검사용 두 가상 도구를 고정 순서로 반환합니다."""

        return modules

    modules = (
        SimpleNamespace(
            __name__="rollback_first",
            register=register_first,
            unregister=unregister_first,
        ),
        SimpleNamespace(
            __name__="rollback_failure",
            register=register_failure,
            unregister=unregister_failure,
        ),
    )
    original_loader = lifecycle._load_tool_modules
    lifecycle._load_tool_modules = load_modules
    try:
        try:
            lifecycle.register()
        except RuntimeError as error:
            if "Rollback 검사" not in str(error):
                raise
        else:
            raise AssertionError("도구 등록 실패가 Root까지 전파되지 않았습니다.")
    finally:
        lifecycle._load_tool_modules = original_loader
    if calls != ["register_first", "register_failure", "unregister_first"]:
        raise AssertionError(f"등록 Rollback 순서가 잘못되었습니다: {calls}")
    if lifecycle.is_registered() or hasattr(bpy.types, "COLMAP_MT_tools"):
        raise AssertionError("등록 실패 뒤 Root Shell 상태가 남았습니다.")


def main() -> int:
    """통합 Extension을 한 번 등록·해제하고 실패를 Process 종료 코드로 반환합니다."""

    extension = _load_extension_package()
    registered = False
    try:
        _assert_atomic_registration_rollback()
        extension.register()
        registered = True
        extension.register()
        _assert_registered_contract()
        extension.unregister()
        registered = False
        extension.unregister()
        _assert_unregistered_contract()
    except Exception:
        traceback.print_exc()
        if registered:
            try:
                extension.unregister()
            except Exception:
                traceback.print_exc()
        return 1
    print("COLMAP Tools 통합 등록·해제 검사 통과")
    return 0


def _exit_process(status: int) -> None:
    """Blender가 Python SystemExit을 삼키지 않도록 검사 결과를 Process 코드로 종료합니다."""

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)


if __name__ == "__main__":
    _exit_process(main())
