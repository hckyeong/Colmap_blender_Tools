# COLMAP Tools 독립 도구 통합 계획서

- 작성일: 2026-08-27
- 대상 Blender: 5.2 LTS
- 문서 상태: 구현 전 통합 기준안
- 대상 프로젝트: `COLMAP_Align`, `COLMAP_Modifier`, `COLMAP_Projection_Paint`

## 1. 문서 목적

현재 폴더에 복사된 세 Blender 도구를 하나의 Blender Extension과 하나의 Git 저장소로 통합하기 위한 제품 경계, 코드 구조, 이력 이전, 검증 절차와 완료 조건을 정의합니다.

세 도구는 순서대로 실행하는 파이프라인이 아닙니다. 서로의 데이터나 실행 결과를 전제로 하지 않는 독립 도구이며, 통합은 설치·배포·메뉴 진입점을 하나로 묶는 것을 의미합니다.

```text
COLMAP Tools
├─ COLMAP Align
├─ COLMAP Modifier
└─ Projection Painting
```

## 2. 핵심 결론

1. 최종 제품은 `COLMAP Tools`라는 단일 Blender Extension으로 배포합니다.
2. 세 도구는 독립된 기능 모듈, Blender 상태, Runtime Session, Workspace, Handler와 테스트를 소유합니다.
3. 통합 UI는 작업 순서를 제시하지 않고 세 도구의 독립 실행 진입점만 제공합니다.
4. 한 도구의 실행 여부, 데이터, 오류 또는 종료는 다른 도구의 실행 조건이 되지 않습니다.
5. 초기 통합에서는 기능 코드와 COLMAP 파서를 합치지 않고 현재 동작을 그대로 보존합니다.
6. 공통화는 동일한 동작 계약과 회귀 테스트가 확보된 코드에만 후속 적용합니다.
7. 기존 Operator ID와 Scene Property 이름은 첫 통합 버전에서 유지합니다.
8. 세 하위 Git 저장소를 그대로 중첩하거나 Submodule로 유지하지 않고 단일 Monorepo로 이전합니다.
9. 빌드 산출물, 가상환경, 실행 중 생성된 파일과 대용량 검증 자료는 Git에 포함하지 않습니다.
10. 첫 번째 통합 완료 기준은 기존 세 도구가 하나의 설치 ZIP에서 각각 독립적으로 동작하는 것입니다.

## 3. 현재 프로젝트 업무 경계

### 3.1 COLMAP Align

`COLMAP Align`은 여러 COLMAP 또는 OBJ 데이터를 가져와 정렬·검토·내보내는 독립 도구입니다.

주요 책임은 다음과 같습니다.

- COLMAP sparse model 가져오기와 검증
- OBJ 메시 가져오기와 표시
- A/P 듀얼 Viewport 표시와 독립 선택
- 대응 마커 기반 Sim(3) 정렬
- small_gicp 또는 선택형 Backend를 이용한 ICP 정렬
- 정렬 미리보기, 승인과 취소
- Data Layer 표시·선택·변환
- COLMAP 구조 병합 및 내보내기
- 정렬된 OBJ 내보내기
- 프로젝트 상태 저장과 복원

이 도구는 Modifier 또는 Projection Painting이 먼저 실행되었거나 특정 데이터를 제공했다고 가정하지 않습니다.

### 3.2 COLMAP Modifier

`COLMAP Modifier`는 하나의 COLMAP 재구성을 Blender에서 검토하고 포인트·카메라 관련 편집을 수행한 뒤 안전하게 내보내는 독립 도구입니다.

주요 책임은 다음과 같습니다.

- COLMAP 텍스트·바이너리 모델 가져오기
- Point3D와 Camera 장면 표시
- Point와 Camera 편집 모드
- 관측 Camera 역추적
- Camera Review와 Timeline
- Camera Filter와 편집 이력
- Undo/Redo 재결합
- 원본 지문 확인
- 백업·Staging·Rollback을 포함한 COLMAP Export
- 전용 Modifier Workspace 진입과 복원

이 도구는 Align의 정렬 결과 또는 Projection Painting의 Surface 상태를 요구하지 않습니다.

### 3.3 Projection Painting

`Projection Painting`은 COLMAP 카메라와 촬영 이미지를 Blender Texture Paint에 연결하는 독립 도구입니다.

주요 책임은 다음과 같습니다.

- COLMAP Camera, Image Pose와 Point3D 가져오기
- Camera Review와 배경 이미지 표시
- 독립 Projection Paint Workspace 진입과 복원
- Paint Target과 관리 Material 구성
- 단일 이미지와 UDIM 이중 Backend
- Original Layer, 일반 Layer, Mask와 Adjustment
- Stencil 연결과 최근접 Camera 선택
- Composite Layer 생성·재생성·해제
- 최종 Base Color Texture 내보내기
- 저장, Load, Undo/Redo 뒤 Runtime 재구축

이 도구는 Modifier의 Camera Review Session이나 Align의 Project Store를 직접 사용하지 않습니다.

## 4. 통합 목표

### 4.1 제품 목표

- 사용자는 Extension 하나만 설치합니다.
- Blender의 `Window → COLMAP Tools` 메뉴에서 세 도구를 각각 실행할 수 있습니다.
- 한 도구를 사용하지 않아도 나머지 도구를 완전하게 사용할 수 있습니다.
- 각 도구의 기존 기능, UI 의미와 파일 입출력 결과를 보존합니다.
- 각 도구는 기존 전용 Workspace를 독립적으로 생성·복원합니다.
- 통합 Extension의 등록·해제와 재설치를 일관되게 관리합니다.

