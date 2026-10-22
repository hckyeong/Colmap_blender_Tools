"""검증한 원본을 작업별 Windows 공유 메모리로 직접 변환해 전달합니다."""
from __future__ import annotations

from contextlib import ExitStack
from multiprocessing.shared_memory import SharedMemory
import struct
from typing import Any
from uuid import uuid4

import numpy as np

from . import icp_geometry as geometry
from . import icp_point_kd_contract as contract

_HEADER = struct.Struct('<8sQ32s16x')
_MAGIC = b'CPKDIPC1'


def _allocate(owner: ExitStack, count: int, dtype: str) -> tuple[SharedMemory, dict[str, Any]]:
    """예측 불가능한 새 이름으로 버퍼를 만들고 작업 소유자에게 해제를 등록합니다."""
    byte_count = count * 3 * np.dtype(dtype).itemsize
    token = uuid4().hex
    name = 'Local\\ColmapPointKd_' + token
    shared = SharedMemory(name=name, create=True, size=_HEADER.size + byte_count)
    owner.callback(shared.close)
    _HEADER.pack_into(shared.buf, 0, _MAGIC, byte_count, token.encode('ascii'))
    return shared, {'name': name, 'byte_count': byte_count}


def _write_sources(owner: ExitStack, sources: tuple) -> dict[str, Any]:
    """공통 좌표와 합쳐진 면 번호를 전송 버퍼에 직접 작성하고 배열 뷰는 즉시 해제합니다."""
    kind = sources[0][0].geometry_kind
    count = sum(len(item[1]) for item in sources)
    is_mesh = kind == 'TRIANGLE_MESH'
    if is_mesh and count > np.iinfo(np.uint32).max:
        raise geometry.CcCoreGeometryError('A 메시 전체 정점 수가 삼각면 인덱스의 지원 범위를 넘습니다.')
    positions_memory, positions_descriptor = _allocate(owner, count, '<f8')
    destination = np.ndarray((count, 3), dtype='<f8', buffer=positions_memory.buf, offset=_HEADER.size)
    try:
        offset = 0
        for source, positions, _triangles in sources:
            combined = np.asarray(source.applied_alignment_float64) @ np.asarray(source.display_transform_float64)
            output = destination[offset:offset + len(positions)]
            try:
                with np.errstate(over='ignore', invalid='ignore'):
                    np.matmul(np.asarray(positions, dtype=np.float64), combined[:3, :3].T, out=output)
                    output += combined[:3, 3]
                if not np.isfinite(output).all():
                    raise geometry.CcCoreGeometryError(f'{source.owner_model_id}: 공통 좌표 변환 결과가 유한하지 않습니다.')
            finally:
                del output
            offset += len(positions)
    finally:
        del destination
    provenance = geometry.GeometryProvenance(tuple(item[0] for item in sources))
    result = {'kind': kind, 'positions_mapping': positions_descriptor, 'point_count': count,
              'provenance': contract.plain(provenance)}
    if is_mesh:
        face_count = sum(len(item[2]) for item in sources)
        triangle_memory, triangle_descriptor = _allocate(owner, face_count, '<u4')
        destination = np.ndarray((face_count, 3), dtype='<u4', buffer=triangle_memory.buf, offset=_HEADER.size)
        try:
            offset = face_offset = 0
            for _source, positions, triangles in sources:
                np.add(triangles, np.uint32(offset), out=destination[face_offset:face_offset + len(triangles)])
                face_offset += len(triangles)
                offset += len(positions)
        finally:
            del destination
        result.update(triangles_mapping=triangle_descriptor, triangle_count=face_count)
    return result


def create_shared_geometry_snapshot(runtime: Any, owner: ExitStack, *, moving_model_id: str,
                                    reference_model_ids: tuple[str, ...], initial_alignment_float64: object):
    """불러오기 때 고정한 원본은 참조만 전달하고 구형 저장소의 작업별 입력도 지원합니다."""
    provenance, moving, references = geometry._capture_sources(runtime, moving_model_id=moving_model_id,
        reference_model_ids=reference_model_ids, initial_alignment_float64=initial_alignment_float64,
        verify_content=True, validate_geometry=True, point_kd_input=True)
    modes = ('POINT' if item[0].geometry_kind == 'COLMAP_POINT_CLOUD' else 'MESH' for item in (moving, references[0]))
    mode = '_TO_'.join(modes)
    fingerprint = geometry._digest_value(provenance.to_mapping())
    metadata = geometry.CcCoreGeometrySnapshotMetadata(provenance, fingerprint, mode, mode == 'MESH_TO_POINT')
    writer = _reference_sources if hasattr(runtime.model_repository, 'alignment_input_for') else _write_sources
    manifest_geometry = {'moving': writer(owner, (moving,)), 'reference': writer(owner, references),
                         'input_fingerprint': fingerprint, 'provenance': contract.plain(provenance)}
    return metadata, manifest_geometry


def _reference_sources(owner: ExitStack, sources: tuple) -> dict[str, Any]:
    """원본 공유 버퍼 수명을 작업까지 유지하고 변환 행렬·레이어별 설명자만 작성합니다."""
    from ..geometry_buffers import geometry_mapping_descriptor
    parts = []
    for source, positions, triangles in sources:
        part = {'positions_mapping': geometry_mapping_descriptor(positions), 'point_count': len(positions),
                'transform_4x4': (np.asarray(source.applied_alignment_float64)
                                  @ np.asarray(source.display_transform_float64)).tolist()}
        if triangles is not None:
            part.update(triangles_mapping=geometry_mapping_descriptor(triangles), triangle_count=len(triangles))
        parts.append(part)

    def release_references(retained=sources):
        """작업 종료까지 원본·정리 배열을 붙잡고 종료 시 참조만 반납합니다."""
        return None

    owner.callback(release_references)
    result = {'kind': sources[0][0].geometry_kind, 'point_count': sum(p['point_count'] for p in parts),
              'parts': parts, 'provenance': contract.plain(geometry.GeometryProvenance(tuple(s[0] for s in sources)))}
    if sources[0][2] is not None:
        if result['point_count'] > np.iinfo(np.uint32).max:
            raise geometry.CcCoreGeometryError('A 메시 전체 정점 수가 삼각면 인덱스의 지원 범위를 넘습니다.')
        result['triangle_count'] = sum(p['triangle_count'] for p in parts)
    return result
