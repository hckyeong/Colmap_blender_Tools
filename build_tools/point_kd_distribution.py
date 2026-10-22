"""고정 의존성으로 점 기반 정렬 작업기를 빌드하고 배포 무결성을 검사합니다."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

import cccore_distribution as shared

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools/align/native/point_kd_worker'
RUNTIME = ROOT / '.point-kd-runtime/point_kd'
PROTOCOL = 'point-kd-native-v1'


def run(arguments, **kwargs):
    """보이지 않는 자식 프로세스의 실패를 그대로 전달합니다."""
    return subprocess.run([str(item) for item in arguments], check=True,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)


def write_json(path, value):
    """빌드 감사 정보를 UTF-8 JSON으로 기록합니다."""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def checked_child(root, relative):
    """배포 매니페스트의 파일이 지정된 폴더 안에 있는지 검사합니다."""
    target = (root / relative).resolve()
    if target == root.resolve() or not target.is_relative_to(root.resolve()):
        raise RuntimeError('배포 파일 경로가 지정한 폴더를 벗어났습니다.')
    return target


def environment(root):
    """실제 작업기의 최소 검색·강체 계산과 프로토콜을 검사합니다."""
    completed = run([root / 'colmap_point_kd_worker.exe', '--check-environment'],
                    capture_output=True, text=True, encoding='utf-8', timeout=30)
    result = json.loads(completed.stdout)
    if result.get('protocol_version') != PROTOCOL or result.get('self_test') != 'passed':
        raise RuntimeError('점 기반 정렬 작업기의 실제 환경 검사가 실패했습니다.')
    return result


def validate_native(root):
    """런타임 파일·원문·잠금 정보와 자체 검사의 일치를 확인합니다."""
    root = Path(root).resolve()
    manifest = shared.read_json(root / 'runtime_manifest.json')
    if manifest.get('protocol_version') != PROTOCOL:
        raise RuntimeError('점 기반 정렬 런타임의 프로토콜이 다릅니다.')
    if manifest.get('dependencies') != shared.read_json(SOURCE / 'dependencies.lock.json'):
        raise RuntimeError('점 기반 정렬 런타임의 의존성 잠금 정보가 다릅니다.')
    required = {'colmap_point_kd_worker.exe', 'tbb12.dll', 'point_kd_rebuild_sources.zip',
                'licenses/Eigen-MPL-2.0.txt', 'licenses/json-MIT.txt', 'licenses/oneTBB-Apache-2.0.txt'}
    hashes = manifest.get('file_sha256', {})
    if not required.issubset(hashes):
        raise RuntimeError('점 기반 정렬 배포의 필수 파일 목록이 부족합니다.')
    for name, digest in hashes.items():
        if shared.file_hash(checked_child(root, name)) != digest:
            raise RuntimeError(f'점 기반 정렬 배포 파일 해시가 다릅니다: {name}')
    for name, digest in manifest.get('source_sha256', {}).items():
        if shared.file_hash(checked_child(SOURCE, name)) != digest:
            raise RuntimeError(f'점 기반 정렬 실행 파일을 현재 소스로 다시 빌드해야 합니다: {name}')
    environment(root)
    return manifest


def prepare():
    """검증한 아카이브와 소스만 사용해 이동 가능한 네이티브 배포 폴더를 만듭니다."""
    lock = shared.read_json(SOURCE / 'dependencies.lock.json')
    base = ROOT / '.point-kd-runtime'
    cache = base / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    eigen, tbb, json_lock = lock['eigen'], lock['tbb'], lock['json']
    archives = {}
    old_cache = ROOT / '.cccore-runtime/cache'
    for name, item, old_folder in (('eigen', eigen, 'point-kd-deps'), ('tbb', tbb, 'cccore-deps')):
        path = cache / item['archive_name']
        old = old_cache / old_folder / item['archive_name']
        if not path.exists() and old.exists() and shared.file_hash(old) == item['archive_sha256']:
            shutil.copy2(old, path)
        shared._download(item['archive_url'], path, item['archive_sha256'])
        archives[name] = path
    shared._download(json_lock['header_url'], cache / 'json.hpp', json_lock['header_sha256'])
    shared._download(json_lock['license_url'], cache / 'json-LICENSE.MIT', json_lock['license_sha256'])
    with tempfile.TemporaryDirectory(prefix='dependencies-', dir=base) as folder:
        deps = Path(folder)
        with tarfile.open(archives['eigen']) as archive:
            archive.extractall(deps, filter='data')
        shared._extract(archives['tbb'], deps)
        eigen_root, tbb_root = deps / eigen['archive_root'], deps / tbb['archive_root']
        vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
        vs = run([vswhere, '-latest', '-products', '*', '-version', '[17.0,18.0)',
                  '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                 capture_output=True, text=True).stdout.strip()
        if not vs:
            raise RuntimeError('Visual Studio 2022 C++ 빌드 도구를 찾지 못했습니다.')
        cmake = Path(vs) / 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'
        build = base / 'build'
        run([cmake, '-S', SOURCE, '-B', build, '-G', 'Visual Studio 17 2022', '-A', 'x64',
             f'-DEIGEN_INCLUDE_DIR={eigen_root}', f'-DJSON_INCLUDE_DIR={cache}',
             f'-DTBB_DIR={tbb_root / tbb["cmake_dir"]}'])
        run([cmake, '--build', build, '--config', 'Release', '--parallel', '8'])
        with tempfile.TemporaryDirectory(prefix='staging-', dir=base) as staging:
            staged = Path(staging) / 'point_kd'
            staged.mkdir()
            for name in ('colmap_point_kd_worker.exe', 'tbb12.dll'):
                shutil.copy2(build / 'bin' / name, staged / name)
            cc = ROOT / '.cccore-runtime/cccore'
            for name in ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll'):
                shutil.copy2(cc / name, staged / name)
            licenses = staged / 'licenses'
            licenses.mkdir()
            for source, name in ((eigen_root / 'COPYING.MPL2', 'Eigen-MPL-2.0.txt'),
                                 (cache / 'json-LICENSE.MIT', 'json-MIT.txt'),
                                 (tbb_root / tbb['license_source'], 'oneTBB-Apache-2.0.txt'),
                                 (tbb_root / tbb['third_party_source'], 'oneTBB-third-party-programs.txt'),
                                 (ROOT / 'LICENSE', 'COLMAP-Tools-GPL.txt')):
                shutil.copy2(source, licenses / name)
            probe = environment(staged)
            write_json(staged / 'self_test.json', probe)
            source_files = sorted(path for path in SOURCE.iterdir() if path.is_file())
            with zipfile.ZipFile(staged / 'point_kd_rebuild_sources.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in source_files:
                    archive.write(path, 'native/' + path.name)
                archive.write(Path(__file__), 'build_tools/point_kd_distribution.py')
                archive.write(Path(shared.__file__), 'build_tools/cccore_distribution.py')
                for path in sorted((ROOT / 'tests/align/native').glob('test_point_kd*')):
                    if path.is_file():
                        archive.write(path, 'tests/align/native/' + path.name)
            manifest = {'schema_version': 1, 'protocol_version': PROTOCOL, 'backend_id': 'POINT_KD_ICP',
                        'dependencies': lock, 'self_test': probe,
                        'source_sha256': {path.name: shared.file_hash(path) for path in source_files},
                        'file_sha256': {path.relative_to(staged).as_posix(): shared.file_hash(path)
                                        for path in sorted(staged.rglob('*')) if path.is_file()}}
            write_json(staged / 'runtime_manifest.json', manifest)
            validate_native(staged)
            runtime_target = checked_child(ROOT, '.point-kd-runtime/point_kd')
            backup = checked_child(base, Path(staging).name + '/previous')
            if runtime_target.exists():
                runtime_target.rename(backup)
            try:
                staged.rename(runtime_target)
            except Exception:
                if backup.exists():
                    backup.rename(runtime_target)
                raise
    return str(RUNTIME)


def stage(source, destination):
    """검증한 네이티브 런타임만 새 패키지 폴더로 복사합니다."""
    source, destination = Path(source), Path(destination)
    validate_native(source)
    shutil.copytree(source, destination)
    validate_native(destination)


def validate_archive(path):
    """설치 ZIP에 포함된 점 기반 작업기와 원문·라이선스의 해시를 검사합니다."""
    prefix = 'tools/align/runtime/point_kd/'
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(prefix + 'runtime_manifest.json'))
        if manifest.get('protocol_version') != PROTOCOL:
            raise RuntimeError('설치 ZIP의 점 기반 정렬 프로토콜이 다릅니다.')
        for name, digest in manifest['file_sha256'].items():
            if '..' in Path(name).parts or Path(name).is_absolute():
                raise RuntimeError('설치 ZIP의 파일 경로가 올바르지 않습니다.')
            if hashlib.sha256(archive.read(prefix + name)).hexdigest() != digest:
                raise RuntimeError(f'설치 ZIP의 점 기반 정렬 파일 해시가 다릅니다: {name}')


def main():
    """빌드·스테이징·설치 ZIP 검사 명령을 실행합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare')
    staging = commands.add_parser('stage')
    staging.add_argument('--source', default=str(RUNTIME))
    staging.add_argument('--destination', required=True)
    native = commands.add_parser('validate-native')
    native.add_argument('path')
    archive = commands.add_parser('validate-archive')
    archive.add_argument('path')
    args = parser.parse_args()
    if args.command == 'prepare':
        print(prepare())
    elif args.command == 'stage':
        stage(args.source, args.destination)
    elif args.command == 'validate-native':
        validate_native(args.path)
    else:
        validate_archive(args.path)


if __name__ == '__main__':
    main()
