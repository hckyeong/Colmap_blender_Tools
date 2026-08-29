# COLMAP Tools Camera Marker 세션 격리 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-08-28
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 대상 도구: `COLMAP Modifier`, `Projection Paint`
- 문서 상태: 구현 전 구조변경 계획
- 기준 버전: 0.1.7

## 2. 쉬운 결론

Modifier에서 사용 중인 카메라 프레임 키가 Projection Paint가 사용할 프레임과 중복되어 Import하지 못하고 있다.

Object와 Collection은 View Layer로 Workspace별 표시를 분리할 수 있지만, Camera Marker는 `Scene.timeline_markers`에 저장되는 Scene 공용 데이터라서 Workspace를 바꿔도 남는다. 따라서 Camera Marker는 영구 데이터가 아니라 **현재 활성 도구 세션에서만 Scene에 올리는 임시 실행 데이터**로 변경한다.

```text
Modifier 활성
└─ Modifier Camera Marker만 Scene에 존재

Projection Paint 활성
└─ Projection Paint Camera Marker만 Scene에 존재

일반 Blender Workspace
└─ 두 도구 Camera Marker 모두 Scene에서 내려간 상태
```

COLMAP 데이터, Camera Object, 편집 결과와 Marker Manifest는 삭제하지 않는다. Workspace를 다시 열면 저장된 Manifest로 해당 도구의 Marker만 복원한다.

## 3. 현재 문제와 확인 결과

### 3.1 실제 재현 상태

MCP로 사용자 Scene을 확인한 결과는 다음과 같다.

- Modifier Dataset UUID: `1ea72db2-3ef7-4d11-9c2b-404d9e705cef`
- Modifier Camera Review 수: 593개
- Scene Timeline Marker 수: 593개
- Projection Paint `Frame Start`: 1
- Projection Paint 기존 Camera Manifest: 없음
- Frame 1~5 Marker는 모두 Modifier 소유 Camera를 참조함

오류에 표시된 아래 Marker는 Align 병합 파일이 만든 것이 아니라 기존 Modifier Camera Review가 만든 것이다.

```text
COLMAP Review 1ea72db2 IMAGE_ID 1@1
COLMAP Review 1ea72db2 IMAGE_ID 2@2
COLMAP Review 1ea72db2 IMAGE_ID 3@3
COLMAP Review 1ea72db2 IMAGE_ID 4@4
COLMAP Review 1ea72db2 IMAGE_ID 5@5
```

Projection Paint는 새 Camera를 Frame 1부터 배치하려 하지만 같은 Frame에 자기 Manifest가 소유하지 않은 Camera Marker가 있어 Import를 중단한다. 오류에는 충돌 목록의 첫 5개만 표시된다.

### 3.2 구조적 원인

- Modifier 종료는 Viewport Runtime만 해제하고 Camera Marker는 Scene에 유지한다.
- Projection Paint Camera Guard는 Manifest가 있으면 Workspace가 비활성 상태여도 누락 Marker를 다시 생성할 수 있다.
- Workspace Handoff는 Window 단위 도구 종료 순서만 관리하며 Scene 전역 Marker 사용권은 관리하지 않는다.
- Blender의 Timeline Marker는 Workspace나 View Layer가 아닌 현재 Scene의 모든 Timeline이 공유한다.

Blender 공식 API 기준도 `Scene.timeline_markers`를 현재 Scene의 모든 Timeline에서 사용하는 Marker Collection으로 정의한다.

- <https://docs.blender.org/api/5.2/bpy.types.Scene.html#bpy.types.Scene.timeline_markers>

## 4. 목표

1. 현재 활성 도구의 Camera Marker만 Scene에 존재하게 한다.
2. Modifier 종료 후 Marker는 제거하지만 COLMAP 데이터와 편집 결과는 보존한다.
3. Projection Paint 종료 후 Marker는 제거하지만 Camera Manifest와 Paint 데이터는 보존한다.
4. 재진입 시 Manifest에서 같은 IMAGE_ID, Frame과 Camera 연결을 복원한다.
5. Modifier와 Projection Paint가 같은 Frame 1부터 사용해도 서로 충돌하지 않게 한다.
6. 사용자 Marker와 다른 애드온 Marker는 수정하거나 삭제하지 않는다.
7. Workspace 전환, 직접 탭 이동, 저장·재로드와 Undo/Redo에서도 같은 규칙을 유지한다.
8. 같은 Scene을 여러 Blender Window에서 동시에 사용하는 경우 Marker 소유 충돌을 차단한다.

