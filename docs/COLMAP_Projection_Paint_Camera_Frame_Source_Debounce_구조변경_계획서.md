# COLMAP Projection Paint Camera Frame Source Debounce 구조변경 계획서

## 1. 목적

Timeline에서 카메라 프레임을 빠르게 이동할 때 중간 프레임 이미지를 반복해서 불러오지 않습니다. 마지막 프레임 변경 이후 0.3초 동안 추가 입력이 없을 때 해당 프레임의 Camera와 Source Image를 정확히 한 번 연결합니다.

핵심 목표는 다음과 같습니다.

- 빠르게 이동하는 동안에는 마지막 요청만 유지합니다.
- 마지막 프레임에서 0.3초간 멈추면 한 번만 이미지를 연결합니다.
- 정상적인 프레임 변경을 Camera 오류 복구 횟수로 계산하지 않습니다.
- 최종 Source Image 연결이 성공하기 전에는 요청을 완료 처리하지 않습니다.
- Camera Stencil 외의 Blender Paint Tool, Brush, Layer와 Undo에는 개입하지 않습니다.

## 2. 확인된 현재 문제

현재 `frame_change_post` 흐름은 다음과 같습니다.

```text
Frame 변경
→ CameraRuntime.pending_frame에 최신 Frame 기록
→ 0.3초 주기의 Camera Guard Timer가 pending_frame 처리
→ activate_managed_frame()
→ refresh_frame_source()
```

이 구조에는 세 가지 문제가 있습니다.

### 2.1 0.3초 주기 확인과 최종 대기가 다름

현재는 마지막 입력 이후 0.3초를 기다리는 방식이 아닙니다. Camera Guard가 0.3초마다 실행되며 그 시점의 최신 프레임을 처리합니다. 따라서 계속 이동 중이어도 0.3초마다 중간 이미지를 불러올 수 있습니다.

### 2.2 정상 프레임 변경이 Camera 복구 횟수에 포함됨

`_repair_runtime()`은 `pending_frame`을 정상 처리한 경우에도 `wrote=True`로 기록합니다. 이 값이 Camera Object·Data·Marker 오류 복구와 같은 `repair_attempts`에 누적됩니다.

빠르게 이동하면 정상적인 프레임 변경만으로도 복구 상한을 초과해 다음 오류 상태가 됩니다.

```text
Camera Guard가 반복 복구 상한을 초과했습니다.
```

이후 `queue_frame_change()`가 요청을 받지 않아 최종 프레임과 연결 이미지가 달라집니다.

### 2.3 Source 연결 성공 전에 요청을 제거함

`activate_managed_frame()`은 Source Image 연결보다 먼저 `pending_frame`을 제거합니다. 이미지 로드나 Source Commit이 실패하면 같은 최종 프레임을 다시 처리할 근거가 사라집니다.

## 3. 목표 사용자 경험

기본 최종 대기 시간은 `0.30초`로 고정합니다.

```text
Frame 10 → 11 → 12 → 30 → 55를 빠르게 이동
→ 중간 이미지는 연결하지 않음
→ Pending Target은 계속 Frame 55로 교체

Frame 55에서 정지
→ 마지막 입력부터 0.30초 대기
→ IMAGE_ID 55를 정확히 한 번 로드·연결
→ Camera, Marker, Source Image의 Frame을 최종 검증
```

0.30초 안에 Frame 56으로 이동하면 기존 대기는 취소되고 Frame 56 기준으로 0.30초를 다시 계산합니다.

대기 중에는 이전 Source Image를 유지하며 Tool이나 Brush를 교체하지 않습니다. 사용자가 현재 프레임과 Source Image가 일치하기 전에 Camera Stencil로 칠하지 않도록 N 패널 상태를 `Camera Image 연결 대기 중`으로 표시합니다. 별도의 전역 입력 차단이나 Blender 기본 Tool 가로채기는 추가하지 않습니다.

## 4. 상태 모델

`CameraRuntime`에 저장되지 않는 Runtime 전용 상태를 추가합니다.

| 필드 | 역할 |
| --- | --- |
| `pending_frame` | 가장 마지막으로 요청된 Frame |
| `pending_frame_serial` | 새 Frame 입력마다 증가하는 최신 요청 번호 |
| `pending_frame_changed_at` | 마지막 Frame 변경 시각 |
| `pending_frame_retry_count` | 동일 최신 요청의 Source 연결 재시도 횟수 |
| `pending_frame_error` | 최신 요청의 마지막 실패 원인 |

이 상태는 `.blend`에 저장하지 않습니다. Load, Undo, Redo, Extension 해제와 Runtime 재구축 때 폐기합니다.

