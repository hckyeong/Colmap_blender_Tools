"""Extension 등록과 Blender 수명주기 중단 장벽을 한곳에서 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from importlib import import_module
from typing import Any, Callable, Iterable

import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty

from .ownership import OWNER_TOKEN, SCENE_UUID_KEY


_REGISTRY_NAMESPACE_KEY = "projection_paint.lifecycle_registry"
_CALLBACK_OWNER_ATTRIBUTE = "_projection_paint_owner"
_CALLBACK_KEY_ATTRIBUTE = "_projection_paint_callback_key"


@dataclass(frozen=True, slots=True)
class EpochToken:
    """지연 작업 Commit에 필요한 수명주기 식별값을 보관합니다."""

    epoch: int
    scene_pointer: int
    scene_uuid: str
    dataset_revision: int
    request_serial: int


@dataclass(frozen=True, slots=True)
class SessionOwner:
    """전역 단일 Session을 소유한 Window와 Scene 범위를 식별합니다."""

    epoch: int
    window_pointer: int
    scene_pointer: int
    scene_uuid: str
    view_layer_name: str


@dataclass(frozen=True, slots=True)
class CallbackFailure:
    """격리된 Callback 예외의 최초 진단 정보를 보관합니다."""

    component: str
    exception_type: str
    message: str
    epoch: int


@dataclass(slots=True)
class _HandlerRecord:
    """등록된 Handler 목록과 실제 Wrapper Identity를 보관합니다."""

    handler_list: list
    callback: Callable[..., Any]
    wrapper: Callable[..., Any]


@dataclass(slots=True)
class _TimerRecord:
    """등록된 Timer 원본과 실제 Wrapper Identity를 보관합니다."""

    callback: Callable[[], float | None]
    wrapper: Callable[[], float | None]
    component: str
    epoch: int


def _pointer(value: Any) -> int:
    """유효한 RNA 객체의 현재 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError):
        return 0


def _scene_uuid(scene: Any) -> str:
    """Scene을 변경하지 않고 저장된 식별자만 읽습니다."""

    try:
        return str(scene.get(SCENE_UUID_KEY, "") or "") if scene else ""
    except (AttributeError, ReferenceError, RuntimeError):
        return ""


def _callback_key(callback: Callable[..., Any]) -> str:
    """Reload 뒤에도 비교 가능한 Callback의 모듈 경로를 반환합니다."""

    return f"{callback.__module__}.{callback.__qualname__}"