### 4.2 개발 목표

- 단일 Git 저장소에서 기능 변경과 회귀 테스트를 원자적으로 관리합니다.
- 도구 사이의 직접적인 Runtime 의존을 금지합니다.
- 등록 Class, Handler, Timer, Draw Handler와 Keymap의 소유자를 명확하게 합니다.
- 공통화 여부를 파일명이나 코드 모양이 아니라 검증된 동작 계약으로 결정합니다.
- Blender Background, GUI와 설치본 검증을 하나의 테스트 체계로 통합합니다.
- 배포 ZIP에 소스와 실제 런타임 필수 파일만 포함합니다.

## 5. 비목표

첫 통합 작업에서는 다음을 수행하지 않습니다.

- 세 도구를 하나의 순차 Workflow 또는 Wizard로 변경
- Align 결과를 Modifier나 Projection Painting에 자동 전달
- 세 도구의 Scene 상태를 하나의 거대한 PropertyGroup으로 병합
- 세 도구의 Undo/Redo 이력을 하나로 통합
- 도구 사이에서 Runtime 객체나 Blender ID Pointer를 직접 공유
- 세 가지 COLMAP 파서를 즉시 하나로 교체
- 기존 Operator ID 일괄 변경
- 기존 `.blend`의 Property 경로를 근거 없이 변경
- UI 명칭과 조작 방식을 대규모로 재설계
- 기존 알고리듬이나 파일 출력 형식 변경
- Open3D 또는 small_gicp Backend 알고리듬 변경
- 새로운 기능 추가

## 6. 제품 UI 구조

### 6.1 공통 메뉴

Root Shell이 `Window → COLMAP Tools` 메뉴를 한 번만 등록합니다.

```text
Window
└─ COLMAP Tools
   ├─ COLMAP Align 열기
   ├─ COLMAP Modifier 열기
   └─ Projection Painting 열기
```

메뉴 항목 사이에는 선행·후행 관계를 나타내는 번호, 화살표, 단계 표시 또는 완료 상태를 넣지 않습니다.

### 6.2 도구별 Workspace

- `COLMAP Align`은 Align 전용 Workspace와 A/P Viewport를 소유합니다.
- `COLMAP Modifier`는 Modifier 전용 Workspace를 소유합니다.
- `Projection Painting`은 Paint 전용 Workspace를 소유합니다.
- Root Shell은 각 Workspace의 내부 Layout을 구성하거나 복구하지 않습니다.
- 각 도구는 자신이 생성한 Workspace와 Screen만 변경합니다.
- 도구 종료 시 해당 도구가 저장한 진입 전 Workspace를 복원합니다.
- 다른 도구의 Workspace를 잔여본으로 판단하여 삭제하지 않습니다.

### 6.3 독립 실행 원칙

- 각 도구의 `poll()`은 자기 도구에 필요한 조건만 검사합니다.
- 다른 도구의 Scene Property나 Runtime 존재 여부를 실행 조건으로 사용하지 않습니다.
- 한 도구의 진행 작업이 다른 도구와 실제 Blender 자원 충돌을 일으키는 경우에만 명시적으로 실행을 거부합니다.
- 단순히 같은 Extension에 포함되었다는 이유로 전역 단일 작업 잠금을 만들지 않습니다.

## 7. 목표 저장소 구조

```text
COLMAP_Tools/
├─ .git/
├─ .gitignore
├─ AGENTS.md
├─ README.md
├─ LICENSE
├─ __init__.py
├─ blender_manifest.toml
│
├─ shell/
│  ├─ __init__.py
│  ├─ lifecycle.py
│  ├─ menu.py
│  ├─ preferences.py
│  └─ diagnostics.py
│
├─ tools/
│  ├─ align/
│  │  ├─ __init__.py
│  │  ├─ lifecycle.py
│  │  ├─ core/
│  │  ├─ blender/
│  │  ├─ workers/
│  │  └─ resources/
│  ├─ modifier/
│  │  ├─ __init__.py
│  │  ├─ lifecycle.py
│  │  ├─ core/
│  │  ├─ blender/
│  │  └─ resources/
│  └─ projection_paint/
│     ├─ __init__.py
│     ├─ lifecycle.py
│     ├─ core/
│     ├─ blender/
│     └─ resources/
│
├─ shared/
│  ├─ __init__.py
│  ├─ contracts/
│  └─ blender_runtime/
│
├─ tests/
│  ├─ architecture/
│  ├─ align/
│  ├─ modifier/
│  ├─ projection_paint/
│  ├─ coexistence/
│  ├─ blender_background/
│  ├─ blender_gui/
│  └─ fixtures/
│
├─ docs/
│  ├─ README.md
│  ├─ architecture/
│  ├─ tools/
│  ├─ decisions/
│  ├─ migration/
│  └─ archive/
│
├─ build_tools/
└─ build/
```

### 7.1 Root Shell 책임

`shell/`은 다음 책임만 가집니다.

- 단일 Extension Root `register()`와 `unregister()` 조정
- 공통 `COLMAP Tools` 메뉴 등록과 해제
- Add-on Preferences 등록
- 도구별 Lifecycle 호출 순서와 실패 Rollback
- 설치본 버전과 진단 정보 표시
- 공통 자원 소유권 규칙 제공

Root Shell은 다음 책임을 가지지 않습니다.

- COLMAP 데이터 가져오기 또는 쓰기
- 정렬, 편집, 페인트 알고리듬
- 각 도구의 Scene 상태 해석
- 각 도구의 Workspace Layout 생성
- 도구별 Undo, Handler 또는 진행 상태 제어

