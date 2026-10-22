"""정식 CC 배포의 이동식 Python·네이티브 원문·파일 무결성을 검증합니다."""
from __future__ import annotations
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT
PYTHON_VERSION = "3.12.10"
PYTHON_ARCHIVE = "python-3.12.10-embed-amd64.zip"
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
NUMPY_WHEEL = "numpy-1.26.4-cp312-cp312-win_amd64.whl"
NUMPY_SHA256 = "08beddf13648eb95f8d867350f6a018a4be2e5ad54c8d8caed89ebca558b2818"
NUMPY_URL = "https://files.pythonhosted.org/packages/16/2e/86f24451c2d530c88daf997cb8d6ac622c1d40d19f5a031ed68a4b73a374/" + NUMPY_WHEEL


def read_json(path):
    """UTF-8 JSON 파일을 읽습니다."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def file_hash(path):
    """파일 전체를 작은 블록으로 읽어 SHA-256을 계산합니다."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _forbidden_package_path_markers():
    """현재 개발 루트와 사용자 홈을 직접·JSON·UTF-16 바이트 표식으로 만듭니다."""
    path_texts = {str(ROOT.parent.resolve()), str(ROOT.resolve()), str(SOURCE.resolve()), str(Path.home().resolve())}
    markers = set()
    for path_text in path_texts:
        for variant in (path_text, path_text.replace("\\", "/"), path_text.replace("\\", "\\\\")):
            normalized = variant.casefold()
            markers.add(normalized.encode("utf-8"))
            markers.add(normalized.encode("utf-16le"))
    return tuple(sorted(markers))


def _scan_zip_for_forbidden_paths(archive, markers, *, prefix="", depth=0):
    """배포 ZIP과 포함된 재빌드 ZIP에서 호스트 절대 경로가 든 엔트리를 찾습니다."""
    hits = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        payload = archive.read(info)
        lowered = payload.lower()
        entry_name = f"{prefix}{info.filename}"
        if any(marker in lowered for marker in markers):
            hits.append(entry_name)
        if depth < 3 and info.filename.casefold().endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(payload)) as nested:
                    hits.extend(_scan_zip_for_forbidden_paths(
                        nested, markers, prefix=entry_name + "!", depth=depth + 1,
                    ))
            except zipfile.BadZipFile:
                pass
    return hits


def validate_package_archive(path):
    """배포 ZIP의 호스트 경로와 CC 제품 바이너리·라이선스·원문 해시를 검사합니다."""
    with zipfile.ZipFile(Path(path)) as archive:
        hits = _scan_zip_for_forbidden_paths(archive, _forbidden_package_path_markers())
        prefix = "tools/align/runtime/cccore/"
        if prefix + "runtime_manifest.json" in archive.namelist():
            manifest = json.loads(archive.read(prefix + "runtime_manifest.json").decode("utf-8-sig"))
            expected = dict(manifest["binary_sha256"])
            expected.update({"licenses/" + name: value for name, value in manifest["license_sha256"].items()})
            expected["cccore_rebuild_sources.zip"] = manifest["source_archive_sha256"]
            for name, fingerprint in expected.items():
                if hashlib.sha256(archive.read(prefix + name)).hexdigest() != fingerprint:
                    raise RuntimeError(f"배포 ZIP의 CC 파일 해시가 다릅니다: {name}")
    if hits:
        raise RuntimeError("배포 ZIP에 호스트 절대 경로가 남아 있습니다: " + ", ".join(sorted(hits)))


