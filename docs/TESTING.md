# 통합 검증 방법

모든 명령은 Repository Root에서 실행합니다.

## 정적 구조와 공존 식별자

```powershell
python -m unittest discover -s tests/architecture -p "test_*.py"
python -m unittest discover -s tests/coexistence -p "test_*.py"
```

## 순수 Python Core 회귀

```powershell
python -m unittest discover -s tests/modifier/core -p "test_*.py"
python -m unittest discover -s tests/projection_paint/core -p "test_*.py"
```

## Blender Background 통합 등록

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" `
  --background --factory-startup `
  --python ".\tests\blender_background\test_extension_registration.py"
```

이 검사는 등록 실패 Rollback, 중복 등록·해제, 공통 메뉴, 세 도구 진입 Operator와 기존 Scene 속성의 생성·정리를 확인합니다.

## Align OBJ UV 보존 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\align\blender\test_mesh_extension.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\align\blender\test_obj_export.py"
```

첫 검사는 `vt` 1~3성분, 양수·음수 UV 인덱스, UV seam, 부분 UV 면 집합, `mtllib`·`usemtl`·`map_Kd`, 재질별 face-corner 텍스처 배치, 잘못된 입력 거부와 Runtime 불변 배열을 확인합니다. 두 번째 검사는 `모델표시이름.obj`·`모델표시이름.mtl`·`원본이미지명`의 단일 폴더 출력, 모델·텍스처 파일명 충돌 거부, 동일 원본 텍스처 공유, Windows 260자 경로 제한 사전 진단, UV 증분 기록, Reflection 정점·UV Corner 동시 반전, Report Schema 3, 원본 이미지 바이트 복사·SHA-256, 부분 Coverage와 Blender 5.2 기본 OBJ Importer의 실제 UV Layer·이미지 재질 재생성을 확인합니다.

백그라운드 Blender는 GPU 컨텍스트를 초기화하지 않으므로 Align 텍스처 셰이더는 실제 GUI 프로세스에서 같은 `test_mesh_extension.py`를 실행해 추가 검증합니다. 이 검사는 유효한 PNG를 생성하고 GPU Texture, 재질별 TRIS 배치와 자원 해제를 확인합니다.

격리 설치본 검사는 같은 UV Fixture를 설치 ZIP 내부 Reader·Writer로 왕복한 뒤 Blender Mesh의 face-corner UV seam까지 추가로 확인합니다.

## Workspace Seed 검증

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python ".\build_tools\workspaces\validate_workspace_seed.py" -- `
  --input ".\tools\align\resources\workspaces\colmap_align_seed_5_2.blend" `
  --tool-id align --workspace-name "__COLMAP_ALIGN_SEED_5_2__"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python ".\build_tools\workspaces\validate_workspace_seed.py" -- `
  --input ".\tools\modifier\resources\workspaces\colmap_modifier_seed_5_2.blend" `
  --tool-id modifier --workspace-name "__COLMAP_MODIFIER_SEED_5_2__"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python ".\build_tools\workspaces\validate_workspace_seed.py" -- `
  --input ".\tools\projection_paint\resources\workspaces\projection_paint_seed_5_2.blend" `
  --tool-id projection_paint --workspace-name "__PROJECTION_PAINT_SEED_5_2__"
```

각 Validator는 내부 Workspace ID, Tool ID, Seed Schema, 생성 Blender Version, Scene Runtime ID 부재와 모든 Viewport Object 필터의 중립 `True` 상태를 확인합니다.

## Workspace Factory와 실행 순서 GUI 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup `
  --python-use-system-env `
  --python ".\tests\blender_background\test_workspace_factory.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup `
  --python-use-system-env `
  --python ".\tests\blender_gui\test_workspace_isolation.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup `
  --python-use-system-env `
  --python ".\tests\blender_gui\test_camera_marker_handoff.py"
```

Factory GUI 검사는 세 Seed의 독립 Workspace·Screen Pointer, CREATING → READY 전환, 역순 공식 삭제와 두 ID의 완전 제거를 확인합니다. 실행 순서 검사는 Align N 패널에 수동 종료 버튼이 없는 상태에서 Align → Modifier → Projection Paint → Align 전환과 Modifier → Align 전환, 기존 COLMAP Workspace 탭 및 일반 Blender Workspace 탭을 직접 선택한 수동 전환, 이전 세션의 자동 종료 완료 대기, 사용자가 선택한 일반 Workspace 유지, 세 도구의 READY Workspace 유지·재사용, Source 복귀, FAILED Workspace 부재와 사용자 Screen 보존을 확인합니다. 특히 일반 Workspace에서 기존 READY Align Workspace 탭을 다시 선택하는 즉시 N 패널 표시가 유지되고, 세션 재연결이 끝난 뒤 입력 가능 상태로 돌아오는지도 검사합니다. Blender 파일 로드와 동일하게 Workspace Message Bus 소유 구독을 지운 뒤 `load_post` 복구를 실행하고 Modifier → Align 탭 직접 전환이 다시 감지되는지도 검증합니다. Camera Marker 교대 검사는 Modifier와 Projection Paint가 같은 Frame 1에 실제 Camera를 Import하고 서로 전환한 뒤 활성 도구 Marker만 존재하는지, 일반 Workspace에서 두 Marker가 모두 제거되는지와 Manifest·Object·사용자 Marker 보존을 확인합니다. Projection Paint Background 검사는 준비 중 이탈한 `CREATING` Workspace는 삭제하고 정상 종료한 `READY` Workspace는 보존하는 상태별 계약도 확인합니다.

Blender 5.2는 Append Workspace 공식 삭제 시 `ID user decrement error: SRLayout` 내부 진단을 출력하지만 삭제 결과와 Workspace·Screen ID 제거는 정상입니다. 사용자 수 조작 우회는 사용하지 않습니다.