상태 전이는 다음과 같습니다.

```text
IDLE
  └─ Frame 변경 → WAITING

WAITING
  ├─ 0.30초 안에 새 Frame 변경 → WAITING 시간·Target 갱신
  └─ 0.30초 경과 → COMMITTING

COMMITTING
  ├─ 최신 Serial·Frame 일치 + Source Commit 성공 → IDLE
  ├─ 더 최신 요청 발견 → WAITING
  └─ Source Commit 실패 → RETRY_WAIT

RETRY_WAIT
  ├─ 새 Frame 변경 → WAITING, 재시도 횟수 초기화
  ├─ 제한 내 재시도 성공 → IDLE
  └─ 재시도 상한 초과 → SOURCE_ERROR

SOURCE_ERROR
  └─ 새 Frame 변경 또는 명시적 Reconnect → WAITING
```

## 5. Timer 구조 변경

### 5.1 Camera Guard 책임

기존 `_repair_timer()`는 다음 작업만 담당합니다.

- 관리 Camera Object 복구
- Camera Data 복구
- Marker 복구
- 실제 비정상 변경에 대한 반복 복구 상한 관리

정상적인 Timeline Frame 변경과 Source Image 연결은 Camera Guard의 `repair_attempts`에 포함하지 않습니다.

### 5.2 Frame Source Debounce 책임

별도 `_frame_source_debounce_tick()`을 추가합니다.

- Pending 요청이 없으면 즉시 종료합니다.
- 마지막 변경 후 0.30초가 지나지 않았으면 남은 시간을 반환합니다.
- 최신 `pending_frame`과 `pending_frame_serial`을 Snapshot합니다.
- `scene.frame_current`가 Pending Frame과 다르면 최신 Frame으로 요청을 다시 기록합니다.
- Camera·Marker·Source Image를 최종 Frame 기준으로 연결합니다.
- Commit 뒤에도 Serial과 현재 Frame이 같을 때만 Pending을 제거합니다.

Timer는 Frame 변경이 있을 때만 등록하고 Pending이 끝나면 해제합니다. 항상 실행되는 0.3초 Polling Timer로 만들지 않습니다.

## 6. 최종 Frame Commit 계약

최종 Frame 연결은 다음 순서를 지킵니다.

1. Pending Frame·Serial·현재 Frame을 확인합니다.
2. Manifest에 해당 Frame과 IMAGE_ID가 존재하는지 검증합니다.
3. 관리 Camera Object와 Marker를 해당 Frame으로 맞춥니다.
4. Source Image 요청 Token을 생성합니다.
5. Cache에서 Image를 얻거나 원본 경로에서 로드합니다.
6. 요청 Serial과 `scene.frame_current`가 여전히 최신인지 확인합니다.
7. 관리 Stencil Texture의 Source Image를 Commit합니다.
8. `active_image_id`, `current_image_id`, `current_image_name`을 갱신합니다.
9. Camera Frame, Marker, `active_image_id`와 실제 Source Image Pointer 일치를 검증합니다.
10. 모든 검증이 성공한 경우에만 Pending을 제거합니다.

중간 프레임은 Cache Load와 Blender Image 생성 단계에 들어가지 않습니다.

## 7. 실패와 재시도

Source 연결 실패를 Camera Guard 오류로 기록하지 않습니다.

- 동일 최종 프레임은 짧은 간격으로 최대 2회 재시도합니다.
- 재시도 중 새 Frame이 입력되면 이전 실패를 폐기하고 새 최종 Frame의 0.30초 대기를 시작합니다.
- 재시도 상한을 넘으면 기존 Source Image를 유지하고 N 패널에 최종 Frame과 실패 원인을 표시합니다.
- 실패한 요청은 `active_image_id`와 현재 Source Image Pointer를 부분 변경하지 않습니다.
- 명시적 `Reconnect Stencil Texture`는 Debounce 대기를 건너뛰고 현재 Frame을 즉시 연결할 수 있습니다.

## 8. Blender 기본 도구 호환 경계

이번 변경에서 다음 항목은 수정하지 않습니다.

- 활성 Workspace Tool ID
- Draw·Blur·Smear·Clone·Fill·Mask Brush 선택
- Brush 크기, 강도, Texture와 사용자 설정
- Clone Source 위치와 `clone_offset`
- Layer 선택과 Paint Target
- Blender Undo·Redo Stack

Frame Source Debounce는 관리 Camera Source Image와 관련 Metadata만 변경합니다. Camera View 이동·확대에 따른 Stencil 위치·크기 추적은 기존 Viewport Timer가 계속 담당합니다.

## 9. 수정 대상

