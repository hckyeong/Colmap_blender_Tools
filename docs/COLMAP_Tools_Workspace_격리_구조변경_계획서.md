# COLMAP Tools Workspace 격리 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-08-28
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 대상 도구: `COLMAP Align`, `COLMAP Modifier`, `Projection Paint`
- 문서 상태: 구조변경 구현·자동 QC 완료, 사용자 보유 실데이터 최종 확인 대기
- 관련 장애: Align에서 Modifier로 이동한 뒤 COLMAP 데이터 Import는 성공하지만 3D Viewport에 보이지 않는 문제

## 2. 결론

세 도구의 Workspace는 더 이상 현재 활성 Workspace를 복제하지 않는다. 각 도구는 Extension에 포함된 자기 전용 중립 Workspace Seed를 `bpy.ops.workspace.append_activate()`로 새로 불러오고, 새로 생성된 자기 소유 Workspace와 Screen만 구성한다.

핵심 원칙은 다음과 같다.

1. 다른 도구 또는 사용자 Workspace의 `Screen`, `Area`, `SpaceView3D` 상태를 초기값으로 사용하지 않는다.
2. Workspace 이름이 아니라 소유자, 도구 ID, 인스턴스 UUID, Layout Schema Version으로 관리 대상을 식별한다.
3. 각 도구는 자기 Viewport 표시 계약을 매번 명시적으로 적용하고 검증한다.
4. 기존 복제 방식으로 만들어진 Workspace는 재사용하지 않고 안전하게 교체한다.
5. 사용자 소유 Workspace와 다른 도구 소유 Workspace는 수정하거나 삭제하지 않는다.

이 변경으로 `COLMAP Align → COLMAP Modifier`, `Projection Paint → COLMAP Modifier`처럼 도구 실행 순서에 따라 Viewport 상태가 오염되는 경로를 구조적으로 제거한다.

## 3. 현행 문제와 확인 근거

### 3.1 세 도구가 모두 활성 Workspace를 복제함

현재 구현은 아래 위치에서 `bpy.ops.workspace.duplicate()`를 호출한다.

| 도구 | 현재 생성 코드 | 문제 |
| --- | --- | --- |
| COLMAP Align | `tools/align/operators/workspace.py` | 호출 당시 활성 Workspace의 Screen과 Editor 상태를 상속함 |
| COLMAP Modifier | `tools/modifier/blender/modifier_workspace.py` | Align에서 실행하면 Align의 Viewport 필터를 상속함 |
| Projection Paint | `tools/projection_paint/blender/paint_workspace.py` | 사용자 또는 다른 도구의 Editor 상태를 상속함 |

복제 직후 Area를 합치거나 나누어 화면 모양은 다시 구성하지만, `SpaceView3D` 내부의 모든 속성을 초기화하지는 않는다. 따라서 Area 타입과 배치가 정상이어도 오브젝트 타입 표시 필터, Local View, Shading, Overlay 등 복제 원본의 내부 상태가 남을 수 있다.

### 3.2 이번 장애의 직접 원인

실제 Blender 상태에서 다음을 확인했다.

- Modifier Import 결과: Camera 440개, Image 440개, Point3D 563,983개
- Point Mesh의 평가 결과: 563,983 Points
- 관리 Object와 Collection: 존재하고 숨김 상태가 아니며 View Layer에 연결됨
- Modifier Workspace의 모든 `show_object_viewport_*` 값: `False`
- Modifier Workspace 생성 원본: `COLMAP Align`

Align은 GPU 전용 A/P View에서 Scene Object를 숨기기 위해 모든 `show_object_viewport_*` 값을 의도적으로 `False`로 설정한다. Modifier가 이 Workspace를 복제하면 Mesh, Camera, Empty, Point Cloud를 포함한 모든 Scene Object가 Viewport 필터 단계에서 차단된다.

따라서 데이터 로더나 Scene 생성 실패가 아니라 Workspace의 `SpaceView3D` 상태 상속이 직접 원인이다.

### 3.3 Blender 5.2 API 제약

Blender 5.2 공식 API와 설치된 Blender 5.2.0 LTS에서 다음을 확인했다.