class LifecycleRegistry:
    """Class, Handler, Timer, Epoch와 단일 Session 소유권을 관리합니다."""

    def __init__(self) -> None:
        """비활성 상태의 빈 Registry를 생성합니다."""

        self.epoch = 0
        self.active = False
        self.suspended = True
        self.session_state = "IDLE"
        self.session_owner: SessionOwner | None = None
        self._classes: list[type] = []
        self._handlers: list[_HandlerRecord] = []
        self._timers: dict[Callable[..., Any], _TimerRecord] = {}
        self._barrier_hooks: list[Callable[[str, int], None]] = []
        self._rebuild_hooks: list[Callable[[Any, str, int], None]] = []
        self._failures: dict[tuple[str, str, str], CallbackFailure] = {}
        self._properties: list[tuple[Any, str]] = []
        self._menu_callbacks: list[tuple[Any, Callable[..., Any]]] = []
        self._keymap_items: list[tuple[Any, Any]] = []
        self._unregister_after_native_bake = False
        self._unregister_timer_registered = False
        self._unregister_timer_callback: Callable[[], float | None] | None = None

    @property
    def failures(self) -> tuple[CallbackFailure, ...]:
        """중복 제거된 Callback 오류를 불변 목록으로 반환합니다."""

        return tuple(self._failures.values())

    def activate(self) -> None:
        """Registry를 활성화하고 새 Epoch에서 쓰기를 허용합니다."""

        self.active = True
        self.suspended = False
        self.epoch += 1
        self.session_state = "IDLE"

    def register_class(self, cls: type) -> None:
        """Blender Class를 중복 없이 등록하고 해제 순서를 기억합니다."""

        if cls in self._classes:
            return
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            existing = getattr(bpy.types, cls.__name__, None)
            if existing is not cls:
                raise
            return
        self._classes.append(cls)

    def register_classes(self, classes: Iterable[type]) -> None:
        """여러 Blender Class를 선언 순서대로 등록합니다."""

        for cls in classes:
            self.register_class(cls)

    def attach_property(self, owner: Any, name: str, value: Any) -> None:
        """동적 RNA Property를 등록하고 안전한 제거 대상을 기억합니다."""

        if hasattr(owner, name):
            return
        setattr(owner, name, value)
        self._properties.append((owner, str(name)))

    def append_menu_callback(
        self,
        menu_type: Any,
        callback: Callable[..., Any],
    ) -> None:
        """Blender Menu Callback을 중복 없이 추가하고 해제 대상을 기억합니다."""
        record = (menu_type, callback)
        if record in self._menu_callbacks:
            return
        draw_functions = tuple(
            getattr(getattr(menu_type, "draw", None), "_draw_funcs", ())
        )
        if callback not in draw_functions:
            menu_type.append(callback)
        self._menu_callbacks.append(record)

    def register_keymap_item(
        self,
        keymap_name: str,
        operator_idname: str,
        event_type: str,
        *,
        space_type: str = "EMPTY",
        region_type: str = "WINDOW",
        alt: bool = False,
        any_modifier: bool = False,
        head: bool = False,
    ) -> None:
        """애드온 단축키를 우선순위·보조키 및 보조키 자체의 입력 조건과 함께 등록합니다."""
        keyconfig = getattr(bpy.context.window_manager.keyconfigs, "addon", None)
        if keyconfig is None:
            return
        keymap = keyconfig.keymaps.new(
            name=str(keymap_name),
            space_type=str(space_type),
            region_type=str(region_type),
        )
        for candidate in keymap.keymap_items:
            if (
                candidate.idname == str(operator_idname)
                and candidate.type == str(event_type)
                and candidate.value == "PRESS"
                and bool(candidate.alt) is bool(alt)
                and bool(candidate.any) is bool(any_modifier)
            ):
                return
        item = keymap.keymap_items.new(
            str(operator_idname),
            str(event_type),
            "PRESS",
            alt=bool(alt),
            any=bool(any_modifier),
            head=bool(head),
        )
        self._keymap_items.append((keymap, item))

    def add_barrier_hook(self, callback: Callable[[str, int], None]) -> None:
        """수명주기 중단 때 Runtime을 비우는 Hook을 중복 없이 추가합니다."""

        if callback not in self._barrier_hooks:
            self._barrier_hooks.append(callback)

    def remove_barrier_hook(self, callback: Callable[[str, int], None]) -> None:
        """등록된 수명주기 중단 Hook을 제거합니다."""

        if callback in self._barrier_hooks:
            self._barrier_hooks.remove(callback)

    def add_rebuild_hook(self, callback: Callable[[Any, str, int], None]) -> None:
        """수명주기 복구 때 Scene Manifest를 재구축할 Hook을 추가합니다."""

        if callback not in self._rebuild_hooks:
            self._rebuild_hooks.append(callback)

    def remove_rebuild_hook(self, callback: Callable[[Any, str, int], None]) -> None:
        """등록된 Scene Runtime 재구축 Hook을 제거합니다."""

        if callback in self._rebuild_hooks:
            self._rebuild_hooks.remove(callback)

    def fail_component(self, component: str, exc: Exception) -> None:
        """예상하지 못한 오류를 한 번 기록하고 해당 Timer를 중지합니다."""

        key = (str(component), type(exc).__name__, str(exc))
        self._failures.setdefault(
            key,
            CallbackFailure(
                component=str(component),
                exception_type=type(exc).__name__,
                message=str(exc),
                epoch=self.epoch,
            ),
        )
        self.session_state = "ERROR"
        for callback, record in tuple(self._timers.items()):
            if record.component == component:
                self.unregister_timer(callback)

    def call_safely(
        self,
        component: str,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any | None:
        """Callback 예외를 Blender Event Loop 밖으로 전파하지 않습니다."""

        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            self.fail_component(component, exc)
            return None

    def register_handler(
        self,
        handler_list: list,
        callback: Callable[..., Any],
        *,
        component: str,
        keep_after_load: bool,
    ) -> Callable[..., Any]:
        """예외 격리 Wrapper로 Handler를 정확히 한 번 등록합니다."""

        for record in self._handlers:
            if record.handler_list is handler_list and record.callback is callback:
                if record.wrapper not in handler_list:
                    handler_list.append(record.wrapper)
                return record.wrapper
        callback_key = _callback_key(callback)
        for candidate in tuple(handler_list):
            if (
                getattr(candidate, _CALLBACK_OWNER_ATTRIBUTE, "") == OWNER_TOKEN
                and getattr(candidate, _CALLBACK_KEY_ATTRIBUTE, "") == callback_key
            ):
                handler_list.remove(candidate)

        @wraps(callback)
        def guarded_handler(*args: Any, **kwargs: Any) -> None:
            """Blender Handler 예외를 격리하고 실패 상태를 기록합니다."""

            self.call_safely(component, callback, *args, **kwargs)

        setattr(guarded_handler, _CALLBACK_OWNER_ATTRIBUTE, OWNER_TOKEN)
        setattr(guarded_handler, _CALLBACK_KEY_ATTRIBUTE, callback_key)
        wrapper = persistent(guarded_handler) if keep_after_load else guarded_handler
        handler_list.append(wrapper)
        self._handlers.append(_HandlerRecord(handler_list, callback, wrapper))
        return wrapper

    def unregister_handler(
        self,
        handler_list: list,
        callback: Callable[..., Any],
    ) -> None:
        """원본 Callback과 일치하는 등록 Handler를 제거합니다."""

        for record in tuple(self._handlers):
            if record.handler_list is not handler_list or record.callback is not callback:
                continue
            if record.wrapper in handler_list:
                handler_list.remove(record.wrapper)
            self._handlers.remove(record)

    def register_timer(
        self,
        callback: Callable[[], float | None],
        *,
        component: str,
        first_interval: float = 0.0,
        persistent_timer: bool = False,
    ) -> Callable[[], float | None]:
        """Epoch 검증과 자체 해제를 포함한 Timer를 중복 없이 등록합니다."""

        existing = self._timers.get(callback)
        if existing is not None and bpy.app.timers.is_registered(existing.wrapper):
            return existing.wrapper
        captured_epoch = self.epoch

        @wraps(callback)
        def guarded_timer() -> float | None:
            """오래되거나 실패한 Timer를 재등록하지 않고 종료합니다."""

            record = self._timers.get(callback)
            if (
                record is None
                or captured_epoch != self.epoch
                or self.suspended
                or not self.active
            ):
                self._timers.pop(callback, None)
                return None
            try:
                delay = callback()
            except Exception as exc:
                self._timers.pop(callback, None)
                self.fail_component(component, exc)
                return None
            if delay is None:
                self._timers.pop(callback, None)
                return None
            return max(0.0, float(delay))

        record = _TimerRecord(callback, guarded_timer, str(component), captured_epoch)
        self._timers[callback] = record
        bpy.app.timers.register(
            guarded_timer,
            first_interval=max(0.0, float(first_interval)),
            persistent=bool(persistent_timer),
        )
        return guarded_timer

    def unregister_timer(self, callback: Callable[[], float | None]) -> None:
        """원본 Callback에 대응하는 Timer를 안전하게 해제합니다."""

        record = self._timers.pop(callback, None)
        if record is None:
            return
        if bpy.app.timers.is_registered(record.wrapper):
            bpy.app.timers.unregister(record.wrapper)

    def cancel_timers(self) -> None:
        """현재 Registry가 소유한 모든 Timer를 해제합니다."""

        for callback in tuple(self._timers):
            self.unregister_timer(callback)

    def barrier(self, reason: str) -> int:
        """Load·Undo·Redo 전에 Epoch와 모든 Runtime 작업을 무효화합니다."""

        self.session_state = "SUSPENDING"
        self.suspended = True
        self.epoch += 1
        self.session_owner = None
        self.cancel_timers()
        for callback in tuple(self._barrier_hooks):
            self.call_safely("lifecycle_barrier", callback, str(reason), self.epoch)
        self.session_state = "IDLE"
        return self.epoch

    def rebuild(self, scene: Any, reason: str) -> None:
        """새 Blender 상태를 권위 Manifest 기준으로 재구축합니다."""

        self.suspended = False
        self._failures.clear()
        for callback in tuple(self._rebuild_hooks):
            self.call_safely("lifecycle_rebuild", callback, scene, str(reason), self.epoch)

    def capture_token(
        self,
        scene: Any,
        *,
        dataset_revision: int = 0,
        request_serial: int = 0,
    ) -> EpochToken:
        """현재 지연 작업의 Epoch와 Scene 식별값을 캡처합니다."""

        return EpochToken(
            epoch=self.epoch,
            scene_pointer=_pointer(scene),
            scene_uuid=_scene_uuid(scene),
            dataset_revision=int(dataset_revision),
            request_serial=int(request_serial),
        )

    def token_is_current(
        self,
        token: EpochToken,
        scene: Any,
        *,
        dataset_revision: int | None = None,
        request_serial: int | None = None,
    ) -> bool:
        """Commit Token이 현재 Epoch와 Scene 계약에 일치하는지 확인합니다."""

        if not self.active or self.suspended or token.epoch != self.epoch:
            return False
        if token.scene_pointer != _pointer(scene):
            return False
        if token.scene_uuid and token.scene_uuid != _scene_uuid(scene):
            return False
        if dataset_revision is not None and token.dataset_revision != int(dataset_revision):
            return False
        if request_serial is not None and token.request_serial != int(request_serial):
            return False
        return True

    def claim_session(self, window: Any, scene: Any, view_layer: Any) -> bool:
        """전역 단일 Session의 Window·Scene 소유권을 원자적으로 획득합니다."""

        candidate = SessionOwner(
            epoch=self.epoch,
            window_pointer=_pointer(window),
            scene_pointer=_pointer(scene),
            scene_uuid=_scene_uuid(scene),
            view_layer_name=str(getattr(view_layer, "name", "") or ""),
        )
        if not candidate.window_pointer or not candidate.scene_pointer:
            return False
        if self.session_owner is None:
            self.session_owner = candidate
            self.session_state = "ENTERING"
            return True
        return self.session_owner == candidate

    def session_is_owned_by(self, window: Any, scene: Any, view_layer: Any) -> bool:
        """현재 Context가 전역 Session 소유 범위와 일치하는지 확인합니다."""

        owner = self.session_owner
        if owner is None or owner.epoch != self.epoch:
            return False
        return bool(
            owner.window_pointer == _pointer(window)
            and owner.scene_pointer == _pointer(scene)
            and owner.scene_uuid == _scene_uuid(scene)
            and owner.view_layer_name == str(getattr(view_layer, "name", "") or "")
        )

    def release_session(self) -> None:
        """전역 Session 소유권과 상태를 IDLE로 되돌립니다."""

        self.session_owner = None
        self.session_state = "IDLE"

    def request_unregistration_after_native_bake(self) -> None:
        """활성 Native Bake 또는 Texture Export Modal이 끝난 뒤 Extension 해제를 예약합니다."""

        self._unregister_after_native_bake = True

    def cancel_pending_unregistration(self) -> None:
        """사용자가 다시 Extension을 활성화하면 예약된 해제를 취소합니다."""

        self._unregister_after_native_bake = False
        callback = self._unregister_timer_callback
        self._unregister_timer_callback = None
        self._unregister_timer_registered = False
        if callback is not None and bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)

    def schedule_pending_unregistration(self) -> None:
        """Modal이 반환된 다음 이벤트 틱에 예약된 Extension 해제를 실행합니다."""

        if (
            not self._unregister_after_native_bake
            or self._unregister_timer_registered
        ):
            return
        self._unregister_timer_registered = True

        def finish_after_modal() -> float | None:
            """Native Job과 Texture Export Modal이 모두 끝난 뒤 실제 Extension 해제를 실행합니다."""

            self._unregister_timer_registered = False
            self._unregister_timer_callback = None
            if not self._unregister_after_native_bake:
                return None
            from .composite_layer import composite_native_bake_pending

            if composite_native_bake_pending():
                self.schedule_pending_unregistration()
                return None
            from .texture_export import texture_export_running

            if texture_export_running():
                self.schedule_pending_unregistration()
                return None
            self._unregister_after_native_bake = False
            unregister()
            return None

        self._unregister_timer_callback = finish_after_modal
        bpy.app.timers.register(finish_after_modal, first_interval=0.0)

    def unregister_all(self, reason: str = "unregister") -> None:
        """등록 자원을 안전한 역순으로 전부 해제합니다."""

        self.barrier(reason)
        for record in reversed(tuple(self._handlers)):
            if record.wrapper in record.handler_list:
                record.handler_list.remove(record.wrapper)
        self._handlers.clear()
        for owner, name in reversed(self._properties):
            if hasattr(owner, name):
                delattr(owner, name)
        self._properties.clear()
        for menu_type, callback in reversed(self._menu_callbacks):
            try:
                menu_type.remove(callback)
            except RuntimeError:
                pass
        self._menu_callbacks.clear()
        for keymap, item in reversed(self._keymap_items):
            try:
                keymap.keymap_items.remove(item)
            except (ReferenceError, RuntimeError):
                pass
        self._keymap_items.clear()
        for cls in reversed(self._classes):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        self._classes.clear()
        self._barrier_hooks.clear()
        self._rebuild_hooks.clear()
        self.cancel_pending_unregistration()
        self.active = False
        self.suspended = True