def validate_cccore_runtime(runtime_root):
    """독립 테스트 파일 없이 CC v8 실행 계약과 제품 원문·배포 파일 해시를 검사합니다."""
    root = Path(runtime_root).resolve()
    manifest_path = root / "runtime_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("CC Runtime Manifest가 없습니다.")
    manifest = read_json(manifest_path)
    if manifest.get("protocol_version") != "cccore-native-v8":
        raise RuntimeError("CC Runtime Manifest가 v8 네이티브 계약이 아닙니다.")
    options = manifest.get("build_options")
    if not isinstance(options, dict) or options.get("CCCORELIB_USE_TBB") is not True:
        raise RuntimeError("CC Runtime Manifest에서 oneTBB 빌드가 활성화되지 않았습니다.")
    binaries = manifest.get("binary_sha256")
    if not isinstance(binaries, dict):
        raise RuntimeError("CC Runtime Manifest에 바이너리 해시 목록이 없습니다.")
    if type(manifest.get("mesh_cleanup_abi")) is not int or manifest["mesh_cleanup_abi"] != 1:
        raise RuntimeError("CC 메시 검사 DLL의 ABI가 고정 계약과 다릅니다.")
    required_binaries = {"colmap_cccore_worker.exe", "colmap_mesh_cleanup.dll", "CCCoreLib.dll", "tbb12.dll"}
    if not required_binaries.issubset(binaries):
        missing = ", ".join(sorted(required_binaries.difference(binaries)))
        raise RuntimeError(f"CC 런타임 필수 바이너리 해시가 없습니다: {missing}")
    for filename, expected in binaries.items():
        if not isinstance(filename, str) or not filename.strip():
            raise RuntimeError("CC Runtime Manifest의 바이너리 파일명이 올바르지 않습니다.")
        path = (root / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise RuntimeError("CC Runtime Manifest의 바이너리 경로가 런타임 밖을 가리킵니다.") from error
        if not isinstance(expected, str) or len(expected) != 64 or not path.is_file():
            raise RuntimeError(f"CC 런타임 바이너리 또는 해시가 없습니다: {filename}")
        if file_hash(path).lower() != expected.lower():
            raise RuntimeError(f"CC 런타임 바이너리 해시가 Manifest와 다릅니다: {filename}")
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, dict):
        raise RuntimeError("CC Runtime Manifest에 의존성 잠금 정보가 없습니다.")
    tbb = next(
        (value for key, value in dependencies.items()
         if str(key).replace("_", "").lower() in {"tbb", "onetbb"} and isinstance(value, dict)),
        None,
    )
    if tbb is None or tbb.get("version") != "2022.0.0":
        raise RuntimeError("CC Runtime Manifest의 oneTBB 고정 버전이 올바르지 않습니다.")
    license_name = str(tbb.get("license_file", "licenses/oneTBB-Apache-2.0.txt"))
    license_path = (root / license_name).resolve()
    try:
        license_path.relative_to(root)
    except ValueError as error:
        raise RuntimeError("CC Runtime Manifest의 oneTBB 라이선스 경로가 런타임 밖을 가리킵니다.") from error
    expected_license_hash = tbb.get("license_sha256")
    if not license_path.is_file() or not isinstance(expected_license_hash, str) or len(expected_license_hash) != 64:
        raise RuntimeError("CC 런타임의 oneTBB 라이선스 또는 Manifest 해시가 없습니다.")
    if file_hash(license_path).lower() != expected_license_hash.lower():
        raise RuntimeError("CC 런타임의 oneTBB 라이선스 해시가 Manifest와 다릅니다.")
    self_test = manifest.get("self_test")
    if not isinstance(self_test, dict) or self_test.get("protocol_version") != "cccore-native-v8":
        raise RuntimeError("CC Runtime Manifest의 자체 검사 결과가 v8 계약이 아닙니다.")
    expected_libraries = {"library_path": "CCCoreLib.dll", "tbb_library_path": "tbb12.dll"}
    for name, expected in expected_libraries.items():
        if self_test.get(name) != expected:
            raise RuntimeError("CC Runtime Manifest 자체 검사의 DLL 상대 식별자가 고정 계약과 다릅니다.")
    for name in ("distance_multithread_support", "octree_multithread_support"):
        if self_test.get(name) is not True:
            raise RuntimeError(f"CC Runtime Manifest 자체 검사의 {name}가 활성 상태가 아닙니다.")
    return manifest


