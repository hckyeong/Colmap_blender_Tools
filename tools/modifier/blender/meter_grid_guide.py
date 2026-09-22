"""데이터셋과 독립된 Modifier 월드 미터 눈금 및 단위 구독을 관리합니다."""

from uuid import uuid4

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix


GUIDE_OWNER_KEY = "colmap_modifier_meter_owner"
GUIDE_OWNER_TOKEN = "modifier_meter_guide"
GUIDE_UUID_KEY = "colmap_modifier_meter_uuid"
GUIDE_SCHEMA_KEY = "colmap_modifier_meter_schema"
GUIDE_SCHEMA_VERSION = 1
GUIDE_ROLE_KEY = "colmap_modifier_meter_role"
GUIDE_COLLECTION_NAME = "COLMAP Modifier Meter Guide"
_SUBSCRIPTION_OWNER = object()
_REGISTERED = False
_RESTORE_VISIBILITY = None


def is_meter_guide(value):
    """이름과 무관하게 현행 미터 눈금 전용 소유권을 확인합니다."""
    return bool(
        value is not None
        and value.get(GUIDE_OWNER_KEY) == GUIDE_OWNER_TOKEN
        and value.get(GUIDE_SCHEMA_KEY) == GUIDE_SCHEMA_VERSION
        and value.get(GUIDE_UUID_KEY)
    )


def _mark(value, guide_uuid, role):
    """눈금 전용 소유권과 스키마를 데이터블록에 기록합니다."""
    value[GUIDE_OWNER_KEY] = GUIDE_OWNER_TOKEN
    value[GUIDE_UUID_KEY] = guide_uuid
    value[GUIDE_SCHEMA_KEY] = GUIDE_SCHEMA_VERSION
    value[GUIDE_ROLE_KEY] = role


def iter_meter_guide_collections(scene):
    """Scene 최상위에 연결된 눈금 전용 컬렉션을 반환합니다."""
    return tuple(c for c in scene.collection.children if is_meter_guide(c) and c.get(GUIDE_ROLE_KEY) == "COLLECTION")


def meter_in_blender_units(scene):
    """Scene 단위를 변경하지 않고 1미터에 대응하는 Blender 길이를 반환합니다."""
    units = scene.unit_settings
    return 1.0 if units.system == "NONE" else 1.0 / units.scale_length


def _ensure_object(collection, role, object_type):
    """지정 역할의 소유 오브젝트를 재사용하거나 누락된 표시 자원을 생성합니다."""
    guide_uuid = collection[GUIDE_UUID_KEY]
    matches = tuple(
        obj for obj in collection.objects
        if is_meter_guide(obj) and obj.get(GUIDE_UUID_KEY) == guide_uuid
        and obj.get(GUIDE_ROLE_KEY) == role
    )
    if len(matches) > 1:
        raise RuntimeError("Modifier 미터 눈금 역할이 중복되었습니다.")
    if matches:
        obj = matches[0]
        if (obj.type != object_type or not is_meter_guide(obj.data)
                or obj.data.get(GUIDE_UUID_KEY) != guide_uuid
                or obj.data.get(GUIDE_ROLE_KEY) != role):
            raise RuntimeError("Modifier 미터 눈금 데이터의 소유권 또는 종류가 손상되었습니다.")
    else:
        data = next((curve for curve in bpy.data.curves
                     if is_meter_guide(curve) and curve.users == 0
                     and curve.get(GUIDE_UUID_KEY) == guide_uuid
                     and curve.get(GUIDE_ROLE_KEY) == role
                     and isinstance(curve, bpy.types.TextCurve) == (object_type == "FONT")), None)
        if data is None:
            data = bpy.data.curves.new(f"Meter {role}", object_type)
        _mark(data, guide_uuid, role)
        obj = bpy.data.objects.new(f"Meter {role}", data)
        _mark(obj, guide_uuid, role)
        collection.objects.link(obj)
    obj.parent = None
    obj.matrix_world = Matrix.Identity(4)
    obj.hide_select = True
    obj.hide_render = True
    obj.hide_viewport = False
    obj.show_in_front = True
    obj.lock_location = (True,) * 3
    obj.lock_rotation = (True,) * 3
    obj.lock_scale = (True,) * 3
    return obj