| 파일 | 변경 내용 |
| --- | --- |
| `tools/projection_paint/blender/camera_scene.py` | Pending Serial·시각·재시도 상태, 전용 Debounce Timer, Camera Guard와 정상 Frame 처리 분리 |
| `tools/projection_paint/blender/paint_runtime.py` | Source Commit 성공 여부와 최신 요청 검증을 명확히 반환하고 실패 시 부분 상태 Rollback |
| `tools/projection_paint/blender/state_handlers.py` | `frame_change_post`에서 최신 Frame Debounce 요청만 기록 |
| `tools/projection_paint/blender/lifecycle.py` | Load·Undo·Redo·해제 경계에서 Debounce Timer와 Runtime 상태 정리 검증 |
| `tests/projection_paint/blender_background/test_camera_review_background.py` | 빠른 Frame 변경과 Camera·Marker 최종 일치 회귀 |
| `tests/projection_paint/blender_background/test_paint_runtime_mask_stencil.py` | 최종 Source Image Commit·실패 Rollback·Stencil 무간섭 회귀 |
| `tests/projection_paint/gui_stencil_tool_independence.py` | 실데이터 빠른 이동 뒤 최종 Image·Brush·Stencil GUI 회귀 |
| `docs/TESTING.md`·Release Note·구현 현황 | 검증 명령, 최종 동작과 패키지 정보 기록 |

Modifier, Align, 공용 Snapshot과 Layer 합성 구조는 변경하지 않습니다.

## 10. 테스트 계획

### 10.1 Blender Background

- 0.30초 이내에 Frame을 20회 변경해도 Source Commit은 0회입니다.
- 마지막 변경 후 0.29초에는 Commit하지 않고 0.30초 이후 정확히 1회 Commit합니다.
- 대기 중 새 Frame이 들어오면 대기 시간이 마지막 입력 기준으로 다시 시작됩니다.
- 처리 대상은 항상 마지막 Frame과 IMAGE_ID입니다.
- 중간 Frame Image는 Cache에 새로 로드하지 않습니다.
- 정상 Frame 전환을 반복해도 `repair_attempts`가 증가하지 않습니다.
- Camera Object·Data를 실제로 훼손했을 때만 Camera Guard 복구 횟수가 증가합니다.
- Source 연결 성공 전에는 Pending이 제거되지 않습니다.
- Source Load 실패 후 재시도 성공 시 최종 Frame이 정상 연결됩니다.
- 재시도 중 새 Frame이 들어오면 이전 Frame Commit을 취소합니다.
- Load·Undo·Redo·Extension 해제 뒤 오래된 Timer가 실행되지 않습니다.
- Tool ID, Brush Pointer, Layer Target과 Undo 상태는 Frame Debounce 전후 동일합니다.

시간 테스트는 실제 대기 대신 주입 가능한 단조 시계 값을 사용해 결정적으로 검증합니다.

### 10.2 실제 GUI

사용자 `Untitled.blend`는 저장하지 않고 다음을 확인합니다.

1. Projection Paint Painting Mode 진입
2. Draw Brush와 Camera Stencil 연결
3. Timeline에서 20개 이상의 Camera Frame을 0.30초보다 빠르게 이동
4. 이동 중 중간 Source Image가 로드되지 않는지 확인
5. 마지막 Frame에서 0.30초 정지
6. 최종 Frame, Camera Marker, IMAGE_ID와 Source Image Pointer 일치 확인
7. Camera Stencil 위치·크기 확인
8. Camera Texture Stroke와 Blender Undo 확인
9. Draw → Clone → Draw 왕복 후 동일 전용 Draw Brush 유지 확인
10. Runtime 오류와 임시 Debug 로그가 없는지 확인

## 11. 구현 순서

### 단계 1. 실패 회귀 고정

- 빠른 Frame 이동 후 최종 Frame과 `active_image_id`가 다른 현재 실패를 자동화합니다.
- 정상 Frame 처리만으로 Camera Guard 상한을 초과하는 현재 오류를 고정합니다.

완료 조건: 수정 전 테스트가 확인된 원인으로 실패해야 합니다.

### 단계 2. Camera Guard와 Frame 요청 분리

- `_repair_runtime()`에서 `pending_frame` 처리를 제거합니다.
- Camera Guard의 `repair_attempts`는 실제 Camera·Data·Marker 복구만 계산합니다.
- 별도 Debounce Runtime 상태와 Timer를 추가합니다.

완료 조건: Frame을 반복 변경해도 Camera Guard 오류가 발생하지 않아야 합니다.

### 단계 3. Trailing-edge Debounce 구현

