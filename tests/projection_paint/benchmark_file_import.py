"""분리된 Blender에서 기존 복사와 직접 파일 입력의 시간·메모리·복원을 비교합니다."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import struct
import sys
from time import perf_counter
import zlib

import bpy


def _arguments():
    """Blender 구분자 뒤의 검증 입력과 결과 위치를 읽습니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("baseline", "direct", "obj", "restore", "model-restore"), required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--points", type=int, default=100000)
    parser.add_argument("--obj", type=Path)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--point-limit", type=int, default=20_000)
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])


def _peak_memory_bytes() -> int:
    """Windows가 집계한 현재 Blender 프로세스의 최대 작업집합을 반환합니다."""
    class Counters(ctypes.Structure):
        """프로세스 메모리 조회의 공식 필드 배치를 정의합니다."""
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage",
            )
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current_process = ctypes.windll.kernel32.GetCurrentProcess
    current_process.restype = wintypes.HANDLE
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not query(current_process(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


def _write_fixture(root: Path, count: int) -> None:
    """관측 연결이 유효한 16개 카메라와 지정 점 수의 TXT 입력을 만듭니다."""
    model, images = root / "sparse" / "0", root / "images"
    model.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    def chunk(name: bytes, payload: bytes) -> bytes:
        """검증용 PNG 청크의 길이와 CRC를 기록합니다."""
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\x00" + bytes((32, 64, 128)) * 4) * 3)) + chunk(b"IEND", b"")
    (model / "cameras.txt").write_text("7 PINHOLE 4 3 4 4 2 1.5\n", encoding="utf-8")
    with (model / "images.txt").open("w", encoding="utf-8") as stream:
        for image_id in range(1, 17):
            name = f"frame_{image_id}.png"
            (images / name).write_bytes(png)
            stream.write(f"{image_id} 1 0 0 0 {image_id / 10} 0 0 7 {name}\n")
            stream.write(" ".join(f"1 1 {10000000000 + i}" for i in range(count)) if image_id == 1 else "")
            stream.write("\n")
    with (model / "points3D.txt").open("w", encoding="utf-8") as stream:
        for index in range(count):
            stream.write(f"{10000000000 + index} {index % 1000 / 100} {index // 1000 / 100} 2 32 64 128 0.1 1 {index}\n")


def _paint_asset(scene):
    """입력 교체가 보존해야 하는 실제 메시·레이어·픽셀을 만듭니다."""
    mesh = bpy.data.meshes.new("입력 검증 페인트 메시")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    target = bpy.data.objects.new("입력 검증 페인트 대상", mesh)
    scene.collection.objects.link(target)
    image = bpy.data.images.new("입력 검증 보존 이미지", 2, 2, alpha=True)
    image.pixels[:] = [0.125, 0.25, 0.5, 1.0] * 4
    image.pack()
    scene.projection_paint.paint_target = target
    layer = scene.projection_paint.layers.add()
    layer.name, layer.image = "입력 교체 보존 레이어", image
    return target, image, tuple(image.pixels)


def main() -> None:
    """시험·사용자 원본의 직접 입력과 참고 메타데이터·이전 저장 자료의 복원을 검증합니다."""
    args = _arguments()
    sys.path.insert(0, str(args.source_root.resolve()))
    args.output_root.mkdir(parents=True, exist_ok=True)
    fixture = args.dataset_root or args.output_root / "fixture"
    from tools import projection_paint as painter
    from shared.blender_runtime.scene_marker_session import acquire_scene_marker_session
    from tools.projection_paint.blender.snapshot_update import snapshot_is_ready
    painter.register()
    scene = bpy.context.scene
    settings = scene.projection_paint
    result = {"mode": args.mode, "blender": bpy.app.version_string, "source_root": str(args.source_root), "runs": []}
    if args.mode == "model-restore":
        assert snapshot_is_ready(scene), "직접 입력 자료가 준비 상태로 복원되지 않았습니다."
        target = settings.paint_target
        assert target is not None and target.get("pp_record_kind") == "MODEL_OBJECT"
        assert len(target.data.vertices) == 4 and len(target.data.polygons) == 1
        assert len(target.data.uv_layers) == 1 and target.parent.get("pp_record_kind") == "MODEL_AXIS_ROOT"
        assert not Path(target["pp_source_path"]).exists(), "원본 없는 복원 조건이 아닙니다."
        result.update(ready=True, original_obj_missing=True, vertices=4, faces=1, uv=True, source_kind=settings.source_kind)
    elif args.mode == "restore":
        assert snapshot_is_ready(scene), "저장된 Painter 데이터가 준비 상태로 복원되지 않았습니다."
        assert settings.paint_target is not None and len(settings.layers) == 1
        pixels = tuple(settings.layers[0].image.pixels)
        assert all(abs(actual - expected) < 0.005 for actual, expected in zip(pixels, (0.125, 0.25, 0.5, 1.0) * 4))
        result.update(ready=True, points=settings.snapshot_point_count, source_kind=getattr(settings, "source_kind", "MODIFIER_LEGACY"), pixels=pixels)
        if hasattr(settings, "reference_metadata_json"):
            result["reference_metadata"] = json.loads(settings.reference_metadata_json)
            if result["reference_metadata"]:
                assert result["reference_metadata"]["imported_point_count"] == settings.snapshot_point_count
    elif args.mode == "obj":
        started = perf_counter()
        outcome = bpy.ops.projection_paint.import_obj(filepath=str(args.obj))
        assert outcome == {"FINISHED"}, settings.status
        models = [obj for obj in scene.objects if obj.get("pp_record_kind") == "MODEL_OBJECT"]
        assert len(models) == 1
        result.update(seconds=perf_counter() - started, obj_path=str(args.obj), file_bytes=args.obj.stat().st_size,
                      vertices=len(models[0].data.vertices), faces=len(models[0].data.polygons))
    else:
        if args.dataset_root is None:
            _write_fixture(fixture, args.points)
        target, image, pixels = _paint_asset(scene)
        phase_seconds = {}
        if args.mode == "direct":
            from tools.projection_paint.blender import camera_import
            original_advance = camera_import._advance_import_session

            def profiled_advance(context, session):
                """검증 프로세스에서만 준비·재검증·생성 단계별 시간을 집계합니다."""
                phase = session.stage
                job = session.request_job if phase == "PREPARE_REQUEST" else session.validation_job if phase == "REVALIDATE" else None
                if job is not None:
                    phase += ":" + str(getattr(job.progress.phase, "value", job.progress.phase))
                started = perf_counter()
                try:
                    return original_advance(context, session)
                finally:
                    phase_seconds[phase] = phase_seconds.get(phase, 0.0) + perf_counter() - started

            camera_import._advance_import_session = profiled_advance
        modifier_seconds = 0.0
        if args.mode == "baseline":
            from shared.colmap_dataset import register as register_legacy
            from tools import modifier
            register_legacy()
            modifier.register()
            scene.colmap_import.dataset_root = str(fixture)
            scene.colmap_import.input_format = "TEXT"
            started = perf_counter()
            assert bpy.ops.colmap.import_model() == {"FINISHED"}, scene.colmap_import.status
            modifier_seconds = perf_counter() - started
        else:
            settings.dataset_root = str(fixture)
            settings.input_format = "AUTO" if args.dataset_root else "TEXT"
            if hasattr(settings, "import_point_limit"):
                settings.import_point_limit = args.point_limit
        acquire_scene_marker_session(scene, "projection_paint")
        for index in range(3):
            phase_seconds.clear()
            started = perf_counter()
            outcome = (bpy.ops.projection_paint.update_colmap_snapshot() if args.mode == "baseline"
                       else bpy.ops.projection_paint.import_colmap())
            elapsed = perf_counter() - started
            assert outcome == {"FINISHED"}, settings.status
            assert snapshot_is_ready(scene), "가져온 데이터가 준비 상태가 아닙니다."
            assert settings.paint_target is target and settings.layers[0].image is image and tuple(image.pixels) == pixels
            result["runs"].append({"index": index, "seconds": elapsed, "peak_working_set_bytes": _peak_memory_bytes(), "phase_seconds": dict(phase_seconds)})
            if index == 0:
                bpy.ops.wm.save_as_mainfile(filepath=str(args.output_root / f"{args.mode}.blend"))
        result.update(modifier_import_seconds=modifier_seconds, initial_total_seconds=modifier_seconds + result["runs"][0]["seconds"],
                      point_count=settings.snapshot_point_count, image_count=settings.snapshot_image_count, fixture=str(fixture))
        if hasattr(settings, "reference_metadata_json"):
            result["reference_metadata"] = json.loads(settings.reference_metadata_json)
    result["peak_working_set_bytes"] = _peak_memory_bytes()
    result["success"] = True
    (args.output_root / f"{args.mode}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


main()
