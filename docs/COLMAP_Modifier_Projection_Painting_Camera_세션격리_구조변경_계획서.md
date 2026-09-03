# COLMAP Modifier·Projection Painting Camera 세션 격리 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-09-02
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.3.34`
- 대상 기능: Modifier Camera Review, Projection Painting Camera Review, Workspace Handoff
- 제외 기능: Align Camera·Marker, COLMAP 데이터 구조, Paint Layer·Texture·Undo
- 문서 상태: 구현·자동 회귀 완료

## 2. 쉬운 결론

Modifier와 Projection Painting의 Camera Object와 Background Image 슬롯은 이미 독립적이다. 현재 충돌하는 값은 같은 Scene이 하나만 가지는 `scene.camera`와 Frame 변경 Handler이다.

현재는 Projection Painting 작업공간을 나가도 Camera Runtime과 전역 Frame Handler가 남아 있다. Modifier에서 Frame이 바뀌면 비활성 Projection Painting이 요청을 받고, 0.30초 뒤 자기 Camera를 `scene.camera`에 다시 기록한다.

```text
현재 오류
Modifier가 IMAGE_118 활성화
→ Modifier Background Image 표시
→ 비활성 Projection Painting도 Frame 118 감지
→ 0.30초 뒤 PP_IMAGE_118.001을 scene.camera에 기록
→ Modifier Camera가 교체되어 Background Image가 사라짐
```

목표 구조에서는 Camera Runtime을 삭제하지 않고 **비활성 세션을 완전한 수동 대기 상태로 전환**한다.

```text
Modifier 활성
├─ Modifier만 Scene Camera 사용권 보유
├─ Modifier Frame Handler만 동작
└─ Projection Painting Runtime·Camera는 보존되지만 쓰기 금지

Projection Painting 활성
├─ Projection Painting만 Scene Camera 사용권 보유
├─ Projection Painting Frame Debounce만 동작
└─ Modifier Runtime·Camera는 보존되지만 쓰기 금지