### 7.2 도구 모듈 책임

각 `tools/<tool>/` 패키지는 다음을 독립적으로 소유합니다.

- 기능 Core와 Blender Adapter
- Blender Class 목록
- Scene Property
- Runtime Registry와 Session
- Handler, Timer, Draw Handler와 Keymap
- Workspace 진입·종료·복구
- 오류 상태와 사용자 메시지
- 도구별 테스트 Fixture와 검증 절차

## 8. 의존성 규칙

### 8.1 허용 의존 방향

```text
Root __init__
└─ shell.lifecycle
   ├─ tools.align.lifecycle
   ├─ tools.modifier.lifecycle
   └─ tools.projection_paint.lifecycle

각 도구 blender 계층
└─ 같은 도구 core 계층
   └─ 검증된 shared 계층
```

### 8.2 금지 의존

- `tools.align`에서 `tools.modifier` 또는 `tools.projection_paint` import 금지
- `tools.modifier`에서 `tools.align` 또는 `tools.projection_paint` import 금지
- `tools.projection_paint`에서 `tools.align` 또는 `tools.modifier` import 금지
- 도구 Lifecycle에서 다른 도구 Lifecycle 직접 호출 금지
- 도구 Runtime Registry에 다른 도구의 Session 저장 금지
- 다른 도구의 Scene Property를 읽어 실행 여부를 결정하는 코드 금지
- 다른 도구가 만든 Blender ID를 이름만으로 찾아 수정·삭제하는 코드 금지
- `bl_ext`, 설치 Repository 이름 또는 절대 Extension Module 이름 하드코딩 금지

이 규칙은 정적 Architecture 테스트로 고정합니다.

## 9. 상태와 데이터 소유권

### 9.1 첫 통합 버전의 Scene Property

기존 `.blend` 호환성을 위해 첫 통합 버전에서는 현재 Property 이름을 유지합니다.

```text
Scene.colmap_align_settings
Scene.colmap_import
Scene.projection_paint
```

Root Shell은 별도의 통합 작업 상태를 Scene에 만들지 않습니다. 공통 설정이 필요하면 Add-on Preferences에 저장하며, 도구별 프로젝트 상태는 해당 도구 Property에 유지합니다.

### 9.2 Runtime 상태

- Align Runtime은 Align 패키지에서만 접근합니다.
- Modifier Runtime은 Modifier 패키지에서만 접근합니다.
- Projection Painting Runtime은 Projection Paint 패키지에서만 접근합니다.
- Window별 Session은 해당 도구가 자체 Key와 Registry로 관리합니다.
- Driver Namespace를 사용할 경우 도구별로 충돌하지 않는 고유 Key를 사용합니다.
- 파일 Load, Undo/Redo와 Extension 해제 시 각 도구는 자기 Runtime만 정리합니다.

### 9.3 Blender 데이터 소유권

- 각 도구가 생성한 Object, Collection, Material, Image, Node와 Workspace에는 고유 도구 Prefix와 소유 태그를 기록합니다.
- 정리는 이름이 아니라 소유 태그, 역할과 대상 UUID를 검증한 뒤 수행합니다.
- 다른 도구의 Prefix 또는 소유 태그가 있는 데이터는 수정·삭제하지 않습니다.
- 사용자 데이터와 소유권을 확인할 수 없는 데이터는 자동 삭제하지 않습니다.

권장 소유 Prefix는 다음과 같습니다.

```text
COLMAP_ALIGN
COLMAP_MODIFIER
PROJECTION_PAINT
```

## 10. Blender 등록과 해제

### 10.1 Root 등록 순서

```text
1. Root Preferences와 공통 Menu Class 등록
2. Align Lifecycle 등록
3. Modifier Lifecycle 등록
4. Projection Painting Lifecycle 등록
5. Window 메뉴 Callback 등록
6. Extension 등록 완료 표시
```

등록 도중 예외가 발생하면 성공한 단계를 역순으로 해제하고 예외를 다시 전달합니다. 부분적으로 등록된 상태를 정상 상태처럼 유지하지 않습니다.

### 10.2 Root 해제 순서

```text
1. Window 메뉴 Callback 해제
2. Projection Painting Lifecycle 해제
3. Modifier Lifecycle 해제
4. Align Lifecycle 해제
5. Root Menu Class와 Preferences 해제
```

### 10.3 도구 Lifecycle 계약

각 도구 Lifecycle은 최소 다음 공개 함수를 제공합니다.

```text
register()
unregister()
is_registered()
```

요구 사항은 다음과 같습니다.

- 중복 `register()` 호출은 같은 Class, Handler 또는 Timer를 중복 등록하지 않습니다.
- `unregister()`는 자기 도구가 등록한 자원만 역순으로 해제합니다.
- 등록 실패 시 해당 도구가 성공적으로 등록한 자원을 모두 되돌립니다.
- 실행 중 작업이 있으면 기존 도구의 검증된 종료·Rollback 정책을 사용합니다.
- 다른 도구의 오류 상태나 Runtime을 정리하지 않습니다.

### 10.4 Runtime 실패 독립성

Extension 등록은 원자적으로 처리하지만 등록 완료 뒤의 기능 실행은 독립적이어야 합니다.

- Align Import 실패가 Modifier와 Projection Painting을 비활성화하지 않습니다.
- Modifier Export 실패가 Align Session을 종료하지 않습니다.
- Projection Bake 실패가 Align 또는 Modifier의 Scene 데이터를 변경하지 않습니다.
- 한 도구의 Workspace 복원 실패가 다른 도구의 Workspace를 삭제하지 않습니다.

