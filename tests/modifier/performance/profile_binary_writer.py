from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path


if "--" not in sys.argv:
    raise RuntimeError("Binary Writer 계측 인자가 없습니다.")

arguments = sys.argv[sys.argv.index("--") + 1 :]
if len(arguments) != 5:
    raise RuntimeError(
        "Binary Writer 계측에는 package, sparse, version, run ID, temp 경로가 필요합니다."
    )

package_directory, sparse_directory, expected_version, run_id, temp_parent = arguments
package_directory = Path(package_directory).resolve()
sparse_directory = Path(sparse_directory).resolve()
temp_parent = Path(temp_parent).resolve()
sys.path.insert(0, str(package_directory.parent))

from tools import modifier as blender_colmap
from tools.modifier.core import binary_writer
from tools.modifier.core.binary_io import read_model_binary


loaded_package_file = Path(blender_colmap.__file__).resolve()
try:
    loaded_package_file.relative_to(package_directory)
except ValueError as exc:
    raise RuntimeError(
        "요청한 격리 package가 아닌 경로에서 blender_colmap을 로드했습니다: "
        f"{loaded_package_file}"
    ) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


if blender_colmap.__version__ != expected_version:
    raise RuntimeError(
        f"요청 버전 {expected_version}과 로드 버전 "
        f"{blender_colmap.__version__}이 다릅니다."
    )

model = read_model_binary(sparse_directory, modern=True)
timings = defaultdict(float)
originals = []

for name in (
    "_write_cameras",
    "_write_images",
    "_write_points3d",
    "_write_rigs",
    "_write_frames",
):
    original = getattr(binary_writer, name)
    originals.append((name, original))

    def timed(path, reconstruction, *, _name=name, _original=original):
        started = time.perf_counter()
        try:
            return _original(path, reconstruction)
        finally:
            timings[_name] += time.perf_counter() - started

    setattr(binary_writer, name, timed)

output = Path(tempfile.mkdtemp(prefix="writer_", dir=temp_parent))
try:
    started = time.perf_counter()
    paths = binary_writer._write_model_binary_validated(
        output,
        model,
        modern=True,
    )
    timings["writer_total"] = time.perf_counter() - started
    hashes = {path.name: sha256_file(path) for path in paths}
    sizes = {path.name: path.stat().st_size for path in paths}
finally:
    for name, original in originals:
        setattr(binary_writer, name, original)
    shutil.rmtree(output)

print(
    "BLENDER_COLMAP_BINARY_WRITER_PROFILE="
    + json.dumps(
        {
            "version": blender_colmap.__version__,
            "run_id": run_id,
            "timings": dict(timings),
            "hashes": hashes,
            "sizes": sizes,
            "counts": {
                "cameras": len(model.cameras),
                "images": len(model.images),
                "points3d": len(model.points3d),
            },
        },
        sort_keys=True,
    )
)