## Modifier 고정 COLMAP Fixture GUI 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup `
  --enable-event-simulate `
  --python-use-system-env `
  --python ".\tests\modifier\blender_gui\probe_modifier_workspace_workflow.py"
```

단일 Camera·Image·Point3D TXT Fixture를 실제 Import하고 Frame All, Camera Review, Point Edit, Global Undo, BIN Export와 Workspace 재진입을 검증합니다. Blender의 합성 입력 허용 옵션으로 Modifier Camera View에 실제 F7 PRESS·RELEASE 이벤트를 보내 관리 Background Image가 OFF → ON으로 전환되는지도 확인합니다. 종료 뒤에는 Modifier 관리 Object 데이터 블록이 삭제되지 않은 채 일반 View Layer에서 모두 제외되고, 재진입하면 Modifier 전용 View Layer가 활성화되어 관리 Collection이 다시 포함되는지 확인합니다. Pending Camera의 개별 숨김 상태와 Pending Undo도 그대로 유지되어야 합니다.

Modifier 데이터 View Layer의 순수 포함·제외 계약은 Background에서도 별도로 검증합니다.

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_modifier_data_visibility.py"
```

## F7 세션 격리 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_camera_review_background.py"
```

Projection Paint의 F7 배경 토글은 현재 Window가 해당 Scene의 Projection Paint Workspace를 표시할 때만 허용되어야 합니다. Modifier 또는 일반 Blender Workspace의 Camera View에서는 Projection Paint Operator가 입력을 소비하지 않아야 하며, Modifier F7은 기존 Modifier 세션·Workspace·Camera Review 조건을 독립적으로 검사합니다.

## Camera Marker 세션 격리 회귀

```powershell
python -m unittest discover -s tests/coexistence -p "test_scene_marker_session.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_camera_review_background.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_camera_review_load_rehydrate_background.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_camera_import_atomicity.py"
```

공용 검사는 Scene별 단일 소유, 같은 Window 재획득, 다른 도구와 다른 Window 차단 및 정확한 소유자 반납을 확인합니다. Modifier와 Projection Paint 검사는 Manifest 기반 Marker Attach·Detach·Rollback, 사용자 Marker 보존, 비활성 Load·Undo·Guard의 Marker 무생성, Extension 해제와 데이터 보존을 확인합니다. `tests/blender_gui/test_workspace_isolation.py`는 도구를 자동 또는 수동으로 전환할 때 이전 Marker 사용권이 반납된 뒤 다음 도구가 획득하는 순서도 함께 검사합니다.

## Extension 검증과 ZIP 빌드

```powershell
.\build_tools\build_extension.ps1
```

기본 `SmallGicp` Production 빌드는 다음 순서로 동작합니다.

1. 통합 Source만 `.artifacts` 아래의 격리 Staging으로 복사합니다.
2. Align 프로필을 `SMALL_GICP`로 고정하고 Open3D·Hybrid 선택형 구현을 제외합니다.
3. CPython 3.12.10, NumPy 1.26.4와 small_gicp 1.0.1 런타임을 `tools/align/runtime/python/`에 복사합니다.
4. Staging 복사본의 Python으로 합성 GICP를 실행합니다.
5. Blender Manifest 검증과 Extension Build를 실행합니다.
6. ZIP의 필수 Source·Native Runtime·라이선스와 제외 경로를 검사한 뒤 `build/`에 반영합니다.

런타임은 명시한 `-SmallGicpRuntimePath`, 통합 Root의 `.small-gicp-runtime/python`, 보존 중인 기존 Align의 `.small-gicp-runtime/python` 순서로 찾습니다. 세 위치에 모두 없으면 아래 준비 스크립트를 호출해 통합 Root의 Git 제외 캐시에 생성합니다.

```powershell
.\build_tools\prepare_small_gicp_runtime.ps1 `
  -PackagePython ".\.venv-small-gicp\Scripts\python.exe"
```

준비용 Python은 CPython 3.12 Windows x64여야 합니다. 공식 Embeddable Python ZIP은 고정 SHA-256으로 검증하며, 준비 결과도 합성 GICP를 통과해야 사용할 수 있습니다.

선택형 Backend를 보존한 개발 ZIP이 필요할 때만 다음과 같이 `Full` 프로필을 사용합니다. 이 프로필에는 내장 Small GICP 런타임이 포함되지 않습니다.

```powershell
.\build_tools\build_extension.ps1 -AlignProfile Full
```

## 격리 설치본 검증

```powershell
.\build_tools\test_installed_extension.ps1
.\build_tools\test_installed_extension.ps1 -WorkspaceGui
```

현재 사용자 설정과 설치본을 건드리지 않도록 `.artifacts` 아래에 임시 Blender 사용자 설정과 로컬 Repository를 생성합니다. Build ZIP을 설치·활성화해 공통 메뉴, 세 진입 Operator, Scene 속성과 세 Workspace Seed 내부 계약을 검사합니다. 이어 설치된 `tools/align/runtime/python/python.exe`로 합성 GICP를 실행하고 고정 버전을 확인한 뒤 임시 영역을 제거합니다. `-WorkspaceGui`는 같은 격리 설치본으로 전체 Workspace 실행 순서와 실제 Camera Marker 교대 GUI 회귀까지 실행합니다.

이 검사는 Native Runtime의 패키징·이동성·최소 연산과 Workspace 배포 자원을 확인하는 자동 Smoke Test입니다. 사용자 보유 실데이터의 Align·Projection Paint 전체 GUI 회귀, 대형 점군 성능, 취소·종료와 Native 장애 격리 검증은 별도의 최종 승인 범위입니다.