일반 Blender 작업공간
├─ 두 COLMAP Camera 세션 모두 비활성
├─ COLMAP Frame Handler의 Camera 쓰기 없음
└─ 사용자의 Camera 상태 유지
```

## 3. 용어 정의

이 문서에서 **세션**은 현재 Window에서 활성화된 하나의 COLMAP 도구 작업공간과 그 도구가 가진 입력·Camera·Marker 사용권을 뜻한다.

다음 항목은 세션이 아니다.

- 저장된 Projection Painting Snapshot
- Modifier 독립 Runtime Domain
- 재진입을 위해 메모리에 남겨 둔 Camera Runtime Cache
- Blender에 보존된 Camera Object, Camera Data와 Background Image 슬롯

따라서 작업공간을 나가도 데이터와 Cache는 남을 수 있지만, Scene·Viewport·Frame·Marker를 변경할 권한은 반드시 반납해야 한다.

## 4. 확인된 현재 상태

### 4.1 MCP 재현 결과

사용자 Scene에서 확인한 값은 다음과 같다.

- 활성 작업공간: `COLMAP Modifier`
- Modifier Review: `Camera 118/593, IMAGE_ID 118`
- Modifier Background 설정: 활성
- Modifier Camera Object: `IMAGE_118`
- 실제 `scene.camera`: `PP_IMAGE_118.001`
- Projection Painting 작업공간: 비활성
- Projection Painting Camera Runtime: 존재
- Projection Painting 활성 Image: `118`
- Projection Painting Frame Handler: `frame_change_post`에 계속 등록

`IMAGE_118`과 `PP_IMAGE_118.001`은 서로 다른 Camera Object이며 각자 Background Image 슬롯을 가진다. 이미지 원본 파일 경로만 같다.

### 4.2 코드상 원인

1. `tools/projection_paint/blender/paint_workspace.py::request_paint_workspace_exit()`는 Paint Session, Marker와 View Layer를 정리하지만 Camera Runtime과 Frame Debounce 요청은 정지하지 않는다.
2. `tools/projection_paint/blender/state_handlers.py::frame_change_handler()`는 Projection Painting 작업공간 활성 여부를 확인하지 않고 모든 Scene Frame 변경을 `queue_frame_change()`에 전달한다.
3. `tools/projection_paint/blender/camera_scene.py::_frame_source_debounce_tick()`은 Runtime Token만 검사하고 현재 Camera 세션 소유권을 검사하지 않는다.
4. 최종 Commit의 `activate_managed_frame()`은 비활성 상태에서도 `scene.camera`를 Projection Painting Camera로 교체할 수 있다.
5. 기존 Marker 사용권은 종료 시 정상 반납되지만 Camera 전환 로직이 Marker 사용권을 Camera 쓰기 권한으로 검사하지 않는다.

## 5. Blender 공유 자원 구분

| 자원 | 현재 소유 형태 | 목표 |
| --- | --- | --- |
| Modifier Camera Object·Data | Modifier 독립 | 유지 |
| Projection Painting Camera Object·Data | Projection Painting 독립 | 유지 |
| Background Image 슬롯 | 각 Camera Data 독립 | 유지 |
| 이미지 원본 경로 | 같은 파일을 참조할 수 있음 | 유지 |
| `SpaceView3D.camera` | Viewport별 값 | 각 작업공간의 Local Camera로 사용 |
| `Scene.camera` | Scene 전역 단일 값 | 활성 Camera 세션만 조건부 사용 |
| `Scene.frame_current` | Scene 전역 단일 값 | 공유하되 활성 세션만 반응 |
| Timeline Camera Marker | Scene 전역 | 기존 Marker 세션 사용권 유지 |
| Render 해상도 | Scene 전역 | 활성 Camera 세션에서만 변경·조건부 복원 |

Blender 5.2의 `SpaceView3D.camera`와 `use_local_camera`는 Scene 활성 Camera와 분리된 Viewport 전용 Camera를 지원한다.

- <https://docs.blender.org/api/5.2/bpy.types.SpaceView3D.html>
- <https://docs.blender.org/api/5.2/bpy.types.Scene.html#bpy.types.Scene.camera>

## 6. 핵심 설계

### 6.1 기존 Marker 사용권을 Camera 쓰기 권한으로도 사용

이미 `shared/blender_runtime/scene_marker_session.py`가 같은 Scene에서 Modifier와 Projection Painting 중 하나만 Marker 사용권을 갖게 한다. 별도의 중복 사용권을 만들지 않고 다음 조건을 **Camera 세션 활성 조건**으로 정의한다.

```text
Camera 세션 활성
= 현재 Window의 해당 도구 작업공간 활성
+ 해당 도구 Workspace Session 활성
+ 해당 도구가 Scene Marker 사용권 보유
+ 현재 Runtime Generation과 Session Token 일치
```

네 조건 중 하나라도 맞지 않으면 Camera Object·Manifest·Cache를 읽을 수는 있지만 다음 쓰기는 금지한다.

- `scene.camera` 변경
- `SpaceView3D.camera` 변경
- Camera View 강제 진입
- Render 해상도 변경
- Marker 생성·복구
- Frame Source·Stencil Source 변경
- Camera 상태 UI 문구 변경

### 6.2 Viewport는 Local Camera를 우선 사용

Modifier와 Projection Painting 전용 3D View는 다음 값을 사용한다.

```text
space.use_local_camera = True
space.camera = 현재 도구의 Camera Object
region_3d.view_perspective = "CAMERA"
```

이렇게 하면 각 Workspace Screen이 자기 Camera를 보존하므로 다른 작업공간의 `scene.camera` 변경에 직접 끌려가지 않는다.

`scene.camera`가 반드시 필요한 Blender 동작은 활성 Camera 세션 안에서만 허용하고, 세션 진입 전 Pointer를 저장해 종료 시 조건부 복원한다.

### 6.3 모든 Camera 쓰기를 단일 Gateway로 통합

각 도구 내부에 Camera 쓰기 Gateway를 둔다.

```text
Modifier
require_modifier_camera_session(context, scene)
activate_modifier_camera(context, image_id, session_token)