## 5. 제외 범위

- Align의 Marker Alignment 기능 변경
- COLMAP 파일 형식이나 Align Merge 결과 변경
- Modifier와 Projection Paint의 Object·Collection 데이터 삭제
- 두 도구의 Camera Object 또는 Manifest 통합
- 도구 사이 데이터 전달 Pipeline 추가
- 사용자 Timeline Marker 자동 이동 또는 삭제
- 충돌을 숨기기 위한 Frame 자동 오프셋
- Blender Global Undo Stack 초기화

Frame 자동 오프셋은 문제를 피할 수 있지만 두 도구의 Timeline 의미가 달라지고 저장 파일마다 Frame 범위가 달라지므로 기본 해결책으로 사용하지 않는다.

## 6. 핵심 설계

### 6.1 Manifest와 실제 Marker를 분리한다

각 도구의 Manifest를 영구 원본으로 유지하고 실제 `TimelineMarker`는 활성 세션의 실행 사본으로 취급한다.

| 구분 | Modifier | Projection Paint |
| --- | --- | --- |
| 영구 원본 | `review_marker_manifest_json` | `pp_camera_manifest` |
| 보존 데이터 | Camera Object, 편집 상태, Pending·Undo | Camera Object, Camera Manifest, Paint 상태 |
| 세션 실행 데이터 | `COLMAP Review ...` Marker | `PP_CAM_...` Marker |
| 비활성 상태 | Manifest만 유지, Marker 없음 | Manifest만 유지, Marker 없음 |
| 활성 상태 | Manifest에서 Marker 연결 | Manifest에서 Marker 연결 |

Marker 이름만으로 소유권을 판단하지 않는다. 다음 값을 함께 확인한다.

- 도구 Owner Token
- Dataset UUID
- IMAGE_ID
- Marker 이름
- Frame
- 연결 Camera Object Pointer와 소유 Metadata

### 6.2 Marker 세션 상태

Scene별 Marker 세션은 아래 상태를 가진다.

```text
SUSPENDED
  → ATTACHING
  → ACTIVE
  → DETACHING
  → SUSPENDED

실패 시
ATTACHING 또는 DETACHING → ERROR
```

- `SUSPENDED`: Manifest는 있지만 Scene에 도구 Marker가 없음
- `ATTACHING`: 외부 충돌 검증과 Marker 복원이 진행 중
- `ACTIVE`: 현재 도구가 Scene Marker 사용권을 소유함
- `DETACHING`: Guard를 중지하고 정확한 소유 Marker를 제거하는 중
- `ERROR`: 일부 적용을 Rollback한 뒤 사용자 확인이 필요한 상태

상태는 Runtime Pointer만으로 판단하지 않는다. 저장·재로드와 비정상 종료 복구를 위해 Scene에 도구별 상태와 Schema를 기록한다.

### 6.3 Scene 단위 Marker 사용권

`shared/blender_runtime/scene_marker_session.py`에 도구 의미를 모르는 Scene 단위 사용권 조정기를 추가한다.

공용 계층의 책임은 다음으로 제한한다.

- Scene Pointer와 Scene UUID별 현재 Marker 소유 도구 기록
- 이전 도구의 `detach` 완료 확인
- 다음 도구의 `attach` 시작 허용
- 동일 Scene을 사용하는 다른 Window의 충돌 차단
- 파일 Load와 Extension 해제 시 Runtime 사용권 폐기

Modifier와 Projection Paint는 아래 Callback 계약만 등록한다.

```text
tool_id
is_marker_session_active(scene)
request_marker_detach(context, scene)
is_marker_detached(scene)
request_marker_attach(context, scene)
report_marker_error(message)
```

공용 계층은 각 도구의 Manifest, Camera Object와 Runtime을 직접 Import하거나 해석하지 않는다. 기존 독립 도구 경계를 유지한다.

### 6.4 Workspace Handoff 연결 순서

`Modifier → Projection Paint` 전환은 다음 순서로 진행한다.