## 11. Operator와 UI 식별자 호환성

첫 통합 버전은 기존 Operator Namespace를 유지합니다.

```text
colmap_align.*
colmap.*
projection_paint.*
```

기존 Panel, UIList와 Gizmo ID도 충돌이 없는 경우 유지합니다.

새 Root Shell 전용 식별자만 다음 Prefix를 사용합니다.

```text
colmap_tools.*
COLMAPTOOLS_*
```

기존 식별자 변경이 필요하면 별도 호환성 계획, `.blend` 재열기 검증과 Keymap 이전 절차를 먼저 작성합니다.

## 12. 공통화 원칙

### 12.1 초기 원칙

첫 통합 단계에서는 세 도구의 기존 Core와 Blender 구현을 도구 패키지 안에 그대로 보존합니다. 같은 이름의 파일이 있다는 이유만으로 합치지 않습니다.

특히 다음 영역은 즉시 공통화하지 않습니다.

- COLMAP 데이터 모델
- Binary/Text Reader와 Writer
- Dataset Discovery
- Axis 변환
- Camera Pose 변환
- Fingerprint와 Validation
- Import/Export 진행 Session
- Progress Overlay
- Lifecycle Registry
- State Handler와 Undo 처리

### 12.2 공통화 승인 조건

코드를 `shared/`로 이동하려면 다음 조건을 모두 만족해야 합니다.

1. 둘 이상의 도구가 실제로 같은 책임을 사용합니다.
2. 입력과 출력 타입의 의미가 같습니다.
3. 예외와 실패 처리 계약이 같습니다.
4. 성능과 메모리 요구가 호환됩니다.
5. Blender Context 요구 사항이 같습니다.
6. 각 도구의 기존 결과와 동일함을 검증하는 회귀 테스트가 있습니다.
7. 공통 모듈이 어느 한 도구의 Runtime을 import하지 않습니다.
8. 공통화 후 각 도구를 독립적으로 등록·테스트할 수 있습니다.

### 12.3 공통화 후보

초기 통합이 완료된 뒤 다음 영역을 후보로 검토합니다.

- 도구 독립적인 COLMAP Camera Model 정의
- TXT/BIN 입력의 저수준 숫자·문자열 검증
- 비연속 ID와 관측 관계 검증 규칙
- 안전한 임시 파일 확정과 원자적 파일 교체 유틸리티
- 도구 Prefix를 받는 일반 Lifecycle 자원 원장
- Extension 사용자 저장 경로 생성 유틸리티
- 공통 확인창의 텍스트 줄바꿈처럼 제품 의미가 없는 UI 유틸리티

후보라는 이유만으로 이동하지 않으며, 먼저 계약 테스트를 작성합니다.

## 13. COLMAP Core 통합 검토 기준

세 도구의 COLMAP 구현은 목적과 표현 방식이 다르므로 별도 검토 단계에서 다음 Fixture를 사용해 비교합니다.

- TXT와 BIN sparse model
- legacy `cameras/images/points3D` 3파일 모델
- modern `rigs/cameras/frames/images/points3D` 5파일 모델
- 비연속 Camera, Image와 Point3D ID
- 비어 있거나 관측이 없는 Point3D
- 공백·한글·중첩 경로가 포함된 Image 이름
- 공유 Camera Intrinsics
- 여러 Rig와 Frame
- 손상된 Track과 `POINT2D_IDX`
- 매우 큰 Point3D 모델
- 읽기→쓰기→재읽기 왕복 모델

정규화된 비교 결과에는 다음을 포함합니다.

- Entity 수와 ID 집합
- Camera Model과 Parameter
- Image Pose와 Camera 참조
- Point2D 좌표와 Point3D 참조
- Point3D 위치·색상·오차와 Track
- Rig, Sensor와 Frame 관계
- 원본 파일 형식과 경로 판정
- 오류 종류와 실패 지점

공통 구현을 선정하더라도 도구별 View 또는 Adapter를 허용합니다.

- Align은 대용량 NumPy PointCloud View를 사용할 수 있습니다.
- Modifier는 편집 가능한 Reconstruction과 Export Transaction을 사용할 수 있습니다.
- Projection Painting은 Camera/Image 중심의 읽기 전용 View를 사용할 수 있습니다.

공통 Core가 세 도구의 내부 표현까지 동일하게 만들 필요는 없습니다.

## 14. Git 저장소 이전 계획

### 14.1 현재 상태 보호

현재 루트에는 Git 저장소가 없고 세 하위 폴더가 각각 독립 Git 저장소입니다. 세 저장소 모두 커밋되지 않은 변경이 있으므로 파일 이동이나 `.git` 삭제 전에 각 저장소를 보존해야 합니다.

필수 선행 조치는 다음과 같습니다.

1. 각 저장소의 현재 Branch와 `git status`를 기록합니다.
2. 추적 파일과 미추적 소스 파일을 검토합니다.
3. 현재 유효 변경을 Checkpoint Commit으로 보존합니다.
4. 활성 Branch를 명시한 Git Bundle을 저장소 밖에 만듭니다.
5. Bundle에서 Commit History를 복원할 수 있는지 검증합니다.
6. 기존 세 폴더를 읽기 전용 원본으로 별도 보관합니다.

Align 저장소에는 깨진 임시 Codex Ref와 많은 도달 불가능 객체가 있으므로 `--all` 전체 객체 복사보다 활성 Branch를 명시한 깨끗한 Clone 또는 Bundle을 사용합니다.