Projection Painting
require_projection_camera_session(context, scene)
activate_projection_camera(context, image_id, session_token)
```

Gateway는 다음 순서로 처리한다.

1. Workspace·Window·Marker 사용권·Session Generation 검증
2. 대상 Camera가 해당 도구와 Dataset 소유인지 검증
3. 전용 Viewport의 Local Camera 설정
4. 필요한 경우에만 `scene.camera` 설정
5. Background 슬롯·현재 IMAGE_ID·Marker 일치 검증
6. 마지막으로 적용한 Camera Pointer와 Session Token 기록

`scene.camera = ...`를 여러 함수에서 직접 실행하지 않는다. Architecture 테스트로 허용된 Gateway 밖의 직접 쓰기를 차단한다.

### 6.4 종료 시 Projection Painting Frame 작업 완전 취소

`request_paint_workspace_exit()`의 종료 순서를 다음으로 바꾼다.

```text
1. Camera Session Generation 무효화
2. 새 Frame 요청 수신 중단
3. pending_frame·retry·error 폐기
4. Frame Source Debounce Timer 해제
5. Paint·Stencil 진행 작업 종료
6. Projection Painting Marker Detach
7. Scene Marker 사용권 반납
8. 마지막 적용 Camera가 그대로일 때만 진입 전 scene.camera 복원
9. Render 해상도도 마지막 적용값과 같을 때만 복원
10. Source Workspace와 View Layer로 복귀
```

Camera Runtime, Manifest, Camera Object, Background Image, Paint Layer와 Texture는 삭제하지 않는다.

### 6.5 비활성 Handler는 읽기와 쓰기를 모두 중단

Projection Painting의 `frame_change_handler()`는 다음 조건을 먼저 검사한다.

```text
if Projection Painting Camera 세션이 비활성:
    pending 요청 폐기
    Timer 등록하지 않음
    Status 변경하지 않음
    return
