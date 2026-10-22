# COLMAP Tools 완료 기능·구조 통합 문서

- 정리일: 2026-09-09
- 기준: `main`, `fd2d9cf`, COLMAP Tools **0.3.41**
- 지원 기준: Windows x64, Blender 5.2 이상. 기록된 실행 검증 환경은 Blender 5.2.0 LTS다.
- 범위: 기존 문서 107개의 완료 기능, 현행 설계, 검증 결과와 남은 확인 사항을 정리한다. 중복·완료 문서 98개를 정리하고 후속 집필·검증에 필요한 원문 9개만 `docs/temp/`에 남겼다. 이전 원문 전체는 Git 커밋 `fd2d9cf`에 보존되어 있다.

현재 동작을 확인할 때는 이 문서를 먼저 읽는다. 설치·조작 절차는 [사용자 매뉴얼](manual/README.md)을 참고한다. 보관 문서의 첫머리에 남아 있는 `구현 전`, `실험판`, 구버전 설명은 후속 구현 기록과 다를 수 있다. 이 문서에서는 최신 완료 기록과 현재 코드로 차이를 확인했다. 계획만 있는 항목, 미달한 성능 목표, 별도 사용자 검수가 필요한 부분은 완료 기능과 구분한다.

## 찾아보기

- 0.3.83 선택적 배율 보정: [Sim(3) 정밀 정렬 변경·검증](COLMAP_Align_Sim3_배율보정.md). 기본 강체 모드를 유지하며 별도 옵션을 켜면 회전·이동·균일 배율을 함께 계산한다.