def _clear_previous_registry() -> None:
    """모듈 Reload 전에 남은 이전 Registry의 Callback을 해제합니다."""

    previous = bpy.app.driver_namespace.get(_REGISTRY_NAMESPACE_KEY)
    if previous is None:
        return
    try:
        previous.unregister_all("module_reload")
    except Exception:
        pass


_clear_previous_registry()
LIFECYCLE = LifecycleRegistry()
bpy.app.driver_namespace[_REGISTRY_NAMESPACE_KEY] = LIFECYCLE


def lifecycle_registry() -> LifecycleRegistry:
    """현재 Extension의 단일 Lifecycle Registry를 반환합니다."""

    return LIFECYCLE


def _module_classes(module_name: str) -> tuple[type, ...]:
    """모듈이 공개한 Blender Class 목록을 불변 Tuple로 반환합니다."""

    module = import_module(module_name, package=__package__)
    return tuple(getattr(module, "CLASSES", ()))


def _rebuild_layer_ui_event_tracker(_scene: Any, _reason: str, _epoch: int) -> None:
    """등록·Load·Undo·Redo rebuild 뒤 Layer UI 입력 추적 Timer를 다시 arm합니다."""
    from .layers import layer_ui_event_tracker_heartbeat

    LIFECYCLE.register_timer(
        layer_ui_event_tracker_heartbeat,
        component="layer_ui_event_tracker",
        first_interval=0.1,
    )


