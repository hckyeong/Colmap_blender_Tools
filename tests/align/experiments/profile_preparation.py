"""제품 전처리를 그대로 실행하고 함수·행별 시간을 검증용 프로세스에서만 수집합니다."""
import argparse
from collections import defaultdict
from functools import wraps
import hashlib
import inspect
import json
import linecache
import os
from pathlib import Path
import shutil
import sys
import time

import numpy as np


class PreparationCaptured(BaseException):
    """전처리 완료 지점에서 ICP에 진입하지 않고 시험 실행을 끝냅니다."""


class Timings:
    """중첩된 함수의 포함 시간과 자식 호출을 뺀 시간을 따로 기록합니다."""

    def __init__(self):
        """호출 스택·집계·교체 함수 복원 목록을 초기화합니다."""
        self.stack = []
        self.rows = defaultdict(lambda: dict(calls=0, inclusive_ns=0, exclusive_ns=0))
        self.restores = []
        self.active = False

    def enter(self, name):
        """호출의 경로와 시작 시각을 기록합니다."""
        path = tuple(entry[0] for entry in self.stack) + (name,)
        self.stack.append([name, time.perf_counter_ns(), 0, path])

    def leave(self):
        """자식 시간을 한 번만 차감하여 호출별 비용을 누적합니다."""
        _, started, children, path = self.stack.pop()
        elapsed = time.perf_counter_ns() - started
        row = self.rows[path]
        row['calls'] += 1
        row['inclusive_ns'] += elapsed
        row['exclusive_ns'] += elapsed - children
        if self.stack:
            self.stack[-1][2] += elapsed

    def patch(self, owner, name, label):
        """현재 시험 프로세스 안에서만 측정용 래퍼를 설치합니다."""
        original = getattr(owner, name)

        @wraps(original)
        def measured(*args, **kwargs):
            """원래 함수의 인자·결과·예외를 유지하고 경과 시간만 기록합니다."""
            if not self.active:
                return original(*args, **kwargs)
            self.enter(label(*args, **kwargs) if callable(label) else label)
            try:
                return original(*args, **kwargs)
            finally:
                self.leave()

        setattr(owner, name, measured)
        self.restores.append((owner, name, original))

    def report(self):
        """시간 단위를 초로 변환하여 읽기 쉬운 경로별 자료를 만듭니다."""
        return [dict(path=list(path), calls=row['calls'], inclusive_seconds=row['inclusive_ns'] / 1e9,
                     exclusive_seconds=row['exclusive_ns'] / 1e9) for path, row in sorted(self.rows.items())]

    def restore(self):
        """프로세스 종료 전 설치한 함수 래퍼를 역순으로 복원합니다."""
        for owner, name, original in reversed(self.restores):
            setattr(owner, name, original)


class SelectedLines:
    """선택한 원본 함수의 행 실행 시간만 수집하며 함수 간 합산은 하지 않습니다."""

    def __init__(self, functions):
        """실제 코드 객체로 대상을 제한하여 다른 라이브러리 행 추적을 피합니다."""
        self.codes = {function.__code__: function.__qualname__ for function in functions}
        self.frames = {}
        self.rows = defaultdict(lambda: [0, 0])

    def global_trace(self, frame, event, arg):
        """선택된 함수 호출에만 행 추적기를 연결합니다."""
        if event == 'call' and frame.f_code in self.codes:
            label = self.codes[frame.f_code]
            if frame.f_code.co_name == 'prepare_mesh_alignment_geometry':
                label += ':float32' if frame.f_locals.get('native_precision') else ':float64'
            self.frames[id(frame)] = [label, None, time.perf_counter_ns()]
            return self.local_trace
        return None

    def local_trace(self, frame, event, arg):
        """직전 행부터 현재 이벤트까지의 시간을 해당 행에 누적합니다."""
        if event not in ('line', 'return'):
            return self.local_trace
        now = time.perf_counter_ns()
        state = self.frames[id(frame)]
        if state[1] is not None:
            row = self.rows[(state[0], frame.f_code.co_filename, state[1])]
            row[0] += 1
            row[1] += now - state[2]
        if event == 'return':
            del self.frames[id(frame)]
        else:
            state[1] = frame.f_lineno
            state[2] = time.perf_counter_ns()
        return self.local_trace

    def report(self):
        """원문 위치·내용·횟수와 포함 시간을 큰 순서로 반환합니다."""
        return sorted([dict(function=key[0], file=key[1], line=key[2],
                            source=linecache.getline(key[1], key[2]).strip(), calls=value[0], seconds=value[1] / 1e9)
                       for key, value in self.rows.items()], key=lambda row: -row['seconds'])


def read_json(path):
    """기존 보고서와 입력의 UTF-8 JSON을 읽습니다."""
    return json.loads(path.read_text(encoding='utf-8-sig'))