- [제품 구조와 개발 규칙](#boundaries)
- [작업공간·세션·카메라 격리](#workspaces)
- [COLMAP Align](#align)
- [COLMAP Modifier](#modifier)
- [Projection Painting](#projection)
- [저장·복원·외부 파일 의존성](#storage)
- [빌드·배포](#build)
- [검증 결과와 재현 방법](#verification)
- [완료된 주요 변경과 대체 관계](#history)
- [미완료·미검증·미채택 항목](#pending)
- [남긴 자료와 이전 문서 복원](#archive)

<a id="boundaries"></a>
## 1. 제품 구조와 유지할 규칙

`COLMAP Tools`는 세 도구를 하나의 Blender Extension으로 배포한다. 사용자가 세 도구를 반드시 순서대로 실행할 필요는 없다.

| 도구 | 역할 | 데이터 경계 | 진입 오퍼레이터 |
|---|---|---|---|
| COLMAP Align | 여러 COLMAP 데이터·메시의 위치·회전·균일 배율 정렬 | 점군·카메라·메시·정렬 결과를 독립 소유 | `colmap_align.open_workspace` |
| COLMAP Modifier | COLMAP 가져오기, 점·카메라 편집, 내보내기 | 독립 Runtime Domain이 편집 상태의 권위 | `colmap.open_modifier_workspace` |
| Projection Painting | 촬영 이미지로 텍스처 페인팅·보정·출력 | 파일에서 가져온 COLMAP·OBJ와 Paint 데이터를 독립 소유 | `projection_paint.open_workspace` |

Align 결과를 Modifier에서 사용하려면 내보낸 COLMAP을 다시 가져온다. Projection Painting은 Dataset의 COLMAP·OBJ 불러오기로 파일을 직접 가져온다. Modifier 편집 결과는 COLMAP으로 내보낸 뒤 불러온다. 이후 Modifier 편집을 자동 반영하지 않으며, 두 도구의 Blender Object·Camera Data·Point Mesh도 공유하지 않는다.

```text
shell/                      Extension 등록·해제, 메뉴, 환경설정
tools/align/                Align 기능·상태·입출력
tools/modifier/             Modifier 편집 Domain·전용 이력·파생 표시
tools/projection_paint/     Painting Snapshot·레이어·브러시 연결·출력
shared/blender_runtime/     작업공간 소유권·세션 교대·범용 OBJ 가져오기
shared/colmap_io/           도구 실행 상태가 없는 공통 COLMAP 읽기·검증
shared/colmap_transfer/     Blender ID 없는 불변 값 전달
shared/colmap_dataset/      과거 0.2.0 데이터의 Legacy 입력
tests/                      구조·공존·Core·Blender 회귀
build_tools/                빌드·실행 환경·설치본 검증
docs/README.md              완료 기능과 현재 구조 통합본
docs/temp/                 후속 집필·검증에 필요한 원문 9개
```

유지할 개발·운영 규칙은 다음과 같다.

- Root `__init__.py`는 Shell 생명주기를 호출하며, 도구 기능은 각 도구 경계에 둔다. Root Shell이 `Window → COLMAP Tools` 메뉴와 공통 환경설정을 소유한다.
- 도구 사이 직접 import를 금지한다. 각 도구가 자기 Runtime, Handler, Timer, Draw Handler, Keymap, UI와 오류 정리를 소유한다.
- `shared/colmap_transfer`에는 Camera·Image·Point·Pose·경로·출처 및 Domain 식별자·Revision·지문 같은 값만 전달한다. Blender ID나 도구 Runtime 포인터를 전달하지 않는다.
- 같은 이름이나 유사한 코드라는 이유만으로 도구의 자원·UI·Undo를 합치지 않는다. 공통 계약을 바꾸기 전에는 기존 공개 API와 영향 범위를 확인한다.
- 등록·해제는 반복 호출과 등록 실패 복구를 지원한다. 자기 소유권이 확인된 자원만 정리하며 사용자·다른 도구의 자원을 이름만 보고 삭제하지 않는다.
- 장기 작업은 준비·검증·확정 경계를 구분한다. 실패·취소 때 임시 결과를 정리하고 이전 확정 결과를 유지한다. 검증 실패를 다른 엔진이나 손상된 부분 결과로 우회하지 않는다.
- 변경 계획은 `docs/`의 Markdown으로 기록한다. 본문에서 현행 규칙, 과거 결정과 아직 수행하지 않은 작업을 구분한다.

설계 근거: [제품 경계](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/architecture/PRODUCT_BOUNDARIES.md), [패키지 구조](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/architecture/PACKAGE_STRUCTURE.md), [독립 도구 결정](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/decisions/0001-independent-tools.md), [불변 Snapshot 결정](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/decisions/0008-independent-projection-snapshot.md).

<a id="workspaces"></a>
## 2. 작업공간·세션·카메라 격리

### 2.1 작업공간 생성과 이동

각 도구는 현재 화면을 복제하지 않고 자기 Blender 5.2 Workspace Seed에서 독립 Workspace와 Screen을 만든다. Tool ID·UUID·Workspace/Seed 스키마·Screen 소유권을 검사한다. Align의 Object 표시 필터가 Modifier·Painting 화면으로 전파되지 않으며, Modifier와 Painting은 필요한 Object 필터와 Local View 상태를 자체 구성한다.

Modifier와 Projection Paint의 신규 작업공간도 Align과 같은 N 패널 기본 폭(화면 배율 적용 전 약 370)을 사용한다. 두 도구의 중립 Seed를 Align Seed의 저장된 화면 배치에서 생성하며, 도구별 내부 이름과 Tool ID는 분리한다. 생성 시에는 반드시 `append_seed_workspace(..., preserve_seed_ui=True)`로 저장된 폭을 보존한다. 런타임에서는 기존 작업공간과 사용자가 조절한 폭을 덮어쓰지 않는다. 재생성에는 `build_tools/workspaces/create_workspace_seed.py --template`에 Align Seed 경로를 지정한다. `--template`은 배경 모드에서 실행할 수 있고 `--sidebar-width`와 동시에 지정하지 않는다.

0.3.42에서는 두 Seed만 변경하고 위 보존 옵션을 빠뜨려 `workspace.append_activate`가 패널 폭을 Blender 기본값으로 초기화했다. 0.3.43에서 두 도구의 호출을 수정했다. Blender 5.2, UI 배율 1.25의 실제 작업공간 생성 경로에서 수정 전 Modifier·Painting 350px / Align 461px를 재현했고, 수정 후 세 도구 모두 461px를 확인했다. `tests/blender_gui/test_sidebar_width.py`는 캡처 없이 실제 표시 폭을 비교한다. 이미 만들어진 작업공간의 폭을 자동 변경하는 마이그레이션은 포함하지 않는다. 새 기본 폭은 새 파일에서 작업공간을 생성할 때 적용된다.

다른 도구나 일반 Blender 작업공간으로 이동하면 공통 교대 조정기가 이전 세션의 정식 종료 완료를 확인한 후 다음 진입을 실행한다. 공통 계층은 각 도구가 등록한 시작·종료·점유·완료 판정 함수를 호출하고, 도구 내부 Runtime을 직접 참조하지 않는다. 사용자가 고른 작업공간을 준비 Timer가 다시 덮어쓰지 않는다.

정상 종료한 `READY` 작업공간과 데이터는 보존·재사용한다. 생성 실패나 중단으로 남은 `CREATING`·`FAILED` 작업공간은 소유권·다중 Window 점유를 확인한 뒤 공식 API로 정리한다. 파일 로드 전 대기 포인터와 Timer를 폐기하고, `load_post`에서 초기화된 Message Bus 구독을 다시 연결한다.

### 2.2 표시·카메라·타임라인

- 0.3.46부터 Align·Modifier·Painting의 기능 메뉴 제목은 모두 최상위 패널의 왼쪽 기준선에 배치한다. 0.3.47에서는 본문 여백을 기존 1.0에서 0.5 UI 단위로 줄였다(화면 배율 적용 전 약 10px). 각 메뉴의 개별 접기·펼치기는 유지하며, 전체 도구 부모 메뉴에 따른 추가 제목 들여쓰기는 제거한다. Align의 기존 부모 패널은 연결 상태 안내만 표시하고 Painting의 Layers도 독립 메뉴로 표시한다.
- 이 변경 전 복귀 지점은 `926699a`다. 0.3.46에서 빌드 검사 298개와 패널 계층 검사, 숨김 GUI에서 17개 패널의 펼친 상태 그리기를 확인했다. 실제 설치본의 세 패널·버전 파일도 소스와 일치한다. 별도 실행한 Align 전체 등록 검사에는 기존 `test_marker_ui_mirror_and_default_display_setting`의 `SMALL_GICP` 기본값 기대가 현행 CCCoreLib와 맞지 않는 실패가 남아 있다. 이번 UI 변경 범위에서는 엔진 테스트를 수정하지 않았다.

- Modifier 관리 Root는 전용 View Layer에서만 보인다. 그 전용 View Layer에서는 일반 Scene 데이터를 숨기며 일반 작업공간의 표시 상태와 데이터 블록은 보존한다.
- Painting의 Snapshot 자원도 자기 전용 View Layer에서 관리한다. Align 데이터는 두 도구의 전달·표시 구조에 의존하지 않는다.
- 실제 `Scene.timeline_markers`는 활성 도구 한 곳만 사용한다. 영구 Camera Marker Manifest는 보존하고, 진입 때 실제 Marker를 붙이며 종료 때 정확히 소유한 Marker만 내린다.
- 사용자 Marker·소유권이 불명확한 Marker와 충돌하면 임의 삭제 대신 오류를 표시한다. 같은 Scene을 표시하는 다른 Window의 중복 점유도 차단한다.
- Camera 쓰기 권한은 Window·Workspace·Scene Marker 사용권·세대 Token으로 제한한다. 비활성 도구의 Frame Handler나 대기 Timer는 Scene Camera, Render 해상도, Background Image를 바꾸지 않는다.
- 작업공간 구성이 `READY`가 된 다음 UI Tick에서 Camera 세션을 연결한다. 도구별 Local Camera를 사용하며 작업공간을 나가도 Camera와 Background 자원을 보존한다.
- Modifier와 Painting의 F7은 각각 자기 작업공간·세션에서만 배경 이미지를 전환한다. 키맵 등록 순서로 대상 도구를 선택하지 않는다.

완료 근거: [작업공간 격리](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/WORKSPACE_ISOLATION_RELEASE_NOTE.md), [자유 이동](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/WORKSPACE_FREE_NAVIGATION_RELEASE_NOTE.md), [Camera Marker](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/CAMERA_MARKER_SESSION_RELEASE_NOTE.md), [Camera 세션 격리 0.3.35](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/CAMERA_WORKSPACE_SESSION_ISOLATION_RELEASE_NOTE.md).

<a id="align"></a>
## 3. COLMAP Align

<a id="align-input"></a>
### 3.1 입력과 레이어

COLMAP 희소 점군·카메라, COLMAP에 연결한 OBJ, 독립 OBJ를 가져올 수 있다. 표준 데이터셋은 다음과 같다. 기존 TXT 입력과 `sparse/`에 모델 파일이 직접 있는 구조도 지원한다.

0.3.79에서는 표시와 마커 선택이 위치·인덱스 GPU 버퍼를 공유하면서, 법선·UV 버퍼는 표시 배치만 참조한다. 모델당 선택에서 참조하는 정점 속성을 32바이트에서 12바이트로 줄이며 원본 정점·면·UV와 배정밀도 교차를 유지한다. 모델 간 공유나 클릭 시 큰 배열 생성은 없다. 기존 단발성 1.60초 기록의 상세 프로파일은 없지만, 같은 두 모델에서 재현한 446ms 지연의 대부분이 GPU 결과 읽기 대기였음을 확인했다. 구현·정확도·최초 업로드와 정상 첫 선택을 구분한 측정은 [GPU 버퍼 공유 문서의 0.3.79 절](COLMAP_Align_GPU_버퍼_공유.md#0379-첫-선택-지연-분석과-위치-버퍼-분리)에 기록한다.

0.3.78부터 내장 OBJ의 코너 UV는 Blender 원본과 같은 `float32 × 2`로 보관한다. 성분 수는 모델 전용 한 바이트를 참조하는 읽기 전용 배열로 표현하고, UV 번호는 같은 모델의 원본 코너 번호 저장소를 참조한다. 서로 다른 레이어의 정점·면·UV·내장 Mesh는 공유하지 않는다. 원본 면·UV 저장·복원·내보내기의 기준은 기존 내장 Mesh로 유지하고, 표시·마커 GPU 준비는 불러오기 중에 끝낸다. 마커의 배정밀도 원본 교차와 삼각형 ID는 바꾸지 않는다.

내부 표시 코드는 `TriangleMeshModelData.texture_coordinates`로 준비된 UV를 직접 읽는다. 기존 `texcoords_float64` 생성 인자·조회 API와 OBJ의 배정밀도 세 성분 입력은 계속 지원한다. 작은 코너 UV에 대한 기존 조회 API는 명시적으로 호출했을 때만 호환 사본을 반환하므로, 불러오기·표시·마커·정렬의 준비 경로에서는 사용하지 않는다. 실제 검증 자료는 `build/verification/uv-compact-20261021/`에 기록한다.

2026-10-21 데스크톱 2에서 같은 대형 OBJ를 각각 독립적으로 불러온 두 모델을 동시에 보관해 0.3.77과 0.3.78을 각 2회 비교했다. 모델마다 정점 30,109,486개, 삼각형 60,206,247개, 코너 UV 180,618,741개이며, 원본 파일은 7,640,571,796바이트다. 기존 사용자 Blender는 계속 열어 두었다. 아래는 검증 프로세스의 중앙값이며, 메모리 단위는 GiB다. 준비 완료 메모리와 준비 중 최고값은 정확도 검사에 들어가기 전에 기록했다.

| 두 모델 합계 측정 항목 | 0.3.77 | 0.3.78 |
|---|---:|---:|
| 애드온 원본 배열의 실제 저장 공간 | 14.915 | 7.850 |
| 준비 후 프로세스 전용 커밋 | 55.806 | 47.915 |
| 준비 후 물리 작업 집합 | 45.091 | 38.121 |
| 준비 중 최고 전용 커밋 | 58.082 | 52.063 |
| 준비 중 최고 물리 작업 집합 | 53.154 | 49.731 |
| 불러오기·CPU·GPU 준비 단계 합계, 초 | 198.356 | 188.822 |
| 불러오기부터 실제 첫 표시까지 합계, 초 | 204.017 | 194.202 |

배열 저장 공간은 모델 내부 중복 제거만으로 정확히 7.065GiB 감소했다. 전용 커밋 감소는 약 7.891GiB다. 첫 표시까지 시간은 중앙값 약 4.81% 감소했지만 범위가 변경 전 188.279~219.756초, 변경 후 185.599~202.806초로 겹친다. 파일 캐시를 비우지 않았고 실행 환경 변동이 있으므로 모든 입력에서 같은 속도 개선을 보장하지 않는다. 파일 선택과 작업 타이머의 대기 시간은 제외했다. 원본 Blender 내장 Mesh와 GPU 버퍼 크기는 그대로 유지했다.

네 회차의 전체 정점·삼각형·UV·원본 면/코너 매핑 지문과 화면 전체 픽셀이 일치했다. 모델마다 세 지점의 마커 삼각형 번호와 배정밀도 좌표가 같았으며, 첫 회차에는 첫 지점을 전체 원본 삼각형의 독립 광선 교차와 대조해 좌표 오차 0을 확인했다. 모든 모델의 형상·UV 저장소와 내장 자산 ID는 서로 독립적이다. 변경 후 첫 회차에서 두 번째 모델의 첫 선택에 1.597초 지연이 한 번 있었고 같은 지점 반복은 최대 17.33ms, 다음 회차의 모델별 첫 선택 중앙값은 17.20ms였다. 이 단발성 지연의 원인은 확정하지 않았으며 마커 속도 개선을 주장하지 않는다.

새 UV 회귀 4개, 관련 회귀 파일 10개, 점 KD 검사 22개와 정식 빌드·격리 설치 검증을 통과했다. 전체 Align 소스 회귀는 52개 파일 중 51개 통과이며, `test_display_axes.py`의 가짜 모델에 `positions`가 없는 기존 실패 1건은 변경 전 패키지에서도 동일하게 재현했다. 요청 범위 밖의 해당 검사는 수정하지 않았다. 측정 도구의 타이머 재진입으로 실패한 회차는 `after-2-reentrant/`에 보존하고 집계에서 제외했으며, 검증 도구만 수정한 뒤 다시 측정했다. 실제 설치본 0.3.78의 1,235개 파일이 검증 ZIP과 일치하고 사용자 환경설정도 유지됨을 확인했다. 실행 중인 사용자 세션은 보존했으므로 새 표현은 재시작 후 불러오기에서 적용한다.

```text
데이터셋/
├─ images/
├─ sparse/0/
│  ├─ cameras.bin
│  ├─ images.bin
│  └─ points3D.bin
└─ model.obj
```

COLMAP 연결 OBJ 후보는 확인된 데이터셋 루트 바로 아래에서 찾으며 하위 폴더를 재귀 탐색하지 않는다. 확장자는 대소문자를 구분하지 않는다. 후보가 없으면 COLMAP만 가져오고, 하나면 자동 연결 후보로 제시하며, 여러 개면 선택한다. 후보 탐색과 실제 가져오기의 재질·텍스처 처리는 별개다.

COLMAP과 연결 OBJ는 하나의 데이터 레이어이며 같은 공통 정렬 행렬을 공유한다. 레이어 행의 점군·표면 아이콘은 그 레이어의 표시 및 정렬 입력을 선택한다. 둘 중 하나만 선택하고, 전체 숨김은 눈 아이콘으로 처리한다. 이름 더블클릭의 A/P 이동과 Ctrl/Shift 선택은 유지한다.

A는 기준 그룹이고 P는 이동 대상이다. 정밀 정렬은 선택한 P 하나와 기준 A 데이터를 사용한다. 눈으로 숨긴 A도 정렬 기준에 포함될 수 있으므로 눈 상태와 기준 그룹·입력 종류를 구분한다. A 데이터의 입력 종류가 섞이면 정렬 시작을 차단한다.

<a id="align-axes"></a>
### 3.2 데이터셋 축 설정

새 RC 입력은 COLMAP에 `RC COLMAP`의 Forward `Z` / Up `-Y`, OBJ에 `RC 표준 OBJ`의 Forward `Y` / Up `Z`를 사용한다. Native OBJ Import는 `Y/Z`와 Identity Object Matrix 확인을 기준으로 하고, 화면 축 보정은 별도 데이터로 관리해 이중 축 변환을 피한다.

레이어 톱니 팝업에서 COLMAP 점군·카메라와 연결 OBJ의 원본 축 프리셋·Forward/Up은 각각 편집한다. XYZ 이동·회전·배율은 아래 데이터셋 공통 변환 한 벌로 함께 적용한다. 회전·배율은 두 형상의 전체 바운딩 박스 중심을 기준으로 하며, 중심을 월드 원점으로 함께 옮길 수 있다. 배율 자물쇠는 현재 XYZ 비율을 유지한다. 개별 RC 초기화는 원본 축만, 변환 초기화는 공통 이동·회전·배율만 초기화한다.

**기즈모를 바운딩 박스 중심으로**는 배치를 유지하며 기즈모 중심만 지정하고, **물체를 이동해 기즈모를 원점으로**는 현재 기즈모를 월드 원점에 맞추도록 팝업 대상 데이터셋만 평행이동한다. 연결 OBJ도 함께 이동하며 원본 배열·공통 정렬 행렬은 유지한다. 0.3.81부터 두 버튼은 클릭 즉시 독립적으로 확정되고 각각 Undo 한 건이다. 팝업 취소로 되돌아가지 않으며, 팝업을 닫고 실행 취소한다. 미적용 축 입력은 별도로 보존하며 버튼은 현재 표시 배치를 기준으로 동작한다. 기즈모 중심은 세션 상태이며 물체 이동은 기존 표시 변환으로 저장된다. 저장된 경계의 코너만 사용하므로 큰 원본 배열을 다시 순회하지 않는다. 팝업 중 기즈모는 위치만 표시하고 드래그를 잠근다.

팝업이 열린 동안 해당 레이어에 존재하는 COLMAP과 OBJ를 모두 표시하고, 한 종류만 있으면 그것만 표시한다. 기존 눈 상태와 점군·표면 선택은 변경하지 않으며 적용·취소·Esc·창 종료·작업공간 이탈 시 원래 표시로 돌아간다. 적용은 Undo 한 건이다.

형상별 원본 축 `Aᵢ`, 기존 개별 배치 보존 기준 `Kᵢ`, 데이터셋 공통 변환 `U`로 표시 행렬 `Nᵢ = U Kᵢ Aᵢ`를 구성한다. 공통 정렬 `C`와 원본 점·정점·카메라는 그대로 유지하며 화면 좌표는 `C × Nᵢ × 원본 좌표`다. 표시·법선·피킹·마커는 해당 형상의 현재 변환을 사용한다. 실제 정렬에 사용된 변환이 바뀌면 관련 품질 기록을 무효화하고 Undo/Redo로 기록까지 복원한다. [개별 축·공통 변환 분리 설계](COLMAP_Align_개별축_공통변환_분리.md)에 기존 배치와 구버전 저장 호환성 및 검증 범위를 기록한다.

### 3.3 마커와 3점 초기 정렬

희소 점군과 연결·독립 메시 모두 마커를 지정할 수 있다. 메시 피킹은 삼각형 프록시에서 수행하며 원본 출처와 로컬 위치를 보존한다. 마커 입력을 시작할 때 표시·형상·축·정렬을 기록한다.

모든 마커쌍에서 **A끼리**, **P끼리** 각각 같은 형상 종류를 사용한다. A와 P는 서로 다른 종류여도 된다. 예를 들어 A 전체를 메시로, P 전체를 점군으로 지정할 수 있다. 도중에 다른 종류나 대상 형상으로 바꾸면 안내 후 차단한다.

기존 3점 정렬과 마커 배율 옵션을 사용한다. 프록시에서 얻은 변환은 레이어의 공통 행렬에 합성되므로 연결 COLMAP 점군·카메라·메시가 함께 이동한다. 3점 정렬 미리보기는 A·P 양쪽 뷰에 기준 A와 정렬된 P를 함께 표시하고 각 시점을 유지한다. 점군·메시는 일반 표시를 유지하며 A·P 대표 삼각형만 양쪽에 반투명하게 표시한다. 취소하면 원래 역할별 표시로 돌아간다. CC 결과 미리보기 단계에서는 대표 삼각형을 숨기며, 결과를 버려 마커 미리보기로 돌아오면 다시 표시한다.

`codex/artec-align-engine`의 표면 더블클릭 피벗 선택도 마커와 같은 GPU ID·깊이 경로를 사용한다. 표시 버퍼를 공유하고 원본 삼각형 후보를 배정밀도로 재교차한다. 최초 BVH 구축 제거, 반복 선택의 비용 증가, 원본 전체 교차를 통한 정확도와 데스크톱 2 검증 결과는 [GPU 피벗 선택 검증](COLMAP_Align_GPU_피벗선택_검증.md)에 기록했다.

<a id="align-cc"></a>
### 3.4 점 기반 강체 정렬

`codex/artec-align-engine`의 기본 경로는 `POINT_KD_ICP`다. 알고리즘은 `point-kd-icp-v2`, Python 작업은 `point-kd-job-v1`, 네이티브 작업은 `point-kd-native-v1`로 구분한다. Artec 실행에서 관찰한 구조를 참고한 독립 구현이며 v2는 확인된 상대 변화 종료식을 적용한다.

| 이동 P | 기준 A | 계산 |
|---|---|---|
| 점군 | 점군 | 작은 쪽을 질의하는 점–점 SVD 강체 정렬 |
| 점군 | 메시 | 점군을 질의하고 메시 정점 접평면에 맞춤 |
| 메시 | 점군 | 점군→메시 계산을 역변환하여 P에 적용 |
| 메시 | 메시 | 작은 쪽 정점과 큰 쪽 정점 접평면의 정렬 |

동종 입력의 점 수가 같으면 P를 질의한다. A가 질의인 경우 계산된 변환의 역행렬을 P의 기존 공통 정렬에 왼쪽 합성한다. A는 이동하지 않으며 마커 배율과 형상별 표시·축 변환을 보존한다.

선택한 전체 유효점으로 대상 KD-tree를 한 번 만들고, 거리·법선 조건을 만족하는 정확 최근접점을 찾는다. 양쪽에 법선이 있으면 `abs(dot) ≥ cos(45°)`를 사용하고 먼 대응의 5%를 가중치에서 제외한다. 메시 법선은 변환 후 삼각면의 면적가중 정점 법선이다. 입력 축소·표면 표본·삼각형 내부 BVH 정렬·추가 배율·실행 중 독립 후보 평가는 수행하지 않는다.

반경은 `max(0.01D, 3h, 2e)`이며 D는 두 입력 중 작은 대각선이다. h는 최대 4,096개 결정적 검사점으로 측정한 대상 최근접 간격이고, 이 검사점은 ICP 입력을 줄이지 않는다. 자동 반경이 0.05D를 넘으면 초기 배치·밀도를 확인하도록 중단한다.

최대 갱신은 30회다. 수렴은 `|E_k−E_(k−1)|/(E_(k−1)+1e-9) < τ` 또는 이동·회전 갱신 벡터의 `‖v_k−v_(k−1)‖/(‖v_(k−1)‖+1e-9) < τ`를 둘 다 만족할 때 성립한다. `E`는 재검색한 목적함수 RMS, `v`는 한 번의 갱신 이동 또는 축×회전각이며 처음 벡터 이력은 영이다. 기본 `τ`는 단정도 0.001의 배정도 값 `0.0010000000474974513`이다. 대응 수 일치 조건은 없으며 상한 검사가 수렴 검사보다 우선한다. 같은 대응에서 목적함수가 증가한 갱신의 거부와 대응 수 절반 이하 감소의 검토 분기는 유지한다. 배정도 잔차 계산과 마지막 승인 후보 반환은 유지하므로 Artec 전체 동작의 복제는 아니다.

새 화면 지표는 **정렬 대응 RMS**이며 독립 겹침 평가를 하지 않았음을 표시한다. 점–평면 목적함수와 유클리드 대응 RMS는 별도 기록한다. 성긴 메시·밀도 차이가 큰 점군에서는 정점 접평면 근사의 편향이 생길 수 있다. 실제 자료와 합성 밀도 차이 검증의 결과·제한은 [구현 검증 보고서](COLMAP_Align_Artec_방식_엔진교체_구현검증.md)를 따른다. 허용치 완화 이후 같은 두 메시의 속도·정확도·검토 상태는 [0.001 재검증](COLMAP_Align_수렴기준_001_재검증.md), Artec의 정확한 종료식은 [종료식 분석](COLMAP_Align_Artec_종료식_분석.md)에 기록했다.

종료식 교체 후 같은 두 메시의 비교는 [정확한 종료식 적용 검증](COLMAP_Align_Artec_종료식_적용검증.md)을 따른다. 앞선 0.001 재검증은 세 조건을 동시에 요구하던 v1의 역사 기록이다.

네이티브 엔진 `1.2.0`은 실측한 Artec 경로에 맞춰 KD 생성의 개수 중앙값 분할을 최장축 경계상자의 공간 중간값 분할로 바꿨다. 파일에서 읽은 좌표·명시 법선 배열은 소유권을 이전하고 Eigen 뷰로 재사용한다. 알고리즘 `point-kd-icp-v2`의 종료식·배정도 계산·원본점 전체·Python 검증 경계는 유지한다. 실제 호출 근거와 같은 두 모델의 성능·정확도 비교는 [KD 생성·입력 준비 검증](COLMAP_Align_Artec_KD_입력준비_개선검증.md)을 따른다.

네이티브 엔진 `1.3.0`과 Windows 입력 준비는 변환 좌표를 작업별 공유 메모리에 직접 작성하여 대형 불변 사본과 바이너리 파일 전달을 제거했다. 원본 전체 해시·종료식·정확도 계약은 유지한다. 같은 두 모델에서 전체 시간 10.3~13.6%, 입력 준비 시간 30.9~35.9% 감소와 동일한 최종 행렬을 확인했다. 공유 버퍼의 메모리·수명 및 구엔진 회귀 검사 한계는 [Python 입력 전달 개선 검증](COLMAP_Align_Python_입력전달_개선검증.md)을 따른다.

### 3.5 실행 환경·검증·승인

Blender는 전용 외부 Python 조정자를 실행하고 조정자는 작업당 C++ 프로세스 하나와 대상 KD-tree 하나를 사용한다. 검색과 행렬 누적은 oneTBB 병렬 처리이며 고정 블록 순서로 누적한다. GPU 계산과 작업 간 KD-tree 상주는 포함하지 않는다.

0.3.77부터 원본 좌표·삼각형을 불러올 때 읽기 전용 버퍼로 고정하고 저장소 등록 시 전체 검사·내용 지문·정렬 입력을 준비한다. Align 시작·완료 때는 준비된 지문과 모델·개정·배열의 동일성을 확인하고 원본 전체를 재검사·재해시하지 않는다. Windows에서는 원본 공유 매핑과 변환 행렬을 외부 Python 작업 프로세스에 전달하며, 좌표 변환은 그 프로세스에서 수행한다. 모델 제거와 작업 종료 시 참조를 해제한다. 기존 512 MiB 캐시와 전체 해시 경로는 구형 저장소 호출·다른 엔진의 호환 경로에 남는다. 같은 두 대형 모델의 클릭 정지 18.47초 → 12.6밀리초, 전체 시간 37.77% 감소, 정확도·메모리·설치 검증 결과는 [입력 버퍼 상주 재사용](COLMAP_Align_입력버퍼_상주재사용.md)에 기록했다.

작업별 식별자·프로토콜·행렬·원본 내용을 다시 확인한 결과만 미리보기로 전달한다. 실패·취소 때 이전 확정 상태를 유지하며 다른 엔진으로 자동 재시도하지 않는다. 미리보기·승인·버리기·Undo/Redo 및 출처 저장은 기존 공통 경로를 사용한다. 과거 CC 확정 행렬과 품질 기록을 새 엔진 기록으로 바꾸지 않는다.

명시적 `CCCoreLib` 비교 프로필은 기존 표면 표본·강체/배율 후보·독립 평가 경로를 유지한다. 그 경로의 잠금 버전·패치·원문은 [CC 네이티브 문서](../tools/align/native/cccore_worker/README.md)를 따른다. 새 엔진의 계약과 구현 범위는 [교체 계획서](COLMAP_Align_Artec_방식_엔진교체_계획서.md)에 정의한다.

<a id="align-export"></a>
### 3.6 OBJ 원본 보존과 내보내기

Blender 기본 OBJ Importer가 읽은 삼각화 전 Mesh를 비연결 `bpy.data.meshes` 데이터 블록으로 내장한다. 소유권 UUID와 Fake User로 보존하며 일반 Scene의 편집용 Object와 자동 공유하지 않는다. `calc_loop_triangles()`는 원본 Polygon을 바꾸지 않고 표시·피킹·CC 계산용 프록시를 만든다.

OBJ 내보내기는 내장 원본 정점에 확정 행렬을 적용하고 원본 Polygon Corner·UV 순서를 사용한다. 삼각형·쿼드·N곤과 UV seam을 보존한다. 정상 내장 후 외부 OBJ는 출처 정보이며, 복원·내보내기 때 그 파일의 존재 여부나 SHA-256을 다시 요구하지 않는다. COLMAP 원본과 외부 텍스처의 의존성은 별개다.

UV·diffuse 이미지가 있는 OBJ는 `COLMAP RGB`에서 텍스처로 표시할 수 있다. 출력은 `모델표시이름.obj`, 같은 이름의 MTL과 원본 이름의 이미지가 한 폴더에 놓이는 구조다. 공유 이미지는 한 번 복사하며, 중복 모델 이름이나 서로 다른 이미지의 같은 파일명 충돌은 사전 오류로 처리한다. MTL은 출력 상대 경로로 다시 작성하며 UV·재질·복사 결과를 검증한 뒤 출력 후보를 확정한다.

Normal·Roughness·Metallic 등 임의 MTL 확장 전체를 지원한다고 해석하지 않는다. 파일 경로 길이와 충돌 검사, 취소·실패 시 기존 출력 보존을 유지한다. COLMAP 출력은 점·카메라에 해당 좌표 변환을 일관되게 적용하며 독립 OBJ가 COLMAP 카메라 데이터를 만들어 내지는 않는다.

<a id="align-ui"></a>
### 3.7 진입·패널·복원 관련 완료 수정

- Edit·Sculpt·Texture Paint 등에서 Align을 열면 이전 도구 종료 후 Object Mode로 전환한다. 전환할 유효 컨텍스트가 없으면 부분 작업공간을 만들지 않고 오류를 표시한다.
- 작업공간을 이동하면 세션이 자동 종료되므로 N 패널의 수동 종료 버튼은 제거했다. 내부 종료 API는 유지한다.
- READY Align 작업공간은 세션 재연결 중에도 패널을 유지하고 입력만 잠근다. 연결 실패 시 실제 원인을 표시한다.
- 새 작업공간의 왼쪽 A 뷰 N 패널은 닫고, 오른쪽 P 뷰 패널은 연다. 기본 폭은 화면 배율 적용 전 370이며 기존 `.blend`와 사용자 변경 폭을 덮어쓰지 않는다.
- 내보내기를 실행하지 않은 유휴 상태에서 파일을 열면 과거 내보내기 오류를 비운다. 실제 내보내기를 중단한 경우에는 정리 사유를 남긴다.
- Align 작업공간에서 저장한 `.blend`는 데이터 복원 뒤 세션을 자동 연결한다. 일반 작업공간에서 저장한 파일은 해당 작업공간을 유지한다.

완료 근거: [CC 정식 이관](temp/migration/ALIGN_CC_MAIN_RELEASE_NOTE.md), [OBJ 내장 원본·프록시](temp/COLMAP_Align_OBJ_원본토폴로지_보존_정렬프록시_계획서.md), [겹침 우선 샘플링 0.3.41](temp/migration/ALIGN_OVERLAP_SAMPLING_RELEASE_NOTE.md).

<a id="modifier"></a>
## 4. COLMAP Modifier

표시 축 설정은 `COLMAP Import → Dataset Root 경로 오른쪽 폴더 아이콘`으로 여는 파일 탐색기 오른쪽 `Display Axis`에서 조절한다. 선택창의 값은 확정 전까지 임시로 보관해 취소 시 기존 경로와 축을 유지한다. 0.3.45부터 파일 선택창의 `Import`를 누르면 기존 증분 Import와 진행창을 바로 시작한다. N패널에는 경로 입력·폴더 아이콘·Input Format과 Point/Camera Display를 표시하며 별도의 Import 버튼은 두지 않는다. 내부 `colmap.import_model` API 및 원본 좌표계 Export 방식은 유지한다.

0.3.44 소스 및 실제 설치본의 숨김 GUI에서 `tests/blender_gui/test_modifier_dataset_browser.py`로 선택창 취소 시 기존 설정 보존과 확정 시 경로·사용자 정의 축 적용을 검증했다.

0.3.45에서는 같은 GUI 검증을 실제 데이터 가져오기까지 확장했다. 소스 및 실제 설치본에서 취소 시 설정 보존, 파일 선택창 Import 한 번으로 점 1개·카메라 1개 생성과 진행 작업 종료를 확인했다.

### 4.1 편집 권위와 되돌리기

0.3.50에서는 데이터셋 배율과 점·카메라 표시 크기를 분리한다. 공개 Point Scale은 유지하고 내부 반지름만 역배율로 보정하며, 카메라 중심은 변환하되 최종 표시 배율을 1로 유지한다. Modifier 전용 View Layer의 별도 컬렉션에는 +X·+Y 방향 1~10m 텍스트 20개와 기준 눈금을 배치한다. 눈금은 Scene 단위에 맞춰 환산하고 선택·렌더·데이터 출력에서 제외한다. [표시 크기·미터 눈금 계획 및 검증](COLMAP_Modifier_표시크기_고정_미터눈금_계획서.md)을 참고한다.

2026-10-06에는 Modifier 진입 후 새로 만든 Painting·일반 표시 레이어에 눈금이 노출되는 누락을 수정했다. 표시 레이어·작업공간·Scene 전환 알림에서 소유 눈금 컬렉션의 제외 상태만 재적용하며 Modifier로 복귀하면 기존 21개 눈금을 유지한다. [후발 레이어 격리 검증·설치 기록](COLMAP_Modifier_미터눈금_후발레이어_격리.md)을 참고한다.

0.3.49에서는 Modifier 일반 탐색 중 연속 클릭도 Align과 동일하게 Blender의 더블클릭 시간·거리 설정으로 판정한다. 점 더블클릭으로 기즈모 피벗과 화면 회전 중심을 함께 이동하며, 점·카메라 편집 모드에서는 기존 선택 입력을 유지한다. 실제 뷰포트에서 제어된 클릭 간격으로 피벗·회전 중심 이동과 편집 이력 불변을 확인했다.

0.3.48부터 내부 `AXIS_ROOT`는 뷰포트에서 숨기고 선택을 잠근다. 이전에는 화살표 Empty가 카메라와 함께 선택되면 외부 오브젝트 혼입 검사로 Remove가 차단됐다. 신규 Import에서 숨김·선택 잠금을 적용하며, 기존 파일도 Modifier 작업공간 진입 시 해당 데이터셋 보조 축의 선택을 해제하고 같은 정책을 적용한다. 하위 점군·카메라 표시와 부모 변환, 별도로 그리는 Dataset Transform 기즈모는 유지한다. 실제 Blender에서 신규·기존 루트 처리, 자식 표시·변환과 카메라·점 선택을 검증했다.

0.3.34부터 독립 Python Runtime Domain이 현재 COLMAP Reconstruction·확정 EditOperation·관계 Index·전용 Journal을 소유한다. Scene Property, Point Mesh, Camera와 Timeline Marker는 저장 연결 또는 다시 만들 수 있는 파생 표시다. Domain UUID·Generation·Dataset 지문·Revision·History Hash로 현재 상태를 확인한다.

점·카메라 제거와 Pending Filter 적용은 변경된 범위의 Forward/Inverse Delta를 전용 Command로 기록한다. Modifier 작업공간에서 `Ctrl+Z`는 전용 Undo, `Ctrl+Shift+Z`와 `Ctrl+Y`는 전용 Redo다. 빈 Journal에서도 Blender Global Undo로 입력을 넘기지 않으며 `Ctrl+Alt+Z`는 안내를 표시한다. 다른 작업공간의 Blender Undo는 그대로 유지한다.

Blender Global Undo가 파생 ID를 바꾸어도 Domain·Journal은 보존한다. 오래된 ID 참조와 세션 연결만 폐기하고 권위 Runtime으로 표시를 다시 동기화한다. 모든 Modifier 오퍼레이터의 Global Undo Step을 제거한 것은 아니다.

Import는 Loader 결과를 새 Domain에 설치한다. 다른 데이터셋을 가져오면 새 Domain을 생성한다. Export 성공은 출력 상태를 새 기준선으로 확정하고 Journal을 비우며 실패·취소는 기존 데이터와 이력을 유지한다. 점·카메라 선택, 관측 관계 검토, Pending Filter와 원본 COLMAP 좌표계 출력 흐름을 유지한다.

### 4.2 원본 검사와 현재 복원 동작

명시적 Import는 Source SHA-256과 Reconstruction 무결성·이미지 경로 검사를 수행한다. 저장된 같은 Source의 COLD 복원은 SHA-256 한 번과 읽는 동안의 경로·크기·수정 시각·파일 식별 정보를 확인하고, 중복 전체 무결성 검사와 두 번째 전체 SHA-256을 생략한다. 관계 인덱스·이미지 경로·확정 편집 재생과 마지막 원자 확정은 유지한다. Source가 바뀌었거나 식별 정보가 없으면 이전 편집을 임의 적용하지 않고 재Import를 요구한다.

**현재 0.3.34 이후의 정상 Active Payload는 파일 로드 후 자동 복원한다.** `state_handlers.invalidate_loaded_state()`가 저장 정보를 읽고 `process_pending_load_restore()`를 예약해 Source·Operation을 UI Tick 예산으로 재구성한다. 복원한 Undo Command는 사용할 수 있지만 저장 당시 Redo 분기는 복원하지 않는다. 같은 실행에서 유효한 Runtime으로 재진입하면 원본을 다시 읽지 않는다.

과거 0.3.9의 “파일을 열 때 원본을 읽지 않고 작업공간 진입/수동 업데이트에서만 복원” 설명은 현재 Active Payload의 자동 복원과 구분한다. 이 차이는 현재 코드와 [전용 Domain 완료 기록](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/MODIFIER_INDEPENDENT_RUNTIME_UNDO_RELEASE_NOTE.md)을 기준으로 정리했다.

0.3.33 이하의 Modifier 저장 데이터는 0.3.34의 새 Domain 형식으로 자동 이전하지 않으며 한 번 재Import한 뒤 저장해야 한다. Painting Snapshot은 독립 상태로 유지된다. 복원 실패 안내와 작업공간 표시 경계는 유지한다.

근거: [전용 Runtime·Undo 결정](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/decisions/0009-independent-modifier-runtime-undo.md), [빠른 원본 복원](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/MODIFIER_FAST_RUNTIME_RESTORE_RELEASE_NOTE.md), [현재 파일 로드 처리](../tools/modifier/blender/state_handlers.py).

<a id="projection"></a>
## 5. Projection Painting

### 5.1 독립 COLMAP·OBJ 파일 입력

0.3.73부터 `COLMAP 불러오기 / OBJ 불러오기`로 파일을 직접 가져온다. COLMAP은 `shared/colmap_io`, OBJ는 `shared/blender_runtime/obj_import.py`의 읽기 코드만 Align과 공유하며 이후 자원은 Painter가 독립 소유한다. Modifier 업데이트 버튼·오퍼레이터·자동 제공자 등록은 제거했다. 이전 Modifier 복사 자료는 저장·복원 호환용으로 유지한다.

준비 중 원본 파일이 바뀌거나 입력이 실패·취소되면 임시 자원만 정리한다. COLMAP 재입력은 Paint Target, Layer, Texture Pixel, Mask, Adjustment와 Composite를 교체하지 않는다. OBJ는 원본 면·UV·재질·텍스처를 유지하는 새 실제 메시로 추가하고 기존 페인트 대상을 자동 교체하지 않는다. 두 입력의 축은 별도로 지정한다.

진행창은 Point 버퍼·Mesh·색상, Camera Data/Object 생성, Scene Commit 등 실제 작업량과 경과 시간을 표시한다. 실패·취소 시 Staging·Timer·Draw Handler를 함께 정리한다. 기존 READY Snapshot은 Modifier Runtime 준비 여부와 별개로 Painting 재진입에 사용한다. 새 파일 입력과 이전 저장 자료의 실제 복원·벤치마크 결과는 [독립 가져오기 구현 검증](COLMAP_Painter_독립_가져오기_구현검증.md)을 참고한다.

2026-10-06의 후속 개선에서 새 COLMAP 입력의 참고 점 상한 기본값을 20,000개로 설정하고 파일 탐색기에서 변경하도록 했다. 카메라·촬영 이미지는 모두 유지하며 기존 저장 점군을 자동 축소하지 않는다. 전체 파일 재파싱을 반복하던 확정 전 검사를 내용 해시·경로 검사로 바꾸고, 참고용 보관·배열 기록·모달 갱신·카메라 조회를 개선했다. 사용자 실제 709,048점·593이미지 자료의 화면 입력 중앙값은 95.46초에서 13.22초로 줄었다. 조건·호환 검사·현재 설치 반영 여부는 [속도 개선 구현 검증](COLMAP_Painter_COLMAP_가져오기_속도개선_구현검증.md)에 구분해 기록한다.

### 5.2 레이어·선택·활성화·합성

- Layer·Layer Mask·Adjustment·Adjustment Mask를 한 번 클릭하면 UI 선택만 바뀐다. 수정키 없는 더블클릭은 실제 Paint 대상을 활성화한다. Ctrl/Shift는 복수 선택을 처리한다.
- 파란 배경은 선택, 별도 활성 기호는 실제 Paint 대상을 나타낸다. 트리 밖 클릭은 선택만 비우며 Paint Image를 바꾸지 않는다.
- `-`와 레이어 트리 문맥의 `Del`은 선택된 항목을 삭제한다. 부모·자식 중복 삭제를 피하고, Composite·잠금 항목이 섞이면 삭제 전체를 시작 전에 중단한다.
- 복수 삭제 후 Shader Graph를 한 번 재조정한다. 마지막 쓰기 가능한 레이어 삭제 시 빈 `Layer 1`로 대체한다. 다른 Editor의 Delete는 가로채지 않는다.
- 복수 선택 중 페인트 스트로크를 차단하는 상태와 실제 Paint 활성 대상을 구분한다. 스크립트의 기존 `EXEC_DEFAULT` 선택 API는 즉시 활성화 호환을 유지한다.
- 레이어 개인 단독 보기는 검정 바탕에 해당 레이어의 색·Alpha·Mask·Adjustment만 반영한다. 아래 레이어까지 합성하던 0.3.3 방식은 0.3.5에서 대체됐다. Mask 단독 보기는 흑백으로 표시하며 세 단독 보기 방식 중 하나만 활성화한다.

외부 FILE·TILED Original은 같은 Blender Image를 읽기 전용으로 직접 참조한다. Original 본체를 일반 스트로크로 칠하지 않지만 눈·이름·Mask·Adjustment·Composite 참여는 유지한다. 원본 경로·색 공간·Alpha·Packed 상태를 임의 변경하지 않는다. 관리 레이어와 외부 Original의 해상도는 다를 수 있다. 외부 Original 본체와 관리 Mask의 소유권 검사도 구분한다.

Composite는 실제 Paint Target으로 직접 편집한다. 합성에 참여한 Source 레이어는 잠그고 Composite와 이후 추가한 독립 일반 레이어를 편집할 수 있다. 과거 Composite 기반 Clone Source·Slot·파생 레이어의 자동 이전·저장 차단은 현행 구조가 아니다. 기존 데이터셋을 자동 초기화하지 않는다.

### 5.3 Blender 도구·브러시·Stencil

Projection Paint의 3D 뷰는 Blender 기본 Texture Paint의 도구 막대, 상단 도구 설정, 브러시 자산 선반을 사용한다. Align 기반 화면 템플릿에 저장된 숨김 상태는 기존·신규 작업공간에서 한 번 복구한다. 도구 막대와 상단 설정은 작업공간 준비 때, 브러시 선반은 페인트 모드에서 해당 영역이 사용 가능해진 첫 도구 추적 시점에 복구한다. 이후 사용자가 숨긴 상태는 재진입 때 보존한다. 도구·활성 브러시·모드 선택은 이 표시 복구로 변경하지 않는다.

Draw·Blur·Smear·Clone·Fill·Mask와 사용자 Brush의 Blender 기본 동작을 유지한다. Tool ID가 바뀐 순간 필요한 종류만 연결하고, 호환 사용자 Brush가 있으면 유지한다. Clone은 활성 Layer Image를 Source와 Target으로 쓰며 `use_clone_layer=False`를 적용한다. Clone Source Image·Slot·Offset을 별도로 소유하지 않으며 `Ctrl+LMB` 위치 지정은 Blender에 전달한다.

수동 `Reconnect Stencil Texture`에서 편집 가능한 프로젝트 전용 `Projection Paint Stencil Draw` Brush를 최초 한 번 만든다. 읽기 전용 Essentials Brush에 직접 쓰지 않으며 이후 같은 프로젝트 Brush를 재사용한다. 다른 도구의 Brush를 복제하거나 작업공간 이동마다 강제 선택하지 않는다. 재연결 실패 때는 호출 전 상태와 그 호출에서 만든 자원만 복구·정리한다.

Stencil과 Painting 활성 상태는 별개다. 유효한 Layer·Mask가 Paint Target이면 Stencil이 끊겨도 Painting은 활성이다. Mask 편집 중에는 Camera Stencil을 끊고 자동 재연결·투영을 차단한다. Camera View 밖에서도 Source Image·Layer는 유지하고 Stencil 연결만 일시 중지한다. 같은 프로젝트 Draw Brush·유효 Source·Camera View 조건이 맞으면 기존 연결을 복구하며, 사용자의 수동 해제나 다른 Brush 선택을 덮어쓰지 않는다.

Camera Frame 변경은 마지막 입력 후 0.30초 대기해 최신 Camera·Source를 한 번 연결한다. 실패하면 이전 연결을 복구하고 같은 최신 요청만 제한 재시도하며, 새 Frame이 오면 오래된 요청을 폐기한다. 수동 Reconnect는 대기를 건너뛴다. Camera View 이탈은 Frame 대기보다 먼저 처리한다. 작업공간 복귀 때 Camera View·Paint Tool 추적 Timer를 각각 다시 연결한다.

현재 Stencil 배치의 저장은 Blender Brush 상태를 따른다. 구형 별도 위치·크기·회전 복원 필드와 장기 Brush 임대·복구 구조를 다시 도입하지 않는다. 상태는 최상단 한 줄에서 연결·Camera 이탈·Mask 편집·Brush 변경·수동 해제·오류 사유로 구분한다.

연결된 Draw 스텐실의 3D 페인트 뷰에서 Ctrl을 누르는 동안 기본 Erase Soft를 일시 선택한다. 기본 Ctrl 반전 대신 정상 지우기 스트로크를 호출하며 원래 브러시의 텍스처·배치·색·강도는 보존한다. 마지막 Ctrl 해제 또는 창·작업공간·모드 이탈, 저장·수명주기 중단·종료 시 같은 자산과 도구로 복구한다. 이 복구 상태는 입력 중 메모리에만 보관한다. Mask·Clone·연결 해제 상태에는 적용하지 않는다.

| 입력·표시 | 적용 범위 |
|---|---|
| F6 | 유효한 Painting 세션·Snapshot·일반 Layer에서 수동 Stencil 재연결 |
| F7 | 해당 도구 작업공간·Camera Review의 배경 이미지 전환 |
| Ctrl 누르기 / 놓기 | 연결된 Draw 스텐실에서 임시 Erase Soft / 원래 브러시 복귀 |
| Alt+좌/가운데/우 드래그 | Painting 3D View에서 Blender 기본 회전/이동/확대 호출 |
| Material Preview / Diffuse Color | Painting 진입 시 재질 색 표시, 사용자 변경을 보존하며 종료 시 자기 설정만 복구 |

### 5.4 가시 레이어 텍스처 출력

`Export Texture`는 Composite 생성 여부와 관계없이 실제 Base Color Graph에서 눈이 켜진 레이어를 출력한다. 레이어 순서, Mask, Adjustment, Alpha를 반영하고 활성 Composite의 잠긴 Source를 중복 합성하지 않는다. 단독 보기 상태는 출력 범위를 결정하지 않으며 영구 Composite나 레이어 눈 상태를 바꾸지 않는다. 출력할 가시 레이어가 없으면 파일 선택 전에 안내한다.

GUI에서는 Color·Alpha Bake를 Blender Native `OBJECT_BAKE` Job으로 실행한다. Color → Alpha → Combining → Writing → Staging → Committing 단계를 분리하고 Bake 중에는 장치·해상도·경과 및 활동형 진행 표시를 사용한다. 실제 알 수 없는 백분율을 만들어 표시하지 않는다. 긴 문자열은 BLF 픽셀 폭으로 줄여 진행창 안에 유지한다.

출력 중 일반 입력은 차단하고 Esc만 취소로 전달한다. Native Bake가 완전히 멈춘 뒤 임시 Node·Image와 렌더·선택·파일 상태를 정리한다. 전체 Image Pixel 복사·SHA-256을 시작과 끝에 반복하던 검사는 제거했고 입력 잠금으로 사용자 변경을 막는다. 기존 출력 Backup·원자 확정·실패 Rollback은 유지한다.

`Overwrite Existing`이 단일·UDIM 전체 출력의 덮어쓰기 권한이다. Blender 파일 브라우저의 별도 기본 확인과 이중으로 충돌시키지 않는다. Background 검증 경로는 동기 Bake를 유지한다.

근거: [독립 Snapshot](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/INDEPENDENT_PROJECTION_SNAPSHOT_RELEASE_NOTE.md), [선택·활성화·삭제](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/LAYER_SELECTION_ACTIVATION_DELETE_UX_RELEASE_NOTE.md), [프로젝트 Draw Brush](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/PROJECT_STENCIL_DRAW_BRUSH_RELEASE_NOTE.md), [Native Bake 출력](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/migration/TEXTURE_EXPORT_NATIVE_BAKE_PROGRESS_RELEASE_NOTE.md).

<a id="storage"></a>
## 6. 저장·복원·외부 파일 의존성

| 데이터 | `.blend`에 남는 내용 | 다시 열 때의 조건 |
|---|---|---|
| Align | 스키마 10, 마커·개별 원본 축·공통 변환·개별 배치 보존 기준·확정 행렬·품질/출처, OBJ 원본 Polygon·UV·내장 UUID | COLMAP 원본은 외부 참조. 정상 내장 OBJ는 외부 OBJ 없이 복원·출력. 외부 텍스처는 별도 보관 필요 |
| Modifier | Source Metadata·Domain Identity·확정 EditOperation을 Active/Staging Text로 저장 | 같은 Source를 읽고 편집·Undo를 재구축. Reconstruction·Index·Delta·Redo 분기 자체는 저장하지 않음 |
| Projection Painting | 독립 COLMAP Snapshot과 Paint Layer·Mask·Composite·Brush 상태 | 외부 Original·촬영 이미지 의존성을 보존. Modifier 최신 편집은 COLMAP 내보내기 후 파일 재입력 |

Align은 구현된 이전 스키마와 과거 엔진 승인 기록을 읽으며 새 알고리즘으로 자동 재정렬하지 않는다. 구형 외부 OBJ 저장 형식의 내장 전환은 별도 검증 경로를 거친다. Modifier의 구형 Domain 미지원 데이터와 Painting의 Composite 초기화 정책을 동일한 자동 마이그레이션 규칙으로 묶지 않는다.

`.blend` 저장과 COLMAP·OBJ·텍스처 내보내기는 목적이 다르다. 형상 내장이 외부 이미지 파일까지 모두 포장했다는 의미도 아니다. 다른 컴퓨터로 전달할 때 각 도구가 참조하는 외부 원본과 이미지를 함께 확인한다.

<a id="build"></a>
## 7. 빌드·배포

기본 빌드 산출물은 출력 폴더의 **`colmap_tools.zip`**이다. CPython 3.12.10·NumPy 1.26.4, 새 점 기반 워커·Eigen 3.4.0·oneTBB 2022.0.0, 기존 CC 비교 실행 환경, 라이선스와 재빌드 원문을 포함한다. 이번 브랜치의 검증 ZIP은 구현 검증 보고서에 별도 경로와 해시를 기록하며, 기존 최상위 ZIP은 자동 교체하지 않는다. 사용자는 별도 Python을 설치하지 않아도 된다. 업데이트 전에 열린 Blender는 이전 모듈을 유지하므로 재시작해야 새 설치본이 적용된다. 0.3.43 실제 설치본에서도 숨김 GUI 검증으로 세 도구의 N 패널 폭이 모두 461px(UI 배율 1.25)임을 확인했다.

| 빌드 프로필 | 기본 엔진 | 용도 |
|---|---|---|
| 생략 / `PointKd` | 점 기반 강체 | 이번 브랜치 기본 배포 |
| `CCCoreLib` | CC | 이전 엔진 명시적 비교 |
| `SmallGicp` | Small GICP | 모든 A/P가 점군인 명시적 비교 |
| `Full` | 점 기반 강체 | 선택형 이전 엔진 소스를 포함하는 개발 비교. 해당 외부 Python은 별도 지정 |

기본 PointKd ZIP에 Small GICP·Open3D 실행 패키지를 혼합하지 않는다. 저장소 최상위의 `colmap_tools.zip` 하나를 최신 정식 설치본으로 Git에 포함한다. 기본 빌드는 검증을 통과한 파일로 이 ZIP을 교체하고 같은 출력 폴더의 이전 버전 설치 ZIP을 정리한다. 나머지 실행 환경·ZIP·로그·캐시는 Git에서 제외하고 `.cccore-runtime/`, `.point-kd-runtime/`과 빌드 Staging에서 관리한다. 새 실행 환경 빌드에는 Visual Studio 2022 C++ 도구와 고정 원문 다운로드가 필요하다.

Root 환경설정의 CPU 스레드는 0 자동, 1 단일, 2 이상 수동 상한이다. 자동은 논리 CPU 하나를 남기는 정책이며 실행기는 활성 논리 프로세서 수로 다시 제한한다. 실행 중 변경값은 다음 정렬에 반영한다. 현재 엔진은 CPU/oneTBB 경로이며 GPU 정렬 엔진은 포함하지 않는다.

CCCoreLib 고정 커밋은 `dc8c7d80f4ef8fd6a2c302c8d5f9c35099ba8432`다. 잠금 파일의 버전·패치·적용 전후 원문·바이너리 SHA-256과 실제 연산을 검사한다. 패치는 LF 줄바꿈으로 보관한다. 배포 원래 바이트의 해시 검사를 줄바꿈 정규화로 대체하지 않는다.

런타임 고지상 CCCoreLib는 LGPL-2.0-or-later, oneTBB는 Apache-2.0이며 다른 구성요소와 Microsoft 재배포 파일의 조건은 각각 유지한다. CC 공유 DLL의 수정·재빌드 가능한 원문과 패치를 제공한다. CloudCompare GUI/qCC를 배포하지 않는다. 정확한 구성은 [잠금 파일](../tools/align/native/cccore_worker/dependencies.lock.json), [제삼자 고지](../tools/align/native/cccore_worker/THIRD_PARTY_NOTICES.md), [재빌드 절차](../tools/align/native/cccore_worker/REBUILD.md)를 따른다.

```powershell
# 저장소 루트에서 기본 빌드
.\build_tools\build_extension.ps1

# 이미 검증된 실행 환경으로 빌드
.\build_tools\build_extension.ps1 `
  -PythonPath "$PWD\.cccore-runtime\python\python.exe" `
  -CcCorePythonPath "$PWD\.cccore-runtime\python" `
  -CcCoreRuntimePath "$PWD\.cccore-runtime\cccore"

# 격리 설치본과 작업공간·파일 재연결까지 검사
.\build_tools\test_installed_extension.ps1 `
  -PackagePath "$PWD\colmap_tools.zip" -WorkspaceGui
```

새 네이티브 실행 환경 준비와 다른 세부 검사 명령은 [보관된 검증 문서](temp/TESTING.md)와 현재 빌드 스크립트를 참조한다. 보관 문서의 실험판 경로·과거 버전 고정 인자는 그 시점의 재현 기록이다.

<a id="verification"></a>
## 8. 완료된 검증과 수치의 해석

이 절은 **기존 완료 기록**을 통합한 것이다. 이번 문서 정리에서 Blender 정렬이나 성능 검사를 다시 실행한 것으로 해석하지 않는다.

### 8.1 0.3.41 검증

| 검사 | 완료 기록 |
|---|---|
| 네이티브·메시 필터 접근 위반 회귀 | 38개 통과 |
| 겹침 후보 표본·v4 계약 | 각각 8개·5개 통과 |
| 네 입력 조합·큰 좌표·스레드 동등성 솔버 | 43개 통과 |
| Blender Runtime·UI·저장 | 각각 8개·11개·10개 통과 |
| 합성 부분 겹침 100%·70%·40%·20% | 위치·회전·배율 고정 기준 모두 통과 |
| 기본 배포 | 279개, 통합 등록·스테이징 정렬·ZIP 무결성 통과 |
| 격리 설치본 | 네 조합 정렬·승인·Undo/Redo·OBJ 면/UV·설정 통과 |

최대 2,048회 준비 조회의 요약 저장·복원이 256 KiB 한도 안에서 통과했다. 0.3.41은 정식 Blender에 설치했으며 설치 전후 환경설정 파일 지문이 같았다. 2026-09-09 커밋·푸시 직전에도 표본·계약 13개와 ZIP 무결성을 다시 확인했다.

### 8.2 동일 실데이터 평가

사용자 `Untitled.blend`의 같은 3점 초기 행렬을 사용하고, 비교할 때 **동일한 고정 평가점**에서 다시 거리를 계산했다.

| 후보 | 고정 겹침 RMS | 전체 참고 방향 평균 RMS | 주방향 유효 대응 |
|---|---:|---:|---:|
| 초기 정렬 | 0.345435 | 4.429829 | 100.00% |
| v3 결과 | 1.290505 | 3.483424 | 34.56% |
| v4 결과 | 0.297436 | 4.173361 | 95.40% |

v4는 강체 결과를 선택했다. 초기 대비 고정 겹침 RMS는 약 13.9% 줄었지만 일부 구간의 거리 증가와 더 나쁜 배율 후보 때문에 `REVIEW`를 유지했다. 전체 RMS만 작다는 이유로 더 좋은 정렬로 판단하지 않는다. 이 수치는 독립 정답점 오차나 RC 재구성 정확도 개선율이 아니다.

최종 v4 실데이터 실행은 16스레드에서 벽시계 37.55초였다. 준비 20.67초, 강체 7.57초, 배율 6.18초이며 네이티브 프로세스 1개·명령 153회·원본 메시 적재 각 1회였다. 질의 범위가 달라 옥트리와 메시 격자는 각각 302회·151회 생성됐다. 네이티브 최고 작업집합 113.36 MiB는 Blender·Python 전체 메모리와 다르다.

합성 100%/20% 겹침을 4스레드에서 각각 3회 실행한 중앙값은 15.359초/21.047초이며 최대 정답 위치 RMS는 0.00000667/0.00048647였다. v3 실데이터 기준선은 다른 검사와 동시에 측정됐으므로 v4와의 벽시계 속도 개선율은 확정하지 않았다. 상세 조건·해시는 [0.3.41 기록](temp/migration/ALIGN_OVERLAP_SAMPLING_RELEASE_NOTE.md)에 있다.

### 8.3 기능 변경 때의 검증 경계

```powershell
& .\.cccore-runtime\python\python.exe -X utf8 tests/align/test_icp_cccore_overlap.py
& .\.cccore-runtime\python\python.exe -X utf8 tests/align/test_icp_cccore_v4_contract.py
& .\.cccore-runtime\python\python.exe -X utf8 tests/align/test_icp_cccore_solver.py
& .\.cccore-runtime\python\python.exe -X utf8 tests/align/test_icp_cccore_partial_overlap.py --threads 4
.\build_tools\test_align_source.ps1
```

구조·공개 식별자·등록·독립 실행은 Architecture/Coexistence와 설치본으로, Core는 순수 Python 검사로, Blender ID·저장·Undo는 Background 검사로 확인한다. GPU 표시·실제 입력·Workspace 교대·Native Bake 진행은 GUI 검사가 필요하며 Background 성공만으로 대체하지 않는다. 사용자 원본은 읽기 전용으로 보존하고 테스트 산출물·로그·개발 경로가 ZIP에 섞이지 않도록 검사한다.

<a id="history"></a>
## 9. 주요 완료 이력과 대체된 결정

| 단계 | 완료한 변화 | 현재 해석 |
|---|---|---|
| 초기 통합, 0.1.1~0.1.11 | 단일 Extension, 도구별 Seed, 세션 교대, 데이터·Marker 격리, 파일 로드 구독 복원 | 독립 경계 유지 |
| 0.1.12~0.1.20 | 표시 UI, Align 대표 삼각형, OBJ UV·diffuse·출력 경로, 화면 복구 | OBJ 형상 저장은 이후 내장 원본 구조로 확장 |
| 0.2.0 → 0.3.0 | 공용 COLMAP Resource → 수동 독립 Snapshot | 실시간 Camera·Point 공유와 자동 동기화는 폐기 |
| 0.3.1~0.3.12 | Original·Layer·Mask·Stencil·Composite 기초 동작 | 선택·Stencil 저장·Clone 일부는 후속 버전으로 대체 |
| 0.3.13~0.3.20 | 수동 Stencil, 기본 도구, Composite 직접 편집, 프로젝트 Draw Brush, 추적 Timer | 자동 Composite 이전·별도 Clone Source/Slot 제거, 프로젝트 Brush 재사용 |
| 0.3.21~0.3.27 | 마지막 Frame 대기, 화면 탐색, 선택/활성화/삭제, Camera 이탈, 상태·F6·Diffuse 표시 | 현재 Painting UX |
| 0.3.28~0.3.32 | 가시 레이어 출력, 단일 덮어쓰기 권한, 입력 잠금, Native Bake, 진행 문자열 경계 | 전체 Pixel Hash 반복 검사 없이 Native Job 수명과 원자 출력 유지 |
| 0.3.33~0.3.35 | 빠른 Source 복원, Modifier Domain·전용 Undo, Camera 쓰기 권한 격리 | 정상 Active Payload 자동 복원, Painting은 독립 Snapshot |
| 0.3.36~0.3.40 | Align Object Mode 진입, CC 정식 이관, 유휴 내보내기 상태, N 패널 폭·왼쪽 닫힘 | 정식 main에서 작업 |
| 0.3.41 | 겹침 후보 선별 후 표본, 고정 겹침 평가, CC 접근 위반·출처 저장 한도 수정 | 현재 기본 CC v4 |

설계 결정을 읽을 때 다음 대체 관계를 함께 적용한다.

- ADR-0001의 도구 독립성은 유지한다.
- ADR-0006의 Small GICP 기본 배포는 ADR-0011에서 CC로 대체했다. 외부 프로세스 격리와 명시적 비교 빌드는 유지한다.
- ADR-0007의 실시간 공용 Dataset은 ADR-0008의 수동 Snapshot으로 대체했다. `shared/colmap_dataset`은 Legacy 입력이다.
- ADR-0008의 Modifier 별도 Undo 불필요 부분은 ADR-0009에서 전용 Domain·Journal로 대체했다. 값 복사와 Painting 독립성은 유지한다.
- ADR-0010의 실험판 한정 범위는 ADR-0011과 0.3.37 이관에서 정식 채택으로 바뀌었다. 0.3.41은 같은 제품 경계에서 실행·평가 계약을 v4로 갱신했다.
- 외부 OBJ 재읽기·삼각형 전용 출력, 구형 UV 복원 설명은 내장 원본·삼각형 프록시·스키마 8 구조로 대체했다.
- 한 번 클릭으로 Paint 대상까지 활성화하던 레이어 선택은 0.3.23의 선택/더블클릭 활성화로 대체했다. Align 레이어 이름 더블클릭과는 다른 기능이다.

<a id="pending"></a>
## 10. 미완료·미검증·미채택 항목

| 항목 | 현재 상태 |
|---|---|
| Modifier 데이터셋 변환 기즈모 화면 검수 | [구현·검증 기록](COLMAP_Modifier_기즈모_데이터셋_변환_계획서.md). 전체 점군·카메라 이동·회전·균일 배율, 전용 이력·저장·출력·Painter 전달 구현 및 자동 검증 완료. 0.3.42 빌드·설치본 반영 완료. 실제 GUI 조작감·대용량 화면 성능은 별도 |
| 사용자 매뉴얼 화면 삽입·최종 검수 | [공통 안내](manual/README.md)와 [Align](manual/ALIGN.md) · [Modifier](manual/MODIFIER.md) · [Projection Paint](manual/PROJECTION_PAINT.md) 기능별 본문 및 캡처 위치 22곳 작성. 사용자가 직접 화면을 촬영·삽입한 뒤 최종 대조할 예정. 최초 [구성안](temp/manual/README.md)은 보존 |
| 실데이터 정렬의 독립 품질 판정 | 0.3.41 고정 표본 비교는 완료. 독립 검사점·공통 부위 시각 판정·RC 재구성은 별도 확인 대상 |
| OBJ 실데이터 최종 검수 | 원본 면·UV·저장·외부 OBJ 삭제 회귀는 완료. 초기 계획의 사용자 최종 QC를 자동 검사로 승인한 것으로 보지 않음 |
| CC 가속 구조의 장기 공유 | 기본 형상 적재와 정확 평가 캐시는 구현. 등록 내부 옥트리/격자 추가 성능 패치는 미채택 |
| CC 프로토콜 부가 비용 1% 목표 | 과거 v3 짧은 입력에서 미달. 정식 채택은 전체 시간·정확성·메모리·기능 근거이며 과거 실패 기록은 유지 |
| 페이징·메모리 일반화 | 정식 v3 이관 당시 특정 장비 PDH 검사는 완료. 디스크 페이지 읽기·생애 피크 합계를 모든 환경의 스왑 사용량으로 일반화하지 않음 |
| GPU 정렬·CPU별 최적 상한 | 현재 CPU/oneTBB와 수동 상한 제공. GPU 엔진이나 모든 CPU의 최적 상한 탐색은 미구현 |
| Modifier Global Undo Step 완전 제거 | 편집 권위·전용 Journal 격리는 완료. 파생 Blender ID의 물리적 Undo Step 0개는 조건부 검증 대상 |
| Blender Workspace 삭제 내부 진단 | 과거 Blender 5.2 공식 삭제의 `SRLayout` 진단을 기록. 사용자 수 강제 수정은 미채택이며 이후 Blender 버전의 해결을 확인하지 않음 |

이 항목들은 이번 문서 정리로 구현·승인된 것으로 바꾸지 않는다. 새로운 작업 계획을 세울 때 현재 버전·완료 근거·검증할 범위를 다시 지정한다.

<a id="archive"></a>
## 11. 남긴 자료와 이전 문서 복원

통합본에 반영된 완료 계획·과거 릴리스·대체된 설계·중복 목록 **98개를 삭제**하고, 아래 **9개**를 기존 하위 구조에 남겼다. 상세 사용자 매뉴얼과 추가 실데이터 검수에 필요한 내용, 통합본보다 자세한 재현 명령·벤치마크 근거를 보존한다. 네이티브 재빌드·라이선스 문서는 도구 폴더의 원래 위치에 유지한다.

이 자료들도 당시 버전·실험 환경의 기록이다. 현행 기능은 이 통합본을 기준으로 확인하며, 과거 실패·미채택 항목을 완료로 바꾸지 않는다.

| 남긴 자료 | 보존 이유 |
|---|---|
| [COLMAP Tools 사용자 매뉴얼 구성안](temp/manual/README.md) | 상세 매뉴얼 작성에 사용한 최초 목차·집필 범위 |
| [통합 검증 방법](temp/TESTING.md) | 도구별 상세 재현 명령과 회귀 검사 절차 |
| [COLMAP Align CC 겹침 후보 우선 샘플링 계획서](temp/COLMAP_Align_CC_겹침후보_우선샘플링_계획서.md) | 겹침 선별 계약과 독립 품질 검수 기준 |
| [COLMAP Align OBJ 내장 원본 토폴로지·정렬 프록시 계획서](temp/COLMAP_Align_OBJ_원본토폴로지_보존_정렬프록시_계획서.md) | 원본 면·UV 보존 계약과 실데이터 최종 검수 기준 |
| [Align 겹침 후보 우선 샘플링 0.3.41](temp/migration/ALIGN_OVERLAP_SAMPLING_RELEASE_NOTE.md) | 0.3.41 수치·메모리 오류 수정·배포 해시·검증 한계 |
| [Align CC 정식 승격 0.3.37](temp/migration/ALIGN_CC_MAIN_RELEASE_NOTE.md) | 정식 채택 당시 성능·메모리·페이징 측정 근거 |
| [CCCoreLib 엔진 교체 구현·검증 기록](temp/COLMAP_Align_CCCoreLib_구현검증.md) | CC 교체·저장·내보내기 오류 재현과 단계별 검증 이력 |
| [COLMAP Align CCCoreLib CPU 멀티스레드 적용 계획서](temp/COLMAP_Align_CCCoreLib_CPU_멀티스레드_적용_계획서.md) | 단일·다중 스레드 비교 조건과 실측 결과 |
| [COLMAP Align CCCoreLib 단일 세션 워커·지오메트리 캐시 구조 변경 계획서](temp/COLMAP_Align_CCCoreLib_단일세션워커_지오메트리캐시_계획서.md) | v2/v3 벤치마크와 옥트리·격자 캐시 미채택 근거 |

삭제 전 전체 원문은 [Git 커밋 `fd2d9cf`의 문서 목록](https://github.com/hckyeong/Colmap_blender_Tools/tree/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs)에서 확인한다. 통합본과 남긴 자료에서 삭제된 문서를 참조하는 링크는 이 커밋의 원문으로 연결했다. Git 이력을 삭제하거나 다시 쓰지 않았다.

필요한 문서 한 개를 작업 폴더로 복원하려면 다음처럼 원래 경로를 지정한다. 아래는 과거 설계 문서를 복원하는 예시이며, 현재 문서를 덮어쓰지 않는 경로인지 확인하고 실행한다.

```powershell
git restore --source=fd2d9cf -- docs/architecture/PRODUCT_BOUNDARIES.md
```
