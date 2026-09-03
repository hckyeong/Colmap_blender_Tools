# COLMAP Modifier 독립 Runtime·전용 Undo/Redo 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-09-02
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.3.33`
- 구현 버전: `0.3.34`
- 대상 기능: `COLMAP Modifier` 편집 권위, Undo/Redo, 표시 데이터, 저장 및 Runtime 수명주기
- 연관 기능: Projection Painting의 Blender Native Undo, Workspace Handoff, 공용 COLMAP 데이터 전달
- 제외 기능: COLMAP Align 내부 구현 변경, Projection Painting의 Paint/Layer Undo 변경
- 문서 상태: `0.3.34` 구현 완료

## 2. 최종 결정

COLMAP Modifier를 COLMAP Align과 같은 독립 편집 도메인으로 전환한다.

핵심 구조는 두 가지다.

1. Modifier의 COLMAP 데이터와 확정 편집 상태를 Blender Global Undo와 분리된 독립 Runtime이 소유한다.
2. Modifier의 Undo/Redo 작업 스택을 해당 Runtime 안에서 자체 관리한다.

```text
Modifier 독립 Runtime
├─ 원본 COLMAP Reconstruction
├─ 현재 편집 결과 Reconstruction
├─ 관계 Index
├─ 확정 EditOperation 목록
├─ Modifier 전용 Undo/Redo Journal
└─ Source·Revision·History Token

Blender Scene
└─ Runtime 결과를 보여주는 파생 표시 데이터
   ├─ Point 표시
   ├─ Camera/Frustum 표시
   ├─ Review 이미지와 현재 Camera View
   └─ Marker/Timeline 표시
```

Projection Painting이나 일반 Blender Workspace에서 Global Undo가 실행되어도 Modifier Runtime과 Modifier Undo/Redo Journal은 변경하거나 폐기하지 않는다.

완료 목표는 다음 두 단계로 구분한다.

1. **필수 데이터 격리:** Modifier 편집 데이터와 작업 스택이 Blender Global Undo에 의해 바뀌지 않는다. 파생 Blender ID의 표시용 Undo Step이 남더라도 Runtime 기준으로 다시 표시하며, Modifier 편집 결과에는 영향을 주지 못한다.
2. **조건부 물리 격리:** Point 선택, Camera Review, Timeline, 저장 Dirty 표시까지 Blender ID 변경 없이 제공할 수 있음이 검증된 경우에만 Modifier 관련 Global Undo Step 0개를 추진한다.

Align과 같은 수준이라는 표현은 우선 **편집 권위와 Undo/Redo 데이터의 격리**를 뜻한다. Align의 GPU 전용 표시 구현까지 그대로 복제하는 것은 Point Edit Mode의 Box/Circle/Lasso 선택 UX와 Blender 기본 저장 UX를 대체해야 하므로 별도 승인 Gate로 둔다.

앞선 계획의 다음 결론은 폐기한다.

- Modifier의 편집 권위를 `Scene.colmap_import`에 계속 두는 구조
- Modifier Operator의 Global Undo Step을 유지한 채 `Ctrl+Z` 입력만 분리하는 구조
- Global Undo 결과에 따라 Modifier 편집 Runtime을 폐기하고 재구성하는 구조
- Authority Token 비교만으로 두 Undo 도메인의 충돌을 완전히 해결할 수 있다는 가정
- 파생 Blender ID만 만들면 즉시 Global Undo Step을 모두 제거할 수 있다는 가정

Authority Token은 계속 사용하지만, 용도는 Runtime 내부 무결성과 파생 표시 데이터 재연결 검증으로 한정한다.

## 3. 쉬운 결론

현재는 한 번의 Modifier 편집이 두 곳에 걸쳐 기록된다.

```text
현재
Modifier 편집
├─ 애드온 edit_history_json 갱신
├─ Modifier Runtime 갱신
├─ Blender Mesh/Camera/Marker 갱신
└─ Blender Global Undo Step 생성
```

따라서 Projection Painting에서 Global Undo를 계속 실행하면 Modifier 작업까지 되돌릴 수 있다. Runtime Cache 유지 여부는 복구 속도만 바꿀 뿐, 이 Undo 충돌 자체를 막지 못한다.

목표 구조에서는 Modifier 작업을 한 곳에서만 관리한다.

```text
변경 후
Modifier 편집
├─ Modifier Runtime에 EditOperation 기록
├─ Modifier 전용 Undo/Redo Journal 갱신
└─ Runtime 결과를 화면에 다시 표시