def _rebuild_projection_marker_session(scene: Any, _reason: str, _epoch: int) -> None:
    """활성 작업공간의 카메라 권한을 복구하되 실행 취소·다시 실행 시 현재 뷰를 유지합니다."""

    if scene is None:
        return
    from .camera_scene import (
        activate_managed_frame,
        activate_projection_camera_session,
        attach_projection_markers,
        detach_projection_markers,
        invalidate_projection_camera_session,
        runtime_for_scene,
    )
    from .paint_workspace import PAINT_WORKSPACE_SPEC
    try:
        from ....shared.blender_runtime.scene_marker_session import (
            acquire_scene_marker_session,
            release_scene_marker_session,
            scene_marker_session_owned_by,
        )
        from ....shared.blender_runtime.workspace_factory import workspace_matches_spec
    except ImportError:
        from shared.blender_runtime.scene_marker_session import (
            acquire_scene_marker_session,
            release_scene_marker_session,
            scene_marker_session_owned_by,
        )
        from shared.blender_runtime.workspace_factory import workspace_matches_spec

    window_manager = getattr(bpy.context, "window_manager", None)
    active_window = next(
        (
            window
            for window in tuple(getattr(window_manager, "windows", ()))
            if getattr(window, "scene", None) is scene
            and workspace_matches_spec(
                getattr(window, "workspace", None),
                PAINT_WORKSPACE_SPEC,
                require_ready=False,
            )
        ),
        None,
    )
    if active_window is not None:
        from .snapshot_update import snapshot_is_ready
        from .dataset_view_layer import activate_projection_dataset_view_layer

        activate_projection_dataset_view_layer(scene, active_window)
        acquire_scene_marker_session(scene, "projection_paint", window=active_window)
        camera_token = activate_projection_camera_session(scene, active_window)
        if snapshot_is_ready(scene):
            attach_projection_markers(scene)
        runtime = runtime_for_scene(scene)
        if (
            runtime is not None
            and runtime.camera_session_token == camera_token
            and int(scene.frame_current) in runtime.entries_by_frame
        ):
            activate_managed_frame(
                scene,
                int(scene.frame_current),
                refresh_source=False,
                enter_camera_view=_reason not in {"undo_post", "redo_post"},
            )
        return
    invalidate_projection_camera_session(scene)
    detach_projection_markers(scene)
    if scene_marker_session_owned_by(scene, "projection_paint"):
        release_scene_marker_session(scene, "projection_paint")


