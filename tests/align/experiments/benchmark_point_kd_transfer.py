"""같은 원본 바이너리로 배열 기록 방식의 시간과 내용 보존을 비교합니다."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
from statistics import median
from time import perf_counter

import numpy as np


def main():
    """생성한 시험 파일만 삭제하며 기록 방식과 임시 속성을 교차 비교합니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--geometry', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    arrays = [np.fromfile(args.geometry / name, dtype=np.uint8) for name in ('moving_positions.bin', 'moving_triangles.bin')]
    expected = hashlib.sha256(b'').copy()
    for array in arrays:
        expected.update(array)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    kernel.SetFileAttributesW.restype = wintypes.BOOL
    results = []
    modes = ('NUMPY', 'VIEW', 'NUMPY_TEMP', 'VIEW_TEMP')
    for repeat in range(6):
        for mode in modes if repeat % 2 == 0 else reversed(modes):
            path = args.output / f'{mode}-{repeat}.bin'
            start = perf_counter()
            with path.open('xb') as stream:
                if mode.endswith('TEMP') and not kernel.SetFileAttributesW(str(path.resolve()), 0x100):
                    raise ctypes.WinError(ctypes.get_last_error())
                for array in arrays:
                    if mode.startswith('NUMPY'):
                        array.tofile(stream)
                    else:
                        written = stream.write(memoryview(array))
                        assert written == array.nbytes
            elapsed = perf_counter() - start
            assert path.stat().st_size == sum(a.nbytes for a in arrays)
            if repeat == 0:
                with path.open('rb') as stream:
                    assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected.hexdigest()
            path.unlink()
            results.append({'mode': mode, 'repeat': repeat, 'warmup': repeat == 0, 'seconds': elapsed})
    summary = {mode: median(r['seconds'] for r in results if r['mode'] == mode and not r['warmup']) for mode in modes}
    (args.output / 'results.json').write_text(json.dumps({'runs': results, 'median_seconds': summary,
        'bytes': sum(a.nbytes for a in arrays), 'sha256': expected.hexdigest()}, indent=2), encoding='utf-8')
    print(summary)


if __name__ == '__main__':
    main()