def stable(value):
    """샘플링 진단에서 시간 항목만 제외해 결과와 정책을 엄격 비교합니다."""
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if not key.endswith('_seconds')}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def array_hash(value):
    """배열 형식·크기·순서를 포함한 지문을 측정 구간 밖에서 계산합니다."""
    value = np.ascontiguousarray(value)
    digest = hashlib.sha256(f'{value.dtype.str}:{value.shape}'.encode('ascii'))
    digest.update(memoryview(value).cast('B'))
    return digest.hexdigest()


def process_memory():
    """Windows 계측 프로세스의 준비 완료 시점 최대 작업집합과 커밋량을 읽습니다."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        """운영체제 메모리 통계 구조체의 원래 필드 크기를 보존합니다."""
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    value = Counters()
    value.cb = ctypes.sizeof(value)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(peak_working_set_bytes=value.PeakWorkingSetSize, peak_commit_bytes=value.PeakPagefileUsage,
                working_set_bytes=value.WorkingSetSize, private_bytes=value.PrivateUsage)


def install_timers(timers, solver, contract, overlap):
    """전처리의 주요 함수와 실제 파일 기록·네이티브 왕복 시간을 연결합니다."""
    timers.patch(contract, 'load_geometry', lambda directory, entry: '입력읽기:' + entry['positions_file'])
    timers.patch(solver, '_clean_mesh_geometry', lambda geometry, label, **kw:
                 ('단정도검사:' if kw.get('native_precision') else '원본검사:') + label)
    timers.patch(solver, 'make_normalization', '정규화범위계산')
    timers.patch(solver.Normalization, 'apply', '좌표정규화')
    timers.patch(solver, 'prepare_overlap_policy', '겹침반경결정')
    timers.patch(solver, 'prepare_candidate_samples', '후보표본준비')
    timers.patch(solver, 'make_preparing_search_domain', '공통검색범위식별')
    timers.patch(solver, 'native_build_identity', '실행파일검증')
    timers.patch(solver.NativeRunner, 'start', '네이티브세션시작')
    timers.patch(solver.NativeRunner, 'geometry', lambda self, label, *args, **kw:
                 '네이티브입력전달:' + ('메시' if 'model' in label else '검사점'))
    timers.patch(solver, '_write_array_atomic', lambda path, values:
                 '입력파일기록:' + ('삼각면' if 'triangles' in path.name else '좌표'))
    timers.patch(solver.NativeRunner, 'call', '네이티브거리왕복')
    timers.patch(solver.NativeRunner, '_wait_for_response', '네이티브응답대기')
    for name, label in [('__init__', '표본자료생성'), ('probe', '겹침탐색'), ('sample', '합격표본추출'),
                        ('sample_full', '전체표면표본추출'), ('close', '표본자료해제')]:
        timers.patch(overlap.MeshSurfaceSampler, name, label)
    timers.patch(overlap, '_areas', '삼각면면적계산')
    timers.patch(np.memmap, 'flush', '누적표디스크반영')


def main():
    """명시한 빌드와 입력으로 전처리 경계에서 멈추고 계측·동일성 보고서를 저장합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('solver-directory', 'worker', 'input-directory', 'manifest', 'expected-result', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mode', choices=('control', 'functions', 'lines'), required=True)
    parser.add_argument('--allow-worker-change', action='store_true', help='네이티브 재빌드 비교를 명시적으로 허용')
    args = parser.parse_args()
    source_directory = args.solver_directory.resolve(strict=True)
    sys.path.insert(0, str(source_directory))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver
    import icp_cccore_overlap as overlap
    import icp_mesh_cleanup as cleanup
    assert Path(solver.__file__).resolve().parent == source_directory
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    job = output / 'job'
    job.mkdir()
    manifest = read_json(args.manifest)
    source = read_json(args.input_directory / 'source.json')
    expected = read_json(args.expected_result)
    assert source['input_fingerprint'] == manifest['geometry']['input_fingerprint'] == expected['geometry_input_fingerprint']
    worker = args.worker.resolve(strict=True)
    worker_sha256 = hashlib.sha256(worker.read_bytes()).hexdigest()
    if not args.allow_worker_change:
        assert worker_sha256 == expected['native_worker_sha256']
    input_hashes = {}
    for role in ('moving', 'reference'):
        assert source['geometry'][role] == manifest['geometry'][role]
        for key in ('positions_file', 'triangles_file'):
            relative = manifest['geometry'][role].get(key)
            if relative:
                original = (args.input_directory / relative).resolve(strict=True)
                target = (job / relative).resolve()
                if not original.is_relative_to(args.input_directory.resolve()) or not target.is_relative_to(job):
                    raise ValueError('입력 파일 경로가 작업 폴더를 벗어났습니다.')
                shutil.copy2(original, target)
                input_hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest['job_id'] = job.name
    contract.write_json_atomic(job / 'manifest.json', manifest)
    timers = Timings()
    lines = SelectedLines([cleanup.prepare_mesh_alignment_geometry, overlap.MeshSurfaceSampler.__init__,
                           contract.load_geometry, solver._write_array_atomic, solver.make_normalization,
                           solver.prepare_candidate_samples, solver.prepare_overlap_policy, solver.NativeRunner.geometry])
    captured = {}
    original_init = solver.NativeRunner.__init__
    original_boundary = solver.ExactEvaluationCache.__init__
    if args.mode != 'control':
        install_timers(timers, solver, contract, overlap)

    def begin_runner(self, *values, **options):
        """제품의 준비 타이머 시작 직후부터 원래 세션 초기화를 계측합니다."""
        captured['start_ns'] = time.perf_counter_ns()
        timers.active = args.mode != 'control'
        if timers.active:
            timers.enter('전처리')
        if args.mode == 'lines':
            sys.settrace(lines.global_trace)
        original_init(self, *values, **options)
        captured['runner'] = self

    def stop_at_boundary(self, *values, **options):
        """전처리 완료 직후 측정을 끝내고 표본을 보존한 뒤 세션을 정상 종료합니다."""
        captured['end_ns'] = time.perf_counter_ns()
        sys.settrace(None)
        if timers.active:
            timers.leave()
        timers.active = False
        captured['host_process_memory'] = process_memory()
        caller = inspect.currentframe().f_back
        variables = caller.f_locals
        captured.update(preparing_seconds=variables['preparing_seconds'],
                        sampling=variables['sampling_diagnostics'], normalization=variables['normalization'].to_mapping(),
                        arrays=dict(registration=variables['data_positions'], moving_evaluation=variables['moving_overlap'],
                                    reference_evaluation=variables['reference_overlap'],
                                    **{'full_' + name: value for name, value in variables['full_samples'].items()}))
        del caller, variables
        captured['runner'].close()
        raise PreparationCaptured()

    def progress(event):
        """실제 워커와 같이 진행 JSON을 기록하되 메시지를 터미널에 반복 출력하지 않습니다."""
        contract.write_json_atomic(job / 'progress.json', event)

    solver.NativeRunner.__init__ = begin_runner
    solver.ExactEvaluationCache.__init__ = stop_at_boundary
    try:
        try:
            solver.solve_cccore_job(job, worker, progress_sink=progress)
        except PreparationCaptured:
            pass
        else:
            raise AssertionError('전처리 완료 경계에서 멈추지 않았습니다.')
    finally:
        sys.settrace(None)
        timers.active = False
        timers.restore()
        solver.NativeRunner.__init__ = original_init
        solver.ExactEvaluationCache.__init__ = original_boundary
    runner = captured['runner']
    assert runner.session_exit_reason == 'closed'
    assert all(call['stage'] == 'PREPARING' for call in runner.physical_native_calls)
    assert stable(captured['sampling']) == stable(expected['sampling_diagnostics'])
    assert captured['normalization'] == expected['normalization']
    assert runner.session_statistics['bvh_build_count'] == 2
    assert runner.session_statistics['bvh_eviction_count'] == 0
    report = dict(mode=args.mode, scope='같은 제품 함수의 전처리만 실행, ICP 진입 전 정상 세션 종료',
                  preparing_seconds=captured['preparing_seconds'], measured_seconds=(captured['end_ns'] - captured['start_ns']) / 1e9,
                  host_process_memory=captured['host_process_memory'],
                  input_fingerprint=manifest['input_fingerprint'], geometry_fingerprint=source['input_fingerprint'],
                  native_worker_sha256=runner.native_worker_sha256, input_hashes=input_hashes,
                  worker_change_allowed=args.allow_worker_change,
                  expected_native_worker_sha256=expected['native_worker_sha256'],
                  solver_file=str(Path(solver.__file__)), solver_sha256=hashlib.sha256(Path(solver.__file__).read_bytes()).hexdigest(),
                  sample_hashes={name: array_hash(value) for name, value in captured['arrays'].items()},
                  sample_counts={name: len(value) for name, value in captured['arrays'].items()},
                  sampling_equal=True, normalization=captured['normalization'], sampling=captured['sampling'],
                  function_timings=timers.report(), line_timings=lines.report(), native_calls=runner.physical_native_calls,
                  session_statistics=runner.session_statistics, parameters=manifest['parameters'])
    contract.write_json_atomic(output / 'result.json', report)
    print(json.dumps(dict(mode=args.mode, preparing_seconds=report['preparing_seconds'],
                         measured_seconds=report['measured_seconds'], sampling_equal=True), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