def _paint_handoff_start(context) -> None:
    """Projection Paint의 기존 Workspace 구성 계약을 공용 전환 완료 뒤 실행합니다."""

    from .paint_workspace import open_paint_workspace

    open_paint_workspace(context)


def _paint_handoff_is_engaged(window) -> bool:
    """지정 Window와 Scene에 실제 Paint 또는 Marker 세션이 남아 있는지 반환합니다."""

    try:
        from ....shared.blender_runtime.scene_marker_session import (
            scene_marker_session_owned_by,
        )
    except ImportError:
        from shared.blender_runtime.scene_marker_session import (
            scene_marker_session_owned_by,
        )

    scene = getattr(window, "scene", None)
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return False
    return bool(
        scene_marker_session_owned_by(scene, "projection_paint")
        or bool(getattr(settings, "paint_enabled", False))
        or str(getattr(settings, "paint_state", "IDLE"))
        in {"ENTERING", "ACTIVE", "EXITING"}
    )


def _paint_handoff_request_exit(context, _window) -> None:
    """Paint 준비·활성 세션을 종료하고 전용 Workspace의 재사용 상태를 보존합니다."""

    from .paint_workspace import request_paint_workspace_exit

    request_paint_workspace_exit(context)


def _paint_handoff_is_settled(window) -> bool:
    """Paint 상태 복구와 Source Workspace 복귀가 모두 완료됐는지 반환합니다."""

    from .paint_workspace import paint_workspace_exit_settled

    scene = getattr(window, "scene", None)
    return bool(scene is not None and paint_workspace_exit_settled(scene, window))


