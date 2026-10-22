"""Blender 밖에서 상주 원본을 읽고 기존 계산용 공통 좌표 입력을 만듭니다."""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
import struct
from uuid import uuid4

import numpy as np

if __package__:
    from . import icp_point_kd_contract as contract
else:
    import icp_point_kd_contract as contract

_HEADER = struct.Struct('<8sQ32s16x')
_MAGIC = b'CPKDIPC1'
_BLOCK_ROWS = 262_144


def _cancel(root: Path) -> None:
    """입력 변환 중에도 작업의 취소 요청을 확인합니다."""
    if (root / 'cancel.request').is_file():
        raise contract.PointKdCancelled('정렬 입력 변환을 취소했습니다.')


def _allocate(owner: ExitStack, count: int, dtype: str) -> tuple:
    """외부 작업이 소유하는 변환 결과 공유 배열을 생성합니다."""
    byte_count = count * 3 * np.dtype(dtype).itemsize
    token = uuid4().hex
    name = 'Local\\ColmapPointKd_' + token
    memory = SharedMemory(name=name, create=True, size=_HEADER.size + byte_count)
    owner.callback(memory.close)
    _HEADER.pack_into(memory.buf, 0, _MAGIC, byte_count, token.encode('ascii'))
    return memory, {'name': name, 'byte_count': byte_count}


def _read_part(entry: dict, field: str, dtype: str, count: int) -> tuple:
    """기존 원본 매핑의 길이·토큰을 확인하고 좌표를 복사 없이 엽니다."""
    descriptor = entry[field + '_mapping']
    memory = SharedMemory(name=descriptor['name'], create=False)
    try:
        magic, size, token = _HEADER.unpack_from(memory.buf)
        if (magic != _MAGIC or size != count * 3 * np.dtype(dtype).itemsize
                or size != descriptor['byte_count'] or memory.size < size + _HEADER.size
                or token.decode('ascii') != descriptor['name'].split('_')[-1]):
            raise contract.PointKdError('상주 원본 공유 버퍼의 헤더가 일치하지 않습니다.')
        array = np.ndarray((count, 3), dtype=dtype, buffer=memory.buf, offset=_HEADER.size)
        array.setflags(write=False)
        return memory, array
    except Exception:
        memory.close()
        raise


def _copy_part(root: Path, part: dict, destination: np.ndarray, faces: np.ndarray | None,
               point_offset: int, face_offset: int) -> None:
    """기존 배정밀도 연산 순서로 좌표를 변환하고 여러 기준의 면 번호를 연결합니다."""
    memory, source = _read_part(part, 'positions', '<f8', part['point_count'])
    try:
        matrix = np.asarray(part['transform_4x4'], dtype=np.float64)
        for start in range(0, len(source), _BLOCK_ROWS):
            _cancel(root)
            stop = min(start + _BLOCK_ROWS, len(source))
            output = destination[point_offset + start:point_offset + stop]
            try:
                with np.errstate(over='ignore', invalid='ignore'):
                    np.matmul(source[start:stop], matrix[:3, :3].T, out=output)
                    output += matrix[:3, 3]
                if not np.isfinite(output).all():
                    raise contract.PointKdError('공통 좌표 변환 결과가 유한하지 않습니다.')
            finally:
                del output
    finally:
        del source
        memory.close()
    if faces is not None:
        memory, source = _read_part(part, 'triangles', '<u4', part['triangle_count'])
        try:
            for start in range(0, len(source), _BLOCK_ROWS):
                _cancel(root)
                stop = min(start + _BLOCK_ROWS, len(source))
                np.add(source[start:stop], np.uint32(point_offset), out=faces[face_offset + start:face_offset + stop])
        finally:
            del source
            memory.close()


def _materialize(root: Path, owner: ExitStack, entry: dict) -> dict:
    """역할별 작업 입력을 한 번 만들고 네이티브가 읽을 기존 설명자로 반환합니다."""
    memory, descriptor = _allocate(owner, entry['point_count'], '<f8')
    destination = np.ndarray((entry['point_count'], 3), dtype='<f8', buffer=memory.buf, offset=_HEADER.size)
    faces = None
    try:
        result = {'point_count': entry['point_count'], 'positions_mapping': descriptor}
        if 'triangle_count' in entry:
            memory, descriptor = _allocate(owner, entry['triangle_count'], '<u4')
            faces = np.ndarray((entry['triangle_count'], 3), dtype='<u4', buffer=memory.buf, offset=_HEADER.size)
            result.update(triangle_count=entry['triangle_count'], triangles_mapping=descriptor)
        point_offset = face_offset = 0
        for part in entry['parts']:
            _copy_part(root, part, destination, faces, point_offset, face_offset)
            point_offset += part['point_count']
            face_offset += part.get('triangle_count', 0)
        return result
    finally:
        del destination, faces


@contextmanager
def prepared_native_request(root: Path, request: dict):
    """계산이 끝날 때까지 변환 입력을 유지하고 성공·실패·취소 시 모두 해제합니다."""
    with ExitStack() as owner:
        prepared = dict(request)
        for role in ('source', 'target'):
            if 'parts' in request[role]:
                prepared[role] = _materialize(root, owner, request[role])
        yield prepared
