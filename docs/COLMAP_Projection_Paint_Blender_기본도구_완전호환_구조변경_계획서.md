# COLMAP Projection Paint Blender 기본 도구 완전 호환 구조변경 계획서

## 1. 문서 목적

이 계획은 Projection Paint가 과거에 소유했던 비파괴 Clone Source·Clone Slot·파생 Layer 구조를 완전히 제거하고, Blender 5.2 Texture Paint의 기본 도구와 Brush 기능을 Blender 권위 그대로 사용할 수 있도록 책임 경계를 다시 정의합니다.

여기서 “완전 호환”은 다음 조건을 뜻합니다.

- Blender 5.2의 Draw, Blur, Smear, Clone, Fill, Mask와 사용자 Brush Asset을 Projection Paint가 백그라운드에서 계속 강제 교체하지 않습니다.
- 사용자가 Blender Toolbar에서 도구를 명시적으로 바꾼 경우의 기능 연결과, 최초 수동 Reconnect의 프로젝트 전용 Draw Brush 생성만 Brush Asset 변경의 예외로 둡니다.
- Projection Paint의 Clone 사용자 경험은 같은 이미지 Native Clone 한 경로로 고정합니다. 활성 Layer Image가 Source이자 Target이며 `Ctrl+LMB`로 Source 위치를 지정합니다.
- Projection Paint는 Clone Mode 진입 시 `use_clone_layer=False`만 명시하고, Clone Source Image·Slot·Offset을 선택·저장·복구하지 않습니다.
- Projection Paint가 관리하는 것은 Paint Target, Layer·Mask Image와 사용자가 명시적으로 연결한 Camera Stencil뿐입니다.
- 복수 Layer 선택 차단처럼 기존에 명시된 Projection Paint 안전 규칙은 유지하되 Clone Source 지정·Navigation·비페인팅 도구 입력은 가로채지 않습니다.

목표 버전은 `0.3.18`이며, 구현 전에 실제 수정 범위와 회귀 기준을 고정하기 위한 문서입니다.

## 2. 용어 분리

### 2.1 제거 대상: Projection Paint 비파괴 Clone

다음은 모두 제거 대상입니다.

- Composite를 자동 Clone Source로 연결하는 기능
- `use_clone_layer=True`를 켜거나 다른 Paint Slot Clone을 구성하는 기능
- `paint_clone_slot`을 Projection Paint가 선택·저장·복구하는 기능
- Clone Source Image와 Offset을 Projection Paint Transaction이 캡처·복구·검증하는 기능
- Composite 이후 Layer를 Clone 의존 Layer로 취급하는 기능
- `COMPOSITE_DERIVED`, dependency UUID, Clone Snapshot, Direct Edit Schema 호환 코드
- 삭제한 기능을 유지하기 위한 Deprecated no-op API와 빈 Tuple 호환 Shim

### 2.2 유지 대상: Blender Native Clone

다음은 Blender 기본 기능이므로 제거하지 않습니다.

- Clone Brush와 Clone Tool
- 같은 Paint Image에서 `Ctrl+LMB`로 Source 위치를 지정하는 기능
- 현재 활성 Layer Image 안에서 사용자가 지정한 Clone Source 위치와 Offset
- Blender Global Undo로 Clone Stroke를 되돌리는 기능

Projection Paint는 이 값들을 다루지 않고, 실제 GUI 회귀에서 Blender가 직접 동작하는지만 검증합니다.

### 2.3 Projection Paint Clone 사용자 경험

Clone은 항상 다음 단일 흐름으로 동작합니다.

1. 사용자가 Clone할 Layer를 하나 선택합니다.
2. 해당 활성 Layer Image가 Source이자 Target이 됩니다.
3. Clone Mode 진입 시 Blender 같은 이미지 Clone 모드인 `use_clone_layer=False`를 선택합니다.
4. 3D Viewport에서 `Ctrl+LMB`로 활성 Layer 내부 Source 위치를 지정합니다.
5. Clone Stroke 결과는 같은 활성 Layer Image에 기록됩니다.

Composite Layer를 선택하면 Composite Image 내부에서 Clone하고, Layer 3을 선택하면 Layer 3 Image 내부에서 Clone합니다. Projection Paint 사용자 경험에서는 Blender `Clone from Paint Slot`과 다른 Layer·Composite를 Source로 사용하는 교차 이미지 Clone을 제공하지 않습니다.

## 3. 확인된 현재 문제

### 3.1 실제 사용자 파일에 구형 Clone 상태가 남음

`C:\Users\gksru\Desktop\Untitled.blend`와 `.blend1`을 0.3.16 설치본으로 저장 없이 검사한 결과는 다음과 같습니다.

- `composite_state=ACTIVE`
- Composite·Source Layer 계층은 유효함
- `use_clone_layer=True`
- `clone_image`가 구형 Composite Image를 가리킴
- `paint_clone_slot`도 같은 Composite Slot을 가리킴
- 구형 Clone Snapshot과 dependency ID Property가 저장 파일에 남음

Composite를 해제하고 다시 만들어도 이 값이 남는 이유는 현재 제품이 Blender 외부 Clone 상태를 보존한다는 계약과 테스트를 유지하기 때문입니다.

### 3.2 제품 코드에 Clone 호환 Shim이 남음

`layer_shader.py`에는 `configure_composite_clone_source()` no-op과 공개 Export가 남아 있습니다. `composite_layer.py`의 `CompositeSession`에도 `dependents`와 `derived` 빈 Tuple 호환 접근이 남아 있습니다. 내부 기능은 비활성화됐지만 삭제한 구조의 공개 표면이 계속 유지되고 있습니다.

### 3.3 Reconnect Transaction이 Blender Clone 상태를 소유함

`paint_runtime.py`의 `_CloneSnapshot`과 Reconnect Snapshot은 다음 Blender Native 값을 읽고 실패 시 다시 씁니다.

- `ImagePaint.clone_image`
- `ImagePaint.use_clone_layer`
- `ImagePaint.clone_offset`
- `Material.paint_clone_slot`
- `Material.paint_active_slot`

성공 검증도 같은 값의 불변성을 직접 검사합니다. Camera Stencil Reconnect와 무관한 Native Clone 상태가 Projection Paint Transaction 경계 안으로 들어온 것이 책임 침범입니다.

### 3.4 Projection Paint가 Blender Brush를 계속 강제 동기화함

`paint_tool_sync.py`와 `paint_runtime.py`의 현재 추적 Timer는 Tool ID뿐 아니라 활성 Brush Pointer와 종류까지 Token에 넣고, 불일치가 보이면 Essentials Brush Asset을 다시 활성화합니다. 이 방식은 사용자가 선택한 호환 사용자 Brush와 세부 설정까지 이후 Tick에서 덮을 수 있으므로 완전 호환 목표와 충돌합니다.