def _paint_handoff_report_error(message: str) -> None:
    """비동기 전환 실패 원인을 Projection Paint Scene 상태에 기록합니다."""

    settings = getattr(getattr(bpy.context, "scene", None), "projection_paint", None)
    if settings is not None:
        settings.status = str(message)
        settings.status_kind = "ERROR"
        settings.paint_state = "ERROR"


def _register_paint_handoff_adapter() -> None:
    """Projection Paint의 독립 생명주기 계약을 공용 조정기에 등록합니다."""

    try:
        from ....shared.blender_runtime.workspace_handoff import (
            WorkspaceHandoffAdapter,
            register_workspace_handoff_adapter,
        )
    except ImportError:
        from shared.blender_runtime.workspace_handoff import (
            WorkspaceHandoffAdapter,
            register_workspace_handoff_adapter,
        )

    register_workspace_handoff_adapter(
        WorkspaceHandoffAdapter(
            tool_id="projection_paint",
            label="Projection Paint",
            start=_paint_handoff_start,
            is_engaged=_paint_handoff_is_engaged,
            request_exit=_paint_handoff_request_exit,
            is_settled=_paint_handoff_is_settled,
            report_error=_paint_handoff_report_error,
        )
    )


def _unregister_paint_handoff_adapter() -> None:
    """Projection Paint의 공용 전환 계약을 등록 해제합니다."""

    try:
        from ....shared.blender_runtime.workspace_handoff import (
            unregister_workspace_handoff_adapter,
        )
    except ImportError:
        from shared.blender_runtime.workspace_handoff import (
            unregister_workspace_handoff_adapter,
        )

    unregister_workspace_handoff_adapter("projection_paint")


