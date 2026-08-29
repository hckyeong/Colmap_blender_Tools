"""Blender GUI에서 Modifier와 Projection Paint의 실제 Marker 교대를 검증합니다."""

from __future__ import annotations

import importlib.util
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

import bpy


ROOT = Path(__file__).resolve().parents[2]
USE_INSTALLED = os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1"
PACKAGE_NAME = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "colmap_tools_marker_handoff_test",
)
TIMEOUT_SECONDS = 60.0


def _load_extension_package():
    """Source Tree 또는 격리 설치본의 통합 Extension Package를 불러옵니다."""

    if USE_INSTALLED:
        return import_module(PACKAGE_NAME)
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


def _activate_extension():
    """Source Package를 등록하거나 격리 설치 Extension을 활성화합니다."""

    if not USE_INSTALLED:
        module = _load_extension_package()
        module.register()
        return module
    import addon_utils

    _loaded, enabled = addon_utils.check(PACKAGE_NAME)
    module = import_module(PACKAGE_NAME) if enabled else addon_utils.enable(
        PACKAGE_NAME,
        default_set=False,
        persistent=False,
    )
    if module is None:
        raise RuntimeError("격리 설치 COLMAP Tools Extension을 활성화하지 못했습니다.")
    return module


def _deactivate_extension(extension) -> None:
    """검증이 활성화한 Source 또는 설치 Extension을 안전하게 해제합니다."""

    if USE_INSTALLED:
        import addon_utils

        _loaded, enabled = addon_utils.check(PACKAGE_NAME)
        if enabled:
            addon_utils.disable(PACKAGE_NAME, default_set=False)
        return
    extension.unregister()


def _control_override(window) -> dict[str, object]:
    """현재 화면의 3D View WINDOW Region Context를 반환합니다."""

    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "WINDOW")
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
    }


def _make_modifier_dataset(root: Path) -> None:
    """동일 Frame 1 충돌을 검증할 단일 Camera COLMAP TXT Fixture를 만듭니다."""

    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame.ppm").write_bytes(
        b"P6\n2 2\n255\n" + bytes((255, 64, 32) * 4)
    )
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 64 64 50 50 32 32\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n7 1 0 0 0 0 0 0 1 frame.ppm\n32 32 100\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n100 0 0 2 255 96 64 0.1 7 0\n",
        encoding="utf-8",
    )


def _projection_request(camera_import_module):
    """Modifier와 같은 Frame 1을 쓰는 최소 Projection Paint Import 요청을 만듭니다."""

    camera = SimpleNamespace(
        camera_id=3,
        model_name="PINHOLE",
        width=1920,
        height=1080,
        params=(1200.0, 1190.0, 960.0, 540.0),
    )
    image = SimpleNamespace(
        image_id=11,
        camera_id=3,
        qvec=(1.0, 0.0, 0.0, 0.0),
        tvec=(0.0, 0.0, 0.0),
        name="projection.jpg",
    )
    point = SimpleNamespace(
        point3d_id=5,
        xyz=(1.0, 2.0, 3.0),
        rgb=(16, 32, 64),
        error=0.1,
    )
    return camera_import_module.CameraImportRequest(
        dataset_uuid="projection-marker-handoff",
        dataset_revision=1,
        source_fingerprint="projection-marker-handoff-fingerprint",
        cameras={3: camera},
        images=(image,),
        points=(point,),
        frame_start=1,
        axis_preset="BLENDER",
        camera_frustum_size=1.0,
    )