Projection Painting Global Undo
├─ Projection Painting·일반 Blender 데이터만 변경
├─ Modifier Runtime과 Journal은 유지
└─ Modifier 표시가 영향받았으면 Runtime 기준으로 다시 표시
```

## 4. 현재 코드 기준 문제

### 4.1 독립 Runtime의 기반은 이미 존재한다

`tools/modifier/blender/runtime_state.py`의 `DatasetRuntimeState`는 다음 데이터를 이미 보유한다.

- 원본 `baseline_reconstruction`
- 현재 `current_reconstruction`
- 관계 `indexes`
- 확정 `operations`
- `dataset_uuid`
- `source_fingerprint`
- `edit_revision`
- `history_hash`

따라서 COLMAP 전체 데이터를 새로 한 벌 더 만드는 작업은 필요하지 않다. 기존 Runtime을 작업 권위로 승격하면 된다.

### 4.2 작업 명령 형식도 이미 존재한다

`tools/modifier/core/edit_history.py`에는 Point 제거와 Camera/Image 제거를 표현하는 `EditOperation`이 존재한다. `undo_last_edit()`도 마지막 Operation을 제외하고 기준선부터 이력을 재생한다.

새 Undo/Redo 구조는 Reconstruction 전체 Snapshot을 단계마다 복사하지 않고 이 `EditOperation`을 사용한다.

### 4.3 현재는 Scene Property가 동시에 권위 역할을 한다

`commit_edit()`는 Runtime뿐 아니라 다음 `Scene.colmap_import` 값도 즉시 갱신한다.

- `edit_history_json`
- `edit_revision`
- `history_hash`
- `last_operation_id`
- Source와 Dataset metadata
- Camera/Image/Point 수량

이 값은 Blender Global Undo에 의해 과거 상태로 돌아갈 수 있다. Runtime과 Scene Property가 동시에 권위 역할을 하므로 Global Undo 뒤 어느 쪽을 신뢰할지 다시 판정해야 한다.

### 4.4 Blender 표시 데이터도 편집 결과와 결합되어 있다

현재 `synchronize_scene()`은 Point Mesh, Camera, Collection, Review 상태와 Timeline Marker를 실제 Blender 데이터로 갱신한다. Modifier Operator의 `UNDO` 옵션을 지금 바로 제거하면 이 Blender 데이터 변경이 이전 Global Undo Step과 섞일 수 있다.

따라서 `UNDO` 제거보다 먼저 다음 분리가 필요하다.

```text
편집 권위: Modifier Runtime
표시 결과: Blender 파생 데이터 또는 GPU 표시
```

### 4.5 Global Undo가 Modifier Runtime을 무조건 폐기한다

`tools/modifier/blender/undo_domain_coordinator.py`의 Global Undo 사전 Handler는 현재 모든 임시 상태와 Core Runtime을 함께 폐기한다.

```text
Projection Painting Ctrl+Z
→ Blender undo_pre
→ Modifier Core Runtime 폐기
→ Modifier 재진입
→ Source 읽기·관계 Index 재생성·History 재생
```

독립 Runtime 구조에서는 Global Undo가 Modifier Runtime을 폐기할 이유가 없다.

## 5. 목표와 제외 범위

### 5.1 목표

1. Modifier의 편집 권위를 `DatasetRuntimeState` 기반 독립 Runtime으로 단일화한다.
2. Modifier Undo/Redo Journal을 Blender Global Undo와 완전히 분리한다.
3. Modifier의 `Ctrl+Z`, `Ctrl+Shift+Z`, `Ctrl+Y`가 전용 Journal만 사용한다.
4. Projection Painting과 일반 Workspace의 Blender Undo는 그대로 유지한다.
5. Projection Painting Global Undo가 Modifier 편집 결과·작업 스택·Core Runtime을 변경하지 못하게 한다.
6. Point, Camera, Review, Marker와 Timeline을 Runtime의 파생 표시 데이터로 전환한다.
7. Import·편집·Undo·Redo·Export는 Runtime을 기준으로 실행한다.
8. `.blend` 저장 시에만 작은 영구 상태를 기록하고 작업 중 Scene Property를 권위로 사용하지 않는다.
9. Runtime을 유지해 Workspace 재진입과 교차 Undo 뒤 COLD 복원을 제거한다.
10. 전체 Reconstruction을 Undo 단계마다 복사하지 않는다.
11. Point Edit Mode의 Box/Circle/Lasso 선택과 Camera Review UX를 필수 데이터 격리 단계에서 유지한다.
12. 표준 Blender 저장·미저장 표시와 자동 저장에서 Runtime 편집 유실이 없도록 Dirty 계약을 검증한다.

### 5.2 제외 범위

- Projection Painting Native Paint Undo의 독립 Runtime 전환
- Align Runtime 또는 UndoJournal 코드의 직접 공용화
- 사용자 Blender Global Undo 설정 변경
- Blender Undo Stack 초기화
- COLMAP 전체 Runtime Snapshot의 `.blend` 내장
- 원본 이미지 파일의 `.blend` 내장
- Redo Journal의 `.blend` 영구 저장
- 파일 시스템 Export 결과를 Undo로 되돌리는 기능
- 기존 파일에 대한 자동 데이터 마이그레이션
- 사용자 승인 전 Point 선택 UX를 GPU 전용 Picking으로 교체하는 작업
- 검증되지 않은 방식으로 Blender의 미저장 상태를 강제로 조작하는 작업

## 6. 권위 데이터 구조

### 6.1 Modifier Runtime Domain

기존 `DatasetRuntimeState`를 포함하는 실행 단위 `ModifierRuntimeDomain`을 둔다.

```text
ModifierRuntimeDomain
├─ domain_uuid
├─ dataset_uuid
├─ domain_generation
├─ transient_scene_binding
├─ dataset_state: DatasetRuntimeState
├─ journal: ModifierUndoJournal
├─ persistence_dirty
├─ display_revision
├─ session_generation
└─ last_status
```

`domain_uuid`는 Runtime과 저장 Payload의 안정적인 논리 식별자다. `Scene.as_pointer()`는 Undo·Load 뒤 바뀌거나 재사용될 수 있으므로 Domain Registry Key로 사용하지 않는다. Scene·Window Pointer는 현재 화면을 찾는 일시적인 Binding으로만 사용하고 Global Undo 뒤 다시 탐색한다.

다중 Scene을 지원하려면 `(domain_uuid, dataset_uuid)`별 Domain을 분리한다. 1차 구현에서 단일 활성 Domain만 지원한다면 다른 Scene에서 같은 Dataset을 여는 작업을 명시적으로 차단하고 UI에 이유를 표시한다. 현재 `_STATES[dataset_uuid]` 구조처럼 Dataset UUID만 Key로 사용해서는 안 된다.

### 6.2 Modifier Undo Journal

```text
ModifierUndoJournal
├─ undo_entries
├─ redo_entries
├─ transaction_state
├─ last_action
└─ last_error
```

각 Entry는 전체 Reconstruction이 아니라 작은 명령과 전후 이력만 보관한다.

```text
ModifierEditCommand
├─ action_name
├─ command_kind               # APPEND_OPERATION 또는 RESET_HISTORY
├─ operation                  # 일반 Point/Camera 확정 편집 한 건
├─ inverse_delta              # 변경된 Record·Index 범위의 역방향 Delta
├─ reset_history_before       # Reset 전용, 필요할 때만 Operation Tuple 한 번
├─ dataset_uuid
├─ source_fingerprint
└─ before/after revision·history_hash
```

일반 Command마다 누적 `before_operations`와 `after_operations` 전체를 보관하지 않는다. 이 방식은 편집 횟수가 늘면 Tuple Pointer 메모리가 제곱으로 증가할 수 있다.

현재 `EditOperation`은 제거한 ID만 보관하므로 빠른 Undo를 위해 다음 Core 변경을 먼저 검증한다.

- Baseline에서 복구할 Record ID와 현재 편집에서 바뀐 Survivor Record의 이전값
- `point_to_images`, `image_to_points`, `camera_to_images`, `image_to_frame`의 변경 범위
- Undo 실패 시 Forward 상태로 복귀할 수 있는 검증된 역방향 Delta

대안으로 Tombstone/Persistent Collection을 검토할 수 있지만, 최종 구현은 다음 조건을 만족하는 방식 하나로 고정한다.

- Undo/Redo 중 Source 재읽기 0회
- 관계 Index 전체 재구축 0회
- Reconstruction과 Index 전체 복사 0회
- Command 메모리는 실제 변경 범위에 비례
- 실패 시 이전 Runtime으로 원자 Rollback

`Reconstruction`과 `RelationshipIndexes`는 현재 일반 `dict`를 포함하므로 이름만 불변인 상태가 아니다. Domain 설치 뒤 외부 코드의 제자리 변경을 금지하고, Core Transaction만 변경할 수 있도록 읽기 전용 View 또는 명시적 Mutation Gateway를 둔다.

### 6.3 Scene Property의 역할 변경

`Scene.colmap_import`는 다음 역할만 유지한다.

- 패널에 표시할 수량·상태·진행률 Mirror
- 파일 선택과 Export 옵션 같은 사용자 입력
- Point Scale, Camera Frustum Size 같은 표시 설정
- 현재 Session의 임시 Mode 상태

다음 값은 작업 중 권위가 아니라 Runtime Mirror로 바꾼다.

- `edit_history_json`
- `edit_revision`
- `history_hash`
- `last_operation_id`
- Source·Dataset의 확정 식별값

Global Undo 뒤에는 Scene Property를 Runtime에 적용하지 않고 Runtime 기준으로 Mirror를 다시 쓴다.

다음 현재 의존성도 함께 Domain Token 기준으로 전환해야 한다.

- `runtime_state.py`의 `runtime_restore_token()`, `cached_runtime_matches_scene()`, `ensure_runtime()`
- `projection_snapshot_provider.py`의 Snapshot 생성 중 Token 검증과 Currentness 판정
- `dataset_adapter.py`와 Legacy `Scene.colmap_dataset` Revision 갱신
- `modifier_workspace.py`, `panels.py`, Import/Export Progress의 Runtime 준비 판정
- Point/Camera Edit·Review·Filter Session의 Scene Revision 비교

`Scene.colmap_dataset`은 ADR-0008에 따라 Legacy Fail-Closed 입력이다. 신규 편집 경로에서 계속 Revision 권위로 갱신하지 않으며, Legacy 복구 입력과 UI Mirror 역할을 분리한다.

## 7. 파생 표시 데이터 구조

### 7.1 기본 원칙

Point Mesh, Camera, Frustum, Review Marker와 Timeline Marker는 사용자가 선택할 수 있지만 Modifier 편집의 원본은 아니다.

표시 요소에는 Runtime ID를 연결한다.

- Point 선택 → `POINT3D_ID` 수집
- Camera 선택 → `IMAGE_ID` 수집
- 제거·필터 명령 → 선택 ID를 Runtime Transaction에 전달
- Transaction 성공 → Runtime 결과 기준으로 표시 갱신

### 7.2 편집 흐름

```text
사용자가 Point 선택
→ 표시 Mesh에서 POINT3D_ID 수집
→ Runtime에 RemovePoints EditOperation 생성
→ Runtime Transaction 검증·Commit
→ Modifier Journal에 명령 기록
→ Point/Camera/Review 표시 갱신
```

사용자 UX는 지금과 같지만 실제 편집은 Blender Mesh나 Camera를 원본으로 삼지 않는다.

### 7.3 직접 Blender 편집 처리

다음 조작은 Modifier 데이터 편집으로 인정하지 않는다.

- 관리 Point Mesh의 직접 Vertex 삭제 또는 이동
- 관리 Camera Object의 직접 삭제 또는 Transform 변경
- 관리 Timeline Marker의 직접 이름·Frame 변경

현재처럼 관리 데이터 직접 변경을 차단하거나 Runtime 기준으로 복원하고, 사용자는 Modifier 전용 편집 명령을 사용한다.

### 7.4 표시 구현 단계

표시 구현은 데이터 격리와 물리적 Undo Step 제거를 구분해 전환한다.

1. **필수 파생 Blender ID 단계**
   - 기존 Point Mesh·Camera·Marker를 유지하되 권위에서 제외한다.
   - Global Undo가 표시 ID를 변경하면 Runtime 기준으로 재동기화한다.
   - Blender ID를 실제 변경하는 Operator와 후속 Timer/Handler의 `UNDO` 계약은 유지한다.
   - 이 단계는 Modifier 데이터와 Journal을 보호하지만 Global Undo History에서 표시용 Step까지 제거하지는 않는다.
   - 기존 Mesh Edit Mode의 Box/Circle/Lasso, Camera Object 선택과 Timeline 탐색 UX를 그대로 유지한다.
2. **조건부 독립 표시 단계**
   - 대량 Point와 Camera Frustum은 GPU Draw/Picking으로 이동한다.
   - Review Camera와 이미지 배경은 Session 소유 View/Overlay로 이동한다.
   - Timeline 표시는 Runtime ID 기반 전용 동기화 경계로 분리한다.
   - 편집 Operator가 Undo 대상 Blender ID를 더 이상 변경하지 않을 때 `UNDO`를 제거한다.
   - GPU Picking은 단일 선택뿐 아니라 Box/Circle/Lasso, 선택 해제, 가시성, 성능과 단축키 호환을 모두 제공해야 한다.

1단계부터 Modifier 편집 권위와 작업 스택은 Global Undo와 분리된다. 2단계는 기능 동등성, Blender 저장 Dirty 계약과 공식 `UNDO` 계약을 모두 만족할 때만 사용자 승인 후 진행한다. 조건을 충족하지 못하면 표시용 Step은 유지하되 그 Step이 Modifier Runtime 데이터에는 영향을 주지 못하도록 하는 상태를 제품 완료 기준으로 사용한다.

## 8. Undo/Redo 동작

### 8.1 편집 Commit

```text
1. 현재 Runtime Token 검증
2. 선택 ID로 EditOperation 생성
3. 변경 Record·Index 범위와 Inverse Delta 생성
4. 새 History Revision·Hash 검증
5. Runtime Mutation Gateway에서 Forward Delta 원자 적용
6. Undo Journal에 EditOperation과 Inverse Delta 기록
7. Redo Branch 폐기
8. Scene Property Mirror와 표시 데이터 갱신
9. 저장 필요 상태 표시
```

표시 갱신이 실패하면 Runtime, Journal과 표시를 이전 상태로 되돌린다. 부분 Commit을 성공 상태로 남기지 않는다.

### 8.2 Undo

```text
1. Modifier Journal의 마지막 Command 확인
2. Dataset UUID·Source Fingerprint 검증
3. 현재 Revision·Hash와 Command의 After Token 검증
4. Inverse Delta를 변경 범위에만 적용
5. Reconstruction·Index 부분 무결성 검증
6. Command를 Redo Stack으로 이동
7. 표시와 UI Mirror 갱신
```

Undo할 작업이 없으면 Blender Global Undo로 통과시키지 않고 `되돌릴 Modifier 편집이 없습니다.`를 표시한다.

### 8.3 Redo

```text
1. Modifier Journal의 마지막 Redo Command 확인
2. 현재 Revision·Hash가 Command의 Before Token인지 검증
3. EditOperation의 Forward Delta를 변경 범위에만 재적용
4. Reconstruction·Index 부분 무결성 검증
5. Runtime Revision·History Hash 갱신
6. Command를 Undo Stack으로 이동
7. 표시와 UI Mirror 갱신
```

Redo할 작업이 없으면 `다시 적용할 Modifier 편집이 없습니다.`를 표시한다.

### 8.4 새 Branch

Undo 후 새 Point 제거, Camera 제거 또는 확정 Filter를 실행하면 Redo Stack을 폐기한다. 이는 일반 편집기의 선형 Undo 규칙과 같다.

### 8.5 Reset·Import·Export Barrier

- `Reset Edits`: 현재 Session에서만 되돌릴 수 있는 `RESET_HISTORY` Command로 기록하고 Reset 전 Operation Tuple을 한 번만 보관한다. 저장·재실행 뒤에는 Reset 이전 Journal을 복원하지 않는다.
- 같은 Source 재Import: 현재 코드의 History 보존 의미를 유지할지 새 Baseline으로 전환할지 구현 전 계약 테스트로 확정한다. 무조건 새 Domain으로 처리하지 않는다.
- 다른 Dataset Import: 성공 시 새 Domain을 설치하고 기존 Journal을 종료한다.
- Export 성공: 디스크 결과를 새 Baseline으로 설치하고 기존 History와 Journal을 초기화한다.
- Source Overwrite: Export 직전 Domain Token을 재검증한다. 디스크 반영 뒤 Runtime Baseline 설치가 실패하면 편집을 동결하고 재Import를 요구한다.
- Import·Export 실패 또는 취소: 현재 Domain과 Journal을 유지한다.

### 8.6 Pending Camera Filter

확정되지 않은 Pending Filter는 Review Draft로 유지한다. `Apply Pending Filters`가 성공해 `EditOperation`이 생성된 시점부터 Modifier Journal에 들어간다.

Pending 상태에서 `Ctrl+Z`를 눌렀을 때는 기존 `Undo Last Filter` 사용 안내를 표시하고 확정 Journal은 변경하지 않는다. 향후 Pending까지 하나의 시간순 Journal에 통합하는 작업은 별도 범위다.

1차 Modifier Journal이 되돌리는 대상은 확정 Point 제거, 확정 Camera/Image 제거, 확정 Pending Filter 적용과 Reset뿐이다. 다음 상태는 Journal에 넣지 않는다.

- Point/Camera 선택과 활성 Object
- Point Scale·Camera Frustum Size·Axis 표시 설정
- Camera 배경 표시와 현재 Review Frame
- 확정 전 Pending Filter Queue
- Import/Export 파일 시스템 작업

UI에는 `Ctrl+Z`가 마지막 **확정 Modifier 편집**을 되돌린다는 범위를 명시한다.

## 9. 입력 격리

### 9.1 Modifier Input Guard

Align의 `session_guard.py`와 같은 역할을 Modifier가 독립적으로 구현한다. Align 모듈을 직접 Import하거나 Runtime을 공유하지 않는다.

| 입력 | Modifier Workspace | 다른 Workspace |
| --- | --- | --- |
| `Ctrl+Z` | Modifier 전용 Undo | Blender 기본 Undo |
| `Ctrl+Shift+Z` | Modifier 전용 Redo | Blender 기본 Redo |
| `Ctrl+Y` | Modifier 전용 Redo | Blender 기본 Redo |
| `Ctrl+Alt+Z` | Blender Undo History 차단 안내 | Blender 기본 동작 |

Input Guard는 Modifier 소유 Window·Workspace·Screen이 유효할 때만 입력을 소비한다. File Browser, 확인 Dialog, 다른 Window와 Session 밖에서는 `PASS_THROUGH`한다.

전용 Journal이 비어 있어도 Modifier Workspace의 `Ctrl+Z`를 Blender Global Undo로 통과시키지 않는다. `되돌릴 Modifier 편집이 없습니다.`를 표시하고 입력을 소비한다.

Input Guard는 Edit 메뉴, Operator Search, Python에서 직접 호출한 `bpy.ops.ed.undo()`까지 막을 수 없다. 따라서 입력 가드는 UX Router일 뿐 보안 경계로 취급하지 않으며, Global Undo Handler는 어떤 진입 경로에서도 Modifier Domain을 유지하고 모든 오래된 Blender ID 참조를 해제·재탐색해야 한다.

### 9.2 작업 잠금

Import, Export, Runtime Restore 또는 편집 Transaction 중에는 전용 Undo/Redo를 시작하지 않는다. Global Undo로 통과시키지도 않고 정확한 잠금 사유를 표시한다.

## 10. Global Undo 처리

### 10.1 변경 원칙

Global Undo는 Modifier Runtime Domain을 삭제하거나 수정하지 않는다.

```text
undo_pre
├─ 진행 중 Modifier Transaction이 있으면 안전하게 중단
├─ Blender ID Pointer 기반 Session만 해제
└─ Modifier Runtime·Undo/Redo Journal 유지