반대로 동기화를 전부 삭제하면 `wm.tool_set_by_id`로 Toolbar Tool만 바뀌고 실제 Brush 기능은 이전 도구에 남는 현재 GUI 회귀가 다시 발생할 수 있습니다. 따라서 필요한 것은 지속적인 강제 동기화가 아니라, 사용자가 Tool ID를 명시적으로 변경한 순간 한 번만 실행되는 최소 연결입니다.

### 3.5 입력 차단 Keymap이 Native 제스처와 충돌할 수 있음

`Image Paint` Keymap의 선두에 등록된 `LEFTMOUSE`, `PEN`, `ERASER` 차단 Operator는 복수 선택 또는 읽기 전용 Original에서 입력을 소비합니다. 현재 Operator는 N 패널 `UI` 영역의 Layer 선택과 3D Viewport `WINDOW` 영역의 Paint Tool 입력을 충분히 구분하지 않으므로 같은 `Ctrl+LMB`를 잘못 소비할 수 있습니다.

### 3.6 자동 Stencil Legacy 정규화가 파일 로드 중 Brush를 변경함

`migrate_stencil_binding_state()`는 Extension 등록·Load·Paint 상태 복구에서 자동 실행되며 저장 Scene과 구형 관리 Brush를 수정합니다. 업데이트마다 자동 이전 코드를 누적하지 않는 현재 정책 및 Blender Tool 무간섭 목표와 맞지 않습니다.

## 4. Blender 공식 동작을 기준으로 한 설계 근거

- Blender 5.2 Brush는 Asset으로 관리되며, Brush Asset 선택이 필요한 Brush Tool 활성화를 Blender 내부에서 처리합니다.
- Texture Paint의 같은 이미지 Clone은 현재 활성 Paint Image를 Source이자 Target으로 사용합니다.
- 3D Texture Paint의 `Ctrl+LMB`로 같은 활성 Image 안의 Source 위치를 지정합니다.
- `clone_image`, `use_clone_layer`, `clone_offset`, `paint_clone_slot`은 Blender의 Image Paint·Material 상태입니다.

참고 문서:

- Blender 5.2 Brush 소개: <https://docs.blender.org/manual/en/latest/sculpt_paint/brush/introduction.html>
- Blender Texture Paint Tools: <https://docs.blender.org/manual/en/latest/sculpt_paint/texture_paint/tools.html>
- Blender Python API `ImagePaint`: <https://docs.blender.org/api/current/bpy.types.ImagePaint.html>
- Blender Python API `Material`: <https://docs.blender.org/api/current/bpy.types.Material.html>
- Blender 5.2 Project Paint Source: <https://github.com/blender/blender/blob/blender-v5.2-release/source/blender/editors/sculpt_paint/mesh/paint_image_proj.cc>

## 5. 최종 권위 경계

| 상태 | 최종 권위 | Projection Paint 허용 동작 |
| --- | --- | --- |
| Workspace Tool | Blender | 조회만 허용, Projection Paint가 Tool ID를 전환하지 않음 |
| 활성 Brush Asset | Blender | Toolbar Tool 전환 직후 기능 불일치 연결과 최초 Reconnect의 전용 Draw Brush 활성화만 허용 |
| Brush 종류·크기·강도·Blend·Stroke | Blender | 외부 Brush Property 직접 변경 금지, 전용 Draw Brush는 복제 시점 설정을 이어받고 이후 Blender가 관리 |
| `clone_image` | Blender | 읽기·쓰기·저장·복구·검증 금지 |
| `use_clone_layer` | Projection Paint Clone Mode 진입 | Clone Tool 전환 시 `False` 1회 설정, `True` 설정·저장·복구 금지 |
| `clone_offset` | Blender | 읽기·쓰기·저장·복구·검증 금지 |
| `paint_clone_slot` | Blender | 읽기·쓰기·저장·복구·검증 금지 |
| `paint_active_slot` | Projection Paint Layer 선택 | 현재 Paint Target Image 선택에만 쓰기 허용 |
| `ImagePaint.canvas` | Projection Paint Layer 선택 | 현재 Paint Target Image 선택에만 쓰기 허용 |
| Layer·Mask Image | Projection Paint | 생성·선택·합성·삭제 허용 |
| Camera Stencil Texture | 명시적 Reconnect 이후 Blender | 최초 Reconnect에서 만든 프로젝트 전용 Draw Brush에 연결하고 이후 재사용 |
| Camera·Frame·Source Image | Projection Paint | COLMAP Camera View와 Stencil Source 갱신 허용 |
| Undo Stack | Blender | Native Paint Stroke는 Blender Undo만 사용 |

## 6. 구조변경 상세

### 6.1 Composite에서 Clone 개념 완전 제거

`layer_shader.py`에서 다음을 삭제합니다.

- `configure_composite_clone_source()`
- `__all__`의 동일 Export
- Clone Source 호환 설명과 관련 테스트

`composite_layer.py`에서 `CompositeSession`을 다음 세 필드만 가진 내부 값으로 단순화합니다.

- `session_uuid`
- `composite`
- `sources`

`dependents`, `derived`, 구형 네 인자 생성자 호환은 삭제합니다. 저장 데이터가 아니라 Python 호출 호환만 위한 Shim이며, Repository 내부 활성 호출이 없으므로 다음 버전에서 제거합니다.

Composite는 다음 규칙만 가집니다.

- 직접 편집 가능한 Paint Layer입니다.
- 생성·재생성·해제는 Layer Graph와 Composite Source Metadata만 다룹니다.
- Composite 작업은 Blender Tool, Brush, Clone Source, Clone Offset과 Clone Slot을 변경하지 않습니다.
- UI의 “Paint·Clone·Mask·Adjustment” 문구에서 Projection Paint가 별도 Clone 결과를 소유한다는 표현을 제거합니다. Native Clone Stroke는 일반 Paint 결과에 포함됩니다.

### 6.2 Reconnect에서 Native Clone 상태 제거

`paint_runtime.py`에서 다음을 삭제합니다.

- `_CloneSnapshot`
- `_capture_clone_snapshot()`
- `_ReconnectSnapshot.clone`
- Clone Image·Flag·Offset·Slot 캡처
- Rollback의 Clone Image·Flag·Offset·Clone Slot 쓰기
- Reconnect 성공 검증의 Clone 상태 비교
- Clone과 무관한 함수·문서화 문자열