def validate_cccore_distribution(runtime_root):
    """배포 파일·점평면 패치 이력과 재빌드 원문의 무결성을 대조합니다."""
    root = Path(runtime_root).resolve()
    manifest = validate_cccore_runtime(root)
    lock = read_json(ROOT / "tools/align/native/cccore_worker/dependencies.lock.json")
    if manifest.get("dependencies") != lock:
        raise RuntimeError("CC 런타임의 의존성 잠금 정보가 현재 소스와 다릅니다.")
    safety = lock["cccore"]["registration_safety_patch"]
    grid = lock["cccore"]["mesh_grid_memory_patch"]
    reuse = lock["cccore"]["mesh_grid_reuse_patch"]
    domain = lock["cccore"]["mesh_grid_fixed_domain_patch"]
    icp = lock["cccore"]["icp_mesh_grid_reuse_patch"]
    bvh = lock["cccore"]["mesh_bvh_search_patch"]
    plane = lock["cccore"]["point_to_plane_patch"]
    expected_core_patch = "+".join((
        lock["cccore"]["patch_id"], lock["cccore"]["concurrency_patch"]["patch_id"], safety["id"],
        grid["id"], reuse["id"], domain["id"], icp["id"], bvh["id"], plane["id"],
    ))
    expected_safety = {
        "core_patch": expected_core_patch, "registration_safety_patch": safety["id"],
        "registration_safety_patch_sha256": safety["sha256"],
        "patched_registration_sha256": safety["patched_sha256"],
        "mesh_grid_reuse_patch": reuse["id"],
        "mesh_grid_reuse_patch_sha256": reuse["sha256"],
        "mesh_grid_reuse_source_sha256": {source["path"]: source["patched_sha256"] for source in reuse["files"]},
        "mesh_grid_fixed_domain_patch": domain["id"],
        "mesh_grid_fixed_domain_patch_sha256": domain["sha256"],
        "mesh_grid_fixed_domain_source_sha256": {source["path"]: source["patched_sha256"] for source in domain["files"]},
        "icp_mesh_grid_reuse_patch": icp["id"],
        "icp_mesh_grid_reuse_patch_sha256": icp["sha256"],
        "icp_mesh_grid_reuse_source_sha256": {source["path"]: source["patched_sha256"] for source in icp["files"]},
        "mesh_bvh_search_patch": bvh["id"],
        "mesh_bvh_search_patch_sha256": bvh["sha256"],
        "mesh_bvh_search_source_sha256": {source["path"]: source["patched_sha256"] for source in bvh["files"]},
        "point_to_plane_patch": plane["id"],
        "point_to_plane_patch_sha256": plane["sha256"],
        "point_to_plane_source_sha256": {source["path"]: source["patched_sha256"] for source in plane["files"]},
        "mesh_grid_memory_patch": grid["id"],
        "mesh_grid_memory_patch_sha256": grid["sha256"],
        "mesh_grid_source_sha256": {source["path"]: source["patched_sha256"] for source in grid["files"]},
    }
    if any(manifest.get(name) != expected for name, expected in expected_safety.items()):
        raise RuntimeError("CC 런타임의 등록·격자 메모리 패치 또는 최종 원문 해시가 고정 계약과 다릅니다.")
    if manifest["self_test"].get("core_patch") != expected_core_patch:
        raise RuntimeError("CC 자체 검사의 코어 패치 식별자가 최종 원문과 다릅니다.")
    required = {
        "colmap_cccore_worker.exe", "colmap_mesh_cleanup.dll", "CCCoreLib.dll", "tbb12.dll", "msvcp140.dll",
        "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
        "cccore_rebuild_sources.zip", "REBUILD.md",
        "THIRD_PARTY_NOTICES.md", "runtime_manifest.json",
        *("licenses/" + name for name in (
            "CCCoreLib-LGPL-2.0.txt", "CCCoreLib-build-MIT.txt", "nanoflann-BSD.txt",
            "json-MIT.txt", "oneTBB-Apache-2.0.txt", "oneTBB-third-party-programs.txt", "COLMAP-Tools-GPL.txt")),
    }
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    declared = {str(p).replace(chr(92), "/") for p in manifest.get("runtime_files", ())}
    if actual != required or declared != required:
        raise RuntimeError("CC 배포 파일 목록이 고정 계약과 다릅니다.")
    for name, expected in manifest.get("license_sha256", {}).items():
        path = (root / "licenses" / name).resolve()
        path.relative_to(root / "licenses")
        if file_hash(path) != expected:
            raise RuntimeError(f"CC 라이선스 해시가 다릅니다: {name}")
    if len(manifest.get("license_sha256", {})) != 7:
        raise RuntimeError("CC 라이선스 해시 목록이 누락되었습니다.")
    if (manifest.get("source_archive") != "cccore_rebuild_sources.zip"
            or file_hash(root / "cccore_rebuild_sources.zip") != manifest.get("source_archive_sha256")):
        raise RuntimeError("CC 배포 원문 묶음의 경로 또는 해시가 다릅니다.")
    native_source = ROOT / "tools/align/native/cccore_worker"
    with zipfile.ZipFile(root / "cccore_rebuild_sources.zip") as archive:
        source_hashes = {
            "native/" + safety["file"]: safety["sha256"],
            "cccore-src/src/RegistrationTools.cpp": safety["patched_sha256"],
            "native/" + grid["file"]: grid["sha256"],
            **{"cccore-src/" + source["path"]: source["patched_sha256"] for source in grid["files"]},
            "native/" + reuse["file"]: reuse["sha256"],
            **{"cccore-src/" + source["path"]: source["patched_sha256"] for source in reuse["files"]},
            "native/" + domain["file"]: domain["sha256"],
            **{"cccore-src/" + source["path"]: source["patched_sha256"] for source in domain["files"]},
            "native/" + icp["file"]: icp["sha256"],
            **{"cccore-src/" + source["path"]: source["patched_sha256"] for source in icp["files"]},
            "native/" + bvh["file"]: bvh["sha256"],
            **{"cccore-src/" + source["path"]: source["patched_sha256"] for source in bvh["files"]},
            "native/" + plane["file"]: plane["sha256"],
            **{"cccore-src/" + source["path"]: source["patched_sha256"] for source in plane["files"]},
        }
        for name, expected_hash in source_hashes.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected_hash:
                raise RuntimeError(f"CC 배포 원문의 등록·격자 메모리 패치 해시가 다릅니다: {name}")
        if json.loads(archive.read("native/dependencies.lock.json")) != lock:
            raise RuntimeError("CC 배포 원문의 의존성 잠금 정보가 현재 소스와 다릅니다.")
        cleanup_hashes = manifest.get("mesh_cleanup_source_sha256")
        if not isinstance(cleanup_hashes, dict) or set(cleanup_hashes) != {"mesh_cleanup.h", "mesh_cleanup.cpp"}:
            raise RuntimeError("CC 메시 검사 구현 원문 지문 목록이 올바르지 않습니다.")
        for name, digest in cleanup_hashes.items():
            built_source = archive.read("native/" + name)
            if hashlib.sha256(built_source).hexdigest() != digest:
                raise RuntimeError(f"CC 메시 검사 원문 해시가 Manifest와 다릅니다: {name}")
            if built_source.decode("utf-8").replace("\r\n", "\n") != (native_source / name).read_text(encoding="utf-8"):
                raise RuntimeError(f"CC 메시 검사 빌드 원문이 현재 소스와 다릅니다: {name}")
        bvh_hashes = manifest.get("bvh_source_sha256")
        if not isinstance(bvh_hashes, dict) or set(bvh_hashes) != {"mesh_bvh.h", "mesh_bvh.cpp"}:
            raise RuntimeError("CC 배포의 BVH 구현 원문 지문 목록이 올바르지 않습니다.")
        for name, digest in bvh_hashes.items():
            built_source = archive.read("native/" + name)
            if hashlib.sha256(built_source).hexdigest() != digest:
                raise RuntimeError(f"CC BVH 빌드 원문의 해시가 Manifest와 다릅니다: {name}")
            if built_source.decode("utf-8").replace("\r\n", "\n") != (native_source / name).read_text(encoding="utf-8"):
                raise RuntimeError(f"CC BVH 빌드 원문이 현재 소스와 다릅니다: {name}")
        for field, name in (("worker_source_sha256", "main.cpp"), ("cmake_source_sha256", "CMakeLists.txt")):
            built_source = archive.read("native/" + name)
            if hashlib.sha256(built_source).hexdigest() != manifest.get(field):
                raise RuntimeError(f"CC 빌드 원문의 해시가 Manifest와 다릅니다: {name}")
            # Git 체크아웃의 줄바꿈 차이만 허용하고 실제 빌드 원문의 내용은 그대로 대조합니다.
            normalized = (native_source / name).read_text(encoding="utf-8")
            if built_source.decode("utf-8").replace("\r\n", "\n") != normalized:
                raise RuntimeError(f"CC 런타임이 현재 네이티브 소스에서 빌드되지 않았습니다: {name}")
    return manifest