- [`WorkSpace.screens`](https://docs.blender.org/api/5.2/bpy.types.WorkSpace.html#bpy.types.WorkSpace.screens)는 읽기 전용이다.
- [`Screen.areas`](https://docs.blender.org/api/5.2/bpy.types.Screen.html#bpy.types.Screen.areas)는 읽기 전용이다.
- 설치된 Blender 5.2.0 LTS의 `bpy.data.workspaces`와 `bpy.data.screens`에는 `new()`가 없다.
- 공식 Workspace 생성 경로는 현재 Workspace를 복제하는 `duplicate()` 또는 파일에서 Workspace를 가져오는 [`append_activate()`](https://docs.blender.org/api/5.2/bpy.ops.workspace.html#bpy.ops.workspace.append_activate)이다.

따라서 빈 `WorkSpace`와 `Screen`을 Python Data API로 직접 만들 수 없다. 현재 상태를 상속하지 않는 생산용 해법은 Extension이 소유한 검증된 Seed `.blend`에서 Workspace를 Append하는 방식이다.

## 4. 목표와 제외 범위

### 4.1 목표

- 세 도구의 실행 순서와 무관하게 동일한 Workspace 상태를 만든다.
- 사용자 Workspace의 Screen과 Editor 상태를 변경하지 않는다.
- 기존 복제 기반 Workspace를 자동으로 식별하고 안전하게 재생성한다.
- Workspace 생성, 활성화, 실패 Rollback, 종료 복귀를 공통 계약으로 통일한다.
- 도구별 Layout과 Viewport 정책은 각 도구가 독립적으로 소유한다.
- Blender 재시작, `.blend` 저장·재로드, 다중 Window에서도 소유권과 복구 규칙을 유지한다.

### 4.2 제외 범위

- Align, Modifier, Projection Paint의 업무 기능과 데이터 모델 변경
- 세 도구 사이 데이터 전달 Pipeline 추가
- COLMAP Import·Export 포맷 변경
- Align의 Small GICP Runtime 변경
- UI 디자인 전면 개편
- 사용자 Workspace 설정을 자동 보정하거나 초기화하는 기능

## 5. 목표 구조

### 5.1 전체 구조

```text
도구 실행 요청
  → 진입 전 사용자 Workspace Snapshot 저장
  → 기존 도구 Workspace 소유권·Schema 검사
     ├─ 현행 Schema: 검증 후 재사용
     ├─ Legacy Schema: 안전하게 폐기한 뒤 재생성
     └─ 외부 소유: 건드리지 않고 새 관리 인스턴스 생성
  → 도구 전용 Seed .blend에서 Workspace Append
  → 공통 소유 메타데이터 기록
  → 도구별 Layout 구성
  → 도구별 Space 계약 적용
  → 최종 불변식 검증
  → 세션 ACTIVE 전환
```

### 5.2 디렉터리 구성

```text
shared/blender_runtime/
├─ workspace_factory.py
├─ workspace_contract.py
└─ workspace_errors.py

tools/align/resources/workspaces/
└─ colmap_align_seed_5_2.blend

tools/modifier/resources/workspaces/
└─ colmap_modifier_seed_5_2.blend

tools/projection_paint/resources/workspaces/
└─ projection_paint_seed_5_2.blend

build_tools/workspaces/
├─ create_workspace_seed.py
└─ validate_workspace_seed.py
```

`shared/blender_runtime`은 Workspace Append, 공통 소유권 검증, 안전한 삭제와 Rollback만 담당한다. Layout 모양, Sidebar, Shading, Object 표시 정책은 계속 각 도구 모듈이 담당한다. 세 도구가 서로의 모듈을 Import하지 않는 현행 제품 경계는 유지한다.

### 5.3 도구별 Seed

각 Seed는 Blender 5.2 `--factory-startup` 상태에서 만든 중립 Workspace를 포함한다.

Seed 요구사항은 다음과 같다.

- 도구마다 다른 내부 Workspace ID를 사용한다.
- Workspace와 연결 Screen은 해당 Seed 파일에서 새 ID로 Append할 수 있어야 한다.
- Viewport 오브젝트 타입 필터의 중립 기본값은 모두 `True`로 한다.
- Local View, 임시 Region 상태, 실행 중 Modal Operator, Scene 선택 상태를 포함하지 않는다.
- Tool Runtime 데이터, COLMAP 데이터, Scene Object, Image, Material을 포함하지 않는다.
- Blender 5.2에서 생성하고 5.2 Background 검증을 통과한 파일만 배포한다.
- Seed Schema와 생성 Blender Version을 Custom Property로 기록한다.

Seed는 런타임 상태를 공유하는 공용 Workspace가 아니다. 각 도구가 자기 Seed 파일에서 독립된 Workspace와 Screen ID를 새로 Append하기 위한 읽기 전용 배포 자원이다.

### 5.4 공통 Workspace 식별자

새 구조에서는 이름을 식별자로 사용하지 않는다.

| Key | 예시 | 용도 |
| --- | --- | --- |
| `colmap_tools_workspace_owner` | `colmap_tools` | Extension 소유 여부 |
| `colmap_tools_workspace_tool_id` | `modifier` | 소유 도구 구분 |
| `colmap_tools_workspace_uuid` | UUID 문자열 | Workspace 인스턴스 구분 |
| `colmap_tools_workspace_schema` | `2` | Runtime Workspace 구조 Version |
| `colmap_tools_workspace_seed_schema` | `1` | Seed 호환성 Version |
| `colmap_tools_source_workspace` | 진입 전 Workspace 이름 | 저장·재로드 시 복귀 보조 정보 |
| `colmap_tools_workspace_state` | `READY` | `CREATING`, `READY`, `FAILED` 상태 |
| `colmap_tools_screen_owner` | Workspace UUID | Screen 소유권 연결 |

런타임 Session에서는 진입 전 Workspace Pointer와 이름을 함께 보존한다. Pointer가 유효하면 Pointer를 우선하고, 저장·재로드 뒤에만 이름을 보조 복구 수단으로 사용한다.

기존 `colmap_align_owned`, `colmap_modifier_owned`, `pp_owner` 기반 Workspace는 Legacy로만 인식한다. Legacy 태그가 있다는 이유만으로 새 Schema Workspace로 재사용하지 않는다.

## 6. 공통 Workspace Factory 계약

### 6.1 생성 전 검사

1. 현재 Window와 Screen이 유효한지 확인한다.
2. 도구 Session이 이미 다른 Window에서 실행 중인지 확인한다.
3. 진입 전 Workspace Pointer, 이름, Screen Pointer를 저장한다.
4. Seed 파일 존재, 내부 Workspace ID, Seed Schema, Blender Version을 검증한다.
5. 같은 Tool ID의 기존 Workspace를 찾고 Schema와 사용 중인 Window를 검사한다.

### 6.2 생성

1. Append 전 `bpy.data.workspaces` Pointer 집합을 저장한다.
2. 현재 Window를 포함한 정확한 Context Override에서 `bpy.ops.workspace.append_activate()`를 호출한다.
3. Append 전후 Pointer 차이로 새 Workspace가 정확히 하나인지 확인한다.
4. 현재 Window의 Workspace와 Screen이 새 ID로 전환됐는지 Event Tick에서 확인한다.
5. 공통 소유 메타데이터와 Screen 소유 메타데이터를 기록한다.
6. 도구별 Layout Builder에 제어를 넘긴다.

### 6.3 최종 검증

세션을 `ACTIVE`로 바꾸기 전에 다음을 모두 만족해야 한다.

- Workspace의 Tool ID와 기대 도구가 일치한다.
- Workspace Schema와 Seed Schema가 지원 Version과 일치한다.
- 현재 Screen이 해당 Workspace의 `screens`에 속한다.
- Screen Owner UUID가 Workspace UUID와 일치한다.
- Layout의 Area 수, Area 타입, 배치 관계가 도구 계약과 일치한다.
- 각 `SpaceView3D`의 핵심 표시 속성이 도구 계약과 일치한다.
- 사용자 Workspace의 Screen Pointer와 핵심 Space Snapshot이 변경되지 않았다.

### 6.4 실패 Rollback

Append 또는 구성 중 실패하면 다음 순서로 처리한다.

1. Timer, Draw Handler, MsgBus, 임시 Runtime을 해제한다.
2. 진입 전 Workspace가 유효하면 현재 Window를 복귀시킨다.
3. 이번 작업에서 새로 만들어진 Workspace Pointer만 삭제 대상으로 지정한다.
4. 다른 Window가 사용하지 않고 소유 UUID가 일치할 때만 삭제한다.
5. 즉시 삭제할 수 없으면 상태를 `FAILED`로 표시하고 안전한 UI Tick에서 지연 삭제한다.
6. 사용자 Workspace 또는 다른 Tool ID의 Workspace는 삭제하지 않는다.
7. 세션을 `INACTIVE`로 되돌리고 구체적인 실패 사유를 UI 상태에 기록한다.

## 7. 도구별 Layout·표시 계약

### 7.1 COLMAP Align

| 항목 | 계약 |
| --- | --- |
| Layout | 좌우 2개의 `VIEW_3D` |
| View 역할 | 왼쪽 A Reference, 오른쪽 P Moving |
| Scene Object 표시 | 모든 `show_object_viewport_* = False` |
| 데이터 표시 | Align GPU Draw Handler만 사용 |
| Sidebar | 오른쪽 P View의 `COLMAP Align` 탭 활성화 |
| View 초기화 | Align View Preset Version에 따라 명시적으로 설정 |
| 재사용 | 역할 Registry, Draw Handler, View Preset 검증 후 허용 |

Align의 모든 Object 필터 비활성화는 의도된 전용 정책으로 유지한다. 다만 이 상태는 Align이 새로 Append한 자기 Screen 안에서만 존재해야 한다.

### 7.2 COLMAP Modifier

| 항목 | 계약 |
| --- | --- |
| Layout | 상단 `VIEW_3D`, 하단 `TIMELINE` |
| Scene Object 표시 | 지원되는 모든 `show_object_viewport_* = True` |
| 최소 가시 대상 | Mesh, Camera, Empty, Point Cloud |
| Local View | 비활성 |
| Shading | Modifier 기본값을 명시적으로 적용 |
| Overlay | 관리 Point, Camera, Axis 표시 계약에 맞게 활성화 |
| Sidebar | `COLMAP Modifier` 탭 활성화 |
| View 초기화 | Modifier Perspective Preset 적용 |

Modifier의 가시성은 Workspace 타입 필터로 숨기지 않는다. 특정 관리 Object 표시 여부는 기존 Scene·Collection·Review 상태가 담당한다.

### 7.3 Projection Paint

| 항목 | 계약 |
| --- | --- |
| Layout | 왼쪽 상단 `VIEW_3D`, 왼쪽 하단 `TIMELINE`, 오른쪽 상단 `OUTLINER`, 오른쪽 하단 `PROPERTIES` |
| Scene Object 표시 | 지원되는 모든 `show_object_viewport_* = True` |
| 설정 모드 | Workspace Open 시 `OBJECT` |
| Paint 모드 | Paint 진입 뒤 `TEXTURE_PAINT`, Material Shading, Combined Pass |
| Point 표시 | Paint Session의 기존 숨김·복구 계약으로 제어 |
| Sidebar | `Projection Paint` 탭 활성화 |
| Properties | Object Context |
| Outliner | View Layer Mode |

Projection Paint도 Workspace 필터는 중립 가시 상태로 시작하고, Paint Target과 Dataset Point의 실제 표시 전환은 현재 도구 소유 로직이 담당한다.

## 8. Legacy Workspace 마이그레이션

### 8.1 Legacy 판정

다음 중 하나이면 Legacy 또는 손상 Workspace로 판정한다.

- 기존 도구별 Owner Key만 있고 공통 Workspace Schema가 없음
- Workspace Schema가 현재 Version보다 낮음
- Screen Owner UUID가 없거나 Workspace UUID와 다름
- 기대 Layout과 Area 타입이 일치하지 않음
- 핵심 `SpaceView3D` 표시 계약이 일치하지 않음
- 현재 Workspace가 다른 도구 Workspace에서 복제됐음을 나타내는 기존 Source 정보가 있음

### 8.2 교체 절차

1. Legacy Workspace가 어떤 Window에서도 사용 중이 아닌지 확인한다.
2. 현재 Window가 사용 중이면 먼저 유효한 진입 전 Workspace로 전환한다.
3. 다른 Window가 사용 중이면 자동 삭제하지 않고 작업을 중단해 사용 Window를 알린다.
4. Scene Pointer와 Session Pointer에서 Legacy Workspace 참조를 제거한다.
5. 소유 태그와 실제 Tool ID가 일치하는 Workspace만 삭제한다.
6. 도구 Seed에서 새 Workspace를 Append한다.
7. Layout과 표시 계약을 새로 적용하고 최종 검증한다.

기존 Workspace의 사용자 View 각도 같은 편의 상태는 기본적으로 마이그레이션하지 않는다. 오염된 `SpaceView3D` 상태를 선별 복사하면 동일 문제가 다시 유입될 수 있기 때문이다. Align의 A/P View Snapshot처럼 도구가 명시적으로 소유하고 검증하는 값만 도구 Runtime에서 복원한다.

### 8.3 이름 충돌

Workspace 이름은 표시용이며 소유권 판단에 사용하지 않는다.

- 같은 이름의 사용자 Workspace가 있어도 수정하거나 삭제하지 않는다.
- 새 관리 Workspace는 Blender가 부여한 안전한 접미사를 허용한다.
- UI와 Session은 Tool ID와 UUID로 관리 Workspace를 찾는다.
- 이름만 같고 소유 메타데이터가 없는 Workspace는 항상 외부 소유로 취급한다.

## 9. 구현 단계

### 단계 0. 기준선 고정과 API Spike

- 현행 세 도구 Workspace 진입·종료 테스트 결과를 보존한다.
- 설치된 Blender 5.2에서 Seed Append와 Screen 전환 시점을 GUI Probe로 확인한다.
- 같은 Blender Session에서 세 도구 Seed를 연속 Append해 Workspace와 Screen Pointer가 모두 다른지 확인한다.
- Append된 Workspace만 삭제했을 때 원본 사용자 Workspace와 다른 도구 Workspace가 유지되는지 확인한다.

완료 조건: Seed Append, 활성화, 삭제, 재진입의 ID 생명주기가 Blender 5.2 GUI에서 재현 가능해야 한다.

### 단계 1. Seed 제작·검증 도구 추가

- `--factory-startup` 기반으로 도구별 내부 Workspace ID와 메타데이터를 기록한다.
- 도구별 Seed `.blend`를 생성한다.
- Seed에 불필요한 Runtime·Scene 데이터가 없는지 Background Validator를 추가한다.
- Seed 파일 Hash와 Schema를 빌드 검증 결과에 기록한다.
- Extension Build ZIP에 세 Seed가 포함되는지 Installed Extension 테스트를 추가한다.

완료 조건: Source Tree와 설치 ZIP 양쪽에서 Seed 파일과 내부 ID를 검증할 수 있어야 한다.

### 단계 2. 공통 Workspace Factory 구현

- `WorkspaceSpec`, Append, 소유 태깅, 검증, 안전 삭제, 실패 Rollback을 구현한다.
- 이름이 아니라 Tool ID와 UUID로 찾도록 한다.
- 생성 중 상태와 실패 상태를 명시적으로 관리한다.
- 공통 Factory에는 도구별 Layout 규칙을 넣지 않는다.

완료 조건: Factory 단위 테스트와 Blender Background 정적 검증이 통과해야 한다.

### 단계 3. COLMAP Modifier 우선 전환

- 현재 장애가 발생한 `_ensure_dedicated_workspace()`의 `workspace.duplicate()` 경로를 제거한다.
- Modifier Seed Append로 교체한다.
- 모든 Viewport Object 타입 필터를 `True`로 정규화한다.
- Legacy Modifier Workspace 교체를 구현한다.
- 실제 소형 COLMAP Fixture Import 후 Point, Camera, Image 가시성을 GUI에서 검증한다.

완료 조건: Align에서 Modifier를 실행해도 Import 데이터가 즉시 보여야 한다.

### 단계 4. COLMAP Align 전환

- Align Seed Append로 교체한다.
- 기존 좌우 View 구성 Timer와 Registry를 새 Screen에만 적용한다.
- 모든 Object 타입 필터 `False` 정책을 Align 소유 Screen에서만 적용한다.
- A/P View Snapshot 복원과 사용자 Workspace 복귀를 회귀 검증한다.

완료 조건: Align 종료 후 원본 Workspace의 Viewport 필터가 진입 전과 완전히 같아야 한다.

### 단계 5. Projection Paint 전환

- `ensure_paint_workspace()`의 `workspace.duplicate()` 경로를 제거한다.
- Scene별 Paint Workspace Pointer와 READY 재사용 계약을 새 Factory에 맞추고 손상본만 지연 삭제한다.
- 기존 4-Area 단계별 Layout 구성은 자기 소유 Screen에서만 실행한다.
- Paint 진입·종료, Target 가시성, Point 숨김·복구를 회귀 검증한다.

완료 조건: 어느 도구에서 실행해도 같은 4-Area Layout과 Paint 표시 상태가 만들어져야 한다.

### 단계 6. Legacy 마이그레이션과 저장·재로드

- 기존 Owner Key를 읽는 Legacy 탐지기를 추가한다.
- 손상·구버전 Workspace의 안전한 교체와 다중 Window 차단을 구현한다.
- `.blend` 저장 후 재로드 시 READY Workspace 재검증과 Legacy 재생성을 확인한다.
- Extension Disable 시 사용자 Workspace 복귀와 Runtime 해제를 확인한다.

완료 조건: 기존 설치 사용자의 오염 Workspace가 수동 삭제 없이 새 구조로 교체되어야 한다.

### 단계 7. 통합 QC와 배포

- 세 도구의 모든 실행 순서 조합을 검사한다.
- Source 설치와 ZIP 설치를 각각 검사한다.
- Manifest Version을 Patch Version으로 올린다.
- `docs/migration/IMPLEMENTATION_STATUS.md`, `docs/TESTING.md`, Release Note를 갱신한다.
- 새 ZIP을 실제 Blender에 설치한 뒤 최종 GUI QC를 수행한다.

완료 조건: 아래 승인 기준을 모두 만족하고 Debug 로그와 Probe 잔여물이 없어야 한다.

## 10. 테스트 계획

### 10.1 정적·구조 테스트

- Production Source의 세 Workspace 모듈에 `bpy.ops.workspace.duplicate()`가 없어야 한다.
- Seed 생성용 `build_tools/` 외에는 활성 Workspace 복제 호출을 금지한다.
- 세 도구 사이 직접 Import가 없어야 한다.
- Seed 경로는 Extension 설치 루트 내부의 절대 경로로 해석되어야 한다.
- Seed 누락, 내부 ID 불일치, Schema 불일치가 명확한 오류로 처리되어야 한다.

### 10.2 실행 순서 행렬

각 Source Workspace의 Viewport 필터를 의도적으로 서로 다른 값으로 만든 뒤 다음 순서를 모두 검사한다.

| 시작 도구 | 다음 도구 | 필수 결과 |
| --- | --- | --- |
| Align | Modifier | Modifier Object 필터 전체 활성, Import 데이터 가시 |
| Align | Projection Paint | Paint Target과 Camera 가시 |
| Modifier | Align | Align Object 필터 전체 비활성, GPU 데이터만 표시 |
| Modifier | Projection Paint | Paint Layout과 Material View 정상 |
| Projection Paint | Align | Align A/P Layout 정상 |
| Projection Paint | Modifier | Modifier Point와 Camera 가시 |
| 사용자 Layout | 각 도구 | 도구별 계약과 일치 |

각 조합은 종료 후 Source Workspace의 Screen Pointer, Area 타입, Object 필터, Shading 핵심값이 진입 전과 같은지도 검사한다.

### 10.3 Legacy·실패 테스트

- Schema 없는 기존 Align·Modifier·Paint Workspace 교체
- 모든 Object 필터가 `False`인 Legacy Modifier Workspace 교체
- 같은 이름의 사용자 Workspace 보존
- Seed 파일 누락과 손상
- Append 결과가 0개 또는 2개 이상인 비정상 상황
- Layout 구성 중 Area Split 실패
- 다른 Window가 Legacy Workspace를 사용 중인 상황
- 실패 후 Source Workspace 복귀와 생성 ID 정리
- 실패 Workspace 지연 삭제 중 Blender 파일 로드·Undo·Redo

### 10.4 실제 데이터 GUI 회귀

최소 한 개의 소형 고정 Fixture와 사용자가 문제를 재현한 실데이터를 사용한다.

- Import 완료 수치와 Scene Object 수 일치
- 평가 Geometry Point 수 일치
- Camera, Image Empty, Point Mesh 실제 화면 가시
- `Frame All` 후 데이터가 Viewport에 나타남
- Align A/P GPU 표시 정상
- Projection Paint Target 진입과 Brush 표시 정상
- 도구 왕복 후 원본 Workspace 훼손 없음

스크린샷만으로 통과시키지 않고 RNA 값, Pointer, 평가 Geometry, 실제 화면을 함께 검증한다.

## 11. 파일별 예상 변경 범위

| 경로 | 변경 내용 |
| --- | --- |
| `shared/blender_runtime/` | 공통 Workspace Spec, Append, 소유권, 검증, Rollback 추가 |
| `tools/align/operators/workspace.py` | 활성 Workspace 복제 제거, Seed Factory 사용, Schema 검증 추가 |
| `tools/align/constants.py` | 새 Workspace·Screen Schema 상수 추가 |
| `tools/align/session.py` | 이름 의존 제거, UUID·Pointer 기반 복구 강화 |
| `tools/modifier/blender/modifier_workspace.py` | 복제 제거, Seed Factory와 명시적 Viewport 계약 적용 |
| `tools/modifier/blender/modifier_session.py` | 공통 소유 메타데이터와 Legacy 탐지 연동 |
| `tools/modifier/blender/modifier_viewport.py` | Modifier Object 타입 필터 정규화 추가 |
| `tools/projection_paint/blender/paint_workspace.py` | 복제 제거, Seed Factory, 기존 지연 삭제 연동 |
| `tools/projection_paint/blender/ownership.py` | Workspace 공통 Owner와 기존 Paint Owner 역할 분리 |
| `build_tools/workspaces/` | Seed 생성·검증 스크립트 추가 |
| `blender_manifest.toml` | Version 갱신, Seed 배포 포함 검증 |
| `tests/` | 실행 순서, Legacy, 실패 Rollback, 설치 ZIP, 실데이터 GUI 회귀 추가 |
| `docs/` | 구현 현황, 테스트 명령, Release Note 갱신 |

사용자 요청 범위를 벗어난 Importer, Exporter, Alignment Solver, Paint Layer 구조는 수정하지 않는다.

## 12. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| `append_activate()` 후 Screen 전환이 Event Tick 뒤 완료됨 | 현재 단계형 Timer를 유지하고 Workspace·Screen Pointer 전환을 확인한 뒤 Layout 수정 |
| Binary Seed가 Source Review에서 불투명함 | 생성 절차, 내부 메타데이터, Background Validator, Hash 검증을 함께 관리 |
| Blender Patch Version에서 Seed 호환성이 달라짐 | Seed Schema와 생성 Blender Version 검사, 지원하지 않는 Version에서 명확히 중단 |
| 다른 Window가 삭제 대상 Workspace 사용 | 자동 삭제 금지, 사용 Window를 식별해 사용자에게 종료 또는 전환 요청 |
| Workspace 이름 충돌 | 이름을 식별자로 사용하지 않고 UUID와 Tool ID 사용 |
| Legacy View 상태를 복원하며 오염 재유입 | 검증된 도구 소유 View Snapshot 외의 Space 상태는 이전하지 않음 |
| Append 후 구성 실패로 고아 Workspace 발생 | 생성 Pointer 기록, Source 복귀, 소유권 확인 후 즉시 또는 지연 삭제 |
| 세 도구의 독립성이 공통 Factory로 훼손됨 | Factory는 Blender ID 생명주기만 담당하고 도구별 Layout·상태는 각 도구에 유지 |

## 13. 승인 기준

다음을 모두 만족해야 구조변경이 완료된 것으로 본다.

1. Production Source에서 현재 활성 Workspace 복제 호출이 제거되어 있다.
2. 세 도구가 각각 자기 Seed에서 별도 Workspace와 Screen ID를 생성한다.
3. 세 도구 Workspace의 Workspace Pointer와 Screen Pointer가 서로 다르다.
4. Align에서 Modifier로 이동한 뒤 실데이터 Point, Camera, Image가 보인다.
5. Modifier의 모든 지원 Object 타입 필터가 활성화되어 있다.
6. Align의 모든 Object 타입 필터는 Align 소유 Screen 안에서만 비활성화되어 있다.
7. Projection Paint가 실행 순서와 무관하게 동일한 4-Area Layout을 만든다.
8. 도구 종료 후 사용자 Workspace 핵심 상태가 진입 전과 같다.
9. Legacy Workspace가 안전하게 새 Schema로 교체된다.
10. 같은 이름의 사용자 Workspace와 다른 도구 Workspace를 수정하거나 삭제하지 않는다.
11. Seed 누락·손상·Layout 실패 시 Source Workspace로 복귀하고 고아 Runtime이 남지 않는다.
12. Background, GUI, 설치 ZIP, 실데이터 회귀가 모두 통과한다.
13. Debug 로그, 임시 Probe, 테스트용 Workspace와 임시 파일이 배포물에 남지 않는다.

## 14. 구현 순서 결정

구현은 `공통 Factory → Modifier → Align → Projection Paint → Legacy Migration → 전체 QC` 순서로 진행한다.

Modifier를 먼저 전환하는 이유는 현재 확인된 사용자 장애의 직접 발생 지점이기 때문이다. 다만 Modifier만 Object 필터를 강제로 켜는 수정으로 종료하지 않는다. 그 방식은 현재 증상만 가리며 Local View, Shading, Overlay 같은 다른 상속 상태를 계속 허용한다. 최종 완료 조건은 세 도구 모두 활성 Workspace 복제 경로를 제거하는 것이다.

## 15. 구현 결과

2026-08-28 기준 구조변경 구현과 자동 QC를 완료했다.

- 공통 `WorkspaceSeedSpec`과 Append·소유권·Schema·상태·Rollback·안전 삭제 계약을 `shared/blender_runtime/workspace_factory.py`에 구현했다.
- Blender 5.2에서 생성한 Align, Modifier, Projection Paint 전용 Seed 세 개를 배포 자원으로 추가했다.
- 세 도구 Production Source의 `bpy.ops.workspace.duplicate()` 호출을 모두 제거했다.
- Align은 자기 Screen에서만 모든 Object 타입 필터를 끄고, Modifier와 Projection Paint는 자기 Screen의 모든 Object 타입 필터를 켜며 Local View를 해제한다.
- Tool ID, UUID, Workspace Schema, Seed Schema와 Screen Owner가 모두 일치한 뒤에만 `READY`로 전환한다.
- 사용자·Align·Modifier·Projection Paint를 시작점으로 하는 전체 도구 전환 행렬을 Source와 격리 설치 ZIP 양쪽 Blender GUI에서 검증했다.
- Window별 직전 Workspace를 공통 Handoff가 추적해 일반 Blender Workspace 또는 다른 COLMAP Workspace를 직접 선택해도 이전 도구의 정식 종료를 완료한 뒤 선택 목적지를 유지한다.
- Align·Modifier의 기존 강제 복귀 호출과 Projection Paint 준비 중 강제 복귀를 비활성화했으며, 정상 `READY` Workspace는 보존하고 불완전한 `CREATING` Workspace만 정리한다.
- 고정 COLMAP TXT Fixture의 실제 Modifier Import, Frame All, Camera Review, Point Edit, Global Undo, BIN Export와 재진입 회귀를 통과했다.
- `build/colmap_tools-0.1.1.zip`을 생성했고 SHA-256은 `ECE34B202799B3EA82CDA6BAE72C07699F0ED659A6A2F736A6371E172AF679CA`이다.

자동 검증 결과는 정적·순수 Python 218개 통과, 세 Seed Validator 통과, Factory GUI 생명주기 통과, 전체 실행 순서 GUI 행렬 통과, 격리 설치본 등록·Seed·Small GICP·GUI 검사 통과다.

### 15.1 Blender 5.2 확인 제한

Blender 5.2는 `append_activate()`로 추가한 Workspace를 공식 `bpy.ops.workspace.delete()`로 제거할 때 `ID user decrement error: SRLayout` 내부 진단을 출력한다. Factory를 제외한 최소 재현과 저장·재로드 후 삭제에서도 동일했으며, 연산 결과는 `FINISHED`이고 대상 Workspace와 Screen ID가 모두 제거됨을 Pointer로 확인했다.

Screen 사용자 수를 직접 조작하거나 `fake_user`, `user_clear`, `batch_remove`, `orphans_purge`로 우회하면 고아 ID 또는 다른 사용자 데이터 손상 위험이 있어 사용하지 않는다. 제품은 [Blender 공식 Workspace 삭제 연산](https://docs.blender.org/api/5.2/bpy.ops.workspace.html#bpy.ops.workspace.delete)을 유지하고 삭제 뒤 두 ID의 부재를 검증한다.

### 15.2 남은 수동 승인 항목

- 사용자가 장애를 확인한 실데이터로 `Align → Modifier → Import → Frame All`을 실행하고 Camera 440개, Image 440개, Point3D 563,983개와 실제 화면 가시성을 최종 확인한다.
- 같은 데이터로 Projection Paint Target 진입, Brush 표시와 Point 숨김·복구를 확인한다.
- 실제 다중 Window에서 Legacy Workspace가 점유된 상태의 차단 메시지와 `.blend` 저장·재로드 뒤 전체 Extension 마이그레이션을 확인한다.

이 항목들은 구조변경 구현 누락이 아니라 사용자 보유 데이터와 실제 UI 조작이 필요한 최종 승인 회귀다.