Reconnect Snapshot은 Camera, Frame, Viewport와 Projection Paint Source만 보관합니다. 정상 연결 뒤 Stencil 값을 나중에 복구하기 위한 장기 Brush Snapshot은 만들지 않습니다.

`paint_active_slot`은 Clone Snapshot에서 제거하되 Camera Reconnect의 원자성을 버리지는 않습니다. `canvas`, `paint_active_slot`, 활성 Object·Material처럼 Projection Paint가 소유하는 현재 Paint Target은 별도 `_PaintTargetSnapshot`으로 분리해 실패 시에만 대칭 복구합니다. Clone Slot과 Clone Source는 이 Snapshot에 넣지 않습니다.

`overlay_migration.py`의 Image Reference 교체 대상에서도 `ImagePaint.clone_image`를 제거합니다. Overlay 복구가 사용자의 Native Clone Source를 바꾸면 안 됩니다. 다만 교체 전 Overlay Image가 다른 Blender 영역에서 참조 중이면 일반 ID User 수와 Projection Paint 소유권만으로 삭제를 보류하며, Native Clone 값을 읽어 소유권을 판정하지 않습니다.

현재 `repair_managed_overlay_if_needed()`가 Reconnect Snapshot보다 먼저 실행되는 순서도 변경합니다. 먼저 Reconnect Snapshot을 캡처하고 Camera·Brush 단계와 검증을 완료한 뒤, 자체 Rollback을 보장하는 Overlay 복구를 Transaction의 마지막 Commit 단계로 실행합니다. Overlay 복구가 실패하면 앞선 Camera·Brush·Source·Viewport 상태를 Snapshot으로 되돌리고, 복구 성공 뒤에는 실패 가능한 후속 변경을 두지 않습니다.

### 6.3 지속 강제 동기화를 명시적 Tool 전환 연결로 교체

기존 `paint_tool_sync.py`와 Brush Pointer 기반 강제 추적은 삭제하고 `paint_tool_bridge.py`로 책임을 다시 정의합니다.

삭제 대상은 다음과 같습니다.

- `PaintRuntime.paint_tool_brush_token`
- Brush Pointer·Brush Type 변화를 계속 감시하는 `_track_runtime_paint_tool()`
- 매 Tick마다 불일치를 보정하는 `_paint_tool_tracking_tick()`
- Projection Paint 진입·Workspace 복귀만으로 Brush를 바꾸는 경로
- 사용자가 이미 고른 호환 Brush를 Essentials Brush로 교체하는 경로

새 Bridge의 계약은 다음과 같습니다.

- Projection Paint 진입 시 현재 Tool ID를 기준값으로만 기록하고 Brush를 바꾸지 않습니다.
- Projection Paint Workspace·실제 Paint Target 범위에서 Blender Tool ID의 **전환만** 감지합니다.
- Tool ID가 Draw, Blur, Smear, Clone, Fill, Mask 중 하나로 실제 변경됐고 현재 Brush 기능이 그 Tool과 불일치할 때만 대응 Essentials Brush Asset을 한 번 활성화합니다.
- Tool별로 사용자가 마지막으로 선택한 호환 Brush가 있으면 그 Brush를 우선 사용하고, 기록이 없을 때만 Essentials 기본 Asset을 사용합니다.
- Clone Tool로 전환할 때 현재 활성 Layer Image를 Source이자 Target으로 사용하도록 `use_clone_layer=False`를 한 번 설정합니다.
- Projection Paint 작업공간 복귀 시 Clone Tool이 이미 활성 상태여도 `use_clone_layer=False`를 확정하고, 작업공간 이탈로 종료된 Tool Edge Timer를 다시 등록합니다.
- 현재 Brush가 이미 해당 도구와 호환되면 사용자 Asset·크기·강도·Blend·Stroke를 그대로 유지합니다.
- Brush Asset Shelf에서 사용자가 직접 Brush를 선택했지만 Tool ID가 그대로라면 Bridge는 아무 작업도 하지 않습니다.
- 지원 목록 밖의 Blender Tool·사용자 정의 Tool은 읽기만 하고 변경하지 않습니다.
- Bridge는 `use_clone_layer=False` 외에 `clone_image`, `clone_offset`, `paint_clone_slot`, Stencil을 읽거나 쓰지 않습니다.
- Bridge는 Tool ID를 쓰지 않으며 `wm.tool_set_by_id`를 호출하지 않습니다.

Tool ID 변경을 직접 통지하는 안정적인 Blender 콜백이 없으므로, 감지기는 Tool ID 문자열만 저빈도로 비교합니다. 이는 Brush 상태를 계속 강제하는 Timer가 아니라 사용자 Tool 전환을 한 번 연결하기 위한 Edge Detector이며, 동일 ID Tick은 완전한 no-op이어야 합니다. 작업공간 복귀 기준선에서 Clone이 이미 활성인 경우에만 같은 활성 Layer Clone 계약을 확정한 뒤 동일 ID Tick을 no-op으로 처리합니다.

### 6.4 Camera Stencil을 프로젝트 전용 Draw Brush에 연결

Camera Stencil 기능은 유지하되 다음 경계를 적용합니다.

- `Reconnect Stencil Texture`를 사용자가 처음 눌렀을 때 현재 Draw Brush 설정을 복제한 편집 가능한 프로젝트 전용 Draw Brush를 한 번 만듭니다.
- Clone, Blur, Smear, Fill, Mask 또는 사용자 정의 비Draw Tool에서는 상태 변경 없이 Reconnect를 거부합니다.
- 프로젝트 전용 Draw Brush에는 소유권 역할을 기록하고 `ProjectionPaintSettings.stencil_brush`가 같은 Brush를 참조합니다. 이후 Reconnect는 새 Brush를 만들지 않고 이 Brush를 재사용합니다.
- Clone, Blur, Smear, Fill, Mask용 Brush는 복제하지 않으며 Blender 기본 Brush와 사용자가 선택한 Brush를 그대로 사용합니다.
- 외부 Draw Brush 설정을 별도 장기 Snapshot에 저장하거나 Tool 전환·Workspace 전환·저장·종료 시 복구하지 않습니다.
- Camera Texture RGB가 Brush 색상과 곱해져 검게 기록되지 않도록 프로젝트 전용 Draw Brush의 Camera 투영 색상은 흰색으로 맞춥니다. 외부 Draw Brush와 다른 Blender Tool Brush 색상은 변경하지 않습니다.
- 연결된 Stencil Texture, Mapping과 Angle은 프로젝트 전용 Draw Brush의 정상 상태로 유지합니다. Camera View 안에서 이동·확대·영역·Camera Frame이 바뀔 때만 위치·크기를 현재 Camera Frame에 다시 맞춥니다.
- 다른 Tool로 전환해도 프로젝트 전용 Draw Brush에 연결된 Stencil을 제거하지 않습니다. Draw로 돌아오면 저장된 프로젝트 전용 Brush를 우선 활성화해 기존 Stencil과 Brush 설정을 이어서 사용합니다.
- Viewport 회전으로 Camera View를 벗어나면 Source와 Painting Mode를 유지한 채 Stencil Texture 연결만 해제합니다. Camera View로 돌아오면 같은 프로젝트 전용 Draw Brush가 활성인 경우에만 다시 정렬·연결하며 다른 Tool이나 Brush를 강제로 선택하지 않습니다.
- Camera View 이탈 상태가 아닌데 사용자가 Blender에서 Stencil Texture를 바꾸거나 제거하면 그 상태를 존중하며 자동으로 되돌리지 않습니다. 다시 Camera Stencil이 필요할 때만 Reconnect를 누릅니다.
- Save·Save As·자동 저장·파일 재열기에서도 프로젝트 전용 Brush와 연결 Pointer를 Blender 데이터로 저장하고 재사용합니다.
- 현재 Draw Brush가 Essentials 같은 읽기 전용 Asset이어도 최초 Reconnect가 편집 가능한 Local Brush를 생성하므로 사용자가 다른 Brush를 고를 필요가 없습니다.
- 최초 Reconnect 호출 도중 오류가 발생하면 원래 활성 Brush를 복구하고, 해당 호출에서 새로 만든 프로젝트 전용 Brush만 제거합니다. 정상 연결 뒤의 상태는 수명주기 이벤트에서 복구하지 않습니다.