```text
1. Modifier 종료 가능 여부 확인
2. Modifier Marker Guard와 Timeline Callback 중지
3. Modifier 소유 Marker Snapshot 생성
4. 정확히 일치하는 Modifier Marker만 Scene에서 제거
5. Modifier Marker가 0개인지 확인
6. Scene Marker 사용권 반납
7. Modifier Workspace 종료 완료
8. Projection Paint Workspace 활성화
9. Projection Paint가 Scene Marker 사용권 획득
10. 사용자·외부 Marker와 대상 Frame 충돌 검사
11. 기존 Projection Manifest가 있으면 Marker 복원
12. 첫 Import라면 Marker가 없는 활성 상태에서 Import 허용
13. Projection Paint Marker Guard 시작
```

이전 도구의 Marker 제거가 완료되지 않으면 다음 도구를 시작하지 않는다.

### 6.5 일반 Workspace 이동

Modifier 또는 Projection Paint에서 일반 Blender Workspace로 이동할 때도 해당 도구 Marker를 내린다.

- Object 데이터는 기존 View Layer 격리 규칙대로 숨김
- Camera Marker는 Scene에서 제거
- Manifest와 편집 결과는 보존
- 진입 전 사용자 `scene.camera`, Frame 범위와 현재 Frame은 조건부 복원

사용자가 활성 세션 중 Scene Camera나 Frame을 직접 변경한 경우 무조건 덮어쓰지 않는다. 세션이 마지막으로 적용한 값과 여전히 일치할 때만 진입 전 Snapshot을 복원하고, 다르면 사용자 변경을 보존하며 경고를 기록한다.

## 7. Modifier 변경 계획

### 7.1 Timeline Marker Attach·Detach 분리

`tools/modifier/blender/camera_review_timeline.py`에 다음 책임을 추가한다.

- `attach_timeline_markers(context, session)`
- `detach_timeline_markers(session)`
- `timeline_markers_attached(session)`
- `validate_timeline_marker_ownership(session)`

기존 `detach_session()`은 Runtime Callback만 끊으므로 실제 Marker 제거와 구분한다. 새 `detach_timeline_markers()`는 다음을 수행한다.

1. Manifest 전체 검증
2. 현재 Marker의 이름·Frame·Camera 소유권 검증
3. 제거 전 Snapshot 생성
4. Marker Guard와 Pending Timeline Timer 중지
5. 검증된 Marker Pointer만 제거
6. 제거 결과 전수 확인
7. 실패 시 Snapshot으로 원상 복구

### 7.2 Modifier 종료

`exit_modifier_workspace()`에서 Source Workspace로 돌아가기 전에 Marker Detach를 완료한다.

- Point·Camera Edit Mode 종료
- Export와 Pending Apply 진행 여부 확인
- Marker Detach
- Viewport Runtime 해제
- Workspace 복귀
- `is_settled()`에서 Modifier Marker가 남지 않았는지 확인

Marker Detach가 실패하면 Workspace 전환을 중단하고 기존 Modifier 세션을 유지한다.

### 7.3 Modifier 재진입

Modifier Workspace와 전용 View Layer가 준비된 뒤 Manifest에서 Marker를 복원한다.

- Pending Camera를 제외한 현재 ReviewIndex와 Manifest 비교
- 외부 Camera Marker Frame 충돌 검사
- Marker를 새로 생성한 뒤 Camera 연결 검증
- 현재 IMAGE_ID와 Frame 복원
- Timeline Callback과 Marker Guard 시작

Pending Filter와 Modifier 전용 Undo 데이터는 기존 방식으로 보존한다.

### 7.4 Modifier Undo·Load

- 활성 Modifier 세션에서 Undo/Redo: Runtime 재구축 후 Marker를 다시 연결
- 비활성 Modifier 상태에서 Undo/Redo: Manifest와 Object Runtime만 재구축하고 Marker는 생성하지 않음
- `.blend` Load 후 Modifier Workspace가 실제 활성 상태일 때만 Marker Attach
- 일반 Workspace로 로드되면 Modifier Marker는 SUSPENDED 상태 유지

## 8. Projection Paint 변경 계획

### 8.1 Camera Guard의 활성 조건 추가

현재 `_repair_runtime()`은 누락 Marker를 자동 복원한다. Marker 복원은 Projection Paint Marker 세션이 `ACTIVE`일 때만 실행하도록 변경한다.

- Object와 Camera Data 복구는 기존 계약 유지
- `_restore_marker()` 호출만 Marker 세션 활성 여부로 제한
- 비활성 상태의 `rebuild_camera_runtime()`은 Marker가 없어도 오류로 처리하지 않음
- 비활성 상태의 Frame Change Handler는 Marker 복원 Timer를 실행하지 않음

### 8.2 Projection Paint 종료

