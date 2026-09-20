"""배포 ZIP을 격리 설치하고 실제 프로젝트의 동일 마커로 전체 Align을 측정합니다."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import zipfile


def main():
    """기존 사용자 환경과 원본 파일을 보존하며 별도 Blender 프로세스만 실행합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--blender', type=Path, default=Path('C:/Program Files/Blender Foundation/Blender 5.2/blender.exe'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = args.archive.resolve(strict=True)
    manifest = args.manifest.resolve(strict=True)
    project = args.project.resolve(strict=True)
    reference = json.loads(manifest.read_text(encoding='utf-8-sig'))
    package = output/'repository/colmap_tools'
    package.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            target = (package/entry.filename).resolve()
            if not target.is_relative_to(package):
                raise ValueError('검증 ZIP 항목이 설치 폴더 밖을 가리킵니다.')
        bundle.extractall(package)
    environment = {**os.environ, 'PYTHONUTF8': '1'}
    for key, name in [('BLENDER_USER_CONFIG', 'config'), ('BLENDER_USER_SCRIPTS', 'scripts'),
                      ('BLENDER_USER_EXTENSIONS', 'extensions'), ('BLENDER_USER_DATAFILES', 'datafiles')]:
        directory = output/name
        directory.mkdir()
        environment[key] = str(directory)
    module_name = 'bl_ext.colmap_plane_verify.colmap_tools'
    enable = output/'enable.py'
    enable.write_text(
        '"""격리 배포본과 비교 조건의 CPU 상한만 활성화합니다."""\n'
        'import bpy\n'
        f'bpy.ops.preferences.addon_enable(module={module_name!r})\n'
        f'bpy.context.preferences.addons[{module_name!r}].preferences.cc_cpu_thread_limit='
        f'{reference["parameters"]["cc_max_threads"]!r}\n'
        'bpy.ops.wm.save_userpref()\n', encoding='utf-8')
    for index, command in enumerate([
        ['--background', '--command', 'extension', 'repo-add', 'colmap_plane_verify', '--name', '정렬 검증',
         '--directory', str(package.parent), '--clear-all'],
        ['--background', '--python-exit-code', '1', '--python', str(enable)],
    ]):
        with (output/f'setup-{index}.log').open('w', encoding='utf-8') as log:
            subprocess.run([str(args.blender), *command], env=environment, cwd=root,
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    command = [str(args.blender), str(project), '--python-use-system-env', '--python',
               str(root/'tests/align/experiments/benchmark_fixed_domain_blender.py'), '--',
               '--manifest', str(manifest), '--project', str(project), '--output', str(output/'operator'),
               '--package', module_name]
    record = dict(archive=str(archive), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  project=str(project), manifest=str(manifest), command=command,
                  environment={key:value for key,value in environment.items() if key.startswith('BLENDER_USER_')})
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    with (output/'blender.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, env=environment, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                   startupinfo=startup)
        record['pid'] = process.pid
        (output/'environment.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        code = process.wait()
    if code:
        raise RuntimeError(f'검증 Blender가 종료 코드 {code}로 끝났습니다: {output}')
    report = json.loads((output/'operator/summary.json').read_text(encoding='utf-8'))
    if report.get('status') != '완료':
        raise RuntimeError(f'실제 Align 검증이 통과하지 못했습니다: {report}')
    print(json.dumps({key: report[key] for key in ('status', 'addon_version', 'stage_times')},
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