### 6.5 자동 Stencil Legacy 이전 제거

다음을 제품에서 제거합니다.

- `migrate_stencil_binding_state()`
- `STENCIL_BINDING_SCHEMA_VERSION`
- `stencil_follow_active_brush`
- `stencil_binding_schema_version`
- Extension 등록·Load·Paint 상태 복구의 자동 호출
- 구형 관리 Brush를 파일 로드 중 변경하는 테스트

구형 Stencil 상태는 파일을 열었다는 이유만으로 수정하지 않습니다. 명시적 Reconnect는 현재 Blender 상태에서 새 연결을 만들고, 데이터 정리가 필요하면 사용자에게 먼저 알린 뒤 별도 수동 정리를 수행합니다.

### 6.6 N 패널과 3D Viewport 입력 분리

다음 규칙을 적용합니다.

- Layer UI Pointer Tracker는 `PASS_THROUGH` 전용이며 Viewport Tool 입력을 소비하지 않습니다.
- N 패널 `UI` 영역에서 일반 `LMB`는 단일 Layer 선택, `Ctrl/Shift+LMB`는 복수 Layer 선택으로 처리합니다.
- 3D Viewport `WINDOW` 영역에서는 Layer 선택 Operator를 실행하지 않고 Blender Paint·Navigation Keymap에 입력을 전달합니다.
- 복수 선택·읽기 전용 Original의 안전 차단은 **Pixel을 바꿀 수 있는 모든 Paint Stroke**에 적용하며, 수정키 유무만으로 통과시키지 않습니다.
- 활성 Tool이 Clone일 때의 Viewport `Ctrl+LMB`·대응 Tablet 입력은 Native Keymap으로 전달해 현재 활성 Layer Image 안의 Source 위치를 지정합니다.
- Pan·Rotate·Zoom처럼 현재 Paint Tool의 Stroke가 아닌 Blender Navigation 제스처도 Native Keymap으로 전달합니다.
- Draw·Blur·Smear·Fill·Mask의 수정키 입력이라도 Pixel 또는 Mask를 바꿀 수 있으면 복수 선택·읽기 전용 상태에서 차단합니다.
- Projection Paint가 `Image Paint` 선두에 등록한 Alt+RMB Zoom Keymap은 제거해 Blender 기본 Keymap을 우선합니다.
- F7 배경 토글처럼 Brush 입력과 무관한 기능은 기존 Workspace 범위에서 유지합니다.

입력 분류는 `Editor·Region + 현재 Tool ID + 이벤트 종류 + 실제 동작`을 기준으로 합니다. N 패널 복수 선택과 Viewport Clone 입력이 같은 `Ctrl+LMB`를 사용해도 서로 간섭하지 않아야 합니다. 복수 선택 중 같은 이미지 Clone Source 지정은 통과하지만 이후 Clone Stroke는 차단되는지 GUI Pixel Hash로 분리 검사합니다.

### 6.7 사용자 데이터 수동 정리

자동 마이그레이션은 추가하지 않습니다. 제품 코드도 저장 파일의 구형 Clone ID Property를 자동 삭제하지 않습니다.

수동 정리는 Production Extension에 상시 포함하지 않는 별도 유지보수 도구로만 수행합니다. 따라서 제품 코드의 Native Clone RNA 무접근 규칙을 깨지 않습니다.

구현·설치 후 사용자 승인을 받은 별도 수동 작업으로 현재 파일에서 다음 값만 정리합니다.

- 구형 Projection Paint가 연결한 `use_clone_layer`
- 구형 Composite를 가리키는 `clone_image`
- 구형 Composite를 가리키는 `paint_clone_slot`
- 구형 Clone Snapshot ID Property
- 구형 Composite dependency ID Property
- 구형 Stencil Binding ID Property와 과거 버전이 만든 Projection Paint 관리 Local Brush 진단

Clone 값은 대상 Image·Slot이 구형 Projection Paint Composite임을 UUID·소유 태그·실제 Slot Image로 모두 증명한 경우만 정리합니다. 소유권을 증명할 수 없는 외부 Clone 값과 `clone_offset`은 변경하지 않습니다.

정리 전후 다음 값은 동일해야 합니다.

- Layer 수·순서·UUID
- Composite와 Source Session UUID
- 모든 Layer·Mask·Original Image Pointer와 Pixel Hash
- Paint Target Object와 Material
- COLMAP Snapshot과 Camera Marker

정리는 먼저 `.blend` 복사본에서 검증합니다. 실제 사용자 파일 저장은 사용자 승인 없이 수행하지 않습니다.

## 7. 수정 대상