`request_paint_workspace_exit()`에서 Workspace 복귀 전에 Projection Paint 소유 Marker를 제거한다.

- `managed_marker_candidates()`로 정확한 Marker Pointer 수집
- Camera Manifest와 실제 Marker 전수 대조
- Camera Guard 중지
- 검증된 Marker만 제거
- Runtime의 `markers[image_id]`를 `None` 상태로 갱신
- Marker Detach 완료 뒤에만 `paint_workspace_exit_settled()`가 `True`

Paint Layer, Image, Material, Camera Object와 READY Workspace는 기존대로 보존한다.

### 8.3 Projection Paint 진입과 Import

- 기존 Camera Manifest가 있으면 Marker 사용권 획득 후 Marker 복원
- Manifest가 없는 첫 진입은 Marker 0개의 활성 세션으로 시작
- Import Validation은 사용자 Marker와 현재 활성 외부 Marker만 충돌로 판정
- 새 Import Commit은 기존 Projection Paint Marker가 Attach된 경우에만 자기 Marker로 인정
- Import 실패 시 생성 Marker를 모두 제거하고 이전 Marker 세션을 복구

Modifier가 정상적으로 Marker를 내렸다면 Projection Paint는 같은 Frame 1부터 Import할 수 있다.

## 9. 기존 파일 마이그레이션

0.1.7 이전 파일에는 Workspace가 비활성 상태여도 도구 Marker가 남아 있을 수 있다.

첫 Load 또는 첫 Workspace 전환에서 다음 규칙으로 정리한다.

1. 현재 활성 Workspace의 Tool ID를 확인한다.
2. 현재 활성 도구의 검증된 Marker만 ACTIVE 후보로 유지한다.
3. 비활성 도구의 Marker는 해당 도구 Manifest와 Camera 소유권이 정확히 일치할 때만 제거한다.
4. 일반 Workspace라면 두 도구 Marker를 모두 SUSPENDED 상태로 전환한다.
5. 이름만 같거나 Camera 소유권이 모호한 Marker는 자동 삭제하지 않는다.
6. 모호한 상태에서는 Import를 차단하고 충돌 Marker 이름과 소유 상태를 표시한다.

현재 사용자 Scene의 `COLMAP Review 1ea72db2 ...` Marker 593개는 Modifier Manifest와 Camera Metadata를 검증한 뒤 안전하게 SUSPENDED 상태로 전환할 수 있다.

## 10. 다중 Window 규칙

Timeline Marker는 Scene 전역이므로 같은 Scene을 표시하는 두 Window가 서로 다른 Marker 도구를 동시에 활성화할 수 없다.

- 같은 Scene + 같은 도구: 기존 단일 세션 계약에 따라 재사용 또는 차단
- 같은 Scene + 다른 도구: 두 번째 진입 차단
- 다른 Scene + 다른 도구: 각 Scene에서 독립 허용

차단 메시지는 쉽게 표시한다.

```text
현재 Scene의 카메라 프레임 키를 다른 COLMAP 도구가 사용 중입니다.
해당 도구를 종료한 뒤 다시 시도하십시오.
```

## 11. 실패와 Rollback

### 11.1 Detach 실패

- 일부 Marker만 제거됐으면 Snapshot으로 모두 복원
- 이전 도구의 Marker 사용권 유지
- Workspace 전환 중단
- 다음 도구 시작 금지

### 11.2 Attach 실패

- 이번 Attach에서 만든 Marker만 역순 제거
- 사용자·외부 Marker 보존
- Marker 사용권 반납
- Source Workspace 유지 또는 복귀
- 기존 Manifest와 Object 데이터 보존

### 11.3 외부 Marker 충돌

사용자 Camera Marker가 대상 Frame에 있으면 기존처럼 Import를 중단한다. 이번 변경은 Modifier와 Projection Paint가 자기 Marker를 비활성 세션에서 내리도록 하는 것이며, 사용자 Marker를 자동 삭제하는 기능이 아니다.

## 12. 테스트 계획

### 12.1 Modifier

- Camera Review Marker 생성 후 Modifier 종료 시 Marker 0개
- 종료 후 Camera Object, Point Object, Manifest와 편집 Revision 유지
- Modifier 재진입 시 같은 IMAGE_ID·Frame·Camera 연결 복원
- Pending Camera와 Pending Undo 유지
- 비활성 상태에서 Undo/Redo 후 Marker가 다시 생성되지 않음
- 활성 상태에서 Marker 삭제·이동 시 Guard 복구
- Detach 중 오류 발생 시 Marker 전체 Rollback