def _detach_projection_marker_sessions_on_unregister() -> None:
    """Extension 해제 전에 모든 Scene의 검증된 Projection Marker와 자기 lease를 정리합니다."""

    from .camera_scene import (
        detach_projection_markers,
        invalidate_projection_camera_session,
    )
    try:
        from ....shared.blender_runtime.scene_marker_session import (
            release_scene_marker_session,
            scene_marker_session_owned_by,
        )
    except ImportError:
        from shared.blender_runtime.scene_marker_session import (
            release_scene_marker_session,
            scene_marker_session_owned_by,
        )

    failures: list[Exception] = []
    for scene in tuple(bpy.data.scenes):
        try:
            invalidate_projection_camera_session(scene)
            detach_projection_markers(scene)
            if scene_marker_session_owned_by(scene, "projection_paint"):
                release_scene_marker_session(scene, "projection_paint")
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise RuntimeError(f"Projection Marker 해제 실패: {failures[0]}") from failures[0]


def register() -> None:
    """Extension Class, Property와 수명주기 Handler를 최종 순서로 등록합니다."""

    if LIFECYCLE.active:
        LIFECYCLE.cancel_pending_unregistration()
        return
    bpy.app.driver_namespace[_REGISTRY_NAMESPACE_KEY] = LIFECYCLE
    LIFECYCLE.activate()
    try:
        from .properties import PP_PG_settings

        class_modules = (
            ".properties",
            ".camera_import",
            ".model_import",
            ".camera_review",
            ".viewport_navigation",
            ".paint_session",
            ".frame_source",
            ".stencil_session",
            ".stencil_erase",
            ".overlay_durability",
            ".layers",
            ".texture_export",
            ".panels",
        )
        for module_name in class_modules:
            LIFECYCLE.register_classes(_module_classes(module_name))
        LIFECYCLE.attach_property(
            bpy.types.Scene,
            "projection_paint",
            PointerProperty(type=PP_PG_settings),
        )
        from .camera_import import set_import_request_factory
        from .core_adapter import create_camera_import_request_job

        set_import_request_factory(create_camera_import_request_job)
        from .layers import draw_layer_list_context_menu

        LIFECYCLE.append_menu_callback(
            bpy.types.UI_MT_button_context_menu,
            draw_layer_list_context_menu,
        )
        LIFECYCLE.register_keymap_item(
            "Window",
            "projection_paint.layer_adjustment_remove_hovered",
            "DEL",
        )
        LIFECYCLE.register_keymap_item(
            "3D View",
            "projection_paint.review_background_toggle",
            "F7",
            space_type="VIEW_3D",
            region_type="WINDOW",
        )
        LIFECYCLE.register_keymap_item(
            "3D View",
            "projection_paint.reconnect_stencil",
            "F6",
            space_type="VIEW_3D",
            region_type="WINDOW",
            head=True,
        )
        from .stencil_erase import clear_stencil_erase

        for keymap_name in ("3D View", "Image Paint"):
            for control_key in ("LEFT_CTRL", "RIGHT_CTRL"):
                LIFECYCLE.register_keymap_item(
                    keymap_name, "projection_paint.hold_stencil_erase", control_key,
                    space_type="VIEW_3D" if keymap_name == "3D View" else "EMPTY",
                    region_type="WINDOW", any_modifier=True, head=True,
                )
        LIFECYCLE.add_barrier_hook(clear_stencil_erase)
        LIFECYCLE.register_handler(
            bpy.app.handlers.save_pre, clear_stencil_erase,
            component="stencil_erase_save", keep_after_load=True,
        )
        industry_navigation = (
            ("projection_paint.industry_orbit", "LEFTMOUSE"),
            ("projection_paint.industry_pan", "MIDDLEMOUSE"),
            ("projection_paint.industry_zoom", "RIGHTMOUSE"),
        )
        for keymap_name in ("3D View", "Image Paint"):
            for operator_idname, event_type in industry_navigation:
                LIFECYCLE.register_keymap_item(
                    keymap_name,
                    operator_idname,
                    event_type,
                    space_type="VIEW_3D" if keymap_name == "3D View" else "EMPTY",
                    region_type="WINDOW",
                    alt=True,
                    head=True,
                )
        for stroke_event in ("LEFTMOUSE", "PEN", "ERASER"):
            LIFECYCLE.register_keymap_item(
                "Image Paint",
                "projection_paint.block_multiselect_paint_stroke",
                stroke_event,
                region_type="WINDOW",
                head=True,
            )
        from .state_handlers import register_state_handlers
        from .layers import (
            clear_layer_ui_runtime_state,
            enable_layer_ui_event_tracking,
        )
        from .paint_workspace import (
            pause_paint_workspace_cleanup,
            rebuild_paint_workspace_cleanup,
        )
        from .texture_export import clear_texture_export_jobs

        register_state_handlers()
        LIFECYCLE.add_rebuild_hook(_rebuild_projection_marker_session)
        LIFECYCLE.add_barrier_hook(clear_texture_export_jobs)
        LIFECYCLE.add_barrier_hook(pause_paint_workspace_cleanup)
        enable_layer_ui_event_tracking()
        LIFECYCLE.add_barrier_hook(clear_layer_ui_runtime_state)
        LIFECYCLE.add_rebuild_hook(_rebuild_layer_ui_event_tracker)
        LIFECYCLE.add_rebuild_hook(rebuild_paint_workspace_cleanup)
        LIFECYCLE.rebuild(getattr(bpy.context, "scene", None), "register")
        _register_paint_handoff_adapter()
    except Exception:
        from .camera_import import set_import_request_factory

        set_import_request_factory(None)
        _unregister_paint_handoff_adapter()
        LIFECYCLE.unregister_all("register_failure")
        raise