- 마지막 Frame·Serial·변경 시각을 기록합니다.
- 마지막 입력부터 0.30초가 지난 경우에만 Commit합니다.
- 새 입력이 들어오면 대기를 다시 시작합니다.

완료 조건: 빠른 이동 구간에서 중간 Source Commit이 0회이고 최종 Commit만 1회여야 합니다.

### 단계 4. 성공 후 소비와 실패 재시도

- Source 연결과 검증 성공 뒤 Pending을 제거합니다.
- 실패 Rollback과 최대 2회 재시도를 구현합니다.
- 새 Frame 요청이 이전 재시도보다 우선하도록 합니다.

완료 조건: 실패가 있어도 이전 Source를 보존하고 최신 Frame 요청을 놓치지 않아야 합니다.

### 단계 5. 전체 회귀와 설치

- 대상 Background, Architecture, Production 검사를 실행합니다.
- 실제 GUI에서 빠른 이동·최종 연결·Stroke·Undo를 검증합니다.
- 패키지를 빌드해 Blender `user_default`에 설치합니다.
- 해결을 위해 추가한 임시 Probe와 Debug 로그를 제거합니다.

완료 조건: Source Tree와 실제 설치본이 같은 결과를 내야 합니다.

## 12. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 0.30초 대기 중 이전 이미지가 잠시 보임 | 상태를 `Camera Image 연결 대기 중`으로 표시하고 최종 Commit 전에는 Source Metadata를 변경하지 않음 |
| 대기 중 사용자가 Camera Stencil Stroke를 시작함 | 현재 Frame과 `active_image_id` 불일치를 상태 영역에 명확히 표시하고 전역 Paint Tool 입력은 가로채지 않음 |
| 오래된 Frame 로드가 최신 Frame을 덮음 | 요청 Serial·현재 Frame·Cache Token을 Commit 직전에 모두 검증 |
| 이미지 로드 실패가 무한 재시도로 이어짐 | 동일 Serial 기준 최대 2회로 제한하고 오류를 상태에 기록 |
| Camera Guard가 다시 정상 Frame을 오류로 계산함 | Guard 복구 횟수와 Frame Commit 횟수를 별도 상태·테스트로 고정 |
| Timer가 Load·Undo·해제 뒤 살아남음 | Lifecycle Registry로 전용 Timer를 등록하고 Barrier 회귀 추가 |
| Debounce가 Blender Tool이나 Brush를 바꿈 | Tool ID·Brush Pointer·사용자 설정 Hash 불변 회귀 추가 |

## 13. 완료 조건

1. 마지막 Frame 변경 이후 0.30초 동안 추가 입력이 없을 때만 Source Image를 연결합니다.
2. 0.30초 안의 새 Frame 입력은 대기 시간을 다시 시작합니다.
3. 빠른 이동 중 중간 Frame Image를 로드하거나 연결하지 않습니다.
4. 최종 Frame의 Source Commit은 정확히 한 번 실행됩니다.
5. 정상 Frame 변경으로 `repair_attempts`가 증가하지 않습니다.
6. Camera Guard 오류는 실제 Camera Object·Data·Marker 반복 복구에만 사용됩니다.
7. Source Image 연결 성공 전에는 Pending 요청을 제거하지 않습니다.
8. 최신 요청보다 오래된 Image Load 결과가 Commit되지 않습니다.
9. 최종 Camera, Marker, IMAGE_ID와 Source Image가 일치합니다.
10. 실패 시 기존 Source와 Brush 상태를 보존하고 제한적으로 재시도합니다.
11. Draw·Blur·Smear·Clone·Fill·Mask, Layer, Undo와 다른 Workspace 동작은 변경되지 않습니다.
12. 사용자 파일을 저장하지 않은 실제 GUI 회귀와 설치본 검증을 통과합니다.

## 14. 최종 동작

```text
사용자가 Timeline을 빠르게 이동
→ 마지막 Frame만 Runtime Pending으로 보관
→ 새 Frame 입력마다 0.30초 대기 다시 시작
→ Camera Guard 복구 횟수는 증가하지 않음

사용자가 최종 Frame에서 정지
→ 0.30초 경과
→ 최종 Camera·Marker 검증
→ 해당 IMAGE_ID의 Source Image 로드
→ 최신 요청 검증
→ Camera Stencil Source Commit
→ 성공한 경우에만 Pending 제거

다시 Frame을 이동
→ 같은 절차를 새 최종 Frame 기준으로 반복
```

이 구조의 핵심은 Camera 오류 복구와 사용자 Frame 이동을 분리하고, 마지막 입력이 안정된 뒤 최종 Source Image만 연결하는 것입니다.