### 12.2 Projection Paint

- Import 후 Workspace 종료 시 Projection Paint Marker 0개
- Camera Manifest, Paint Layer, Image와 Material 유지
- 재진입 시 Manifest 기반 Marker 복원
- 비활성 상태에서 Camera Guard가 Marker를 재생성하지 않음
- 활성 상태에서는 Camera Guard 복구 유지
- Import 실패 후 이전 Marker 세션과 Manifest 복원

### 12.3 교차 도구

아래 전환을 같은 Scene과 Frame 1 시작 데이터로 검증한다.

| 순서 | 필수 결과 |
| --- | --- |
| Modifier → Projection Paint | Modifier Marker 제거 후 Projection Import 성공 |
| Projection Paint → Modifier | Projection Marker 제거 후 Modifier Marker 복원 |
| Modifier → 일반 Workspace | Modifier Marker 없음, 데이터 보존 |
| Projection Paint → 일반 Workspace | Projection Marker 없음, Paint 데이터 보존 |
| Modifier → Projection Paint → Modifier | 양쪽 Manifest와 편집 상태 유지 |
| 직접 Workspace 탭 이동 | 공용 Handoff와 동일한 Detach·Attach 순서 |

### 12.4 사용자 Marker

- 겹치지 않는 사용자 Marker 보존
- Camera가 없는 일반 Marker 보존
- 겹치는 사용자 Camera Marker는 계속 Import 차단
- 이름이 COLMAP Marker와 비슷해도 소유 Metadata가 없으면 보존

### 12.5 저장·재로드와 다중 Window

- 일반 Workspace에서 저장·재로드 후 두 도구 Marker가 없는 상태 유지
- Modifier Workspace에서 저장·재로드 후 Modifier Marker만 복원
- Projection Paint Workspace에서 저장·재로드 후 Projection Marker만 복원
- 같은 Scene을 사용하는 두 Window의 다른 Marker 도구 동시 진입 차단
- 서로 다른 Scene에서는 독립 동작

### 12.6 실제 데이터 최종 회귀

현재 사용자 데이터로 다음 순서를 검증한다.

```text
1. Modifier Dataset 593 Camera Review 생성
2. Projection Paint로 전환
3. Scene의 Modifier Marker가 0개인지 확인
4. colmap_merged를 Frame Start 1로 Import
5. 외부 Camera Marker 충돌이 발생하지 않는지 확인
6. Projection Paint Camera Review와 F7 확인
7. Modifier로 복귀
8. Modifier Marker 593개와 현재 IMAGE_ID 복원 확인
9. Pending·Undo와 Modifier 편집 결과 확인
```

스크린샷만으로 통과시키지 않고 Marker Pointer, Camera 소유 Metadata, Manifest, Frame과 실제 Camera View를 함께 확인한다.

## 13. 예상 변경 파일

| 경로 | 변경 내용 |
| --- | --- |
| `shared/blender_runtime/scene_marker_session.py` | Scene 단위 Marker 사용권과 도구 Callback 계약 추가 |
| `shared/blender_runtime/workspace_handoff.py` | 이전 Marker Detach 완료 후 다음 도구 시작 순서 연결 |
| `tools/modifier/blender/camera_review_timeline.py` | Modifier Marker Attach·Detach·검증·Rollback 추가 |
| `tools/modifier/blender/modifier_workspace.py` | 종료 전 Detach, 재진입 후 Attach 연결 |
| `tools/modifier/blender/lifecycle.py` | Marker 세션 Adapter 등록·해제와 settled 조건 추가 |
| `tools/modifier/blender/state_handlers.py` | 활성 상태에 따른 Undo·Load Marker 복구 분기 |
| `tools/projection_paint/blender/camera_scene.py` | Marker Guard를 활성 Marker 세션에서만 실행 |
| `tools/projection_paint/blender/camera_import.py` | Attach 상태를 고려한 Import Transaction과 Rollback |
| `tools/projection_paint/blender/paint_workspace.py` | 종료 전 Detach, 재진입 후 Attach 연결 |
| `tools/projection_paint/blender/lifecycle.py` | Marker 세션 Adapter와 Load·Undo 복구 연결 |
| `tests/modifier/` | Modifier Marker 수명주기와 Undo 회귀 추가 |
| `tests/projection_paint/` | Projection Marker Guard와 Import 회귀 추가 |
| `tests/coexistence/` | 도구 간 직접 Import 금지와 공용 계약 검사 |
| `tests/blender_gui/` | 같은 Scene의 전체 Marker 전환 행렬 추가 |
| `docs/` | 구현 상태, 테스트 방법과 Release Note 갱신 |