### 14.2 Monorepo 생성

권장 절차는 다음과 같습니다.

1. 빈 `COLMAP_Tools` 저장소를 생성합니다.
2. 공통 `.gitignore`, `README.md`, `LICENSE`와 Manifest 골격을 추가합니다.
3. 정리한 Align 이력을 임시 `legacy/align` Prefix로 가져옵니다.
4. 정리한 Modifier 이력을 임시 `legacy/modifier` Prefix로 가져옵니다.
5. 정리한 Projection Painting 이력을 임시 `legacy/projection_paint` Prefix로 가져옵니다.
6. 이력 연결과 파일 누락 여부를 검증합니다.
7. 검증된 `git mv`로 목표 `tools/<tool>/` 위치에 이동합니다.
8. 각 이동 단계마다 해당 도구의 기존 테스트를 실행합니다.

History 정리가 어렵거나 활성 저장소 크기를 최소화해야 하면 원본 Git Bundle을 별도 보관하고 현재 Source Snapshot을 세 개의 명시적인 Import Commit으로 가져오는 방식을 사용할 수 있습니다.

### 14.3 Git에 포함하지 않을 항목

```text
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
build/
dist/
.artifacts/
.build_addon/
.venv*/
.small-gicp-runtime/
.small-gicp-runtime-cache/
.small-gicp-runtime-build/
*.zip
.codex_*
```

검증용 `.blend`, 대형 Texture와 실제 Dataset이 자동 테스트에 반드시 필요하면 일반 Source History에 넣지 않고 별도 Fixture 저장소 또는 Git LFS 사용 여부를 결정합니다.

### 14.4 Submodule 비권장

세 도구를 Git Submodule로 유지하지 않습니다.

이유는 다음과 같습니다.

- 단일 Extension 버전 변경을 원자적으로 커밋하기 어렵습니다.
- Root Lifecycle과 각 도구의 호환 버전을 별도로 맞춰야 합니다.
- 설치 ZIP 생성 시 여러 저장소 상태를 조합해야 합니다.
- 공통 테스트와 향후 제한적 공통화가 복잡해집니다.
- 사용자가 하나의 제품을 수정하면서 여러 저장소 Commit을 관리해야 합니다.

## 15. Extension Manifest와 버전

### 15.1 권장 초기 Manifest

```toml
schema_version = "1.0.0"

id = "colmap_tools"
version = "0.1.0"
name = "COLMAP Tools"
tagline = "독립적인 COLMAP 정렬 편집 프로젝션 페인트 도구 모음"
maintainer = "COLMAP Tools Contributors"
type = "add-on"

blender_version_min = "5.2.0"

license = [
  "SPDX:GPL-3.0-or-later",
]

[permissions]
files = "COLMAP 모델 이미지 메시와 텍스처 파일 입출력에 필요합니다"
```

최종 `id`, `maintainer`, 지원 Platform과 Website는 첫 배포 전에 확정합니다.

### 15.2 버전 정책

- 통합 Extension은 기존 개별 도구와 별도의 Semantic Version을 사용합니다.
- 첫 통합 개발 버전 후보는 `0.1.0`입니다.
- 개별 도구의 과거 버전은 Migration 문서와 Git Tag로 보존합니다.
- Manifest 버전을 통합 제품의 단일 배포 기준으로 사용합니다.
- 코드에서 버전을 표시해야 하면 Manifest와의 일치 여부를 빌드 테스트로 검증합니다.
- 기능 하나만 변경되더라도 Extension 전체 버전을 증가시킵니다.

### 15.3 라이선스

현재 Manifest의 GPL 선언에 맞는 실제 Root `LICENSE` 파일을 추가합니다.

다음 항목을 별도로 확인합니다.

- 세 프로젝트 소스의 저작권과 기존 라이선스
- small_gicp 라이선스와 배포 조건
- Open3D 라이선스와 선택형 Backend 배포 조건
- 포함하는 Python Runtime 또는 Wheel 라이선스
- 이미지, 테스트 Dataset과 문서 Screenshot의 배포 권리

## 16. 외부 의존성과 Build Profile

### 16.1 초기 통합 원칙

- 초기 통합은 Align의 검증된 Backend 동작을 변경하지 않습니다.
- 외부 Runtime과 Wheel 전환은 단일 Extension 동작 검증 이후 별도 결정합니다.
- Runtime, Wheel과 Build 산출물은 Source Tree에 자동 생성하되 Git에 Commit하지 않습니다.
- Production 배포에 실제 필요한 파일만 Build Staging에 복사합니다.

### 16.2 small_gicp

small_gicp의 Blender Python 호환 Wheel 사용 가능 여부, In-process 실행 안정성, UI 응답성과 외부 Process 장애 격리를 비교합니다.

검증 항목은 다음과 같습니다.

- Blender 5.2 Python ABI 호환성
- 동일 입력의 정렬 결과와 품질 지표
- 큰 점군 실행 중 Blender 응답성
- 취소와 종료 처리
- Native 예외 발생 시 Blender Process 영향
- Windows 이외 Platform 지원 범위
- 설치 ZIP 크기

검증 전에는 기존 외부 Worker 방식을 제거하지 않습니다.

### 16.3 Open3D

현재 UI에서 비활성화된 Open3D Backend는 Production 기본 구성에 포함하지 않습니다. 개발·검증 Profile로 유지할 경우 사용자 배포판과 분리하고 의존성 크기와 Python ABI를 명시합니다.