| 파일 | 변경 내용 |
| --- | --- |
| `tools/projection_paint/blender/layer_shader.py` | Clone no-op API와 Export 제거 |
| `tools/projection_paint/blender/composite_layer.py` | `CompositeSession` 호환 Shim 제거, Clone 용어 제거 |
| `tools/projection_paint/blender/paint_runtime.py` | Clone Snapshot·복구·검증 제거, Paint Target Snapshot 분리, Tool ID Edge Detector 연결 |
| `tools/projection_paint/blender/paint_tool_sync.py` | 기존 지속 강제 동기화 모듈 삭제 |
| `tools/projection_paint/blender/paint_tool_bridge.py` | 명시적 Tool ID 전환에만 반응하는 최소 Bridge 신규 작성 |
| `tools/projection_paint/blender/overlay_migration.py` | `clone_image` Reference 교체 제거, 외부 ID User 안전 보존 |
| `tools/projection_paint/blender/stencil_session.py` | 최초 Reconnect의 전용 Draw Brush 생성·재사용과 호출 실패 Rollback, 임대·장기 Snapshot 제거 |
| `tools/projection_paint/blender/state_handlers.py` | Load의 Stencil 자동 이전과 Save 경계의 Brush 조작 제거 |
| `tools/projection_paint/blender/paint_state.py` | Paint 상태 복구의 자동 이전 호출 제거 |
| `tools/projection_paint/blender/properties.py` | Stencil Legacy Schema와 남은 Composite Direct Edit·Clone Snapshot 호환 필드 제거 |
| `tools/projection_paint/blender/lifecycle.py` | 등록 시 자동 이전, Tool 추적과 충돌 Keymap 제거 |
| `tools/projection_paint/blender/layers.py` | 수정키 Native 제스처 통과와 Composite 안내 문구 수정 |
| `tests/projection_paint/blender_background/test_composite_layer.py` | Deprecated Shim 테스트 제거, Composite Tool·Brush 무변경 회귀 추가 |
| `tests/projection_paint/blender_background/test_layer_shader.py` | Clone no-op 테스트 제거, Paint Target만 변경하는 회귀 유지 |
| `tests/projection_paint/blender_background/test_paint_runtime_mask_stencil.py` | Clone Snapshot 테스트 제거, Reconnect 책임 범위 회귀 추가 |
| `tests/projection_paint/blender_background/test_stencil_resources.py` | 자동 이전·임대 테스트 제거, 전용 Draw Brush 생성·재사용·실패 Rollback·저장 재열기 회귀 추가 |
| `tests/projection_paint/blender_background/test_paint_tool_sync.py` | 기존 강제 동기화 회귀 삭제 |
| `tests/projection_paint/blender_background/test_paint_tool_bridge.py` | Tool ID 전환·호환 사용자 Brush 보존·Clone 같은 이미지 모드·미지원 Tool 무간섭 회귀 신규 작성 |
| `tests/projection_paint/blender_background/test_layer_ui.py` | 수정키 제스처 통과와 무수정 Stroke 차단 회귀 추가 |
| `tests/projection_paint/blender_background/test_lifecycle_registry.py` | Tool Timer·충돌 Keymap 미등록 검증 |
| `tests/projection_paint/test_architecture.py` | Clone Tool의 `use_clone_layer=False` 단일 쓰기 Allowlist와 나머지 Native Clone RNA 접근 금지 AST 검사 |
| `tests/projection_paint/gui_composite_clone_actual.py` | Blender Native 같은 이미지 Clone·Source 지정·Undo 회귀로 정리 |
| `tests/projection_paint/gui_stencil_tool_independence.py` | 실제 Essentials Draw에서 전용 Draw Brush 생성 후 Native Tool 교대·재사용 검증 확장 |
| `docs/TESTING.md`·Release Note·구조 문서 | 최종 권위와 검증 절차 기록 |
| `__init__.py`·`blender_manifest.toml` | 모든 회귀 통과 뒤 0.3.18로 일치 |

Modifier, Align, 공용 COLMAP Snapshot 구조는 변경하지 않습니다.

## 8. 정적 금지 규칙

제품 코드에 다음 패턴이 남지 않아야 합니다.

- `configure_composite_clone_source`
- `_CloneSnapshot`
- `paint_tool_sync`
- `paint_tool_brush_token`
- Brush Pointer 기반 지속 강제 동기화
- `COMPOSITE_DERIVED`
- `composite_dependency_session_uuid`
- `ensure_composite_direct_edit_schema`
- `composite_direct_edit_schema_version`
- `_migrate_all_direct_edit_schemas`
- Composite Clone Snapshot 저장 필드
- 동일 Tool ID에서 Brush를 반복 교체하는 `brush.asset_activate`
- Projection Paint 제품 코드의 `wm.tool_set_by_id`

`clone_image`, `clone_offset`, `paint_clone_slot`은 제품 코드에서 접근하지 않습니다. `use_clone_layer`는 Clone Tool 전환 함수에서 `False`로 쓰는 한 곳만 허용하며 읽기·저장·복구·`True` 쓰기는 금지합니다. 테스트 코드는 Blender Native 상태를 검증할 때 읽을 수 있습니다.

`brush.asset_activate` 호출은 `paint_tool_bridge.activate_brush_asset()` 한 곳으로 집중합니다. 이 함수는 Toolbar Tool 전환과 저장된 프로젝트 전용 Draw Brush 활성화가 공통으로 사용합니다. Brush `copy()`는 최초 Reconnect의 프로젝트 전용 Draw Brush 생성 한 곳만 허용합니다.

## 9. 테스트 계획

### 9.1 Architecture

- 제품 코드에 Projection Paint Clone Source·Slot API가 없습니다.
- Brush Pointer 기반 지속 강제 동기화가 없습니다.
- Tool Bridge는 Tool ID가 실제 전환된 한 번만 동작하고 동일 ID Tick에서는 no-op입니다.
- `use_clone_layer=False` 단일 쓰기 외 Native Clone RNA 접근이 없습니다.
- 자동 Stencil Migration 식별자와 호출이 없습니다.
- `brush.asset_activate`는 Tool Bridge의 공통 활성화 함수 한 곳에서만 호출됩니다.
- Brush `copy()`는 최초 Reconnect의 프로젝트 전용 Draw Brush 생성 한 곳에서만 호출됩니다.
- 제품 코드에 `wm.tool_set_by_id` 호출이 없습니다.

검사는 단순 문자열 검색이 아니라 Python AST를 사용합니다. 제품 코드 전체의 Attribute Read·Write를 검사하되 테스트 코드는 제외하고, `paint_active_slot`과 `canvas`는 Layer·Mask Target 전환 및 해당 실패 Rollback 함수만 Allowlist로 둡니다.

### 9.2 Blender Background

