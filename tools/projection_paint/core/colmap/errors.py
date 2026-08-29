"""COLMAP Core에서 사용하는 명시적 오류 형식을 정의합니다."""


class ColmapError(ValueError):
    """검증되지 않은 COLMAP 입력을 거부할 때 사용합니다."""


class ImportCancelled(RuntimeError):
    """사용자 요청으로 Import 준비가 중단됐음을 나타냅니다."""