## 14. 구현 순서

### 단계 0. 현재 실패 기준선 고정

- Modifier Marker가 남은 Scene에서 Projection Import 실패를 자동 테스트로 재현
- 사용자 Marker 충돌 테스트와 현재 Manifest 보존 테스트 유지
- 현재 사용자 Scene의 593 Marker 상태를 읽기 전용 Fixture 결과로 기록

### 단계 1. 공용 Scene Marker 사용권 구현

- 도구 독립 Callback Adapter 작성
- Scene UUID와 Window 점유 검사 추가
- Attach·Detach 대기와 오류 전파 테스트 작성

### 단계 2. Modifier Marker 세션화

- Timeline Manifest와 실제 Marker 분리
- 종료 Detach와 재진입 Attach 구현
- Pending·Undo·Camera Review 전체 회귀

### 단계 3. Projection Paint Marker 세션화

- Camera Guard 활성 조건 추가
- Workspace 종료 Detach와 재진입 Attach 구현
- Import Transaction과 기존 Manifest 교체 회귀

### 단계 4. Load·Undo·Legacy 마이그레이션

- 활성 Workspace 기준 Marker 복원
- 비활성 도구 Marker 제거
- 모호한 Legacy 상태 Fail Closed
- 저장·재로드와 Global Undo/Redo 검증

### 단계 5. 통합 GUI와 실데이터 검증

- Modifier ↔ Projection Paint 전체 전환 행렬
- 사용자 `colmap_merged` Frame Start 1 Import
- F7, Camera Review, Pending·Undo와 Paint Session 회귀
- 다중 Window와 직접 Workspace 탭 이동 검증

### 단계 6. 패키징과 실제 설치

- Manifest Patch Version 증가
- Production ZIP 빌드
- 격리 설치본과 Workspace GUI 검사
- 실제 Blender `user_default` 저장소 설치
- MCP로 설치 Version, Marker 수명주기와 실데이터 Import 최종 확인

## 15. 완료 조건

다음을 모두 만족해야 구현 완료로 판단한다.

1. Modifier에서 Projection Paint로 이동하면 Modifier Camera Marker가 Scene에서 제거된다.
2. Projection Paint에서 Modifier로 이동하면 Projection Paint Camera Marker가 Scene에서 제거된다.
3. 현재 활성 도구의 Marker만 Scene에 존재한다.
4. 일반 Workspace에서는 두 도구 Marker가 모두 존재하지 않는다.
5. Marker 제거 후에도 Camera Object, Point Object, Paint 데이터와 Manifest가 보존된다.
6. 재진입 시 IMAGE_ID, Frame과 Camera 연결이 원래대로 복원된다.
7. Modifier와 Projection Paint가 모두 Frame Start 1을 사용할 수 있다.
8. 사용자 Camera Marker 충돌은 계속 안전하게 차단된다.
9. 비활성 도구의 Guard나 Handler가 Marker를 다시 만들지 않는다.
10. Pending Camera, Modifier Undo와 Projection Paint Undo가 유지된다.
11. Save, Load, Undo/Redo와 Extension 재등록 뒤 Marker 상태가 활성 도구와 일치한다.
12. 같은 Scene의 다중 Window 충돌은 명확히 차단된다.
13. 도구 사이 직접 Python Import가 추가되지 않는다.
14. Background, GUI, 설치본과 사용자 실데이터 회귀가 모두 통과한다.
15. Debug 로그, 임시 Probe와 테스트 Marker가 배포물에 남지 않는다.

## 16. 최종 구조

```text
영구 보존
├─ COLMAP 데이터
├─ Camera Object
├─ Modifier 편집 결과와 Pending·Undo
├─ Projection Paint 데이터
└─ 도구별 Camera Marker Manifest

세션에서만 존재
└─ 현재 활성 도구의 실제 Scene Timeline Marker
```

이 구조는 Modifier와 Projection Paint를 하나의 Pipeline으로 합치는 것이 아니다. 두 독립 도구가 Blender의 Scene 전역 Camera Marker 자원을 동시에 사용하지 않도록 수명주기만 조정한다.
