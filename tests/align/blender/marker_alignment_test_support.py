"""최소 3점 마커 정렬 통합 테스트에서 공유하는 실제 COLMAP fixture 도우미입니다."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np


@dataclass(frozen=True)
class ImportedMarkerAlignmentFixture:
    """실제 ImportJob으로 등록한 A/P 모델과 대응 마커 원본 좌표를 보관합니다."""

    reference_model_id: str
    moving_model_id: str
    point_ids: tuple[int, ...]
    reference_positions: np.ndarray
    moving_positions: np.ndarray


def add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""
    import sys

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def blender_data_snapshot() -> tuple[frozenset[int], frozenset[int], frozenset[int]]:
    """정렬 과정이 Blender 데이터블록을 만들지 않는지 비교할 포인터 집합을 반환합니다."""
    import bpy

    return (
        frozenset(item.as_pointer() for item in bpy.data.objects),
        frozenset(item.as_pointer() for item in bpy.data.collections),
        frozenset(item.as_pointer() for item in bpy.data.cameras),
    )


def _write_text_sparse_model(
    directory: Path,
    *,
    point_ids: tuple[int, ...],
    positions: np.ndarray,
    image_name: str,
) -> None:
    """마커 검증에 사용할 양방향 관측 트랙 포함 text sparse model을 기록합니다."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.txt").write_text(
        "# Camera list\n1 SIMPLE_PINHOLE 640 480 500 320 240\n",
        encoding="utf-8",
    )
    point2d_tokens = " ".join(
        f"{index + 0.25:.6f} {index + 0.75:.6f} {point_id}"
        for index, point_id in enumerate(point_ids)
    )
    (directory / "images.txt").write_text(
        "# Image list\n"
        f"1 1 0 0 0 0 0 0 1 {image_name}\n"
        f"{point2d_tokens}\n",
        encoding="utf-8",
    )
    point_lines = []
    for index, (point_id, position) in enumerate(zip(point_ids, positions, strict=True)):
        red = (31 + (index * 67)) % 256
        green = (97 + (index * 43)) % 256
        blue = (173 + (index * 29)) % 256
        point_lines.append(
            f"{point_id} {position[0]:.12f} {position[1]:.12f} {position[2]:.12f} "
            f"{red} {green} {blue} 0.01 1 {index}"
        )
    (directory / "points3D.txt").write_text(
        "# 3D point list\n" + "\n".join(point_lines) + "\n",
        encoding="utf-8",
    )


def _run_job_until_terminal(job: object, runtime: object) -> str:
    """실제 ImportJob을 TIMER와 같은 단일 단계 반복으로 완료 또는 취소까지 진행합니다."""
    for _ in range(128):
        result = job.step(runtime)
        if result != "RUNNING":
            return str(result)
    raise AssertionError("최소 마커 정렬 fixture ImportJob이 제한된 단계 안에 끝나지 않았습니다.")


@contextmanager
def _without_gpu_batch_preparation() -> Iterator[None]:
    """순수 ImportJob 검증 중 GPU 컨텍스트가 필요한 batch 준비만 임시로 생략합니다."""
    from tools.align import importer

    original_prepare = importer._prepare_gpu_batch_for_model
    importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
    try:
        yield
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare


def import_known_sim3_models(runtime: object, root: Path) -> ImportedMarkerAlignmentFixture:
    """알려진 P→A Sim(3) 관계의 실제 text COLMAP 두 모델을 ImportJob으로 등록합니다."""
    from tools.align.importer import create_import_job

    point_ids = (101, 205, 309, 413)
    moving_positions = np.asarray(
        (
            (0.25, -1.50, 0.75),
            (2.75, 0.50, 1.25),
            (-1.00, 3.00, 2.50),
            (0.50, -0.25, 4.75),
        ),
        dtype=np.float64,
    )
    rotation = np.asarray(
        (
            (0.0, -1.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    uniform_scale = 1.75
    translation = np.asarray((8.0, -3.5, 2.25), dtype=np.float64)
    reference_positions = (uniform_scale * (rotation @ moving_positions.T)).T + translation

    reference_directory = root / "reference_sparse"
    moving_directory = root / "moving_sparse"
    _write_text_sparse_model(
        reference_directory,
        point_ids=point_ids,
        positions=reference_positions,
        image_name="reference_marker_fixture.jpg",
    )
    _write_text_sparse_model(
        moving_directory,
        point_ids=point_ids,
        positions=moving_positions,
        image_name="moving_marker_fixture.jpg",
    )

    with _without_gpu_batch_preparation():
        reference_job = create_import_job(
            reference_directory,
            axis_preset="BLENDER",
            axis_forward="Y",
            axis_up="Z",
            model_id_factory=lambda: "marker-reference-model",
        )
        moving_job = create_import_job(
            moving_directory,
            axis_preset="BLENDER",
            axis_forward="Y",
            axis_up="Z",
            model_id_factory=lambda: "marker-moving-model",
        )
        assert _run_job_until_terminal(reference_job, runtime) == "FINISHED"
        assert _run_job_until_terminal(moving_job, runtime) == "FINISHED"

    return ImportedMarkerAlignmentFixture(
        reference_model_id="marker-reference-model",
        moving_model_id="marker-moving-model",
        point_ids=point_ids,
        reference_positions=np.array(reference_positions, dtype=np.float64, copy=True),
        moving_positions=np.array(moving_positions, dtype=np.float64, copy=True),
    )


def add_three_completed_marker_pairs(
    runtime: object,
    fixture: ImportedMarkerAlignmentFixture,
) -> tuple[int, int, int]:
    """알려진 비공선 A/P 원본 좌표 세 쌍을 기존 마커 입력 상태에 확정합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE

    selection = runtime.marker_selection
    pair_ids: list[int] = []
    for point_id, reference_position, moving_position in zip(
        fixture.point_ids[:3],
        fixture.reference_positions[:3],
        fixture.moving_positions[:3],
        strict=True,
    ):
        assert selection.begin_create_draft() is True
        assert selection.select_draft_point(
            ROLE_REFERENCE,
            point_id,
            tuple(float(value) for value in reference_position),
            model_id=fixture.reference_model_id,
        ) is True
        assert selection.select_draft_point(
            ROLE_MOVING,
            point_id,
            tuple(float(value) for value in moving_position),
            model_id=fixture.moving_model_id,
        ) is True
        pair = selection.commit_draft()
        assert pair is not None
        pair_ids.append(pair.pair_id)
    return tuple(pair_ids)