### 16.4 공식 Extension Build

최종 Build Pipeline은 Blender Extension 명령을 사용해 다음 단계를 수행합니다.

```text
1. Manifest와 Source Tree 검증
2. Python Syntax와 Architecture 검사
3. Blender Extension validate
4. Blender Extension build
5. 생성 ZIP 내부 파일 목록 검사
6. 새 사용자 Repository에 install-file
7. Blender 재시작 뒤 설치본 Smoke Test
```

## 17. 테스트 전략

### 17.1 기존 도구 회귀 테스트

통합 과정에서 각 도구의 기존 테스트를 먼저 그대로 실행할 수 있어야 합니다.

- Align Core·Blender·Open3D/Worker·GUI 테스트
- Modifier Core·Blender Background·GUI·성능 테스트
- Projection Painting Core·Architecture·Blender Background·GUI 테스트

파일 이동 단계에서는 Import 경로 변경 외에 테스트 기대값을 변경하지 않습니다.

### 17.2 Architecture 테스트

다음 규칙을 정적으로 검사합니다.

- 도구 사이 직접 Import가 없습니다.
- 각 도구 Core는 `bpy`를 Import하지 않습니다.
- Root Shell은 도구의 구체 Core 모듈을 Import하지 않습니다.
- 등록 가능한 Class ID가 중복되지 않습니다.
- Scene Property 이름이 중복되지 않습니다.
- Driver Namespace Key와 소유 태그 Prefix가 중복되지 않습니다.
- `bl_ext` 또는 Repository Module 이름이 하드코딩되지 않습니다.
- Build 제외 경로가 Manifest에 반영됩니다.

### 17.3 공존 테스트

단일 Extension에서 다음을 검증합니다.

1. 세 도구 전체 등록과 역순 해제
2. 등록·해제 두 번 반복 후 Class, Handler와 Timer 중복 없음
3. `Window → COLMAP Tools` 메뉴가 정확히 하나만 표시됨
4. 메뉴에 세 도구 실행 항목이 각각 한 번만 표시됨
5. Align만 실행하고 종료
6. Modifier만 실행하고 종료
7. Projection Painting만 실행하고 종료
8. Align 종료 후 Modifier 실행
9. Modifier 종료 후 Projection Painting 실행
10. Projection Painting 종료 후 Align 실행
11. 한 도구 오류 뒤 다른 도구 실행 가능
12. 한 도구의 Scene 상태가 다른 도구 실행 후 변하지 않음
13. 한 도구의 Workspace 종료가 다른 도구 Workspace를 삭제하지 않음
14. 파일 Load 뒤 각 도구 Runtime이 자기 상태만 재구축
15. Undo/Redo 뒤 다른 도구의 Handler가 데이터를 변경하지 않음
16. 여러 Blender Window에서 도구별 Session이 올바르게 격리됨

### 17.4 설치본 테스트

- 깨끗한 Blender 사용자 Repository에 ZIP 설치
- Extension 활성화와 비활성화
- Blender 재시작 뒤 자동 등록
- 세 메뉴 진입점 표시
- 각 도구 최소 Import 또는 Workspace 진입 Smoke Test
- Extension 제거 뒤 등록 Class, Handler, Timer와 Keymap 잔여 없음
- Build ZIP에 테스트, 문서 Archive, Cache와 대용량 Artifact가 포함되지 않음

## 18. 문서 구조

### 18.1 현행 문서

```text
docs/
├─ README.md
├─ architecture/
│  ├─ PRODUCT_BOUNDARIES.md
│  ├─ PACKAGE_STRUCTURE.md
│  ├─ LIFECYCLE.md
│  └─ OWNERSHIP.md
├─ tools/
│  ├─ ALIGN.md
│  ├─ MODIFIER.md
│  └─ PROJECTION_PAINT.md
├─ decisions/
├─ migration/
└─ archive/
```

### 18.2 문서 권위

- `docs/README.md`에서 현행 운영 문서와 과거 계획서를 명확하게 구분합니다.
- `docs/tools/`는 현재 동작과 지원 범위를 설명합니다.
- `docs/architecture/`는 현재 코드의 실제 경계와 의존 규칙을 설명합니다.
- `docs/decisions/`에는 통합 과정의 주요 선택을 ADR로 기록합니다.
- 과거 계획서와 결과 보고서는 `docs/archive/<tool>/`로 이동합니다.
- Archive 문서는 현재 구현의 권위 자료로 사용하지 않습니다.
- 버전이 바뀌면 Manifest, Root README와 해당 도구 현행 문서를 함께 갱신합니다.

### 18.3 필수 ADR

- ADR-0001: 단일 Monorepo와 단일 Extension 선택
- ADR-0002: 세 도구의 독립 제품 경계
- ADR-0003: Root Lifecycle과 도구 Lifecycle 책임
- ADR-0004: 기존 Operator와 Scene Property 호환 정책
- ADR-0005: COLMAP Core 공통화 판단 기준
- ADR-0006: small_gicp 배포와 Process 격리 정책
- ADR-0007: Git 이력 정리와 대용량 Artifact 정책

## 19. 단계별 구현 계획

### 단계 0. 원본 보존과 기준점 고정

작업:

- 세 저장소의 현재 Branch, Commit, Status와 미추적 파일 목록 기록
- 유효 변경 Checkpoint Commit
- 활성 Branch Git Bundle 생성과 복원 검증
- 현재 배포 ZIP과 주요 실제 파일 SHA-256 기록
- 도구별 테스트 명령과 Blender 실행 경로 기록
- 기존 폴더 읽기 전용 백업