class CameraMarkerHandoffProbe:
    """실제 두 도구 데이터로 Marker 제거·복원과 데이터 보존을 검증합니다."""

    def __init__(self, extension) -> None:
        """통합 모듈과 단계별 검증 상태를 초기화합니다."""

        self.extension = extension
        self.started_at = time.monotonic()
        self.stage = "OPEN_MODIFIER"
        self.source_workspace = bpy.context.window.workspace
        self.temp_directory = tempfile.TemporaryDirectory()
        self.dataset_root = Path(self.temp_directory.name)
        _make_modifier_dataset(self.dataset_root)
        self.modifier_manifest = ""
        self.modifier_marker_name = ""
        self.modifier_marker_frame = 0
        self.modifier_object_pointers: tuple[int, ...] = ()
        self.projection_object_pointers: tuple[int, ...] = ()
        self.user_marker = bpy.context.scene.timeline_markers.new(
            "사용자 Marker",
            frame=-50,
        )
        self.modifier_session = import_module(
            f"{PACKAGE_NAME}.tools.modifier.blender.modifier_session"
        )
        self.marker_session = import_module(
            f"{PACKAGE_NAME}.shared.blender_runtime.scene_marker_session"
        )
        self.camera_import = import_module(
            f"{PACKAGE_NAME}.tools.projection_paint.blender.camera_import"
        )
        self.camera_scene = import_module(
            f"{PACKAGE_NAME}.tools.projection_paint.blender.camera_scene"
        )

    def _objects_exist(self, pointers: tuple[int, ...]) -> bool:
        """저장한 Blender Object Pointer가 모두 현재 Main에 남았는지 반환합니다."""

        current = {obj.as_pointer() for obj in bpy.data.objects}
        return set(pointers).issubset(current)

    def _finish(self) -> None:
        """성공 결과를 출력하고 Extension과 임시 Fixture를 정리합니다."""

        _deactivate_extension(self.extension)
        self.temp_directory.cleanup()
        print("COLMAP_TOOLS_CAMERA_MARKER_HANDOFF_GUI_OK")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    def run(self) -> float | None:
        """Timer Tick마다 두 도구의 실제 Marker 교대 상태를 검증합니다."""

        if time.monotonic() - self.started_at > TIMEOUT_SECONDS:
            raise TimeoutError(f"Camera Marker 교대 검증 시간 초과: {self.stage}")
        window = bpy.context.window
        scene = bpy.context.scene

        if self.stage == "OPEN_MODIFIER":
            if bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Modifier 진입 요청에 실패했습니다.")
            self.stage = "IMPORT_MODIFIER"
            return 0.05

        if self.stage == "IMPORT_MODIFIER":
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_ACTIVE:
                return 0.05
            if self.marker_session.scene_marker_session_owner(scene) != "modifier":
                return 0.05
            with bpy.context.temp_override(**_control_override(window)):
                settings = scene.colmap_import
                settings.dataset_root = str(self.dataset_root)
                settings.input_format = "TEXT"
                if bpy.ops.colmap.import_model("EXEC_DEFAULT") != {"FINISHED"}:
                    raise AssertionError(settings.status)
            manifest = json.loads(settings.review_marker_manifest_json)
            entry = next(item for item in manifest if item["dataset_uuid"] == settings.dataset_uuid)
            marker = scene.timeline_markers.get(entry["marker_name"])
            if marker is None or marker.frame != 1 or marker.camera is None:
                raise AssertionError("Modifier Marker가 Frame 1에 생성되지 않았습니다.")
            self.modifier_manifest = settings.review_marker_manifest_json
            self.modifier_marker_name = str(entry["marker_name"])
            self.modifier_marker_frame = int(entry["frame"])
            self.modifier_object_pointers = tuple(
                obj.as_pointer()
                for obj in bpy.data.objects
                if obj.get("blender_colmap_dataset_uuid") == settings.dataset_uuid
            )
            if not self.modifier_object_pointers:
                raise AssertionError("보존을 검증할 Modifier 데이터 Object가 없습니다.")
            if bpy.ops.projection_paint.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Projection Paint 전환 요청에 실패했습니다.")
            self.stage = "IMPORT_PROJECTION"
            return 0.05

        if self.stage == "IMPORT_PROJECTION":
            if self.marker_session.scene_marker_session_owner(scene) != "projection_paint":
                return 0.05
            if scene.timeline_markers.get(self.modifier_marker_name) is not None:
                raise AssertionError("Projection Paint 진입 뒤 Modifier Marker가 남았습니다.")
            if scene.colmap_import.review_marker_manifest_json != self.modifier_manifest:
                raise AssertionError("Modifier Marker Manifest가 변경되었습니다.")
            if not self._objects_exist(self.modifier_object_pointers):
                raise AssertionError("Modifier 데이터 Object가 삭제되었습니다.")
            job = self.camera_import.CameraImportJob(
                scene,
                _projection_request(self.camera_import),
            )
            result = job.run_to_completion()
            if result.marker_count != 1 or not self.camera_scene.projection_markers_attached(scene):
                raise AssertionError("Projection Paint Marker Import가 완료되지 않았습니다.")
            projection_manifest = self.camera_scene.scene_manifest(scene)
            if projection_manifest is None or projection_manifest.entries[0].frame != 1:
                raise AssertionError("Projection Paint Marker가 Frame 1을 사용하지 않습니다.")
            self.projection_object_pointers = tuple(
                obj.as_pointer()
                for obj in bpy.data.objects
                if obj.get("pp_dataset_uuid") == projection_manifest.dataset_uuid
            )
            if not self.projection_object_pointers:
                raise AssertionError("보존을 검증할 Projection Paint Object가 없습니다.")
            if bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Modifier 복귀 요청에 실패했습니다.")
            self.stage = "VERIFY_MODIFIER_RETURN"
            return 0.05

        if self.stage == "VERIFY_MODIFIER_RETURN":
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_ACTIVE:
                return 0.05
            if self.marker_session.scene_marker_session_owner(scene) != "modifier":
                return 0.05
            if self.camera_scene.projection_markers_attached(scene):
                raise AssertionError("Modifier 복귀 뒤 Projection Marker가 남았습니다.")
            marker = scene.timeline_markers.get(self.modifier_marker_name)
            if (
                marker is None
                or marker.frame != self.modifier_marker_frame
                or marker.camera is None
            ):
                raise AssertionError("Modifier Marker가 Manifest에서 복원되지 않았습니다.")
            if not self._objects_exist(self.projection_object_pointers):
                raise AssertionError("Projection Paint Camera Object가 삭제되었습니다.")
            if bpy.ops.colmap.exit_modifier_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Modifier 최종 종료에 실패했습니다.")
            self.stage = "VERIFY_SOURCE"
            return 0.05

        if self.stage == "VERIFY_SOURCE":
            if window.workspace is not self.source_workspace:
                return 0.05
            if self.marker_session.scene_marker_session_owner(scene):
                raise AssertionError("일반 Workspace에 Marker 사용권이 남았습니다.")
            if scene.timeline_markers.get(self.modifier_marker_name) is not None:
                raise AssertionError("일반 Workspace에 Modifier Marker가 남았습니다.")
            if not self.camera_scene.projection_markers_detached(scene):
                raise AssertionError("일반 Workspace에 Projection Marker가 남았습니다.")
            if scene.timeline_markers.get(self.user_marker.name) is None:
                raise AssertionError("사용자 Marker가 삭제되었습니다.")
            if not self._objects_exist(
                (*self.modifier_object_pointers, *self.projection_object_pointers)
            ):
                raise AssertionError("Workspace 전환 중 도구 데이터가 삭제되었습니다.")
            self._finish()
            return None

        raise AssertionError(f"알 수 없는 Marker 교대 단계입니다: {self.stage}")


def _run_probe() -> float | None:
    """검증 예외를 출력하고 Process 실패 코드로 전달합니다."""

    try:
        return PROBE.run()
    except Exception:
        traceback.print_exc()
        try:
            _deactivate_extension(EXTENSION)
        except Exception:
            traceback.print_exc()
        PROBE.temp_directory.cleanup()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


EXTENSION = _activate_extension()
PROBE = CameraMarkerHandoffProbe(EXTENSION)
bpy.app.timers.register(_run_probe, first_interval=0.1)