undo_post
├─ Modifier 비활성: 표시 재연결 필요 Flag만 기록
└─ Modifier 활성: Runtime 기준으로 UI Mirror·표시 데이터 재연결
```

### 10.2 Authority Token 용도

Global Undo로 복원된 Scene Property와 Runtime Token을 비교해 어느 쪽을 권위로 선택하지 않는다. Runtime이 항상 권위다.

Token 비교는 다음만 검증한다.

- 재연결하려는 표시 데이터가 같은 Dataset인지
- Runtime 내부 Revision과 History Hash가 일치하는지
- Source가 외부에서 실제 변경되었는지
- 오래된 Blender ID Pointer를 재사용하고 있지 않은지

### 10.3 Runtime 폐기 조건

다음 명시적 경계에서만 Runtime Domain을 폐기한다.

- 새 `.blend` Load
- Extension Unregister
- 사용자가 다른 Dataset Import를 승인하고 Import가 성공함
- 사용자가 Dataset 초기화를 명시적으로 실행함
- Source 재연결 결과가 기존 Source와 다른 Dataset으로 확정됨

Workspace 이동과 Projection Painting Global Undo는 폐기 조건이 아니다.

무결성 오류는 다음처럼 구분한다.

- 파생 Mesh·Camera·Marker 또는 Pointer 불일치: Blender 표시 세션만 폐기하고 Runtime 기준으로 다시 생성
- Domain Revision·History·Index 불일치: Runtime과 저장 Payload를 삭제하지 않고 편집을 동결한 뒤 복구 또는 재Import를 안내
- Source Fingerprint 변경: 기존 Domain을 보존한 채 Source 재연결/새 Dataset Import 선택을 요구

유일한 편집 권위인 Runtime을 오류 처리 과정에서 자동 삭제하지 않는다.

## 11. 저장과 재실행

### 11.1 저장 데이터

Align의 저장 경계를 참고해 저장 직전에 작은 영구 상태를 Blender 소유 Text 데이터블록에 기록한다. 단, `Text`도 Blender `ID`이므로 Global Undo 밖의 저장소로 간주하지 않는다. 실행 중 최신 권위는 Runtime이고, Text는 다음 저장을 위한 검증 가능한 Persistence Mirror다.

```text
ModifierProjectState
├─ schema_version
├─ domain_uuid·domain_generation
├─ dataset_uuid
├─ Source 경로·형식·Fingerprint
├─ 확정 EditOperation 목록
├─ edit_revision
├─ history_hash
├─ payload_checksum
├─ 이미지 폴더 경로
└─ 필요한 표시 설정
```

COLMAP Reconstruction 전체와 관계 Index는 저장하지 않는다. 기존의 대형 Runtime Snapshot 저장 계획은 다시 도입하지 않는다.

저장하는 작업 과정은 확정된 다음 상태로 한정한다.

- Point 제거 `EditOperation`
- Camera/Image 제거 `EditOperation`
- 적용이 완료된 Camera Filter의 확정 `EditOperation`
- 현재 Operation 순서, `edit_revision`과 `history_hash`
- 자동 Source 복구에 필요한 경로·형식·Fingerprint

다음 실행 중 상태는 저장하지 않는다.

- Point/Camera의 현재 선택 상태와 활성 Object
- 현재 Review Frame과 Camera 배경의 임시 연결
- 적용 전 Pending Camera Filter
- 진행 중 Import/Export/편집 Transaction
- Runtime 전용 Redo Stack

### 11.2 저장 시점

- 편집 성공: Runtime의 `persistence_dirty=True`
- Global Undo 뒤: Text/Scene Mirror가 과거 세대로 바뀌었을 수 있으므로 Runtime Generation과 다르면 Stale로 판정
- Blender 저장 직전: Runtime에서 불변 저장 Snapshot 생성
- Text 데이터블록에 Staging 작성·Schema·Checksum·Generation 검증
- 검증 성공 뒤에만 Active owner를 마지막 단계에서 교체
- `save_post`: 저장된 Active Generation을 재검증한 뒤 `persistence_dirty=False`
- `save_post_fail` 또는 저장 결과 불명확: Dirty 유지, Staging 정리와 명확한 오류 표시

작업 중 매 편집마다 Text를 다시 쓰지 않는다.

순수 Python Runtime 변경만으로 Blender의 표준 미저장 표시가 반드시 활성화된다는 공개 계약은 없다. 반대로 매 편집마다 Scene/Text Dirty Token을 쓰면 Blender ID 변경과 `UNDO` 계약이 남는다. 따라서 구현 전에 다음 제품 계약을 실제 Blender에서 검증한다.

1. **기본안:** 표준 Blender 저장·종료 확인 UX를 보존하기 위해 작은 비권위 Dirty Mirror를 갱신하고, 해당 표시용 `UNDO` Step은 허용하되 Modifier 데이터는 포함하지 않는다.
2. **조건부 완전 0-Step안:** Blender ID를 변경하지 않고도 저장 필요 상태와 종료 보호를 신뢰성 있게 제공할 수 있을 때만 채택한다.

Dirty Mirror가 Global Undo로 되돌아가더라도 Runtime의 `persistence_dirty`와 Domain Generation을 변경하지 않는다. 다음 저장 때 Runtime이 Text와 Mirror를 다시 덮어쓴다.

### 11.3 재실행

`.blend`를 다시 열면 저장된 Source metadata와 EditOperation을 읽어 기존 지연 Runtime 복원 경로로 복구한다. Redo Stack은 저장하지 않으므로 비어 있는 상태로 시작한다.

`load_post`는 Active Payload 검증 → Source 읽기 → Operation Replay → Undo용 변경 범위 Delta 재생성 → Domain 원자 설치 → UI/표시 Mirror 생성 → Redo 초기화 순서를 따른다. 중간 실패 시 Active Payload를 삭제하거나 빈 상태로 덮어쓰지 않고 편집을 동결해 복구·재Import 선택을 제공한다.

재실행 시 Source 읽기는 사용자가 Import 버튼을 다시 누르는 작업이 아니다. 저장된 절대 Source 경로와 Fingerprint를 사용한 자동 복구 작업이다. Source가 이동했으면 사용자에게 새 원본 COLMAP 폴더 지정을 요청하고, 파일명·구조·Fingerprint 검증을 통과한 경우에만 복구를 계속한다.

Operation Replay는 최종 Reconstruction만 만드는 것으로 끝내지 않는다. 각 확정 Operation을 순서대로 적용하면서 같은 Session에서 사용할 Inverse Delta와 Undo Command를 다시 생성한다. 따라서 재실행 뒤에도 저장된 확정 편집은 Undo할 수 있지만, 저장 시점에 이미 비어 있거나 Runtime 전용이었던 Redo Stack은 복원하지 않는다.

Runtime이 아직 복원되지 않았거나 복원에 실패한 상태에서 사용자가 저장해도 기존 Active Payload를 빈 상태로 교체하지 않는다. Pending Filter, Point/Camera 선택, 현재 Review Frame처럼 Payload에서 제외한 임시 상태는 재실행 시 폐기된다는 점을 UI와 테스트에 명시한다.

자동 데이터 마이그레이션은 만들지 않는다. 기존 저장 데이터가 새 계약과 호환되지 않으면 자동 변환하거나 조용히 초기화하지 않고 사용자에게 Dataset 재Import 또는 초기화를 요청한다.

## 12. Import·Export·공용 데이터

### 12.1 Import

```text
COLMAP Import
→ Source 읽기·검증
→ 기존 Domain과 Source Identity 비교
→ 새 Domain 생성 또는 검증된 같은 Source 재Import Transaction
→ Baseline·Current Reconstruction과 Index 설치
→ 빈 Journal 생성
→ 표시 데이터 생성
```

다른 Dataset Import는 새 Undo Domain을 여는 경계이며 이전 Dataset의 Undo/Redo를 이어받지 않는다. 같은 Source 재Import는 현재 `prepare_import_state()`가 History를 보존하는 동작이 있으므로, 단계 0에서 사용자 의미를 확정한 뒤 History 보존 또는 새 Baseline 전환 중 하나를 구현한다. 두 의미를 Source 경로만으로 추측하지 않는다.

최초 Import 결과를 Blender Scene 데이터로 만든 뒤 다시 Runtime으로 복사하는 중간 단계는 두지 않는다. Loader가 검증한 Reconstruction과 Relationship Index를 즉시 새 `ModifierRuntimeDomain`에 설치하고, Blender Point·Camera·Marker는 그 Runtime에서 파생해 생성한다. 즉, 독립 Runtime으로의 승격은 별도 대용량 복사가 아니라 Import Commit 자체다.

### 12.2 Export

Export는 Blender Scene 표시물이 아니라 Modifier Runtime의 현재 Reconstruction을 읽는다.

성공 시 현재 코드의 `state_from_successful_export()` 의미를 유지한다.

- Export 결과를 새 Baseline Runtime으로 설치
- 기존 EditOperation History와 Undo/Redo Journal 초기화
- 새 Source Token 설치
- 공용 COLMAP 데이터 Provider 갱신

Export 실패 또는 취소 시 Runtime과 Journal을 변경하지 않는다. 디스크 파일은 Undo 대상이 아니다.

### 12.3 Projection Painting 데이터 업데이트

Projection Painting의 `COLMAP 데이터 업데이트`는 Modifier Runtime의 불변 Dataset Snapshot을 복사한다. Modifier Runtime이나 Journal을 공유하지 않는다.

따라서 이후 Modifier 편집과 Projection Painting Undo는 서로 영향을 주지 않는다. Projection Painting에서 다시 업데이트를 눌렀을 때만 최신 Modifier 결과를 새 Snapshot으로 가져온다.

현재 `projection_snapshot_provider.py`와 Projection Painting의 `snapshot_update.py`는 Scene Revision/Source Token을 Currentness 판정에 사용한다. 이를 다음 Domain 복합 Identity로 바꾼다.

```text
(domain_uuid, domain_generation, dataset_uuid,
 source_fingerprint, edit_revision, history_hash)