- Composite 생성·재생성·해제 전후 Tool ID와 활성 Brush Pointer가 같습니다.
- Composite 작업 전후 Native Clone Image·Flag·Offset·Slot 값이 같습니다.
- Layer·Mask 선택은 `canvas`와 `paint_active_slot`만 대상 Image로 바꿉니다.
- Reconnect 성공·실패가 Native Clone 상태를 읽거나 쓰지 않습니다.
- Reconnect 실패는 동일 호출 안에서 부분 상태를 남기지 않지만, 정상 연결된 Brush 상태에는 장기 복구 Snapshot을 적용하지 않습니다.
- Overlay 복구 뒤 후속 단계 실패 시 Overlay Pointer·Source Metadata도 원래 값으로 복구됩니다.
- Brush Pointer를 추적하거나 동일 Tool ID에서 Brush를 반복 활성화하는 Timer가 없습니다.
- Projection Paint 진입·Workspace 복귀만으로 활성 Brush가 바뀌지 않습니다.
- Toolbar Tool ID 전환 후에만 불일치 Brush가 한 번 연결됩니다.
- Clone Tool 전환 시 `use_clone_layer=False`이고 Source·Target Image Pointer가 현재 활성 Layer Image로 같습니다.
- Clone Tool 전환이 `clone_image`, `clone_offset`, `paint_clone_slot`을 변경하지 않습니다.
- 이미 호환되는 사용자 Brush와 Brush Asset Shelf에서 직접 고른 Brush는 유지됩니다.
- Load·Undo·Redo·Save가 별도 Stencil 해제나 Brush 복구를 실행하지 않습니다.
- 외부 사용자 Brush와 Essentials Brush 원본은 변경되지 않습니다.
- 최초 Reconnect는 프로젝트 전용 Local Draw Brush를 정확히 하나 만들고, 두 번째 호출은 같은 Pointer를 재사용합니다.
- 최초 Reconnect 실패는 원래 활성 Brush를 복구하고 호출 중 만든 전용 Brush만 제거합니다.
- 비Draw Tool 전환 뒤 Draw로 돌아오면 같은 프로젝트 전용 Draw Brush와 Stencil 설정이 유지됩니다.
- Save·Save As·자동 저장과 재열기 뒤에도 같은 프로젝트 전용 Brush·Stencil 상태가 유지됩니다.
- Mask Layer 선택과 Blender Native Mask Tool을 서로 다른 상태로 유지합니다.

### 9.3 실제 GUI Native Tool 행렬

각 도구를 Blender Toolbar와 Brush Asset Shelf에서 직접 선택해 Tool ID뿐 아니라 실제 활성 Brush 기능과 Pixel 결과를 검사합니다.

| 도구 | 필수 검증 |
| --- | --- |
| Draw | Stroke Pixel 변경과 Blender Undo |
| Blur | 선택 Brush 설정 유지와 실제 Blur 결과 |
| Smear | Source 색 이동과 Undo |
| Clone·같은 이미지 | Source·Target Image Pointer 동일, `Ctrl+LMB` 위치 지정, Pixel 복사와 Undo |
| Fill | Native Fill 범위와 Undo |
| Mask | Native Mask Tool 활성과 Projection Paint Mask Layer 상태 분리 |
| 사용자 Brush | Asset·크기·강도·Stroke 설정 무변경 |

Clone은 단일 경로만 검증합니다.

1. 사용자가 편집할 Layer를 하나 선택합니다.
2. Clone Tool로 전환하면 `use_clone_layer=False`가 됩니다.
3. 현재 활성 Layer Image가 Source이자 Target인지 확인합니다.
4. `Ctrl+LMB`로 같은 Image 내부 Source 위치를 지정합니다.
5. Stroke·Undo·Redo가 같은 활성 Layer Image에만 적용되는지 확인합니다.

현재 Layer·Composite 구조에 대한 Target 계약도 검증합니다.

- 모든 편집 가능한 일반 Layer와 Composite Image가 실제 `ShaderNodeTexImage`와 `material.texture_paint_images`에 한 번씩 노출됩니다.
- Layer 3 단일 선택 시 Layer 3 Image가 `paint_active_slot` Target이 됩니다.
- Composite Layer 단일 선택 시 Composite Image가 Source이자 Target이 됩니다.
- Layer 3 단일 선택 시 Layer 3 Image가 Source이자 Target이 됩니다.
- Projection Paint UI와 기본 사용자 흐름에 `Clone from Paint Slot` 선택 단계를 추가하지 않습니다.

각 도구는 실제 Pixel 변화뿐 아니라 Undo·Redo까지 판정합니다. Local·Essentials·Custom Asset, 사용자가 수정한 Brush, 다중 Window와 Workspace별 Tool 상태도 행렬에 포함합니다.

### 9.4 입력 충돌

- N 패널에서 일반 `LMB`는 단일 선택, `Ctrl/Shift+LMB`는 복수 선택으로 동작합니다.
- 같은 입력이라도 3D Viewport에서는 Layer 선택을 바꾸지 않습니다.
- 단일 Layer의 같은 이미지 Clone에서 `Ctrl+LMB`가 Source 위치를 지정합니다.
- 복수 Layer 선택에서도 같은 이미지 Clone `Ctrl+LMB` Source 지정은 Native Tool로 전달됩니다.
- 복수 Layer 선택에서는 Clone Source 지정 뒤의 LMB Stroke가 Pixel을 바꾸지 않습니다.
- 복수 Layer 선택에서 Draw·Blur·Smear·Fill·Mask의 수정키 포함 Stroke도 Pixel이나 Mask를 바꾸지 않습니다.
- Original 읽기 전용 상태에서도 같은 입력 분류를 적용합니다.
- 단일 편집 가능 Layer에서는 각 Native Tool의 수정키 기능이 정상 동작합니다.
- Face Selection의 `Alt+LMB`, `Shift+Alt+LMB`는 통과합니다.
- Primary·Secondary Stencil의 RMB 및 `Shift/Ctrl/Alt+RMB` 기본 제스처는 통과합니다.
- Blender 기본 Zoom·Pan·Rotate와 Tablet 입력이 Projection Paint Keymap에 가로막히지 않습니다.

GUI 입력은 결정적 Plane·UV·비대칭 Pixel Fixture와 `Window.event_simulate`를 사용합니다. `--enable-event-simulate`로 실제 Mouse Press/Release, `Ctrl+LMB/PEN`, `Shift+LMB`, Mask 반전, PEN 압력, Tablet Eraser를 보내며 `clone_offset` 직접 대입이나 Projection Paint 자체 Clone 연산으로 결과를 만들지 않습니다.

### 9.5 사용자 파일 복사본

`Untitled.blend` 복사본에서 다음 순서로 검사합니다.

