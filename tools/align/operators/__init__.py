"""COLMAP Align 1차 기술검증 오퍼레이터 모음입니다."""

from .axis_settings import CLASSES as AXIS_SETTINGS_CLASSES
from .export_model import CLASSES as EXPORT_MODEL_CLASSES
from .export_obj import CLASSES as EXPORT_OBJ_CLASSES
from .import_model import CLASSES as IMPORT_MODEL_CLASSES
from .import_mesh import CLASSES as IMPORT_MESH_CLASSES
from .layer_transform import CLASSES as LAYER_TRANSFORM_CLASSES
from .marker_alignment import CLASSES as MARKER_ALIGNMENT_CLASSES
from .marker_pick import CLASSES as MARKER_CLASSES
from .model_group import CLASSES as MODEL_GROUP_CLASSES
from .project_restore import CLASSES as PROJECT_RESTORE_CLASSES
from .session_guard import CLASSES as SESSION_GUARD_CLASSES
from .view_framing import CLASSES as VIEW_FRAMING_CLASSES
from .workspace import CLASSES as WORKSPACE_CLASSES

CLASSES = (
    WORKSPACE_CLASSES
    + MODEL_GROUP_CLASSES
    + AXIS_SETTINGS_CLASSES
    + IMPORT_MODEL_CLASSES
    + IMPORT_MESH_CLASSES
    + LAYER_TRANSFORM_CLASSES
    + EXPORT_MODEL_CLASSES
    + EXPORT_OBJ_CLASSES
    + MARKER_ALIGNMENT_CLASSES
    + MARKER_CLASSES
    + PROJECT_RESTORE_CLASSES
    + SESSION_GUARD_CLASSES
    + VIEW_FRAMING_CLASSES
)