```

Export 성공 뒤 `edit_revision`이 0으로 돌아가므로 Revision 하나만 전역 단조 Token으로 사용하지 않는다. Snapshot 생성 시작과 완료에서 같은 Domain Identity를 검증하고, 이미 복사된 Projection Painting Snapshot은 이후 Modifier Undo/Redo와 무관하게 유지한다.

## 13. 성능과 메모리

### 13.1 예상 영향

| 항목 | 예상 변화 | 이유 |
| --- | --- | --- |
| 최초 Import | 거의 동일 | 기존 Import가 이미 `DatasetRuntimeState`를 생성함 |
| 일반 Point/Camera 편집 | 계측 필요 | 현재 Indexed 제거가 큰 Dict를 복사하므로 Delta/Tombstone Core 변경 영향 측정 필요 |
| Modifier Undo | 개선 목표 | Baseline 전체 Replay 대신 변경 범위 Inverse Delta 사용 |
| Modifier Redo | 개선 목표 | Source 재읽기 없이 변경 범위 Forward Delta 사용 |
| Workspace 재진입 | 개선 | 유지된 Runtime을 즉시 사용함 |
| Projection Painting Undo 뒤 재진입 | 크게 개선 | Modifier COLD 복원과 관계 Index 재생성을 제거함 |
| 메모리 | 변경 범위만 증가 | Journal이 누적 Tuple이나 전체 Runtime이 아닌 Operation·Delta만 보관함 |
| `.blend` 저장 | 소폭 증가 또는 유사 | 작은 Operation JSON을 저장 시점에만 기록함 |

### 13.2 금지할 성능 구조

- Undo 단계마다 전체 Reconstruction 복사
- Undo 단계마다 전체 관계 Index 복사
- Undo 단계마다 Point Mesh 전체 중복 보관
- Workspace 이동마다 Runtime 직렬화·역직렬화
- Projection Painting Global Undo마다 Modifier Source 재해석

### 13.3 성능 유지 원칙

- EditOperation 객체는 Undo/Redo Stack 간 이동하며 중복 복사하지 않는다.
- 누적 Operation Prefix Tuple을 Command마다 보관하지 않는다.
- 일반 편집의 전체 Dict 복사를 Delta/Tombstone Core로 줄인다.
- 표시 갱신은 변경된 Point/Camera 범위를 우선 사용한다.
- 전체 Scene 동기화가 필요한 경우에만 명시적으로 실행한다.
- Undo/Redo에서 Baseline 전체 Replay를 허용하지 않는다.

### 13.4 성능 계측 Matrix

단일 평균값 대신 소형, 중형과 사용자 실데이터 약 70만 Point Dataset에서 다음을 기록한다.

- Warm 편집·Undo·Redo의 Median과 p95
- Source read/hash 횟수
- 전체 Relationship Index build 횟수
- 변경 범위 Delta 적용 시간
- Point/Camera 표시 재생성 및 GPU redraw 시간
- Process Peak RSS
- Journal Command 20회·100회 뒤 추가 메모리
- Reset·Export 성공 뒤 Baseline 전환 시간

절대 승인 시간은 단계 0 기준선 측정 결과와 함께 문서에 수치로 고정한 뒤 Core 구현을 시작한다. 고정 전에는 단순한 `10% 이내`만으로 성능 완료를 선언하지 않는다.

## 14. Blender `UNDO` 옵션과 물리적 Step 제거 Gate

Blender 공식 문서는 Blender 데이터를 변경하는 Operator가 `UNDO`를 사용해야 한다고 명시한다. 따라서 `UNDO` 옵션을 먼저 일괄 삭제하지 않는다.

- <https://docs.blender.org/api/5.2/bpy.types.Operator.html#modifying-blender-data-undo>
- <https://developer.blender.org/docs/features/core/undo/>
- <https://developer.blender.org/docs/features/interface/operators/>

각 Operator와 그 Operator가 시작한 Timer/Handler는 다음 순서로 전환한다.

1. 편집 권위를 Runtime으로 이동한다.
2. Blender ID 변경을 파생 표시 갱신으로 분리한다.
3. 표시 갱신을 GPU/Overlay 또는 검증된 Blender ID Transaction 경계로 이동한다.
4. Scene, Text, Mesh, Object, Camera, Image, Marker, View Layer, 선택과 Mode를 포함해 Undo 대상 Blender 상태를 변경하지 않는지 검사한다.
5. GUI Global Undo 회귀를 통과한 Operator만 `UNDO`를 제거한다.

목표 Operator는 다음과 같다.

- `colmap.remove`
- `colmap.undo_last_edit`
- 새 `colmap.redo_last_edit`
- `colmap.reset_edits`
- `colmap.review_apply_pending_filters`
- `colmap.import`

필수 데이터 격리 단계에서는 표시와 Dirty Mirror용 `UNDO` Step이 남을 수 있지만 Modifier 편집 권위나 Journal을 포함하지 않아야 한다. Projection Painting Global Undo가 이 Step을 되돌려도 Runtime 기준으로 표시만 복원한다.

Global Undo Step 0개는 Point/Camera 선택 UX와 표준 저장 Dirty 계약까지 Blender ID 변경 없이 제공할 수 있음을 실제 Blender에서 증명한 경우에만 조건부 완료 목표로 승격한다. 이 증명이 없으면 `UNDO`를 억지로 제거하지 않는다.

## 15. UI와 사용자 경험

### 15.1 Modifier 편집

```text
1. 사용자가 Point 또는 Camera를 선택
2. Remove 또는 Filter 적용
3. 화면 데이터가 즉시 갱신
4. Modifier 전용 Undo 가능 상태 표시
5. Ctrl+Z로 마지막 Modifier 편집만 복구
6. Ctrl+Shift+Z로 같은 편집 재적용
```

### 15.2 패널

기존 `Undo Last Edit` 버튼은 전용 Journal Router를 호출한다. `Redo Last Edit` 버튼을 추가한다.

```text
Edit History
├─ [Undo Last Edit] [Redo Last Edit]
└─ Revision 3 · Undo 3 · Redo 1
```

### 15.3 Workspace 왕복

```text
Modifier에서 Point 제거
→ Projection Painting으로 이동
→ Paint Stroke와 Blender Ctrl+Z 실행
→ Modifier로 복귀
→ 기존 Modifier Runtime과 작업 이력 즉시 복구
→ Ctrl+Z: 마지막 Modifier Point 제거만 되돌림
```

Projection Painting의 Paint, Layer, Texture와 Stencil 결과는 Modifier Undo로 변경되지 않는다.

### 15.4 저장·재실행

```text
깨끗한 Blender에서 COLMAP Import
→ Import 결과를 추가 복사 없이 독립 Runtime으로 즉시 설치
→ Modifier 편집과 전용 Undo/Redo 사용
→ Blender 저장
→ 확정 Source/Operation 상태만 .blend에 기록
→ Blender 종료
→ .blend 재실행
→ 저장된 Source를 자동으로 읽고 검증
→ 확정 Operation 순서대로 재적용
→ Undo용 Inverse Delta와 Command 재생성
→ 완성된 독립 Runtime 원자 설치
→ Point·Camera·Review 표시 재생성
```

사용자가 재실행 뒤 Import 버튼을 다시 누를 필요는 없다. 원본 Source가 없거나 바뀌었으면 자동으로 다른 경로를 추측하지 않고 사용자에게 재연결 또는 Dataset 재Import를 요청한다. Redo Stack은 재실행 뒤 비워진다.

## 16. 예상 변경 파일

| 경로 | 변경 내용 |
| --- | --- |
| `tools/modifier/core/edit_journal.py` | 불변 Command, Undo/Redo Branch와 Token 검증 |
| `tools/modifier/core/edit_delta.py` | 변경 Record·Index 범위의 Forward/Inverse Delta와 원자 적용 |
| `tools/modifier/core/edit_replay.py` | Cold Load Operation Replay와 Delta 결과 검증 경계 |
| `tools/modifier/core/model.py` | 설치된 Domain Model의 제자리 변경 금지 또는 읽기 전용 View |
| `tools/modifier/core/removal.py` | 전체 Dict 복사 대신 검증된 변경 범위 계산 |
| `tools/modifier/blender/modifier_runtime_domain.py` | Scene별 독립 Runtime Domain Registry |
| `tools/modifier/blender/runtime_state.py` | Dataset Runtime을 Domain 권위로 승격 |
| `tools/modifier/blender/edit_workflows.py` | Runtime 우선 Staging·Commit·Rollback |
| `tools/modifier/blender/modifier_input_guard.py` | Modifier 전용 Ctrl+Z/Redo 입력 처리 |
| `tools/modifier/blender/undo_domain_coordinator.py` | Global Undo에서 Core Runtime 유지, 표시 참조만 재연결 |
| `tools/modifier/blender/state_handlers.py` | Load·Save·Undo/Redo 수명주기 변경 |
| `tools/modifier/blender/modifier_project_store.py` | 작은 Source/Operation Text 저장 계약 |
| `tools/modifier/blender/projection_snapshot_provider.py` | Scene Token 대신 Domain 복합 Identity로 Snapshot 검증 |
| `tools/modifier/blender/dataset_adapter.py` | 신규 Runtime 권위에서 Legacy 공용 Dataset Revision 쓰기 제거 |
| `tools/modifier/blender/edit_coordinator.py` | Runtime 권위 기반 파생 표시 동기화 |
| `tools/modifier/blender/point_cloud_view.py` | Point 표시와 Picking의 파생 데이터화 |
| `tools/modifier/blender/camera_review_session.py` | Camera/Review 표시의 Runtime ID 기반 재구성 |
| `tools/modifier/blender/camera_review_timeline.py` | Timeline Marker를 파생 표시로 전환 |
| `tools/modifier/blender/operators_point_selection.py` | Undo/Redo Router와 Operator 옵션 단계적 전환 |
| `tools/modifier/blender/operators_camera_review.py` | 확정 Filter의 Journal 연결 |
| `tools/modifier/blender/operators_import.py` | 새 Runtime Domain 생성 경계 |
| `tools/modifier/blender/export_completion.py` | Export 성공 뒤 Domain Baseline 재설정 |
| `tools/modifier/blender/panels.py` | Undo/Redo 상태와 버튼 표시 |
| `tools/modifier/blender/lifecycle.py` | Guard, Domain과 저장 Handler 등록·해제 |
| `tools/projection_paint/blender/snapshot_update.py` | Domain Snapshot Identity 기반 업데이트 완료 검증 |
| `shared/colmap_transfer/contracts.py` | Domain Generation·History Identity 전달 계약 보강 |
| `tests/modifier/core/` | Journal·Replay·Branch·Memory 계약 검사 |
| `tests/modifier/blender_background/` | Runtime 권위·표시 파생·저장 경계 검사 |
| `tests/modifier/blender_gui/` | 실제 단축키·Global Undo·Workspace 교차 회귀 |
| `tests/architecture/` | Runtime의 Blender ID 참조와 Global Undo 의존 금지 검사 |
| `docs/decisions/` | ADR-0008의 Modifier Undo 결정을 대체하는 새 ADR 추가 |
| `docs/architecture/` | Modifier 독립 Runtime·Persistence Mirror 제품 경계 갱신 |

Projection Painting의 Paint/Layer/Texture/Stencil 구현과 Align Source는 수정하지 않는다. Projection Painting 변경은 중립 Snapshot Identity 검증에 한정한다.

ADR-0008은 수동 독립 Snapshot 경계는 그대로 유효하지만 `Modifier에 별도 Ctrl+Z Stack을 만들지 않는다`는 과거 결정을 포함한다. 이번 사용자 결정은 Modifier에 한해 이를 대체하므로 기존 ADR 원문을 조용히 변경하지 않고 새 ADR에서 대체 범위와 이유를 기록한다.

## 17. 단계별 구현 계획

### 단계 0. 제품 계약·기준선·ADR 확정

- 다중 Scene 지원 여부와 Domain Identity를 확정한다.
- Journal이 되돌리는 확정 작업과 제외할 임시 상태를 확정한다.
- Reset, 같은 Source 재Import, 다른 Dataset Import, Export와 Source Overwrite Barrier를 확정한다.
- Runtime-only 편집의 표준 Blender Dirty·Save As·Save Copy·Autosave·미저장 종료 동작을 실제 Blender에서 측정한다.
- Modifier 편집이 생성하는 Blender Global Undo Step 목록을 기록한다.
- Projection Painting Undo가 Modifier 작업을 되돌리는 상황을 재현한다.
- `undo_pre`의 Core Runtime 폐기와 재진입 COLD 복원 시간을 기록한다.
- 실데이터에서 Import, 편집, Undo와 Workspace 재진입 시간·Peak RSS를 기록한다.
- ADR-0008의 Modifier Undo 결정을 대체하는 새 ADR 초안을 승인한다.

완료 조건: 제품 계약, 성능 절대 기준, 현재 충돌과 저장 위험을 자동 재현할 수 있어야 한다.

### 단계 1. 순수 Runtime Domain과 복합 Token

- `domain_uuid`, `domain_generation`, Dataset/Source/History Token을 가진 `ModifierRuntimeDomain`을 추가한다.
- Scene Pointer를 영구 Key로 사용하지 않고 Global Undo 뒤 일시 Binding만 다시 만든다.
- 단일 Domain 또는 다중 Scene 계약을 코드로 강제한다.
- 설치된 Reconstruction/Index의 외부 제자리 변경을 금지한다.
- 이 단계에서는 기존 Blender `UNDO` 옵션을 유지한다.

완료 조건: Scene Pointer나 Scene Property가 바뀌어도 같은 Domain을 안전하게 식별하고 Core 상태가 유지되어야 한다.

### 단계 2. Scene 권위 제거와 Provider 전환

- `commit_edit()`를 Runtime 우선 Transaction으로 바꾼다.
- `Scene.colmap_import`를 UI/표시 Mirror로 낮춘다.
- `runtime_restore_token()`, Export 검증과 Workspace 준비 판정을 Domain Token 기준으로 바꾼다.
- Legacy `Scene.colmap_dataset`을 신규 편집 Revision 권위로 갱신하지 않는다.
- Projection Snapshot Provider와 Currentness 검증을 Domain 복합 Identity로 전환한다.
- 기존 Scene Token 의존 호출부 전체를 목록화하고 Domain Access Facade를 통하게 한다.

완료 조건: Scene Property를 임의로 과거 값으로 바꿔도 Runtime과 Projection Snapshot Provider의 현재 상태 판정이 유지되어야 한다.

### 단계 3. Persistence Mirror와 Dirty 계약

- Version, Domain Generation과 Checksum을 가진 Source/Operation Codec을 구현한다.
- Staging/Active Text owner 교체와 `save_pre`, `save_post`, `save_post_fail` 수명주기를 구현한다.
- Global Undo 뒤 과거 Text/Scene Mirror를 Runtime Generation 기준으로 Stale 처리한다.
- Runtime 미복원·복원 실패 상태의 저장이 기존 Active Payload를 지우지 않게 한다.
- Source 누락·변경·Unknown Schema·Checksum 오류에서 편집 동결과 복구 UI를 제공한다.
- 표준 미저장 표시를 위한 비권위 Dirty Mirror 정책을 실제 Blender 검증 결과대로 구현한다.

완료 조건: Save/Load/Save As/Save Copy/Autosave/저장 실패와 미저장 종료에서 Runtime 편집 유실이 없어야 한다.

### 단계 4. Delta 기반 Runtime Journal

- `EditOperation`과 변경 Record/Index 범위의 Forward/Inverse Delta를 구현한다.
- 전체 Dict 복사 없이 Core Mutation Gateway에서 Delta를 원자 적용한다.
- `ModifierEditCommand`와 Undo/Redo Stack 이동, 새 Branch 폐기를 구현한다.
- Reset, Import, Export와 Source 교체 Barrier를 구현한다.
- Undo/Redo 실패 시 적용한 Delta를 역적용해 호출 전 Runtime을 유지한다.
- 20회·100회 Command의 시간과 메모리 기준을 통과한다.

완료 조건: Warm Undo/Redo에서 Source Read·전체 Index Build·전체 Reconstruction/Index 복사가 모두 0회여야 한다.

### 단계 5. 입력 Router와 Global Undo 생명주기

- Modifier 소유 Input Guard를 구현하고 전용 Undo/Redo Journal에 연결한다.
- Journal이 비어 있을 때 Global Undo로 통과시키지 않는다.
- Edit 메뉴·Operator Search·직접 Global Undo 호출은 Handler에서 안전하게 처리한다.
- Global Undo 전 모든 Blender ID Pointer Session, Timer와 Draw Handler 참조를 폐기한다.
- Core Domain/Journal은 유지하고 Undo 후 현재 ID에서 파생 표시만 재탐색한다.
- Modifier 비활성 상태에서는 무거운 표시 복구를 지연한다.

완료 조건: Projection Painting Global Undo 20회 뒤 Modifier Runtime·Journal·편집 결과가 완전히 같고 Modifier 단축키의 Global Undo 호출이 0회여야 한다.

### 단계 6. 파생 Blender ID 데이터 격리

- Point Mesh, Camera, Review, Timeline과 Marker를 Runtime ID 기반 파생 표시로 정의한다.
- 직접 관리 Mesh/Camera/Marker 변경을 권위 변경으로 받아들이지 않는다.
- Global Undo 뒤 파생 표시를 Runtime에서 재생성한다.
- 기존 Mesh Edit Mode Box/Circle/Lasso와 Camera Review UX를 유지한다.
- Blender ID를 변경하는 Operator의 `UNDO`는 유지한다.

완료 조건: Projection Painting Global Undo가 표시용 Step을 되돌려도 Modifier 데이터와 Journal은 바뀌지 않고 표시가 복구되어야 한다.

### 단계 7. 조건부 독립 표시와 `UNDO` 제거

- 사용자 승인을 받은 경우에만 GPU Draw/Picking 전환을 시작한다.
- Point Box/Circle/Lasso, Camera 선택, Review Image/View와 Timeline UX를 기능 동등하게 구현한다.
- Scene/Text/Mesh/Object/Camera/Image/Marker/View Layer·선택·Mode 부작용을 Operator·Timer·Handler 단위로 감사한다.
- Undo 대상 Blender 상태를 변경하지 않는 Operator부터 `UNDO`를 제거한다.
- 표준 Dirty/Save UX와 물리적 Step 0개를 동시에 만족하지 못하면 표시용 Step을 유지한다.

완료 조건: 기능 동등성과 저장 안전성이 증명된 범위에서만 Global Undo Step 제거를 승인한다.

### 단계 8. 전체 QC·패키징·설치

- Core, Architecture, Background, Coexistence와 GUI 회귀를 실행한다.
- 실제 Blender에서 Modifier/Projection Painting 왕복 Undo를 검증한다.
- 실데이터 Camera 593개·Point3D 약 70만 개로 성능을 비교한다.
- Production ZIP을 생성하고 격리 설치본을 검증한다.
- 검증 완료본을 실제 `user_default` Extension 경로에 설치한다.
- 임시 Probe와 디버그 로그를 제거한다.

완료 조건: 아래 승인 조건과 실제 설치본 QC를 모두 통과해야 한다.

## 18. 테스트 계획

### 18.1 Pure Python

- Point 제거 0개·1개·다수 Undo/Redo
- Camera 제거와 Point 제거 혼합 Journal
- 연속 Undo/Redo와 Revision·History Hash 일치
- Undo 뒤 새 편집의 Redo 폐기
- Dataset UUID·Fingerprint 불일치 차단
- Reset Edits Command 복원
- 일반 Command에 누적 Operation Prefix Tuple이 없는지 확인
- Journal에 Reconstruction 전체·Index 전체·Blender ID가 없는지 확인
- Forward/Inverse Delta의 부분 Record·Index 동등성
- Delta 적용 중 실패한 모든 지점의 원자 Rollback
- 같은 EditOperation 객체가 Undo/Redo Stack 이동에서 중복되지 않는지 확인
- 설치된 Domain Model의 외부 제자리 변경 차단

### 18.2 Blender Background

- Runtime Commit과 Scene Mirror 일치
- Scene Mirror 강제 변경 뒤 Runtime 권위 유지
- 편집 실패 지점별 Runtime·Journal 원자 Rollback
- Pending Filter 확정 전후 Journal 경계
- Import·Export 성공·실패·취소 Domain 수명주기
- Save·Load 뒤 History 복구와 Redo 초기화
- Save·Load 뒤 저장 Operation 기반 Undo Command·Inverse Delta 재생성
- 재실행 시 사용자 Import 조작 없이 자동 Source 복구
- Source 이동 시 사용자 지정 경로의 파일명·구조·Fingerprint 재연결 검증
- Text Active/Staging Generation·Checksum 손상 처리
- Runtime 미복원·Source 누락 상태 저장에서 기존 Payload 보존
- `save_post_fail`, Save As, Save Copy와 Autosave Dirty 유지
- 같은 Dataset을 여러 Scene에서 열 때 Domain 충돌 차단 또는 분리
- Extension 해제 뒤 Guard·Domain·Timer 잔여 0개

### 18.3 입력 격리

- Modifier `Ctrl+Z`에서 Blender Global Undo 호출 0회
- Modifier Redo 단축키에서 전용 Redo 1회
- Modifier에 이력이 없을 때 Global Undo 통과 0회
- Projection Painting·일반 Workspace 단축키 통과
- File Browser·Confirm Dialog·다른 Window 입력 통과
- Import·Export·Transaction 중 Undo/Redo 차단
- Edit 메뉴·Operator Search·직접 `bpy.ops.ed.undo()` 뒤 Domain 유지
- View3D·Timeline·Text 입력 영역별 단축키 Routing

### 18.4 교차 Workspace

```text
1. Modifier Import
2. Point 제거 2회
3. Projection Painting으로 이동
4. Paint Stroke 3회
5. Projection Painting Ctrl+Z/Redo 반복
6. Modifier Runtime·Journal Token 불변 확인
7. Modifier로 복귀
8. Source Read와 Relationship Index Build 0회 확인
9. Modifier Ctrl+Z 2회
10. Modifier Redo 2회
11. Projection Painting Texture·Layer·Snapshot 불변 확인
```

### 18.5 Global Undo 안전성

- Projection Painting Undo/Redo 20회
- 일반 Blender Object Undo/Redo 20회
- Modifier Runtime·Journal 폐기 0회
- Modifier 편집 결과 변경 0회
- 이전 Blender Object·Mesh·Camera Pointer 재사용 0회
- 파생 표시 자동 재연결
- Point Edit Mode Box/Circle/Lasso 선택 UX 유지
- Camera Review Image/View와 Timeline 탐색 유지
- Blender Crash와 Undo Stack 손상 0회

### 18.6 성능 승인

- Projection Painting Undo당 Modifier `clear_runtime()`: 0회
- Projection Painting Undo 뒤 Modifier Source Read: 0회
- Projection Painting Undo 뒤 Relationship Index Build: 0회
- Workspace 재진입 COLD Runtime 복원: 0회
- Undo/Redo 단계당 전체 Reconstruction 복사: 0회
- Undo/Redo 단계당 전체 Index 복사: 0회
- Undo/Redo 단계당 Baseline 전체 Replay: 0회
- 성공 Transaction당 Runtime 설치: 1회
- 실패 Transaction의 Runtime·Journal 변경: 0건
- 소형·중형·약 70만 Point Dataset의 Warm 편집/Undo/Redo Median·p95 기록
- Command 20회·100회 뒤 Peak RSS와 Journal 증가량 기준 통과
- 단계 0에서 확정한 절대 시간·메모리 기준 통과

기존 `test_undo_domain_reconcile_background.py`, `probe_remove_global_undo_crash.py`, Scene Revision 기반 Snapshot Provider 테스트와 Legacy Dataset Adapter 테스트는 새 권위 방향과 반대되는 가정을 포함하므로 삭제하지 않고 새 계약에 맞게 갱신한다.

## 19. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| `UNDO`를 너무 일찍 제거해 Blender Stack 손상 | Runtime 권위·파생 표시·GPU 전환 뒤 개별 제거 |
| Scene Property가 다시 권위로 사용됨 | Architecture 테스트로 Runtime 단방향 Mirror 강제 |
| Command마다 누적 History Tuple이 쌓임 | 일반 Command는 Operation 한 건과 변경 범위 Delta만 보관 |
| Delta 내부에서 전체 Dict를 복사함 | Core Mutation Gateway 계측으로 전체 Reconstruction·Index 복사 0회 강제 |
| Global Undo 뒤 표시가 사라짐 | Runtime 유지 후 현재 Blender ID에서 파생 표시 재연결 |
| 직접 Mesh/Camera 편집이 Runtime과 충돌 | 관리 데이터 직접 편집 차단·Runtime 기준 복원 |
| Undo Replay가 느림 | Baseline Replay 대신 검증된 Inverse Delta 적용 |
| Save 때마다 Runtime 전체 내장 | Source metadata와 Operation만 저장하도록 Schema 제한 |
| Text가 Global Undo로 과거 Generation이 됨 | Runtime Generation 비교 후 Stale 처리, save_pre에 최신 Payload 재작성 |
| 순수 Runtime 편집이 미저장 표시를 만들지 못함 | 비권위 Dirty Mirror 또는 검증된 전용 저장 UX를 단계 0에서 확정 |
| 저장 실패 뒤 Dirty가 해제됨 | save_post 성공 때만 Dirty 해제, save_post_fail·불명확 결과는 Dirty 유지 |
| 기존 `.blend` 자동 변환 오류 | 자동 Migration 금지, 사용자에게 재Import/초기화 요청 |
| Projection Painting 데이터가 실시간 공유됨 | 수동 업데이트 Snapshot 계약 유지 |
| Scene Pointer 재사용으로 다른 Domain 연결 | Domain UUID·Generation 사용, Pointer는 일시 Binding으로 한정 |
| GPU 전환으로 Blender 선택 UX 손실 | 별도 사용자 승인과 Box/Circle/Lasso·Review 기능 동등성 Gate |
| Workspace Guard 누수 | Window Pointer·Session Generation·Timer 소유권 검사 |

## 20. 완료 조건

1. Modifier 편집 권위가 독립 Runtime에만 존재한다.
2. Modifier Undo/Redo Journal이 Runtime에서만 관리된다.
3. Modifier의 Undo/Redo 입력이 Blender Global Undo를 호출하지 않는다.
4. Projection Painting Global Undo가 Modifier Runtime과 Journal을 변경하지 않는다.
5. Projection Painting Undo 뒤 Modifier COLD 복원이 발생하지 않는다.
6. Point·Camera·Review·Marker는 Runtime의 파생 표시 데이터다.
7. Blender 표시 데이터의 직접 변경이 Modifier 편집 권위를 바꾸지 않는다.
8. Modifier Undo/Redo 결과의 Reconstruction·Index·Revision·Hash가 일치한다.
9. Undo/Redo 실패 시 호출 전 Runtime·Journal·표시 상태가 유지된다.
10. Undo 뒤 새 편집이 Redo Branch를 폐기한다.
11. Import 성공은 새 Domain을 만들고 Export 성공은 새 Baseline으로 전환한다.
12. Projection Painting의 공용 데이터는 수동 업데이트 Snapshot으로만 갱신된다.
13. 저장 파일에는 Source metadata와 EditOperation, Domain Generation·Checksum만 기록된다.
14. 기존 데이터에 대한 자동 Migration이 없다.
15. Undo 단계마다 Source 재읽기, Baseline 전체 Replay와 전체 Reconstruction·Index 복사가 없다.
16. Workspace 이동만으로 Runtime이 폐기되지 않는다.
17. 표준 저장·Save As·Save Copy·Autosave·미저장 종료에서 Runtime 편집 유실이 없다.
18. Scene/Text Mirror가 Global Undo로 바뀌어도 Domain Generation이 유지된다.
19. 최초 Import 결과가 추가 대용량 복사 없이 바로 Domain에 설치된다.
20. 재실행 시 수동 Import 없이 Source 자동 읽기와 Operation Replay가 수행된다.
21. 재실행 과정에서 저장된 확정 작업의 Undo Command·Inverse Delta가 다시 생성되고 Redo는 비워진다.
22. Projection Snapshot Provider가 Scene Revision이 아닌 Domain 복합 Identity를 검증한다.
23. Point Edit Mode의 Box/Circle/Lasso와 Camera Review UX가 유지된다.
24. 표시·Dirty용 Global Undo Step이 남더라도 Modifier 편집 데이터와 Journal을 포함하지 않는다.
25. Modifier 관련 Global Undo Step 0개는 Blender ID 부작용 0개와 저장 UX가 증명된 경우에만 조건부 승인한다.
26. Projection Painting과 일반 Blender Undo 동작은 변경되지 않는다.
27. Align Source와 Runtime은 수정하지 않는다.
28. ADR-0008의 Modifier Undo 결정을 대체하는 새 ADR이 추가된다.
29. Core, Architecture, Background, GUI와 실데이터 회귀가 통과한다.
30. Blender Crash, Undo Stack 손상과 데이터 불일치가 없다.
31. 디버그 로그와 임시 Probe가 배포물에 남지 않는다.

## 21. 최종 사용자 흐름

```text
최초 실행
→ COLMAP Import
→ Loader 결과를 독립 Runtime으로 즉시 설치
→ Point·Camera 표시 생성