```

Frame Handler에서 검사를 통과해도 0.30초 뒤 Debounce Commit 직전에 같은 Session Token을 다시 검사한다. 작업공간을 나간 뒤 실행된 오래된 Timer는 아무것도 변경하지 않고 종료한다.

Modifier의 Frame Handler와 Camera Review Timer에도 같은 소유권 검사를 적용한다. 현재 우연히 Workspace Session 검사로 차단되는 경로도 명시적인 Camera 세션 계약으로 고정한다.

### 6.6 재진입 시 현재 Frame을 새 세션에서 연결

Projection Painting 재진입 순서는 다음과 같다.

```text
1. 이전 COLMAP 도구 종료 완료 확인
2. Scene Marker 사용권 획득
3. 새 Camera Session Generation 발급
4. 전용 View Layer와 Workspace 활성화 확인
5. Camera Runtime Cache 재연결 또는 Manifest에서 재구축
6. 현재 scene.frame_current에 해당하는 Projection Painting Camera 확인
7. Local Camera·Marker·Background Image 즉시 연결
8. Frame Handler와 Debounce를 새 Token으로 활성화
```

재진입 전 비활성 기간의 Frame 이동은 하나씩 재생하지 않는다. 현재 최종 Frame만 한 번 연결한다.

Modifier 재진입도 같은 방식으로 현재 Review IMAGE_ID와 Frame의 Modifier Camera를 Local Camera에 연결한다.

## 7. 작업공간별 사용자 경험

### 7.1 Modifier → Projection Painting

1. Modifier의 Frame Handler와 Camera Review Timer 중지
2. Modifier Marker와 Camera 사용권 반납
3. Modifier Camera·Runtime·Undo Journal 보존
4. Projection Painting 사용권 획득
5. Projection Painting 전용 Camera와 Background Image 표시

중간에 이전 도구가 다시 Camera를 바꾸는 화면 깜빡임이 없어야 한다.

### 7.2 Projection Painting → Modifier

1. 대기 중인 0.30초 Frame 요청 즉시 취소
2. Projection Painting Camera·Marker 사용권 반납
3. Modifier 사용권 획득
4. Modifier `IMAGE_ID` Camera와 Background Image 표시
5. 0.30초 이상 기다려도 Projection Painting Camera로 돌아가지 않음

### 7.3 COLMAP 도구 → 일반 Blender 작업공간

- 두 COLMAP Camera 세션 모두 비활성
- 사용자의 Camera와 Viewport를 비활성 Handler가 변경하지 않음
- COLMAP Camera Object와 작업 데이터는 보존
- 다시 COLMAP 작업공간에 들어가면 현재 Frame 기준으로 자동 연결

### 7.4 빠른 Frame 이동 중 작업공간 전환

- 마지막 Projection Painting Frame 요청이 Commit되기 전에 나가면 요청을 폐기
- 전환 후 0.30초가 지나도 Camera·Source Image·Stencil을 변경하지 않음
- 재진입하면 당시 Scene의 최종 Frame을 새 요청으로 사용

## 8. 실패와 Rollback

### 8.1 종료 중 Timer 취소 실패

- Session Generation을 먼저 무효화하므로 오래된 Timer는 Commit 불가
- 사용권 반납 상태를 `exit_settled()`에서 확인
- Timer가 남아 있어도 쓰기 Gateway에서 거부

### 8.2 Camera 복원 실패

- 마지막 적용 Pointer와 현재 Pointer가 다르면 사용자 변경으로 판단해 덮어쓰지 않음
- 해당 도구 사용권은 정상 반납
- Camera Object·Manifest·Paint 데이터는 보존
- 상태창에 복원하지 않은 이유를 표시

### 8.3 새 도구 Camera 연결 실패

- 새 도구가 만든 Marker와 Local Camera 변경만 Rollback
- Scene Camera 사용권 반납
- 이전 도구 데이터는 변경하지 않음
- Source Workspace로 복귀하고 구체적인 Camera·IMAGE_ID 원인을 표시

### 8.4 다중 Window

- 같은 Scene에서 한 Window가 Modifier Camera 세션을 소유하면 다른 Window의 Projection Painting Camera 세션 진입 차단
- 같은 Scene의 같은 도구도 단일 Window 소유 원칙 유지
- 서로 다른 Scene은 독립 허용

## 9. 저장·Load·Undo 계약

- Camera 세션 사용권, Session Generation과 Pending Frame은 `.blend`에 저장하지 않는다.
- Camera Object, Manifest, Background Image 슬롯과 Projection Painting Snapshot은 기존대로 저장한다.
- Load 후 실제 활성 Workspace의 도구만 Camera 세션을 획득한다.
- 일반 Workspace로 Load하면 두 도구 Runtime은 재구축 가능하지만 Camera 쓰기는 하지 않는다.
- Projection Painting Undo·Redo가 비활성 상태에서 실행되면 Camera Runtime 복구를 지연하고 `scene.camera`를 변경하지 않는다.
- Modifier 전용 Undo·Redo는 Modifier Camera 세션이 활성일 때만 파생 Camera 표시를 갱신한다.
- 기존 프로젝트 데이터 마이그레이션이나 Dataset 초기화는 필요하지 않다. 이번 변경은 Runtime 세션 권한만 수정한다.

## 10. 변경 대상

| 파일 | 변경 내용 |
| --- | --- |
| `shared/blender_runtime/scene_marker_session.py` | 기존 Scene 사용권을 Camera 쓰기 권한 검사에도 사용하는 중립 조회 API 보강 |
| `shared/blender_runtime/workspace_handoff.py` | 이전 도구의 Camera 사용권 반납까지 종료 완료 조건에 포함 |
| `tools/projection_paint/blender/paint_workspace.py` | 종료 전 Pending Frame 취소, Camera 상태 조건부 복원, 재진입 시 새 Session Token 발급 |
| `tools/projection_paint/blender/state_handlers.py` | 비활성 Frame Handler 즉시 반환과 오래된 요청 폐기 |
| `tools/projection_paint/blender/camera_scene.py` | Session Token, Debounce 취소, Commit 전 소유권 재검증, 직접 `scene.camera` 쓰기 통합 |
| `tools/projection_paint/blender/camera_review.py` | Local Camera 우선 연결과 활성 세션 검증 |
| `tools/projection_paint/blender/paint_runtime.py` | 비활성 Runtime의 Camera·Stencil Source 쓰기 금지 |
| `tools/projection_paint/blender/lifecycle.py` | Workspace 종료·Load·Undo·해제에서 Camera 세션 정리 검증 |
| `tools/modifier/blender/camera_review_timeline.py` | Modifier Frame Handler와 Camera 전환에 명시적인 사용권 검사 추가 |
| `tools/modifier/blender/camera_review_viewport.py` | Modifier 전용 Viewport Local Camera 적용 |
| `tools/modifier/blender/modifier_workspace.py` | Camera 사용권 획득·반납과 재진입 복구 연결 |
| `tools/modifier/blender/lifecycle.py` | Camera 사용권을 Workspace 종료 완료 조건에 포함 |
| `tests/coexistence/` | 같은 Scene Camera 단일 소유와 다중 Window 계약 검사 |
| `tests/modifier/` | 비활성 Projection Painting의 Modifier Camera 불변 회귀 |
| `tests/projection_paint/` | 종료 후 Pending Timer 무효화와 재진입 복구 회귀 |
| `tests/blender_gui/` | 실제 Camera 깜빡임·Background Image·직접 탭 이동 회귀 |
| `docs/` | ADR, 테스트 방법, 구현 현황과 Release Note 갱신 |

## 11. 테스트 계획

### 11.1 현재 오류 고정

사용자 Scene과 같은 조건을 작은 Fixture로 재현한다.

```text
Projection Painting Snapshot과 Camera Runtime 준비
→ Projection Painting 작업공간 종료
→ Modifier 작업공간 진입
→ Modifier IMAGE_118과 Background Image 연결
→ Frame 118 설정
→ 0.31초 이상 Timer 진행
```

필수 결과:

- `scene.camera` 또는 Modifier Local Camera가 `IMAGE_118` 유지
- `PP_IMAGE_118`로 변경되지 않음
- Modifier Background 슬롯 `show_background_image=True` 유지
- Projection Painting `active_image_id`와 Stencil Source 변경 없음
- Projection Painting Pending Frame과 실행 Timer 없음

### 11.2 Projection Painting 종료 회귀

- Pending Frame 대기 중 종료 후 Commit 0회
- 재시도 대기 중 종료 후 Commit 0회
- 종료 후 일반 Frame 이동 20회에도 Camera 쓰기 0회
- 종료 후 Status 문구 변경 0회
- Camera Runtime·Manifest·Layer·Texture 보존

### 11.3 Modifier 종료 회귀

- Modifier 종료 후 Camera Review Timer 쓰기 0회
- 일반 Workspace와 Projection Painting에서 Modifier `scene.camera` 쓰기 0회
- Modifier Runtime Domain·Journal·Pending Filter 보존
- 재진입 시 마지막 Review IMAGE_ID와 Background Image 복구

### 11.4 전환 행렬

| 전환 | 필수 결과 |
| --- | --- |
| Modifier → Projection Painting | Projection Painting Camera만 활성 |
| Projection Painting → Modifier | Modifier Camera만 활성 |
| Modifier → 일반 Workspace | 사용자 Camera 유지 |
| Projection Painting → 일반 Workspace | 사용자 Camera 유지 |
| Projection Painting Pending → Modifier | 오래된 Debounce Commit 0회 |
| Modifier → Projection Painting → Modifier | 양쪽 Camera Object·Background 상태 보존 |
| 직접 Workspace 탭 이동 | Handoff와 같은 사용권 반납·획득 순서 |

### 11.5 Local Camera

- 두 전용 Workspace의 `SpaceView3D.use_local_camera=True`
- 각 `SpaceView3D.camera`가 자기 도구 Camera를 참조
- 다른 작업공간에서 `scene.camera`가 바뀌어도 비활성 Workspace의 Local Camera Pointer 유지
- F7 Background Image 토글은 현재 활성 도구 Camera에만 반영

### 11.6 저장·Load·Undo·다중 Window

- 일반 Workspace 저장·재열기 후 COLMAP Camera 자동 활성화 없음
- Modifier Workspace 저장·재열기 후 Modifier만 Camera 사용권 획득
- Projection Painting Workspace 저장·재열기 후 Projection Painting만 획득
- Projection Painting Global Undo 후 비활성 Modifier Camera 불변
- Modifier 전용 Undo 후 비활성 Projection Painting Camera 불변
- 같은 Scene의 다른 Window에서 두 Camera 세션 동시 획득 차단

### 11.7 실제 GUI

현재 사용자 프로젝트를 저장하지 않고 다음을 검증한다.

1. Projection Painting에서 Frame 118 Camera와 Stencil 연결
2. Modifier로 이동
3. Modifier `IMAGE_118` Background Image 표시 확인
4. 1초 이상 대기해도 Camera와 Background가 유지되는지 확인
5. Frame을 빠르게 여러 번 이동하고 최종 Modifier Camera 유지 확인
6. Projection Painting 재진입 후 현재 Frame Camera와 Stencil 자동 복구
7. 일반 Workspace 이동 후 사용자 Camera가 유지되는지 확인
8. Marker, Camera Object, Runtime, Layer와 Undo 데이터가 삭제되지 않았는지 확인

## 12. 구현 순서

### 단계 1. 실패 회귀 작성

- 비활성 Projection Painting Debounce가 Modifier Camera를 가로채는 현재 오류를 자동화한다.
- Source Tree와 실제 설치본에서 같은 실패 원인을 확인한다.

### 단계 2. Camera 세션 활성 조건 통합

- Workspace·Window·Marker 사용권·Generation 검사를 하나의 함수로 통합한다.
- 두 도구의 모든 Camera 쓰기 경로가 이 검사를 통과하도록 한다.

### 단계 3. Projection Painting 종료·Timer 무효화

- 종료 시작 시 Generation을 먼저 무효화한다.
- Pending·Retry·Debounce Timer를 취소한다.
- 비활성 Handler와 오래된 Timer의 쓰기를 차단한다.

### 단계 4. Workspace Local Camera 적용

- Modifier와 Projection Painting 전용 Viewport에 Local Camera를 설정한다.
- `scene.camera` 의존 경로를 줄이고 남은 쓰기는 활성 세션 Gateway로 제한한다.

### 단계 5. Modifier 대칭 격리

- Modifier Frame Handler, Review Timer와 재진입도 같은 Camera 세션 계약으로 통일한다.
- 기존 F7, Pending Filter와 전용 Undo/Redo 동작을 유지한다.

### 단계 6. 전체 검증·배포

- Core, Architecture, Coexistence, Background와 실제 GUI 회귀를 실행한다.
- 프로덕션 ZIP을 빌드하고 Blender `user_default`에 설치한다.
- 임시 진단 코드와 Debug 로그를 제거한다.

## 13. 완료 조건

1. Camera Object와 Camera Data는 두 도구가 계속 독립 소유한다.
2. 같은 Scene의 Camera 쓰기 권한은 활성 COLMAP 도구 하나만 가진다.
3. 작업공간 종료와 동시에 Frame 요청과 Camera Timer 권한이 무효화된다.
4. 비활성 Projection Painting은 Modifier·일반 Workspace의 Frame 변경에 반응하지 않는다.
5. 비활성 Modifier는 Projection Painting·일반 Workspace의 Frame 변경에 반응하지 않는다.
6. 오래된 Debounce·Repair Timer는 새 세션 Camera를 변경하지 못한다.
7. Modifier Background Image가 표시된 뒤 1초 이상 사라지지 않는다.
8. 두 작업공간의 3D View는 자기 Local Camera를 사용한다.
9. `scene.camera` 직접 쓰기는 검증된 활성 Camera Gateway 밖에 존재하지 않는다.
10. Workspace 재진입 시 현재 최종 Frame의 Camera만 자동 연결한다.
11. Camera Runtime Cache, Manifest, Camera Object, Paint Layer와 Modifier Journal을 삭제하지 않는다.
12. F7 Background Image 토글은 현재 활성 Camera 세션에서만 동작한다.
13. 직접 탭 이동과 공용 Workspace Handoff가 같은 격리 결과를 만든다.
14. 사용자 Camera 변경은 조건부 복원 규칙으로 보존한다.
15. Load·Undo·Redo와 다중 Window에서도 Camera 단일 소유가 유지된다.
16. 기존 프로젝트의 Dataset 재Import나 자동 마이그레이션이 필요하지 않다.
17. 실제 사용자 프로젝트와 설치본 GUI 회귀를 통과한다.
18. 디버그 로그와 임시 Probe가 배포물에 남지 않는다.

## 14. 최종 동작

```text
Projection Painting 작업 중
→ Projection Painting Camera Session 활성
→ Frame Debounce와 Stencil Source 갱신 허용

