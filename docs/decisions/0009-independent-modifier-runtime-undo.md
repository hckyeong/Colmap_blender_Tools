# ADR-0009: Modifier 독립 Runtime·Undo 도메인

- 상태: Accepted
- 결정일: 2026-09-02

## 배경

Modifier의 권위 상태가 Scene Property, Blender ID와 Global Undo에 나뉘어 있으면 Projection Painting이나 일반 Workspace의 Global Undo가 Modifier Runtime 폐기·복구와 충돌합니다. 이 구조는 비활성 Modifier 데이터까지 다시 읽고 재생하게 만들어 복구 비용도 큽니다.

## 결정

### Runtime 권위

Modifier Python Runtime Domain을 편집 상태의 유일한 권위로 사용합니다. Domain은 다음 복합 Identity로 현재 상태를 검증합니다.

- Domain UUID와 generation
- Dataset UUID와 source fingerprint
- Edit revision과 history hash

Scene Property는 저장과 재연결을 위한 Mirror일 뿐 권위가 아닙니다. Point Mesh, Camera와 Timeline Marker도 Runtime에서 다시 만들 수 있는 파생 표시로 취급합니다.

### 전용 Undo·Redo

각 편집은 확정 `EditOperation`과 변경 범위만 담은 forward/inverse Delta를 하나의 Command로 기록합니다. Modifier 전용 Journal은 이 Command로 warm Undo·Redo를 수행합니다.

Modifier 소유 Window의 활성 Modifier Workspace에서만 다음 입력을 전용 Journal로 라우팅합니다.

- `Ctrl+Z`: Modifier Undo
- `Ctrl+Shift+Z`, `Ctrl+Y`: Modifier Redo
- `Ctrl+Alt+Z`: Blender Undo History를 열지 않고 안내 표시

Journal이 비어 있어도 입력을 Blender Global Undo로 넘기지 않습니다. 다른 Workspace와 Window에서는 Blender Undo를 그대로 유지합니다.

Global Undo가 실행되면 Domain과 Journal은 보존합니다. 이전 Blender ID 참조와 세션 연결만 폐기하고, Global Undo가 끝난 뒤 권위 Runtime에서 파생 표시를 다시 동기화합니다.

현재 Blender ID를 실제 변경하는 Operator의 `UNDO` 옵션은 즉시 제거하지 않습니다. 해당 Global Undo Step은 표시 복구에만 관여하며 권위 Runtime을 변경할 수 없습니다. 완전한 0-Step 전환은 별도 검증 Gate를 통과한 뒤 결정합니다.

### 저장과 복원

`.blend`에는 Active/Staging Text를 사용해 다음 값만 저장합니다.

- Source Metadata
- Domain Identity
- 확정된 Edit Operation

Reconstruction, 관계 Index, Delta와 Redo Stack은 저장하지 않습니다. `load_post`에서는 저장된 Source를 자동으로 읽고 확정 Operation을 재생해 Runtime과 Undo Delta를 다시 구성합니다. Redo Stack은 빈 상태로 시작합니다. Schema나 Source가 맞지 않는 데이터는 자동 마이그레이션하지 않고 명시적인 재Import를 요구합니다.

### Import·Export와 도구 경계

- Import는 Loader 결과를 Scene Mirror를 거치지 않고 새 Domain에 직접 설치합니다.
- 다른 Dataset을 Import하면 기존 Domain을 변경하지 않고 새 Domain을 만듭니다.
- Export 성공은 현재 상태를 새 Baseline으로 확정하고 Journal을 비웁니다.
- Export 실패나 취소는 기존 Domain과 Journal을 유지합니다.
- Projection Painting은 사용자가 요청할 때만 Modifier의 불변 값 Snapshot을 복사합니다.
- Projection Painting은 Modifier Runtime이나 Journal을 공유하지 않습니다.

## 결과

- Modifier 편집 권위가 Blender Global Undo와 분리되어 다른 Workspace의 Undo가 Runtime을 손상시키지 않습니다.
- Modifier Workspace에서는 전용 Undo·Redo의 빈 상태와 잠금 상태를 일관되게 안내할 수 있습니다.
- Global Undo 뒤에는 Source 재해석 대신 권위 Runtime을 사용해 파생 표시만 복구할 수 있습니다.
- 저장 파일은 재구축 가능한 최소 권위 값만 포함하며 Runtime 객체나 Blender ID에 의존하지 않습니다.
- Load 시 Source 읽기와 Operation 재생이 필요하고, 저장 직후의 Redo 분기는 복원되지 않습니다.
- Operator의 완전한 Global Undo Step 제거는 별도 호환성·복구 검증이 필요합니다.

## 검토했지만 채택하지 않은 대안

### 모든 Modifier Operator에서 `UNDO` 즉시 제거

Blender ID 변경과 표시 복구의 기존 동작을 한 번에 바꾸면 회귀 범위가 너무 큽니다. 권위 분리를 먼저 완료하고 0-Step 전환은 별도 Gate로 검증합니다.

### `Ctrl+Z`만 가로채기

Redo, Undo History, 빈 Journal과 다른 Window 경계를 보장하지 못하며 메뉴나 Global Undo에 권위가 다시 섞일 수 있습니다.

### Scene Mirror를 권위로 유지

Global Undo가 Scene Property를 되돌릴 수 있어 Python Runtime과 다시 충돌하고, 대형 상태의 반복 복구 비용도 남습니다.

### Runtime 전체 Snapshot 저장

Reconstruction, Index, Delta와 Redo를 저장하면 파일 크기와 Schema 결합도가 커지고 Blender ID·Python 객체의 수명 문제를 영속화합니다. Source와 확정 Operation으로 재구축하는 방식을 사용합니다.

## 기존 결정과의 관계

ADR-0008의 Projection Painting 수동 독립 Snapshot 결정은 유지합니다. 다만 ADR-0008에 포함된 `Modifier 별도 Ctrl+Z 라우팅 불필요` 결정만 이 ADR이 대체합니다.
