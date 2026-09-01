# COLMAP Projection Paint Stencil 수동 재연결·Blender 도구 독립 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-08-31
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.3.12`
- 구현 버전: `0.3.13`
- 대상 기능: `Projection Paint` Camera Stencil과 Texture Paint Brush 관리
- 문서 상태: 구현·자동 회귀·실데이터 GUI 회귀 완료
- 재현 파일: `C:\Users\gksru\Desktop\Untitled.blend`
- 제외 도구: `COLMAP Modifier`, `COLMAP Align`

### 구현 결과

`0.3.13`에서 계획한 수동 재연결·도구 독립 구조를 구현했습니다. 대상 회귀 153개, Production 빌드 검사 220개, 격리 설치본과 실제 `user_default` 설치본 검증을 통과했습니다. 사용자 `Untitled.blend`의 `IMAGE_ID 593`에서 Draw Reconnect, Clone Tool·Clone Brush 유지, Draw 복귀 시 자동 재연결 없음과 명시적 Reconnect만 최종 연결되는 것을 저장 없이 확인했습니다. 패키지는 `build/colmap_tools-0.3.13.zip`, SHA-256은 `C6253BF13F453A97E6B1C56744B5009DB24D681F3B81FFDA84992A366475594C`입니다.

## 2. 쉬운 결론

Projection Paint는 Blender의 도구와 Brush를 자동으로 바꾸지 않는다.

Camera Stencil을 새 Brush에 연결할 수 있는 동작은 사용자가 직접 누르는 `Reconnect Stencil Texture` 하나로 제한한다. 내부 Timer, Paint 진입, Camera View 복귀, Layer 복귀, Undo·Redo와 파일 Load는 기존 연결만 갱신할 수 있고 새 Brush를 채택할 수 없다.

```text
Clone·Smear·Blur·Fill·Mask 선택
→ Projection Paint가 새 Brush를 복사하거나 활성화하지 않음
→ Blender Tool과 Brush 종류 그대로 유지
→ 이전 Camera Stencil 연결만 안전하게 해제

Camera Stencil Paint로 복귀
→ 사용자가 Draw Brush 선택
→ Reconnect Stencil Texture 클릭
→ 선택한 Draw Brush에만 Camera Stencil 연결
```

## 3. 확인된 현재 문제

실제 사용자 프로젝트에서 다음 불일치가 확인됐다.

```text
Workspace Tool ID: builtin_brush.clone
실제 활성 Brush: Airbrush [Stencil]
실제 Brush 종류: DRAW
```

화면에서는 Clone이 선택됐지만 실제 Stroke는 Draw Brush가 처리하므로 Clone이 실행되지 않는다.

일반 Layer에서도 다음 상태가 확인됐다.

```text
활성 Brush: Mask [Stencil]
Brush 종류: MASK
Blender 기본 Stencil Mask: 활성
Blender 기본 Stencil Mask Image: 없음
```

이 경우 COLMAP Camera Stencil은 정상이어도 Blender 기본 Mask 도구가 `Missing Stencil, detected!`를 표시할 수 있다.

## 4. 원인

현재 `stencil_session.py`는 활성 Brush가 바뀌면 새 Brush를 자동으로 Camera Stencil Brush로 승계한다.

```text
refresh_stencil() 또는 reconnect_stencil_texture()
└─ 활성 Brush != settings.stencil_brush
   └─ _switch_active_brush()
      ├─ 이전 Brush 복구
      ├─ 새 Brush의 편집 가능한 [Stencil] 복사본 생성
      ├─ 복사본 활성화
      └─ Camera Texture 연결