Modifier 편집
→ Python Runtime의 COLMAP 데이터만 변경
→ EditOperation과 Forward/Inverse Delta를 전용 Journal에 기록
→ Ctrl+Z: 마지막 Modifier 작업만 되돌림
→ Ctrl+Shift+Z: 같은 Modifier 작업 다시 적용

Projection Painting으로 이동
→ Paint·Layer 작업과 Blender Undo 실행
→ Modifier Runtime과 작업 스택은 그대로 유지

Modifier로 복귀
→ 원본 COLMAP 재해석 없이 Runtime 즉시 연결
→ 기존 Modifier Undo/Redo 계속 사용

Blender 저장
→ Source metadata·확정 EditOperation·Revision·Checksum 기록
→ Runtime 전체와 관계 Index는 저장하지 않음

Blender 종료·재실행
→ 저장 Payload 검증
→ 원본 COLMAP 자동 Source 읽기
→ 저장된 Operation 재적용
→ Undo용 Delta와 Command 재생성
→ 새 독립 Runtime 설치
→ Point·Camera·Review 표시 재생성
→ Redo Stack은 빈 상태로 시작
```

이 구조의 최종 목적은 Modifier의 입력만 따로 받는 것이 아니다. Modifier의 데이터 권위, 편집 기록과 Undo/Redo 자체를 Blender Global Undo 밖으로 옮겨 Projection Painting과 일반 Blender 작업이 Modifier 편집을 되돌릴 수 없게 만드는 것이다.