완료 조건:

- 파일 이동 실패 시 각 프로젝트를 현재 상태로 복원할 수 있습니다.
- 커밋되지 않은 소스 변경이 누락되지 않았습니다.

### 단계 1. 깨끗한 Monorepo 생성

작업:

- 새 Root Git 저장소 생성
- 공통 `.gitignore`, `README.md`, `LICENSE` 추가
- 정리된 세 이력을 임시 Prefix로 가져오기
- 빌드·가상환경·Artifact와 ZIP 제외
- 파일과 History 검증

완료 조건:

- Root 저장소 하나로 세 소스 이력을 조회할 수 있습니다.
- 중첩 `.git`과 Submodule이 없습니다.
- Source Checkout에 대용량 실행 Artifact가 없습니다.

### 단계 2. 도구별 패키지 이동

작업:

- Align을 `tools/align/`으로 이동
- Modifier를 `tools/modifier/`로 이동
- Projection Painting을 `tools/projection_paint/`로 이동
- Extension Namespace에 맞게 상대 Import 경로 수정
- 기존 Operator와 Scene Property ID 유지
- 각 도구를 별도 테스트 Harness에서 등록 가능하게 유지

완료 조건:

- 세 도구의 기존 자동 테스트가 각각 통과합니다.
- 기능 알고리듬과 UI 기대값 변경이 없습니다.
- 도구 사이 직접 Import가 없습니다.

### 단계 3. Root Shell과 단일 메뉴 도입

작업:

- Root `__init__.py`와 단일 Manifest 작성
- Root Preferences와 Menu Class 작성
- 세 도구 Lifecycle을 순차 호출하는 Root Lifecycle 작성
- 기존 외부 메뉴 탐색, Fallback 메뉴와 메뉴 연결 재시도 Timer 제거
- `Window → COLMAP Tools`에 세 실행 버튼 추가
- 등록 실패 역순 Rollback 구현

완료 조건:

- 메뉴가 하나만 등록됩니다.
- 세 도구 진입점이 순서 의미 없이 독립적으로 표시됩니다.
- Extension 등록·해제 반복 뒤 잔여 자원이 없습니다.

### 단계 4. Lifecycle과 소유권 격리

작업:

- 도구별 Handler, Timer, Draw Handler와 Keymap 원장 검토
- Driver Namespace Key 충돌 제거
- Blender 데이터 소유 태그와 Prefix 검사
- File Load와 Undo/Redo Handler의 변경 범위 고정
- Workspace 생성·종료 시 다른 도구 데이터 불변 테스트
- 교차 도구 Runtime Import 금지 테스트

완료 조건:

- 한 도구의 종료와 오류가 다른 도구의 Runtime을 정리하지 않습니다.
- 각 도구의 등록 자원과 Blender 데이터 소유자를 추적할 수 있습니다.

### 단계 5. 단일 Build와 설치본 검증

작업:

- Manifest Build 제외 규칙 작성
- 기존 Align Backend Staging을 통합 Build에 연결
- Extension validate와 build 자동화
- ZIP 내부 계약 검사
- 새 사용자 Repository 설치 자동화
- Blender 재시작 뒤 세 도구 Smoke Test

완료 조건:

- 사용자가 ZIP 하나만 설치합니다.
- 설치본에서 세 도구가 각각 독립 실행됩니다.
- ZIP에 테스트, Cache, Git, Artifact와 불필요한 Runtime이 없습니다.

### 단계 6. 공존 회귀 테스트 확정

작업:

- 세 도구 실행 순열 검증
- Runtime 실패 독립성 검증
- Save, Load, Undo/Redo와 Blender 재시작 검증
- 기존 실제 `.blend`와 Dataset 검증
- 다중 Window 검증
- 설치 교체와 이전 버전 데이터 검증

완료 조건:

- 공존 테스트 전체가 통과합니다.
- 기존 개별 설치본과 비교하여 허용되지 않은 동작 차이가 없습니다.

### 단계 7. 제한적 공통화 검토

작업:

- 중복 후보 목록 작성
- 공통 COLMAP Fixture와 정규화 비교 테스트 작성
- 입력·출력·실패·성능 계약 비교
- 공통화 가능한 최소 단위만 `shared/`로 이동
- 도구별 Adapter 유지

완료 조건:

- 공통화 전후 각 도구 결과가 같습니다.
- 도구 독립 등록과 테스트가 유지됩니다.
- 공통화로 도구 간 Runtime 의존이 생기지 않습니다.

이 단계는 단일 Extension 배포 완료를 막지 않는 후속 작업입니다.

### 단계 8. 문서와 Legacy 정리

작업:

- Root 현행 문서 완성
- 과거 계획서와 결과 보고서를 도구별 Archive로 이동
- 개별 프로젝트 빌드 스크립트 중 대체된 항목 제거
- 기존 독립 설치본과 통합 설치본의 동시 설치 정책 문서화
- 최종 Release Checklist 작성

완료 조건:

- 현재 동작의 권위 문서와 과거 계획서가 구분됩니다.
- 새 개발자가 Root README에서 각 도구의 경계와 검증 방법을 찾을 수 있습니다.

## 20. 주요 위험과 대응

