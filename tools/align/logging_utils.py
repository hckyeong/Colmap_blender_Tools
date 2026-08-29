"""COLMAP Align의 단계별 진단 로그를 한 형식으로 출력합니다."""

from __future__ import annotations

import logging
import os
from typing import Final


LOGGER_ROOT_NAME: Final = "colmap_align"
LOG_LEVEL_ENV: Final = "COLMAP_ALIGN_LOG_LEVEL"
_HANDLER_OWNER: Final = "colmap_align_console"


def _resolve_log_level(level_name: str | None = None) -> int:
    """환경 변수나 인자로 받은 로그 수준을 안전한 logging 값으로 변환합니다."""
    normalized = (level_name or os.environ.get(LOG_LEVEL_ENV, "INFO")).strip().upper()
    resolved = getattr(logging, normalized, None)
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(level_name: str | None = None) -> logging.Logger:
    """중복 handler 없이 Blender 콘솔용 구조화 로그를 활성화합니다."""
    logger = logging.getLogger(LOGGER_ROOT_NAME)
    logger.setLevel(_resolve_log_level(level_name))
    logger.propagate = False
    for handler in logger.handlers:
        if getattr(handler, "_colmap_align_owner", "") == _HANDLER_OWNER:
            handler.setLevel(logger.level)
            return logger

    handler = logging.StreamHandler()
    handler._colmap_align_owner = _HANDLER_OWNER  # type: ignore[attr-defined]
    handler.setLevel(logger.level)
    handler.setFormatter(
        logging.Formatter(
            "[COLMAP Align] %(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def get_logger(component: str) -> logging.Logger:
    """공통 루트 아래에서 기능별 자식 로거를 반환합니다."""
    suffix = component.strip(".")
    return logging.getLogger(f"{LOGGER_ROOT_NAME}.{suffix}" if suffix else LOGGER_ROOT_NAME)


def shutdown_logging() -> None:
    """애드온이 설치한 handler만 제거해 재등록 중복과 파일 참조를 막습니다."""
    logger = logging.getLogger(LOGGER_ROOT_NAME)
    for handler in tuple(logger.handlers):
        if getattr(handler, "_colmap_align_owner", "") != _HANDLER_OWNER:
            continue
        logger.removeHandler(handler)
        handler.close()
