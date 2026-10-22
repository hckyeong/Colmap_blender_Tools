"""원본 형상을 읽기 전용 버퍼에 한 번 고정하고 동일 배열의 지문·공유 설명자를 재사용합니다."""
from __future__ import annotations

from dataclasses import dataclass
import mmap
import os
import struct
from uuid import uuid4
import weakref

import numpy as np

_HEADER = struct.Struct('<8sQ32s16x')
_MAGIC = b'CPKDIPC1'
_BUFFERS: dict[int, '_BufferRecord'] = {}


@dataclass
class _BufferRecord:
    """배열 수명을 늘리지 않고 불변 원본의 설명자와 최초 지문만 보관합니다."""
    reference: weakref.ReferenceType
    descriptor: dict | None
    fingerprint: str | None = None


def _record(array: np.ndarray) -> _BufferRecord | None:
    """동일 객체·읽기 전용 상태를 만족하는 등록 배열만 찾습니다."""
    record = _BUFFERS.get(id(array))
    return record if record is not None and record.reference() is array and not array.flags.writeable else None


def immutable_geometry_array(value: object, dtype: str) -> np.ndarray:
    """입력 복사를 불러오기 때 한 번 수행하고 쓰기 재활성화가 불가능한 원본 뷰를 반환합니다."""
    array = np.ascontiguousarray(value, dtype=np.dtype(dtype))
    if _record(array) is not None:
        return array
    descriptor = None
    if os.name == 'nt':
        token = uuid4().hex
        name = 'Local\\ColmapPointKd_' + token
        size = _HEADER.size + array.nbytes
        writable = mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_WRITE)
        readonly = None
        try:
            writable[:_HEADER.size] = _HEADER.pack(_MAGIC, array.nbytes, token.encode('ascii'))
            view = memoryview(writable)[_HEADER.size:]
            try:
                view[:] = memoryview(array).cast('B')
            finally:
                view.release()
            readonly = mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)
            result = np.ndarray(array.shape, dtype=array.dtype, buffer=readonly, offset=_HEADER.size)
        except Exception:
            if readonly is not None:
                readonly.close()
            raise
        finally:
            writable.close()
        descriptor = {'name': name, 'byte_count': array.nbytes}
    else:
        result = np.frombuffer(array.tobytes(order='C'), dtype=array.dtype).reshape(array.shape)
    key = id(result)

    def expired(reference):
        """해당 배열이 해제됐을 때 같은 세대의 설명자만 제거합니다."""
        record = _BUFFERS.get(key)
        if record is not None and record.reference is reference:
            _BUFFERS.pop(key, None)

    _BUFFERS[key] = _BufferRecord(weakref.ref(result, expired), descriptor)
    return result


def immutable_geometry_fingerprint(array: np.ndarray, dtype: str) -> str:
    """불러올 때 계산한 전체 원본 해시를 불변 배열의 수명 동안 재사용합니다."""
    from .alignment.icp_array_hash import array_sha256_fingerprint
    record = _record(array)
    if record is None or array.dtype != np.dtype(dtype):
        raise ValueError('정렬 원본이 등록된 불변 배열이 아닙니다. 형상을 다시 불러오십시오.')
    if record.fingerprint is None:
        record.fingerprint = array_sha256_fingerprint(array, dtype)
    return record.fingerprint


def geometry_mapping_descriptor(array: np.ndarray) -> dict:
    """현재 살아 있는 원본의 검증된 Windows 공유 설명자를 반환합니다."""
    record = _record(array)
    if record is None or record.descriptor is None:
        raise ValueError('정렬 원본의 읽기 전용 공유 버퍼가 없습니다.')
    return dict(record.descriptor)


def is_immutable_geometry(array: np.ndarray) -> bool:
    """원본 버퍼가 등록된 불변 배열인지 복사 없이 확인합니다."""
    return _record(array) is not None