1. 자동 데이터 변경 없이 파일 열기
2. 구형 Clone 상태를 진단만 수행
3. 승인된 수동 정리 Transaction 실행
4. Layer·Composite·Pixel Hash 불변 확인
5. Layer 3 단일 선택 후 Clone Tool 선택
6. 같은 이미지 모드에서 `Ctrl+LMB` Source 위치 지정과 Clone Stroke·Undo
7. Composite Layer 단일 선택 후 Composite Image 내부 Clone과 Undo
8. Draw·Blur·Smear·Fill·Mask 교대
9. Workspace 왕복
10. 저장·재열기 뒤 동일 결과

## 10. 구현 순서

### 단계 1. 실패 회귀 고정

- 실제 Tool·Brush·Clone·Layer Pixel·UUID 기준선을 먼저 캡처합니다.
- `use_clone_layer=False` 단일 쓰기 외 Native Clone RNA 무접근 AST Architecture 검사를 추가합니다.
- 제품 구현과 분리된 테스트 코드에서 Native Clone 상태를 직접 캡처해 작업 전후 불변을 확인합니다.
- 현재 Tool 추적 Timer와 Clone Snapshot 때문에 실패하는 상태를 확인합니다.

완료 조건: 제거 대상이 테스트에서 명확히 식별돼야 합니다.

### 단계 2. Composite 비파괴 Clone 표면 삭제

- no-op API, 공개 Export, `CompositeSession` 호환 Shim과 관련 테스트를 제거합니다.
- Composite UI·문서에서 별도 Clone 결과 소유 표현을 제거합니다.

완료 조건: Composite 계층에 Clone Source·Target·Dependency 개념이 없어야 합니다.

### 단계 3. 지속 강제 동기화를 Tool 전환 Bridge로 교체

- `paint_tool_sync.py`, Brush Pointer Token, 반복 보정 Timer를 제거합니다.
- Tool ID 전환만 감지하는 `paint_tool_bridge.py`를 추가합니다.
- 진입 Baseline, 동일 ID no-op, 실제 전환 1회 연결, 호환 사용자 Brush 보존, 미지원 Tool 무간섭을 고정합니다.
- Tool별 마지막 호환 Brush를 기억해 Draw로 돌아왔을 때 사용자가 사용하던 동일 Brush를 우선 연결합니다.
- Clone Tool 전환 시 현재 활성 Layer Image를 Source이자 Target으로 사용하도록 `use_clone_layer=False`를 한 번 설정합니다.

완료 조건: Projection Paint는 사용자 Tool 전환이 없으면 Brush를 바꾸지 않고, 사용자가 Blender 기본 Tool을 선택하면 실제 대응 기능이 한 번에 활성화돼야 합니다.

### 단계 4. Reconnect 전용 Draw Brush 경계 확정

- Clone Snapshot·복구·검증을 제거합니다.
- Overlay 복구를 Snapshot 이후 Transaction 안으로 이동합니다.
- Stencil 임대·장기 Snapshot·Tool 전환 복구를 제거하고, 최초 Reconnect에서만 현재 Draw 설정을 복제한 프로젝트 전용 Brush를 생성합니다.
- 이후 Reconnect와 Draw 복귀는 저장된 동일 전용 Brush를 재사용하고, Clone·Blur·Smear·Fill·Mask Brush는 생성하지 않습니다.
- 자동 Stencil Legacy 이전과 필드를 제거합니다.
- Save·Save As·자동 저장에서 Brush를 조작하지 않는 회귀를 추가합니다.

완료 조건: Reconnect는 프로젝트 전용 Draw Brush와 Camera Stencil만 소유하며 Native Clone과 다른 Tool 상태를 소유하지 않아야 합니다.

### 단계 5. 입력·Keymap 호환

- N 패널 `UI` Region의 Layer 선택과 3D Viewport `WINDOW` Region의 Paint 입력을 먼저 분리합니다.
- 현재 Tool별 Source·Navigation 제스처와 Paint Stroke를 분류합니다.
- 충돌하는 Alt+RMB Image Paint Keymap을 제거합니다.
- 복수 선택의 실제 Stroke 차단 계약은 유지합니다.

완료 조건: N 패널 `Ctrl/Shift+LMB` 복수 선택과 Viewport 같은 이미지 Clone `Ctrl+LMB` Source 지정이 독립적으로 동작하고, 복수 선택·읽기 전용의 실제 Paint Stroke만 차단돼야 합니다.

### 단계 6. 전체 회귀와 사용자 데이터 수동 정리

- Background·GUI Tool 행렬과 Architecture를 통과시킵니다.
- `Untitled.blend` 복사본에서 수동 정리의 무손실을 확인합니다.
- 실제 파일 정리는 사용자 승인 뒤에만 수행합니다.

완료 조건: 기존 Layer와 Pixel을 유지하면서 구형 Projection Paint Clone 상태만 제거돼야 합니다.

### 단계 7. 패키징과 설치

- Production 전체 검사와 Small GICP Runtime 검증을 실행합니다.
- 격리 설치본과 Workspace·Camera Marker GUI를 실행합니다.
- `0.3.18` ZIP을 실제 Blender `user_default`에 설치합니다.
- Blender를 재시작한 실제 설치본으로 Native Tool 행렬을 다시 실행합니다.
- 해결된 임시 Probe와 Debug 로그를 삭제합니다.

완료 조건: Source와 실제 설치본 결과가 같아야 합니다.

## 11. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 강제 동기화 제거 뒤 Toolbar Tool과 실제 Brush 기능이 다름 | 사용자 Tool ID 전환 Edge에서만 불일치 Essentials Brush를 한 번 연결 |
| Tool Bridge가 사용자 Brush를 덮음 | 동일 Tool ID·호환 Brush·Shelf 직접 선택은 no-op이며 사용자 설정 Hash를 회귀로 고정 |
| 읽기 전용 Brush에 Stencil을 연결할 수 없음 | 최초 Reconnect에서 현재 Draw 설정을 복제한 프로젝트 전용 편집 가능 Brush를 한 번 생성 |
| 최초 Reconnect 중 실패해 불완전 Brush가 남음 | 원래 활성 Brush를 먼저 복원하고 이번 호출에서 만든 전용 Brush만 제거 |
| Reconnect Rollback 축소로 변경값이 남음 | Overlay를 포함해 Projection Paint가 실제로 쓰는 필드별 대칭 Snapshot·단계별 실패 주입 테스트 추가 |
| 구형 Clone 값이 기존 파일에 계속 남음 | 자동 이전 금지, 진단 후 승인된 수동 정리만 수행 |
| 수정키를 무조건 통과시켜 복수 선택 Paint가 실행됨 | Tool·이벤트별 허용 목록으로 Clone Source와 Navigation만 통과시키고 Pixel Hash로 검증 |
| 구형 `use_clone_layer=True`가 남아 활성 Layer 내부 Clone이 안 됨 | Clone Tool 전환 시 `use_clone_layer=False`를 한 번 설정하고 GUI에서 Source·Target Pointer 동일 검증 |
| 선택 Layer와 실제 Paint Target이 다름 | `ShaderNodeTexImage`·`paint_active_slot`·활성 Layer Image Pointer 일치를 Background 회귀로 검증 |
| Tool 전환 뒤 사용하던 Draw Brush를 잃음 | Tool별 마지막 호환 Brush를 기억해 해당 Tool 복귀 시 우선 사용 |
| Projection Paint가 정상 Stencil 상태를 복구해 사용자 변경을 지움 | 정상 수명주기에는 Snapshot·해제·복구를 두지 않고 Blender 저장 상태를 권위로 사용 |
| 공개 no-op API 제거가 외부 스크립트를 깨뜨림 | Repository 내부 사용 0개를 확인하고 0.3.18 Release Note에 의도적 삭제 명시 |
| 다른 Workspace Tool까지 영향 | Projection Paint Workspace·실제 Paint Target 범위와 다른 Workspace 무변경 회귀 유지 |