| 위험 | 영향 | 대응 |
|---|---|---|
| 현재 미커밋 변경 누락 | 최신 기능 또는 수정 손실 | 도구별 Checkpoint Commit과 Bundle을 먼저 생성 |
| 중첩 `.git`을 그대로 추가 | Submodule 또는 불완전한 History 생성 | 깨끗한 Clone/Bundle 기반 이력 이전 |
| Align 임시 Git 객체 복사 | 저장소가 불필요하게 수백 MiB 증가 | 활성 Branch의 도달 가능한 객체만 이전 |
| 빌드 ZIP과 Artifact 포함 | 저장소와 배포본 비대화 | Root `.gitignore`, Manifest 제외 규칙과 ZIP 검사 |
| 세 등록 체계 충돌 | Extension 활성화 실패 | Root Lifecycle과 역순 Rollback |
| 메뉴 소유권 중복 | 메뉴 중복 또는 로드 순서 의존 | Root Shell만 공통 메뉴 등록 |
| Handler가 다른 도구 상태 변경 | 데이터 손상과 재현 어려운 오류 | 도구별 소유 Registry와 공존 테스트 |
| Undo/Redo 범위 충돌 | 다른 도구의 작업 상태 소실 | 도구별 Handler 범위 유지와 교차 Undo 테스트 |
| Workspace 정리 범위 오류 | 다른 도구 또는 사용자 Workspace 삭제 | 소유 Pointer·태그 검증 후 자기 Workspace만 정리 |
| COLMAP 파서 조기 통합 | 입력·출력 호환성 또는 성능 회귀 | 첫 통합에서는 보존, 계약 테스트 후 공통화 |
| Operator ID 일괄 변경 | 기존 Keymap·테스트·`.blend` 호환성 손상 | 첫 버전에서 ID 유지 |
| Extension Namespace 하드코딩 | 설치 Repository에 따라 Import 실패 | 상대 Import와 `__package__` 사용 |
| 버전 정보 불일치 | 잘못된 설치본 검증 | Manifest 단일 배포 버전과 자동 일치 검사 |
| 외부 Native 의존성 ABI 불일치 | Align Backend 실행 실패 | Blender ABI별 Wheel/Runtime 검사와 Smoke Test |
| 문서의 과거 계획과 현행 동작 혼동 | 잘못된 유지보수 판단 | 현행 문서와 Archive 분리 |

## 21. 최종 완료 조건

다음 조건을 모두 만족해야 통합을 완료로 판단합니다.

1. Blender Extension ZIP가 하나입니다.
2. Manifest와 Root `__init__.py`가 Extension 규칙에 맞습니다.
3. `Window → COLMAP Tools` 메뉴가 정확히 하나만 표시됩니다.
4. 메뉴에서 Align, Modifier와 Projection Painting을 독립 실행할 수 있습니다.
5. 각 도구는 다른 도구의 데이터나 실행 결과 없이 동작합니다.
6. 세 도구 사이 직접 Python Import가 없습니다.
7. 각 도구의 Scene Property, Runtime Session과 Workspace 소유권이 분리되어 있습니다.
8. 기존 Operator ID와 첫 통합 대상 `.blend` 데이터가 호환됩니다.
9. 한 도구의 실행 실패가 다른 도구의 Runtime과 데이터를 변경하지 않습니다.
10. 등록·해제 반복 뒤 Class, Handler, Timer, Draw Handler와 Keymap 잔여가 없습니다.
11. 각 도구의 기존 자동 테스트가 통과합니다.
12. Architecture와 공존 테스트가 통과합니다.
13. Blender Background와 실제 GUI 검증이 통과합니다.
14. 설치 ZIP로 새 사용자 Repository에서 같은 결과를 재현할 수 있습니다.
15. Build ZIP에 Git, 테스트, Cache, 가상환경과 대용량 Artifact가 없습니다.
16. 기존 세 저장소의 이력과 통합 전 상태를 복원할 수 있습니다.
17. Root `LICENSE`와 외부 의존성 라이선스가 정리되어 있습니다.
18. 현행 문서와 과거 계획 Archive가 명확하게 구분됩니다.

## 22. 구현 시작 전 확정 항목

구현 착수 전에 다음 값을 확정합니다.

1. 최종 Extension 표시 이름: 기본안 `COLMAP Tools`
2. 최종 Extension ID: 기본안 `colmap_tools`
3. 첫 통합 배포 버전: 기본안 `0.1.0`
4. 첫 공식 지원 Platform: 기본안 `Windows x64`
5. 첫 통합에서 지원할 Blender 범위: 기본안 `5.2 LTS 이상`
6. 기존 개별 Extension과 통합 Extension의 동시 설치 허용 여부
7. 원본 Git Bundle과 대용량 실제 검증 자료의 보관 위치
8. 통합 저장소의 원격 Repository와 Branch 정책

## 23. 권장 첫 구현 범위

첫 구현은 다음 범위로 제한합니다.

```text
포함
├─ 원본 Git 상태 보존
├─ 단일 Monorepo
├─ 도구별 패키지 이동
├─ 단일 Manifest
├─ Root Lifecycle
├─ 단일 COLMAP Tools 메뉴
├─ 기존 Operator와 Scene Property 유지
├─ 세 도구 개별 회귀 테스트
├─ 공존 테스트
└─ 단일 설치 ZIP

제외
├─ 도구 간 데이터 전달
├─ 순차 Workflow UI
├─ 공통 COLMAP Core 교체
├─ 알고리듬 변경
├─ 기능 추가
├─ 기존 Property Schema 재설계
└─ 외부 Backend 교체
```

이 범위를 통과한 뒤에만 중복 제거와 공통 Core 후보를 검토합니다. 통합의 성공 기준은 코드 양 감소가 아니라 세 독립 도구의 기존 동작과 실패 경계를 보존한 단일 배포입니다.