def _unregister_now() -> None:
    """Native Bake가 없는 안전한 시점에 Extension 자원을 역순으로 해제합니다."""

    marker_cleanup_error: Exception | None = None
    try:
        _detach_projection_marker_sessions_on_unregister()
    except Exception as exc:
        marker_cleanup_error = exc
    try:
        _unregister_paint_handoff_adapter()
        from .layers import disable_layer_ui_event_tracking
        from .state_handlers import unregister_state_handlers

        disable_layer_ui_event_tracking()
        unregister_state_handlers()
    finally:
        from .camera_import import set_import_request_factory

        set_import_request_factory(None)
        LIFECYCLE.unregister_all("unregister")
        if bpy.app.driver_namespace.get(_REGISTRY_NAMESPACE_KEY) is LIFECYCLE:
            del bpy.app.driver_namespace[_REGISTRY_NAMESPACE_KEY]
    if marker_cleanup_error is not None:
        raise marker_cleanup_error


def unregister() -> None:
    """Native Bake나 Texture Export 중에는 Modal 정리 뒤로 Extension 해제를 미룹니다."""

    if not LIFECYCLE.active and not LIFECYCLE.failures:
        return
    try:
        from .composite_layer import (
            clear_composite_create_jobs,
            composite_native_bake_pending,
        )

        clear_composite_create_jobs()
        if composite_native_bake_pending():
            LIFECYCLE.request_unregistration_after_native_bake()
            return
    except (ImportError, AttributeError):
        pass
    try:
        from .texture_export import clear_texture_export_jobs, texture_export_running

        clear_texture_export_jobs()
        if texture_export_running():
            LIFECYCLE.request_unregistration_after_native_bake()
            return
    except (ImportError, AttributeError):
        pass
    _unregister_now()
