from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path


if "--" not in sys.argv:
    raise RuntimeError("Binary Export 계측 인자가 없습니다.")

arguments = sys.argv[sys.argv.index("--") + 1 :]
if len(arguments) != 7:
    raise RuntimeError(
        "Binary Export 계측에는 package, dataset, sparse, images, version, "
        "run ID, temp 경로가 필요합니다."
    )

(
    package_directory,
    source_dataset_root,
    source_sparse_directory,
    source_images_directory,
    expected_version,
    run_id,
    temp_parent,
) = arguments
package_directory = Path(package_directory).resolve()
source_dataset_root = Path(source_dataset_root).resolve()
source_sparse_directory = Path(source_sparse_directory).resolve()
source_images_directory = Path(source_images_directory).resolve()
temp_parent = Path(temp_parent).resolve()
sys.path.insert(0, str(package_directory.parent))

from tools import modifier as blender_colmap
from tools.modifier.core.binary_io import read_model_binary
from tools.modifier.core.export_transaction import execute_export
from tools.modifier.core.export_validation import validate_export_request
from tools.modifier.core.loader import load_reconstruction


_BINARY_FILENAMES = (
    "rigs.bin",
    "cameras.bin",
    "frames.bin",
    "images.bin",
    "points3D.bin",
)


def _require_within(path: Path, root: Path, label: str) -> Path:
    try:
        return path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} 경로가 허용 Root 밖에 있습니다: {path}") from exc


def _require_outside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    raise RuntimeError(f"{label} 경로가 원본 Dataset 내부에 있습니다: {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _model_counts(model) -> dict[str, int]:
    return {
        "cameras": len(model.cameras),
        "images": len(model.images),
        "points3d": len(model.points3d),
        "rigs": len(model.rigs),
        "frames": len(model.frames),
    }


loaded_package_file = Path(blender_colmap.__file__).resolve()
_require_within(loaded_package_file, package_directory, "로드된 blender_colmap")
if blender_colmap.__version__ != expected_version:
    raise RuntimeError(
        f"요청 버전 {expected_version}과 로드 버전 "
        f"{blender_colmap.__version__}이 다릅니다."
    )

for path, label in (
    (package_directory, "Package Directory"),
    (source_dataset_root, "원본 Dataset Root"),
    (source_sparse_directory, "원본 Sparse Directory"),
    (source_images_directory, "원본 Images Directory"),
    (temp_parent, "계측 Temp Parent"),
):
    if not path.is_dir():
        raise RuntimeError(f"{label}가 존재하는 디렉터리가 아닙니다: {path}")

_require_within(
    source_sparse_directory,
    source_dataset_root,
    "원본 Sparse Directory",
)
_require_within(
    source_images_directory,
    source_dataset_root,
    "원본 Images Directory",
)
_require_outside(temp_parent, source_dataset_root, "계측 Temp Parent")
source_files = tuple(source_sparse_directory / name for name in _BINARY_FILENAMES)
missing_files = tuple(path for path in source_files if not path.is_file())
if missing_files:
    raise RuntimeError(
        "원본 Binary/Modern 파일이 모두 존재해야 합니다: "
        + ", ".join(str(path) for path in missing_files)
    )
source_hashes_before = {
    path.name: _sha256_file(path)
    for path in source_files
}

run_root = Path(tempfile.mkdtemp(prefix="binary_export_", dir=temp_parent)).resolve()
try:
    _require_within(run_root, temp_parent, "실행별 Temp Root")
    clone_dataset_root = run_root / "dataset"
    clone_sparse_directory = clone_dataset_root / "sparse" / "0"
    if clone_sparse_directory == source_sparse_directory:
        raise RuntimeError("계측 Sparse 복사본이 원본 Sparse Directory와 같습니다.")
    _require_outside(
        clone_sparse_directory,
        source_dataset_root,
        "계측 Sparse 복사본",
    )
    _require_outside(run_root, source_dataset_root, "실행별 Temp Root")
    _require_within(clone_sparse_directory, run_root, "계측 Sparse 복사본")

    clone_sparse_directory.mkdir(parents=True)
    for source_path in source_files:
        shutil.copy2(source_path, clone_sparse_directory / source_path.name)

    loaded = load_reconstruction(
        clone_dataset_root,
        clone_sparse_directory,
        source_images_directory,
        input_format="BINARY",
        dataset_uuid=f"binary-export-profile-{run_id}",
    )
    reconstruction = loaded.reconstruction
    expected_counts = _model_counts(reconstruction)
    plan = validate_export_request(
        loaded.source,
        reconstruction,
        backup_enabled=False,
    )

    if plan.source.model_directory != clone_sparse_directory:
        raise RuntimeError("ExportPlan이 계측 Sparse 복사본을 가리키지 않습니다.")
    selected_names = tuple(path.name for path in plan.source_layout.selected_files)
    if selected_names != _BINARY_FILENAMES:
        raise RuntimeError(
            "ExportPlan이 Binary/Modern 5개 파일을 정확한 순서로 선택하지 않았습니다: "
            f"{selected_names}"
        )
    if any(
        path.parent != clone_sparse_directory
        for path in plan.source_layout.selected_files
    ):
        raise RuntimeError("ExportPlan의 대상 파일이 계측 Sparse 복사본 밖에 있습니다.")
    for path in plan.source_layout.selected_files:
        _require_within(path, run_root, "Export 대상 파일")
        if path in source_files:
            raise RuntimeError("Export 대상에 원본 Sparse 파일이 포함되었습니다.")

    started = time.perf_counter()
    result = execute_export(plan, reconstruction)
    elapsed_seconds = time.perf_counter() - started

    if result.cleanup_warnings:
        raise RuntimeError(
            "Binary Export 계측 transaction 정리에 실패했습니다: "
            + " | ".join(result.cleanup_warnings)
        )
    parsed = read_model_binary(clone_sparse_directory, modern=True)
    parsed_counts = _model_counts(parsed)
    if parsed_counts != expected_counts:
        raise RuntimeError(
            "Binary Export 후 모델 개수가 달라졌습니다: "
            f"expected={expected_counts}, actual={parsed_counts}"
        )

    output_files = tuple(
        clone_sparse_directory / name for name in _BINARY_FILENAMES
    )
    hashes = {path.name: _sha256_file(path) for path in output_files}
    sizes = {path.name: path.stat().st_size for path in output_files}
    source_hashes_after = {
        path.name: _sha256_file(path)
        for path in source_files
    }
    if source_hashes_after != source_hashes_before:
        raise RuntimeError("계측 도중 원본 Sparse 파일의 SHA-256이 변경되었습니다.")
    if hashes != source_hashes_after:
        raise RuntimeError("Binary Export 결과의 SHA-256이 원본과 다릅니다.")

    print(
        "BLENDER_COLMAP_BINARY_EXPORT_PROFILE="
        + json.dumps(
            {
                "version": blender_colmap.__version__,
                "run_id": run_id,
                "elapsed_seconds": elapsed_seconds,
                "counts": parsed_counts,
                "hashes": hashes,
                "sizes": sizes,
                "source_hashes": source_hashes_after,
                "hashes_match_source": hashes == source_hashes_after,
                "source_unchanged": True,
                "replaced_files": [
                    path.name for path in result.replaced_files
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
finally:
    _require_within(run_root, temp_parent, "정리할 실행별 Temp Root")
    if run_root.exists():
        shutil.rmtree(run_root)