def validate_python_runtime(root):
    """전용 CPython·NumPy의 버전·격리 경로·실제 수치 연산을 검사합니다."""
    root = Path(root).resolve()
    for name in ("python.exe", "python312.dll", "python312.zip", "python312._pth", "LICENSE.txt",
                 "runtime_manifest.json", "Lib/site-packages/numpy-1.26.4.dist-info/LICENSE.txt"):
        if not (root / name).is_file():
            raise RuntimeError(f"CC Python 필수 파일이 없습니다: {name}")
    manifest = read_json(root / "runtime_manifest.json")
    expected = {"python_version": PYTHON_VERSION, "numpy_version": "1.26.4",
                "platform": "win-amd64", "alignment_backend": "CCCORELIB"}
    if any(manifest.get(k) != v for k, v in expected.items()):
        raise RuntimeError("CC Python Manifest가 고정 버전과 다릅니다.")
    artifacts = {"python_archive": PYTHON_ARCHIVE, "python_archive_sha256": PYTHON_SHA256,
                 "numpy_wheel": NUMPY_WHEEL, "numpy_wheel_sha256": NUMPY_SHA256}
    if any(manifest.get(key) != value for key, value in artifacts.items()):
        raise RuntimeError("CC Python·NumPy 원본 아카이브 식별자 또는 지문이 다릅니다.")
    if (root / "pyvenv.cfg").exists() or list(root.rglob("small_gicp*")):
        raise RuntimeError("CC Python에 가상환경 또는 구엔진 패키지가 포함되었습니다.")
    paths = (root / "python312._pth").read_text(encoding="utf-8-sig").splitlines()
    if [line for line in paths if line] != ["python312.zip", ".", "Lib/site-packages"]:
        raise RuntimeError("CC Python 격리 경로가 이동식 계약과 다릅니다.")
    code = "import json,sys,sysconfig,struct,numpy as n; print(json.dumps({'python':sys.version.split()[0], 'numpy':n.__version__, 'platform':sysconfig.get_platform(), 'bits':struct.calcsize('P')*8, 'norm':float(n.linalg.norm([3.,4.]))}))"
    result = subprocess.run([str(root / "python.exe"), "-I", "-c", code], check=True,
                            capture_output=True, text=True, encoding="utf-8", timeout=45,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    probe = json.loads(result.stdout)
    if probe != {"python": PYTHON_VERSION, "numpy": "1.26.4", "platform": "win-amd64", "bits": 64, "norm": 5.0}:
        raise RuntimeError("CC Python 실제 버전·수치 연산 검증이 실패했습니다.")
    return probe


def _download(url, path, expected):
    """고정 HTTPS 파일을 임시 경로로 받고 해시 확인 뒤 캐시에 반영합니다."""
    if not path.is_file():
        temporary = path.with_suffix(path.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if file_hash(temporary) != expected:
                raise RuntimeError(f"다운로드 원본의 SHA-256이 다릅니다: {path.name}")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if file_hash(path) != expected:
        raise RuntimeError(f"캐시 원본의 SHA-256이 다릅니다: {path.name}")


def _extract(archive_path, destination):
    """고정 해시 아카이브의 모든 엔트리가 대상 내부인지 확인하고 해제합니다."""
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            (destination / info.filename).resolve().relative_to(destination.resolve())
        archive.extractall(destination)


def prepare_python_runtime():
    """공식 고정 CPython ZIP과 NumPy 휠만으로 정식 CC Python을 조립합니다."""
    base = ROOT / ".cccore-runtime"
    destination = base / "python"
    if destination.exists():
        return validate_python_runtime(destination)
    cache = base / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    _download("https://www.python.org/ftp/python/" + PYTHON_VERSION + "/" + PYTHON_ARCHIVE,
              cache / PYTHON_ARCHIVE, PYTHON_SHA256)
    _download(NUMPY_URL, cache / NUMPY_WHEEL, NUMPY_SHA256)
    with tempfile.TemporaryDirectory(prefix="python-stage-", dir=base) as temporary:
        staged = Path(temporary) / "python"
        staged.mkdir()
        _extract(cache / PYTHON_ARCHIVE, staged)
        packages = staged / "Lib/site-packages"
        packages.mkdir(parents=True)
        _extract(cache / NUMPY_WHEEL, packages)
        (staged / "python312._pth").write_text("python312.zip\n.\nLib/site-packages\n", encoding="utf-8")
        manifest = {"schema_version": 1, "python_version": PYTHON_VERSION, "python_archive": PYTHON_ARCHIVE,
                    "python_archive_sha256": PYTHON_SHA256, "numpy_version": "1.26.4",
                    "numpy_wheel": NUMPY_WHEEL, "numpy_wheel_sha256": NUMPY_SHA256,
                    "platform": "win-amd64", "alignment_backend": "CCCORELIB"}
        (staged / "runtime_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        probe = validate_python_runtime(staged)
        staged.rename(destination)
    return probe


def stage_runtime(python_source, native_source, destination):
    """검증한 정식 실행 환경을 새로운 패키지 스테이징으로 복사하고 재검사합니다."""
    python_source, native_source, destination = map(Path, (python_source, native_source, destination))
    validate_python_runtime(python_source)
    validate_cccore_distribution(native_source)
    shutil.copytree(python_source, destination / "python", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(native_source, destination / "cccore")
    validate_python_runtime(destination / "python")
    validate_cccore_distribution(destination / "cccore")


def main():
    """정식 빌드가 요청한 런타임 준비·복사·아카이브 검증을 실행합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare-python")
    stage = sub.add_parser("stage")
    stage.add_argument("--python-source", required=True)
    stage.add_argument("--native-source", required=True)
    stage.add_argument("--destination", required=True)
    archive = sub.add_parser("validate-archive")
    archive.add_argument("path")
    native = sub.add_parser("validate-native")
    native.add_argument("path")
    args = parser.parse_args()
    if args.command == "prepare-python":
        print(json.dumps(prepare_python_runtime(), ensure_ascii=False))
    elif args.command == "stage":
        stage_runtime(args.python_source, args.native_source, args.destination)
    elif args.command == "validate-archive":
        validate_package_archive(args.path)
    else:
        validate_cccore_distribution(args.path)


if __name__ == "__main__":
    main()
