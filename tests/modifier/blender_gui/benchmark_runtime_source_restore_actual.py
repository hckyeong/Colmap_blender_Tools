"""실제 COLMAP 원본에서 전체 Import와 빠른 Source 복원 시간을 비교합니다."""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(
    os.environ.get("COLMAP_TOOLS_PROJECT_ROOT", Path.cwd())
).resolve()
RESULT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_SOURCE_BENCHMARK_RESULT",
        PROJECT_ROOT / "build" / "modifier_source_restore_benchmark.json",
    )
).resolve()
DATASET_ROOT = Path(os.environ["COLMAP_TOOLS_DATASET_ROOT"]).resolve()
MODEL_DIRECTORY = Path(os.environ["COLMAP_TOOLS_MODEL_DIRECTORY"]).resolve()
IMAGES_DIRECTORY = Path(os.environ["COLMAP_TOOLS_IMAGES_DIRECTORY"]).resolve()

sys.path.insert(0, str(PROJECT_ROOT))

from tools.modifier.core.fingerprint import source_fingerprint
from tools.modifier.core.import_job import ImportLoadJob, SCHEMA_VERSION
from tools.modifier.core.import_types import ImportPlan
from tools.modifier.core.layout import resolve_layout
from tools.modifier.core.runtime_restore_job import (
    RuntimeSourceRestoreJob,
    RuntimeSourceRestorePlan,
)


def _run_job(job) -> tuple[float, object]:
    """증분 Core Job을 대기 없는 조건으로 완료하고 경과 시간과 결과를 반환합니다."""

    started_at = time.perf_counter()
    while not job.done:
        job.advance()
    return time.perf_counter() - started_at, job.result


def _counts(loaded) -> dict[str, int]:
    """두 Loader 결과의 핵심 Reconstruction 수량을 비교 가능한 값으로 반환합니다."""

    reconstruction = loaded.reconstruction
    return {
        "camera_count": len(reconstruction.cameras),
        "image_count": len(reconstruction.images),
        "point3d_count": len(reconstruction.points3d),
        "rig_count": len(reconstruction.rigs),
        "frame_count": len(reconstruction.frames),
    }


def main() -> None:
    """동일 Source를 전체 Import와 신뢰 복원으로 읽고 결과·시간을 기록합니다."""

    layout = resolve_layout(MODEL_DIRECTORY, "AUTO")
    expected_fingerprint = str(source_fingerprint(layout))

    fast_job = RuntimeSourceRestoreJob(
        RuntimeSourceRestorePlan(
            dataset_root=DATASET_ROOT,
            model_directory=MODEL_DIRECTORY,
            images_directory=IMAGES_DIRECTORY,
            input_format=layout.input_format,
            expected_dataset_uuid="actual-benchmark-dataset",
            expected_source_fingerprint=expected_fingerprint,
            expected_schema_version=SCHEMA_VERSION,
        )
    )
    fast_seconds, fast_result = _run_job(fast_job)
    fast_counts = _counts(fast_result)
    del fast_result, fast_job
    gc.collect()

    full_job = ImportLoadJob(
        ImportPlan(
            dataset_root=DATASET_ROOT,
            sparse_directory=MODEL_DIRECTORY,
            images_directory=IMAGES_DIRECTORY,
            input_format=layout.input_format,
            matching_dataset_uuid="actual-benchmark-dataset",
            matching_model_directory=MODEL_DIRECTORY,
        )
    )
    full_seconds, full_result = _run_job(full_job)
    full_counts = _counts(full_result)

    payload = {
        "ok": fast_counts == full_counts,
        "fast_restore_seconds": round(fast_seconds, 6),
        "full_import_seconds": round(full_seconds, 6),
        "reduction_percent": round(
            ((full_seconds - fast_seconds) / full_seconds) * 100.0,
            3,
        ),
        "fast_counts": fast_counts,
        "full_counts": full_counts,
        "source_fingerprint": expected_fingerprint,
        "input_format": layout.input_format,
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not payload["ok"]:
        raise RuntimeError("전체 Import와 빠른 Source 복원의 Reconstruction 수량이 다릅니다.")


if __name__ == "__main__":
    main()
