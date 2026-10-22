"""독립 Blender 실험에서 스냅샷·전달·검증 단계의 비용을 관측합니다."""
import functools
from importlib import import_module
import json
from pathlib import Path
import runpy
import sys
from time import perf_counter


def main():
    """지정한 배포본의 파일·공유 입력 함수만 계측하고 동일 모델 실험을 실행합니다."""
    arguments = sys.argv[sys.argv.index('--') + 1:]
    package = Path(arguments[arguments.index('--package-root') + 1]).resolve()
    output = Path(arguments[arguments.index('--output') + 1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(package))
    records = []

    def observe(module, name):
        """원래 함수를 유지하면서 인자 범위와 벽시계 시간만 기록합니다."""
        original = getattr(module, name)

        @functools.wraps(original)
        def measured(*args, **kwargs):
            """중첩 호출의 순서와 성공 여부를 포함해 한 번의 비용을 기록합니다."""
            row = {'function': name, 'start': perf_counter(), 'verify_content': kwargs.get('verify_content')}
            records.append(row)
            try:
                value = original(*args, **kwargs)
                row['success'] = True
                return value
            finally:
                row['seconds'] = perf_counter() - row['start']

        setattr(module, name, measured)

    geometry = import_module('tools.align.alignment.icp_geometry')
    runtime = import_module('tools.align.alignment.icp_point_kd_runtime')
    for name in ('_capture_sources', '_prepare_source', '_geometry_input', '_immutable_array',
                 '_array_fingerprint', 'create_cccore_geometry_snapshot', 'geometry_snapshot_validation_message'):
        observe(geometry, name)
    for name in ('begin_point_kd_job', '_write_geometry', '_launch'):
        observe(runtime, name)
    if Path(runtime.__file__).with_name('icp_point_kd_input.py').is_file():
        shared = import_module('tools.align.alignment.icp_point_kd_input')
        for name in ('create_shared_geometry_snapshot', '_write_sources', '_allocate'):
            observe(shared, name)
    try:
        runpy.run_path(str(Path(__file__).with_name('benchmark_point_kd_blender.py')), run_name='__main__')
    finally:
        (output / 'preparation-profile.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
