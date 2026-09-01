"""Modifier와 Projection Painting 위의 중립 공용 COLMAP Dataset 계층입니다."""

from .contracts import (
    DATASET_OWNER_TOKEN,
    DATASET_SCHEMA_VERSION,
    DatasetImportMetadata,
    DatasetSnapshot,
)


def register() -> None:
    """Blender 환경에서 공용 Dataset Scene Property를 등록합니다."""

    from .blender.lifecycle import register as register_impl

    register_impl()


def unregister() -> None:
    """Blender 환경에서 공용 Dataset Scene Property를 해제합니다."""

    from .blender.lifecycle import unregister as unregister_impl

    unregister_impl()


def is_registered() -> bool:
    """Blender 공용 Dataset 계층의 등록 완료 여부를 반환합니다."""

    from .blender.lifecycle import is_registered as is_registered_impl

    return is_registered_impl()


__all__ = [
    "DATASET_OWNER_TOKEN",
    "DATASET_SCHEMA_VERSION",
    "DatasetImportMetadata",
    "DatasetSnapshot",
    "is_registered",
    "register",
    "unregister",
]