def ensure_meter_grid_guide(scene):
    """원점의 양의 X·Y축에 20개 글자와 한 개 눈금 Curve를 보장합니다."""
    collections = iter_meter_guide_collections(scene)
    if len(collections) > 1:
        raise RuntimeError("Modifier 미터 눈금 컬렉션이 중복되었습니다.")
    if collections:
        collection = collections[0]
        if any(other != scene and collection in tuple(other.collection.children) for other in bpy.data.scenes):
            scene.collection.children.unlink(collection)
            collection = bpy.data.collections.new(GUIDE_COLLECTION_NAME)
            _mark(collection, str(uuid4()), "COLLECTION")
            scene.collection.children.link(collection)
    else:
        collection = bpy.data.collections.new(GUIDE_COLLECTION_NAME)
        _mark(collection, str(uuid4()), "COLLECTION")
        scene.collection.children.link(collection)
    collection.hide_select = True
    collection.hide_render = True
    collection.hide_viewport = False
    unit = meter_in_blender_units(scene)
    for axis in ("X", "Y"):
        for index in range(1, 11):
            obj = _ensure_object(collection, f"{axis}_{index}", "FONT")
            obj.data.body = f"{index} m"
            obj.data.size = 0.15 * unit
            obj.location = (
                (index * unit, 0.15 * unit, 0.01 * unit)
                if axis == "X" else (0.15 * unit, index * unit, 0.01 * unit)
            )
    obj = _ensure_object(collection, "LINES", "CURVE")
    curve = obj.data
    curve.dimensions = "3D"
    curve.bevel_depth = 0.004 * unit
    curve.bevel_resolution = 0
    curve.splines.clear()
    segments = [((0, 0), (10, 0)), ((0, 0), (0, 10))]
    for index in range(1, 11):
        segments.extend((((index, -0.08), (index, 0.08)), ((-0.08, index), (0.08, index))))
    for start, end in segments:
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        for point, (x, y) in zip(spline.points, (start, end)):
            point.co = (x * unit, y * unit, 0.01 * unit, 1.0)
    return collection


def _units_changed(scene):
    """단위 변경을 알린 Scene의 기존 눈금만 갱신합니다."""
    if iter_meter_guide_collections(scene):
        ensure_meter_grid_guide(scene)


def subscribe_meter_grid_units():
    """새 Scene 눈금 진입이 완료되면 해당 단위 알림 구독을 갱신합니다."""
    if _REGISTERED:
        _subscribe_units()


def _subscribe_units():
    """눈금이 존재하는 Scene의 단위 속성에만 알림을 연결합니다."""
    bpy.msgbus.clear_by_owner(_SUBSCRIPTION_OWNER)
    for scene in bpy.data.scenes:
        if not iter_meter_guide_collections(scene):
            continue
        for property_name in ("system", "scale_length"):
            bpy.msgbus.subscribe_rna(
                key=scene.unit_settings.path_resolve(property_name, False),
                owner=_SUBSCRIPTION_OWNER,
                args=(scene,), notify=_units_changed,
            )


@persistent
def restore_meter_grid_guides(_unused=None):
    """로드·Undo·Redo 뒤 기존 Modifier Scene의 눈금과 가시성 구독을 복구합니다."""
    if _RESTORE_VISIBILITY is not None:
        _RESTORE_VISIBILITY()
    _subscribe_units()


def register_meter_grid_guide(restore_visibility):
    """주입된 가시성 복구 함수와 단위 변경 알림을 중복 없이 등록합니다."""
    global _REGISTERED, _RESTORE_VISIBILITY
    _RESTORE_VISIBILITY = restore_visibility
    _REGISTERED = True
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if restore_meter_grid_guides not in handlers:
            handlers.append(restore_meter_grid_guides)
    if not bpy.app.timers.is_registered(_restore_after_registration):
        bpy.app.timers.register(_restore_after_registration, first_interval=0.0)


def _restore_after_registration():
    """확장 등록의 제한된 Context가 끝난 뒤 저장된 눈금을 복구합니다."""
    if _REGISTERED:
        restore_meter_grid_guides()
    return None


def unregister_meter_grid_guide():
    """구독과 핸들러를 해제하고 확인된 눈금 소유 자원만 제거합니다."""
    global _REGISTERED, _RESTORE_VISIBILITY
    _REGISTERED = False
    _RESTORE_VISIBILITY = None
    if bpy.app.timers.is_registered(_restore_after_registration):
        bpy.app.timers.unregister(_restore_after_registration)
    bpy.msgbus.clear_by_owner(_SUBSCRIPTION_OWNER)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if restore_meter_grid_guides in handlers:
            handlers.remove(restore_meter_grid_guides)
    if not hasattr(bpy.data, "objects"):
        return
    for obj in tuple(bpy.data.objects):
        if is_meter_guide(obj):
            bpy.data.objects.remove(obj, do_unlink=True)
    for curve in tuple(bpy.data.curves):
        if is_meter_guide(curve) and curve.users == 0:
            bpy.data.curves.remove(curve)
    for collection in tuple(bpy.data.collections):
        if is_meter_guide(collection) and not collection.objects and not collection.children:
            bpy.data.collections.remove(collection)