## 12. 완료 조건

1. 제품 코드에 Projection Paint 비파괴 Clone Source·Slot·Dependency 기능이 없습니다.
2. `configure_composite_clone_source`, `_CloneSnapshot`, `paint_tool_sync`가 없습니다.
3. 제품 코드는 Clone Tool 전환 시 `use_clone_layer=False`만 한 번 쓰며 `clone_image`, `clone_offset`, `paint_clone_slot`에 접근하지 않습니다.
4. Composite는 일반 편집 가능한 Paint Target이며 Clone 전용 분기가 없습니다.
5. Composite 생성·재생성·해제가 Tool·Brush·Native Clone 상태를 변경하지 않습니다.
6. Projection Paint는 사용자 Tool 전환이 없는 동안 Tool과 Brush Asset을 바꾸지 않습니다.
7. 사용자가 Toolbar에서 Draw·Blur·Smear·Clone·Fill·Mask를 선택하면 실제 대응 Brush 기능이 한 번 활성화됩니다.
8. 호환 사용자 Brush와 Brush Asset Shelf에서 직접 고른 Brush는 Bridge가 교체하지 않습니다.
9. Source와 Target이 같은 Layer Image일 때만 `Ctrl+LMB` Source 위치 지정과 Stroke·Undo가 동작합니다.
10. Projection Paint 사용자 흐름에서 `Clone from Paint Slot`을 사용하거나 다른 Layer·Composite를 Source로 선택하지 않습니다.
11. Camera Stencil은 최초 명시적 Reconnect에서 만든 프로젝트 전용 Draw Brush에 연결됩니다.
12. 다른 Tool로 전환해도 전용 Draw Brush의 Stencil과 사용자 설정을 복구·제거하지 않으며 Draw 복귀 시 같은 Brush를 재사용합니다.
13. Reconnect 성공·실패가 Native Clone 상태를 읽거나 쓰지 않습니다.
14. Brush Pointer 기반 자동 강제 Timer와 자동 Stencil Legacy Migration이 없습니다. Camera View Token 추적은 연결된 같은 Draw Brush의 위치·크기와 Camera View 이탈·복귀만 처리합니다.
15. N 패널 `Ctrl/Shift+LMB`는 Layer 복수 선택, Viewport 같은 이미지 Clone `Ctrl+LMB`는 Source 위치 지정으로 서로 독립 동작합니다.
16. 복수 선택·읽기 전용의 실제 Stroke는 차단되지만 같은 이미지 Clone Source 지정과 Navigation은 통과합니다.
17. Blender 기본 Navigation과 Tablet 입력이 Projection Paint Keymap에 가로막히지 않습니다.
18. 사용자 파일 수동 정리는 Layer·Composite·Mask·Original Pixel과 UUID를 보존합니다.
19. 파일 Load·Undo·Redo·Save·Workspace 왕복이 Tool·Brush·Stencil·Native Clone 상태를 임의 변경하지 않으며, Clone Mode 진입의 `use_clone_layer=False`, Camera View 이동·확대 정렬과 이탈·복귀 연결만 명시적 예외입니다.
20. Modifier와 Align 동작은 변경되지 않습니다.
21. 관련 Architecture·Background·GUI·설치본 회귀가 모두 통과합니다.
22. 0.3.19 Production ZIP이 실제 `user_default`에 설치되고 Blender 재시작 뒤 같은 결과를 냅니다.
23. Stencil 연결 뒤 Tool 왕복과 저장·재열기를 수행해도 동일 프로젝트 전용 Draw Brush와 Stencil 설정이 자연스럽게 이어집니다.

## 13. 최종 사용자 흐름

```text
Projection Painting 진입
→ 기존 Blender Tool과 Brush 그대로 유지

일반 Paint 작업
→ Blender Toolbar에서 Draw·Blur·Smear·Clone·Fill·Mask 선택
→ 실제 기능이 다를 때만 대응 Brush가 한 번 연결
→ 호환 사용자 Brush와 Shelf 선택은 그대로 유지

Clone 작업
→ Clone할 Layer를 하나 선택
→ Blender Clone Tool 선택
→ Clone from Paint Slot을 사용하지 않는 같은 이미지 모드로 진입
→ 활성 Layer Image가 Source이자 Target
→ Ctrl+LMB로 활성 Layer 내부 Source 위치 지정
→ Clone Stroke는 같은 활성 Layer Image에 기록

Camera Stencil 작업
→ Blender Draw Brush 선택
→ Reconnect Stencil Texture 클릭
→ 최초 1회 현재 Draw 설정을 복제한 프로젝트 전용 Draw Brush 생성
→ 전용 Draw Brush를 흰색으로 맞춰 Camera Texture 원색 투영
→ Camera Stencil 연결, 이동·확대 시 Camera Frame을 따라 위치·크기 갱신
→ Camera View 이탈 시 Texture 연결만 해제하고 복귀 시 같은 Draw Brush만 재연결
→ 이후 Reconnect와 저장·재열기에서 같은 Brush 재사용

다른 Tool 전환
→ Clone·Blur·Smear·Fill·Mask는 Blender 기본 Brush 그대로 사용
→ 프로젝트 전용 Draw Brush와 Stencil 상태는 그대로 유지
→ 새 Tool ID는 Projection Paint가 바꾸지 않음
→ 실제 기능이 불일치할 때만 대응 Brush Asset을 한 번 연결
```

이 구조의 핵심은 Projection Paint가 Paint 데이터의 Target만 관리하고, Paint 방법과 도구의 모든 기능은 Blender가 관리하는 것입니다.
