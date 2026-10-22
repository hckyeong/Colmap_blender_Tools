"""COLMAP 파일 오류를 호출부가 구분할 수 있도록 정의합니다."""

from __future__ import annotations


class ColmapIOError(ValueError):
    """COLMAP sparse model을 안전하게 읽거나 검증할 수 없을 때 발생합니다."""


class ColmapDiscoveryError(ColmapIOError):
    """선택한 폴더의 필수 파일 세트가 올바르지 않을 때 발생합니다."""


class ColmapParseError(ColmapIOError):
    """COLMAP text 또는 binary 레코드 구조가 손상됐을 때 발생합니다."""


class ColmapValidationError(ColmapIOError):
    """파싱된 레코드 사이의 수치·참조 무결성이 깨졌을 때 발생합니다."""