Modifier로 이동
→ Projection Painting Pending 요청 즉시 폐기
→ Projection Painting Camera Session 권한 반납
→ Modifier Camera Session 활성
→ Modifier Local Camera와 Background Image 연결

Modifier에서 Frame 이동
→ Modifier만 현재 IMAGE_ID 처리
→ Projection Painting Runtime은 데이터만 보존
→ 0.30초가 지나도 Projection Painting Camera Commit 없음

Projection Painting 재진입
→ 새 Session Token 발급
→ 현재 최종 Frame Camera와 Source Image 한 번 연결
→ 이전 세션의 오래된 요청은 재사용하지 않음
```

핵심은 Camera Runtime을 삭제하는 것이 아니라, **활성 작업공간 세션만 Camera를 쓸 수 있게 하고 비활성 Runtime을 완전히 수동 상태로 만드는 것**이다.

## 15. 구현 결과

- Modifier와 Projection Painting이 Scene Camera를 변경하려면 현재 Window·Workspace·Marker 사용권·세대 Token이 모두 일치해야 한다.
- 작업공간 종료 시작 시 Camera Token과 Frame Pending을 먼저 무효화하고, 데이터와 Runtime Cache는 보존한다.
- 두 전용 3D View는 자기 Local Camera를 사용하며 비활성 도구의 Frame Handler와 Timer는 Camera를 변경하지 않는다.
- Projection Painting 작업공간 생성과 Camera 연결을 분리했다. 첫 단계는 Workspace 전환과 화면 구성만 수행하고, 다음 UI Tick에서 Workspace가 `READY`인지 재확인한 뒤 Camera Runtime을 한 번 연결한다.
- 준비 실패 시 Marker·Camera·View Layer 사용권을 먼저 반납하고 실패 Workspace를 정리하며, 삭제된 Workspace RNA 참조를 사용한 자동 재진입은 실행하지 않는다.
- Projection Painting Camera Background 19개, Modifier Camera Background, Scene Marker 단위 검사와 숨김 GUI의 Modifier → Projection Painting → Modifier → 일반 Workspace 교대를 통과했다.
