from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path


if "--" not in sys.argv:
    raise RuntimeError("Export Job 단계 계측 인자가 없습니다.")

arguments = sys.argv[sys.argv.index("--") + 1 :]
if len(arguments) != 6:
    raise RuntimeError(
        "Export Job 단계 계측에는 package, dataset, sparse, images, temp 경로와 "
        "작업 budget이 필요합니다."
    )

(
    package_directory,
    source_dataset_root,
    source_sparse_directory,
    source_images_directory,
    temp_parent,
    budget_text,
) = arguments
package_directory = Path(package_directory).resolve()
source_dataset_root = Path(source_dataset_root).resolve()
source_sparse_directory = Path(source_sparse_directory).resolve()
source_images_directory = Path(source_images_directory).resolve()
temp_parent = Path(temp_parent).resolve()
work_budget_ms = float(budget_text)
sys.path.insert(0, str(package_directory.parent))

from tools.modifier.core.export_job import ExportTransactionJob
from tools.modifier.core.export_validation import validate_export_request
from tools.modifier.core.loader import load_reconstruction


source_files = tuple(source_sparse_directory.glob("*.bin"))
run_root = Path(
    tempfile.mkdtemp(prefix="export_job_phases_", dir=temp_parent)
).resolve()
try:
    clone_dataset_root = run_root / "dataset"
    clone_sparse_directory = clone_dataset_root / "sparse" / "0"
    clone_sparse_directory.mkdir(parents=True)
    for source_path in source_files:
        shutil.copy2(source_path, clone_sparse_directory / source_path.name)

    loaded = load_reconstruction(
        clone_dataset_root,
        clone_sparse_directory,
        source_images_directory,
        input_format="BINARY",
        dataset_uuid="export-job-phase-profile",
    )
    reconstruction = loaded.reconstruction
    plan = validate_export_request(
        loaded.source,
        reconstruction,
        backup_enabled=False,
    )
    job = ExportTransactionJob(plan, reconstruction)

    phase_seconds: dict[str, float] = defaultdict(float)
    phase_calls: dict[str, int] = defaultdict(int)
    phase_max_tick: dict[str, float] = defaultdict(float)
    slow_ticks: list[dict[str, object]] = []
    started = time.perf_counter()
    while not job.done:
        phase_name = job.progress.phase.name
        tick_started = time.perf_counter()
        job.advance(work_budget_ms=work_budget_ms)
        tick_elapsed = time.perf_counter() - tick_started
        phase_seconds[phase_name] += tick_elapsed
        phase_calls[phase_name] += 1
        phase_max_tick[phase_name] = max(
            phase_max_tick[phase_name],
            tick_elapsed,
        )
        if tick_elapsed >= 0.1:
            read_job = getattr(job, "_read_job", None)
            slow_ticks.append(
                {
                    "phase": phase_name,
                    "seconds": tick_elapsed,
                    "read_index": getattr(read_job, "_index", None),
                    "read_field": (
                        read_job._tasks[read_job._index][0]
                        if read_job is not None
                        and read_job._index < len(read_job._tasks)
                        else None
                    ),
                    "read_record_index": getattr(read_job, "_record_index", None),
                }
            )
    elapsed = time.perf_counter() - started

    print(
        "BLENDER_COLMAP_EXPORT_JOB_PHASES="
        + json.dumps(
            {
                "budget_ms": work_budget_ms,
                "elapsed_seconds": elapsed,
                "phase_seconds": dict(phase_seconds),
                "phase_calls": dict(phase_calls),
                "phase_max_tick_seconds": dict(phase_max_tick),
                "slow_ticks": slow_ticks,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
finally:
    if run_root.exists():
        shutil.rmtree(run_root)