```

이 기능은 일반 Paint Brush를 바꿔도 Camera Stencil을 유지하려고 도입됐다. 그러나 일반 Brush 선택과 Clone·Smear·Mask 같은 Blender 도구 전환을 구분하지 않아 Tool과 실제 Brush 종류가 달라진다.

자동 승계는 Viewport Timer 외에도 다음 경로에 존재한다.

- Paint 진입 완료
- Camera View 이탈·복귀
- Workspace 이탈·복귀
- 일반 Layer 복귀
- Frame Source 갱신
- Undo·Redo와 파일 Load Reconcile
- 최근접 Camera 재연결
- `Project Stencil to Target`

따라서 `_switch_active_brush()` 한 곳만 제거해서는 문제가 해결되지 않는다.

## 5. 확정 설계 원칙

1. Blender Tool과 활성 Brush의 선택권은 사용자와 Blender에 둔다.
2. 내부 Callback과 Timer는 새 Brush를 연결 대상으로 채택할 수 없다.
3. 새 Brush 채택 권한은 `Reconnect Stencil Texture` Operator에만 둔다.
4. Reconnect는 `DRAW` Brush에서만 허용한다.
5. Clone, Smear, Blur, Fill과 Blender 기본 Mask Tool에는 Camera Stencil을 자동 또는 수동으로 연결하지 않는다.
6. 같은 연결 Brush에서 Camera와 Frame이 변할 때만 기존 Stencil을 갱신한다.
7. Texture Paint의 활성 상태와 Camera Stencil 연결 상태를 분리한다.
8. 연결이 끊겨도 Layer·Mask 편집과 `paint_state=ACTIVE`는 유지한다.
9. Brush 상태 변경은 Capture와 Restore가 완전히 대칭이어야 한다.
10. 기존 Composite Clone Source와 Clone Slot 계약은 변경하지 않는다.

## 6. 제외 범위

- Composite 생성·재합성 범위 변경
- Blender 기본 Clone 알고리듬 변경
- Composite Clone의 임의 위치 오프셋 지원
- Projection Paint 전용 Clone Session 또는 Tool Lease 재도입
- Layer, Mask, Adjustment와 Composite Shader 변경
- 별도 Undo Stack 추가
- Modifier와 Projection Paint Dataset 구조 변경
- Align, OBJ Import·Export와 Small GICP 변경

## 7. Stencil 상태 모델 분리

`paint_state`는 Texture Paint 작업 가능 상태만 나타낸다.

```text
paint_state
├─ IDLE
├─ ENTERING
├─ ACTIVE
├─ EXITING
└─ ERROR
```

Camera Stencil 연결은 별도 Runtime 상태로 판정한다.

```text
StencilConnectionState
├─ DISCONNECTED          연결 Brush 없음
├─ CONNECTED             현재 활성 Brush에 Camera Texture 연결
├─ CAMERA_SUSPENDED      Camera View 이탈로 일시 해제
├─ MASK_SUSPENDED        PP Layer Mask 편집으로 일시 해제
├─ BRUSH_CHANGED         사용자가 다른 Brush·Tool로 전환
├─ MANUAL_DISABLED       사용자가 관리 Texture를 직접 해제
└─ ERROR                 연결 또는 복구 Transaction 실패
```

이 상태는 대형 영구 데이터가 아니다. `PaintRuntime`의 Runtime 값과 현재 Blender Pointer를 사용하는 단일 판정 함수로 계산한다. 저장이 필요한 값은 기존 Source Image와 Stencil 배치뿐이다.

상태 우선순위는 다음과 같이 고정한다.

1. 유효한 Paint Target·Runtime 없음 → `DISCONNECTED`
2. PP Layer Mask 활성 → `MASK_SUSPENDED`
3. 연결 Brush 없음 → `DISCONNECTED`
4. 활성 Brush와 연결 Brush가 다름 → `BRUSH_CHANGED`
5. 사용자 수동 해제 → `MANUAL_DISABLED`
6. Camera View 아님 → `CAMERA_SUSPENDED`
7. Texture·Mapping·Source 계약 일치 → `CONNECTED`
8. 그 외 → `ERROR`

`stencil_outside_camera_view` 하나로 Mask, Workspace, Brush 변경과 Camera View 이탈을 모두 표현하지 않는다.

## 8. 연결 권한 API 분리

### 8.1 Resource 준비

새 함수는 관리 Texture, Source 식별자와 저장된 배치만 준비한다.

```text
prepare_stencil_resources(context, scene)
├─ 관리 Texture 준비
├─ Source Metadata 복원
├─ 저장된 위치·크기·회전 준비
└─ 활성 Brush 변경 없음
```

Paint 진입과 Load Reconcile은 이 함수까지만 사용할 수 있다.

### 8.2 사용자 명시적 연결

새 Brush를 연결 대상으로 채택할 수 있는 유일한 경계다.

```text
bind_active_draw_brush_explicitly(context)
├─ PP Painting ACTIVE 검증
├─ 일반 Layer 검증
├─ Camera View·Source 검증
├─ Workspace Tool과 Brush 종류가 DRAW인지 검증
├─ 전체 Tool·Brush Snapshot
├─ 필요할 때만 편집 가능한 Brush 복사
├─ Camera Texture와 STENCIL Mapping 연결
├─ Camera Frame 정렬
├─ Tool·Brush 불변 검증
└─ 실패 시 전체 Rollback
```

이 함수는 `PP_OT_reconnect_stencil` Operator만 호출한다.

### 8.3 기존 연결 갱신

내부 Runtime과 Callback은 새 Brush를 채택하지 않는 함수만 사용한다.

```text
refresh_existing_stencil_binding(context)
├─ 연결 Brush Pointer 존재 확인
├─ 현재 활성 Brush와 동일한지 확인
├─ 동일하면 Source·위치·크기·회전만 갱신
└─ 다르면 BRUSH_CHANGED로 전환하고 새 Brush 불변
```

### 8.4 기존 연결 해제

```text
release_existing_stencil_binding(context, reason)
├─ 이전 연결 Brush의 원래 상태 복구
├─ 관리 Texture 연결 해제
├─ 활성 Brush가 다르면 현재 Tool·Brush 불변
├─ 비활성 관리 임시 Brush만 안전 정리
└─ 연결 해제 이유 기록
```

## 9. Paint 진입 계약

현재 Paint 진입은 `start_stencil_session()`과 `reconnect_current_frame()`을 연속 호출해 Brush를 자동 복사·연결한다. 이를 다음과 같이 변경한다.

```text
Painting Mode 진입
→ Texture Paint Target·Layer·Image 활성화
→ Paint Runtime 설치
→ Camera Source Metadata 준비
→ 관리 Stencil Texture 준비
→ 활성 Tool·Brush는 변경하지 않음
→ Stencil은 DISCONNECTED 상태
→ 사용자가 필요할 때 Reconnect 실행
```

- `paint_session.py`의 `_complete_paint_entry()`에서 새 Brush 채택과 자동 Reconnect를 제거한다.
- `start_stencil_session()`을 Resource 준비와 Brush 연결로 분리한다.
- 활성 Brush가 없어도 Paint 진입 자체는 실패시키지 않는다.
- Camera Stencil을 사용하려면 사용자가 Draw Brush를 선택한 뒤 Reconnect를 실행한다.
- 기존 파일에 같은 유효 연결 Brush가 이미 있고 모든 Pointer가 일치하는 경우에만 기존 연결 갱신을 허용한다. 새 Brush 복사·활성화는 금지한다.

## 10. 자동 호출 경로 변경

### 10.1 Camera View·Frame·Workspace

- 같은 연결 Brush의 Camera View 이탈은 `CAMERA_SUSPENDED`로 전환한다.
- 같은 Brush로 Camera View에 복귀하면 기존 연결만 복구한다.
- 다른 Brush가 활성인 경우 `BRUSH_CHANGED`를 유지하고 재연결하지 않는다.
- Frame 변경은 Source Metadata를 갱신하지만 다른 Brush를 연결하지 않는다.
- Workspace 복귀도 같은 연결 Brush일 때만 기존 연결을 복구한다.

### 10.2 일반 Layer·Mask

- PP Layer Mask 선택 시 `MASK_SUSPENDED`로 전환한다.
- 일반 Layer 복귀 시 연결 Brush가 그대로면 기존 연결만 복구한다.
- Mask 편집 중 Brush나 Tool이 바뀌었으면 일반 Layer 복귀 후에도 `BRUSH_CHANGED`로 남긴다.
- Blender 기본 `image_brush_type=MASK`와 `ImagePaint.use_stencil_layer`는 Projection Paint가 켜거나 끄지 않는다.
- 일반 Layer에서 Blender 기본 Mask Tool이 선택돼 있으면 Reconnect를 거부하고 Draw Brush 선택을 안내한다.

### 10.3 Undo·Redo·Load

- `state_handlers.py`의 Reconcile은 Runtime과 Resource만 복구한다.
- 동일 연결 Brush이면 기존 연결 검증과 추적만 다시 등록한다.
- 다른 Brush이거나 Brush Pointer가 없으면 `DISCONNECTED` 또는 `BRUSH_CHANGED`로 둔다.
- Undo·Redo와 파일 Load가 Asset Brush를 활성화하거나 `[Stencil]` Brush를 만들지 않는다.
- 모든 관련 Scene의 Legacy 자동 추적 값을 정규화한다.

### 10.4 Project Stencil

- `Project Stencil to Target`은 새 Brush 연결 권한을 갖지 않는다.
- 기존 연결이 `CONNECTED`가 아니면 `먼저 Draw Brush에서 Reconnect Stencil Texture를 실행하십시오.`라고 안내한다.
- Project 실행 중 Brush와 Tool을 변경하지 않는다.

### 10.5 최근접 Camera Reconnect

N 패널의 `Reconnect Stencil Texture`가 최근접 Camera 선택까지 수행하는 현재 UX는 유지한다.

- Operator 진입 시에만 새 Draw Brush 채택을 허용한다.
- Camera·Frame 선택과 Brush 연결을 하나의 Transaction으로 묶는다.
- 내부 `reconnect_current_frame()`은 이름과 책임을 분리하고 새 Brush 채택을 금지한다.

## 11. Reconnect Transaction

Reconnect 전에 다음 상태를 모두 Snapshot으로 캡처한다.

### 11.1 Workspace Tool

- `WorkSpaceTool.idname`
- `WorkSpaceTool.idname_fallback`
- `WorkSpaceTool.brush_type`
- Paint Mode

### 11.2 활성 Brush와 Asset

- 활성 Brush Pointer와 이름
- `Brush.image_brush_type`
- Asset Library Type·Identifier·Relative Identifier
- 편집 가능 여부와 관리 소유권

### 11.3 Projection Paint가 변경하는 Brush 값

- `brush.texture`
- `brush.texture_slot.map_mode`
- `brush.texture_slot.angle`
- `brush.color`
- `brush.stencil_pos`
- `brush.stencil_dimension`
- Unified Color 사용 여부

### 11.4 Camera·Stencil·Clone 불변값

- Scene Camera, Frame과 IMAGE_ID
- Viewport Perspective·Camera·Transform
- Stencil Texture와 Source Image
- `ImagePaint.clone_image`
- `ImagePaint.use_clone_layer`
- `ImagePaint.clone_offset`
- `Material.paint_active_slot`
- `Material.paint_clone_slot`

Reconnect는 Clone 설정을 변경하지 않는다. Clone 값은 Rollback과 불변 검증 대상으로만 사용한다.

성공 조건은 다음과 같다.

1. Tool ID·Fallback·Brush Type이 Reconnect 전과 같다.
2. 실제 `Brush.image_brush_type`이 `DRAW`로 유지된다.
3. 관리 Texture와 `STENCIL` Mapping이 현재 Draw Brush에 연결된다.
4. Camera·Frame과 Source IMAGE_ID가 일치한다.
5. Clone Source, Offset과 Paint Slot 값이 바뀌지 않는다.

어느 단계든 실패하면 Tool, Asset Brush, Brush 값, Camera, Frame, Viewport와 Source를 모두 복구한다. Rollback 자체가 실패하면 관리 Pointer와 소유권을 성급히 삭제하지 않고 재시도 가능한 `ERROR` 상태로 유지한다.

## 12. Brush 종료·정리 계약

- 현재 활성 Brush가 연결 관리 Brush와 같을 때만 원래 Asset 재활성화를 시도한다.
- 사용자가 Clone 등 다른 Brush로 전환한 뒤에는 현재 활성 Tool과 Brush를 건드리지 않는다.
- 이전 연결 Brush의 Texture, Mapping, Angle, Color와 Stencil 배치만 복구한다.
- 관리 임시 Brush가 비활성이고 User가 없을 때만 제거한다.
- 원래 Asset 재활성화에 실패하면 임시 Brush를 강제 삭제하지 않는다.
- Workspace 이탈, Paint 종료와 Extension 해제에 같은 계약을 적용한다.

## 13. Legacy Migration

기존 파일에는 다음 값이 저장됐을 수 있다.

- `stencil_follow_active_brush=True`
- `stencil_owns_brush=True`
- `Mask [Stencil]`, `Airbrush [Stencil]` 같은 임시 Brush
- Toolbar Tool과 다른 `image_brush_type`
- 불완전한 원래 Brush Snapshot

Migration은 별도 `STENCIL_BINDING_SCHEMA_VERSION`으로 관리한다. 기존 Paint State Schema가 최신이어도 Stencil Migration은 실행할 수 있어야 한다.

```text
Load Migration
→ 모든 PP Scene의 stencil_follow_active_brush=False 정규화
→ Camera Source와 저장된 Stencil 배치 보존
→ 활성 Legacy Brush의 Texture 연결만 안전 해제
→ Paint Hard 또는 다른 Asset 자동 활성화 금지
→ 비활성 관리 Brush만 안전 정리
→ 원본 Snapshot 불완전 시 삭제하지 않고 경고
→ 새 Brush 연결 없이 DISCONNECTED로 완료
```

`stencil_follow_active_brush` Property는 한 Migration 기간 동안 읽기 호환용으로 유지하되 기본값을 `False`로 바꾸고 동작 제어에는 사용하지 않는다.

## 14. Composite Clone 제약 분리

현재 Composite Clone은 다음 구조다.

```text
ImagePaint.use_clone_layer = True
Material.paint_clone_slot = Composite Image Slot
```

이 경로는 Composite Image의 동일 Clone UV 위치를 Target Layer로 가져온다. Blender의 Ctrl+클릭 `clone_offset`은 `use_clone_layer=False`인 동일 Paint Image 내부 Clone에서 사용되며, 현재 Composite Clone Slot 경로에는 적용되지 않는다.

따라서 이 계획에서 보장하는 것은 다음까지다.

- Clone Tool과 실제 `CLONE` Brush 유지
- Composite Source와 별도 Target Layer 유지
- Composite의 동일 UV 위치 Pixel 복사
- Source Composite Pixel 불변

다음은 별도 구조변경 대상이다.

- Composite의 임의 위치를 Ctrl+클릭으로 선택해 다른 위치에 복사
- Shader 최종 화면색을 실시간 Clone Source로 사용
- `use_clone_layer=True`와 위치 Offset을 동시에 적용

## 15. 코드 변경 범위

### 필수 수정

- `tools/projection_paint/blender/stencil_session.py`
  - Resource 준비, 명시적 연결, 기존 연결 갱신과 해제 API 분리
  - 자동 `_switch_active_brush()` 제거
  - Brush Snapshot에 Angle과 실제 변경값 전체 추가
  - 종료 시 현재 외부 Brush 불변 보장
- `tools/projection_paint/blender/paint_runtime.py`
  - 모든 자동 경로를 기존 연결 갱신 전용으로 교체
  - Stencil 연결 상태와 해제 이유 분리
  - Reconnect Camera·Brush Transaction 확장
- `tools/projection_paint/blender/paint_session.py`
  - Paint 진입의 Brush 생성·자동 Reconnect 제거
- `tools/projection_paint/blender/paint_state.py`
  - Paint 상태와 Stencil 연결 상태 분리
  - Legacy Stencil Migration 분리
- `tools/projection_paint/blender/state_handlers.py`
  - Undo·Redo·Load의 새 Brush 채택 금지
- `tools/projection_paint/blender/properties.py`
  - 자동 추적 기본값 비활성
  - Stencil Binding Migration Schema와 원본 Angle 저장값 추가
- `tools/projection_paint/blender/panels.py`
  - 연결 해제 이유와 Draw Brush 선택 안내

### 계약 확인만 수행

- `tools/projection_paint/blender/layer_shader.py`
- `tools/projection_paint/blender/composite_layer.py`

Clone Source·Target·Slot 계약이 유지되면 위 두 파일의 구조는 변경하지 않는다.

## 16. 테스트 계획

### 16.1 Blender Background

1. Draw Stencil 연결 후 다른 Brush 전환
   - 새 Brush Pointer·Texture·Color·Mapping·종류 불변
   - 이전 Brush의 Texture·Map Mode·Angle·Color·위치·크기 복구
   - `paint_state=ACTIVE`, Stencil은 `BRUSH_CHANGED`
2. 자동 호출별 새 Brush 채택 0회
   - Paint 진입
   - Frame 변경
   - Camera View 이탈·복귀
   - Workspace 이탈·복귀
   - 일반 Layer 복귀
   - Undo·Redo·Load Reconcile
   - Project Stencil
3. 동일 연결 Brush
   - Camera View 이탈 시 `CAMERA_SUSPENDED`
   - Camera View 복귀 시 같은 Brush에만 복구
   - Frame 변경 시 Source·배치 갱신
4. 명시적 Reconnect
   - 편집 가능 Draw Brush 성공
   - 편집 불가 Essentials Draw Brush 성공
   - Tool과 Brush 종류 불변
   - 단계별 실패 주입과 전체 Rollback
5. 비Draw Tool
   - Clone·Smear·Blur·Fill·Mask에서 Reconnect 거부
   - Tool·Brush·Clone 상태 불변
6. 종료 정리
   - 연결 Brush 활성 상태 복구
   - 다른 Brush 전환 후 종료 시 현재 Tool·Brush 불변
   - 원본 Asset 복구 실패 시 임시 Brush 비파괴 유지
7. Legacy
   - Schema가 최신이고 `stencil_follow_active_brush=True`인 파일
   - 활성 Legacy Brush의 Tool 불변
   - 비활성 임시 Brush만 정리
   - 불완전 Snapshot 비파괴 경고
   - 모든 PP Scene 정규화
8. Mask
   - PP Layer Mask 연결 차단
   - Blender 기본 MASK Brush 자동 복사 0회
   - `ImagePaint.use_stencil_layer` 불변
9. Clone 불변
   - `clone_image`, `use_clone_layer`, `clone_offset`, Active·Clone Slot 불변

기존 `test_repeated_brush_switches_keep_following_active_brush`는 반대 계약 테스트로 교체한다.

### 16.2 실제 Blender GUI

`Untitled.blend`에서 다음 순서로 검증한다.

```text
1. Projection Paint Workspace와 Painting Mode 진입
2. Paint 진입만으로 활성 Tool·Brush가 바뀌지 않는지 확인
3. Draw Brush 선택 후 Reconnect 실행
4. Camera Stencil 표시 확인
5. Clone 선택 후 여러 Viewport Timer Tick 대기
6. Tool ID=builtin_brush.clone 확인
7. WorkSpaceTool.brush_type=CLONE 확인
8. Brush.image_brush_type=CLONE 확인
9. 활성 Brush가 [Stencil] Draw Brush로 바뀌지 않는지 확인
10. Composite Source와 별도 일반 Target 확인
11. 식별 가능한 Composite 패턴을 동일 UV 위치로 Clone
12. Target Pixel이 Source Pixel과 일치하는지 확인
13. Source Composite Pixel Hash 불변 확인
14. Smear·Blur·Fill·Mask도 각각 Tool·Brush 유지 확인
15. Draw Brush로 복귀 후 Reconnect로 Camera Stencil 복구
16. Camera·Frame·Layer·Workspace 전환 반복
17. Undo·Redo와 저장·재열기 후 자동 Brush 변경 0회 확인
```

잘못된 Draw Stroke도 단순 Pixel 변화 테스트를 통과할 수 있으므로 Target Pixel 변화만 확인하지 않는다. 예상 Composite Source 색상과 정확히 일치하는지 검증한다.

### 16.3 전체 회귀

- Stencil 자원·Camera View·Mask 연결 제한
- Paint 진입·종료·Workspace 재진입
- Layer·Mask 선택과 Paint ACTIVE 상태
- Undo·Redo·Load Reconcile
- Composite 생성·재생성·Clone Slot
- Camera Review와 최근접 Camera 선택
- Modifier와 Align 독립 동작
- Production ZIP 격리 설치와 실제 `user_default` 설치본

## 17. 구현 순서

### 단계 1. 상태와 API 분리

- Paint 상태와 Stencil 연결 상태를 분리한다.
- Resource 준비, 명시적 연결, 기존 연결 갱신과 해제 API를 만든다.
- 자동 Brush 승계 호출을 제거한다.

완료 조건: 내부 Callback이 새 Brush를 채택할 수 없어야 한다.

### 단계 2. Paint 진입·Runtime·Handler 변경

- Paint 진입의 자동 Brush 생성·연결을 제거한다.
- Camera·Frame·Workspace·Layer Callback을 기존 연결 갱신 전용으로 바꾼다.
- Undo·Redo·Load에서 Tool·Brush 변경을 제거한다.

완료 조건: Reconnect 버튼 외 경로의 새 Brush 채택 횟수가 0회여야 한다.

### 단계 3. Reconnect Transaction 강화

- Draw Brush 전용 Poll과 오류 안내를 추가한다.
- Tool·Brush·Camera·Stencil 전체 Snapshot과 Rollback을 구현한다.
- 최근접 Camera 선택과 Brush 연결을 하나의 Transaction으로 묶는다.

완료 조건: 성공과 실패 모두 사용자 Tool·Clone 상태가 보존돼야 한다.

### 단계 4. 종료·Legacy Migration

- 종료 시 현재 외부 Brush를 건드리지 않게 한다.
- 자동 추적 값을 모든 PP Scene에서 비활성 정규화한다.
- 기존 `[Stencil]` Brush를 비파괴적으로 정리한다.

완료 조건: 기존 파일을 여는 것만으로 Brush Asset이 바뀌지 않아야 한다.

### 단계 5. Background·GUI·설치본 검증

- 자동 호출 경로 전체와 비Draw Tool을 검증한다.
- Composite 동일 UV Clone을 실제 Pixel로 검증한다.
- Production ZIP을 빌드하고 실제 `user_default`에 설치한다.
- Blender 재시작 뒤 `Untitled.blend`로 최종 QC를 수행한다.
- 임시 Probe와 해결된 Debug 로그를 삭제한다.

완료 조건: 설치본에서도 Projection Paint가 Blender 도구 전환에 개입하지 않아야 한다.

## 18. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| Paint 진입 뒤 Stencil이 바로 보이지 않음 | N 패널에 Draw Brush 선택과 Reconnect 안내 표시 |
| 내부 경로가 명시적 연결 함수를 다시 호출 | 사용자 Operator 전용 API와 기존 연결 갱신 API를 분리하고 호출 테스트 고정 |
| 연결 해제 이유가 섞여 자동 복구됨 | 별도 Stencil 상태와 우선순위 판정 사용 |
| Asset 복사 중 Tool이 바뀜 | Tool·Brush 사전 Snapshot, 사후 불변 검증과 전체 Rollback |
| 종료 시 사용자의 새 Brush가 이전 Brush로 바뀜 | 현재 활성 Brush가 관리 Brush인지 확인한 경우에만 Asset 복구 |
| 원본 Brush Angle·Color 손실 | 실제 변경 필드 전체를 대칭 Capture·Restore |
| Legacy Brush 정리가 Paint Hard를 활성화 | 자동 Asset 활성화 제거, 활성 Legacy Brush 비파괴 유지 |
| Clone이 고쳐졌지만 위치 지정은 작동하지 않음 | Composite Clone Slot과 동일 이미지 Offset Clone의 Blender 경로를 명확히 분리 |
| Draw Stroke를 Clone 성공으로 오판 | Source 패턴·예상 Pixel 일치와 Source Hash 불변 검증 |

## 19. 완료 조건

1. Paint 진입이 활성 Tool과 Brush를 변경하지 않는다.
2. Clone 선택 후 Tool ID와 Tool Brush Type이 `CLONE`으로 유지된다.
3. 실제 `Brush.image_brush_type`도 `CLONE`으로 유지된다.
4. Smear, Blur, Fill, Mask와 다른 Brush도 자동 교체되지 않는다.
5. 내부 Timer·Callback·Undo·Load가 새 Brush를 채택하지 않는다.
6. `Reconnect Stencil Texture`만 새 Draw Brush를 연결할 수 있다.
7. 비Draw Tool의 Reconnect는 상태 변경 없이 거부된다.
8. 같은 연결 Brush의 Camera·Frame 갱신은 유지된다.
9. Brush 변경 시 이전 Brush 원본 상태가 Angle을 포함해 복구된다.
10. Reconnect 실패 시 Tool, Brush, Camera, Viewport, Stencil과 Clone 상태가 복구된다.
11. 다른 Brush로 전환한 뒤 Paint 종료해도 현재 Tool과 Brush가 유지된다.
12. PP Layer Mask의 Stencil 차단과 Paint ACTIVE 계약이 유지된다.
13. Blender 기본 Mask Tool과 `use_stencil_layer`를 Projection Paint가 변경하지 않는다.
14. 기존 `.blend`의 Source Image와 Stencil 배치가 보존된다.
15. Legacy 자동 추적 값이 모든 PP Scene에서 비활성화된다.
16. Composite Clone Source·Target·Slot이 유지된다.
17. Composite 동일 UV Clone 결과가 실제 Source Pixel과 일치한다.
18. Source Composite Pixel은 Clone Stroke로 변경되지 않는다.
19. Undo·Redo, Workspace 왕복과 저장·재열기 뒤 자동 Brush 변경이 없다.
20. Modifier와 Align 코드는 변경되지 않는다.
21. 관련 Background, GUI와 설치본 회귀가 모두 통과한다.
22. Production ZIP을 실제 `user_default`에 설치하고 Blender 재시작 뒤 같은 결과를 확인한다.

## 20. 최종 사용자 흐름

```text
Painting Mode 진입
→ 현재 Blender Tool·Brush 그대로 유지

Camera Stencil Paint 필요
→ Draw Brush 선택
→ Reconnect Stencil Texture
→ Camera Stencil 연결

Clone 작업 필요
→ Blender Clone 선택
→ Projection Paint는 개입하지 않음
→ Composite 동일 UV Source를 별도 Layer에 Clone

Camera Stencil Paint 복귀
→ Draw Brush 선택
→ Reconnect Stencil Texture
```

이 구조에서 Projection Paint는 Camera Stencil의 Source와 배치만 관리하고 Blender Tool과 Brush 선택은 건드리지 않는다.
