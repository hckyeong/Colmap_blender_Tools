# 통합 검증 방법

모든 명령은 Repository Root에서 실행합니다.

## Projection Paint 가시 Layer Texture Export 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_texture_export.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_single_image_composite_export.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_layer_shader.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_composite_layer.py"

$env:COLMAP_TOOLS_INSTALLED_MODULE="bl_ext.user_default.colmap_tools"
$env:COLMAP_TOOLS_EXPECTED_VERSION="0.3.32"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" "C:\Users\gksru\Desktop\Untitled.blend" --python-use-system-env --python ".\tests\projection_paint\gui_visible_layer_texture_export.py"

$env:COLMAP_TOOLS_NATIVE_EXPORT_GUI_MODE="SUCCESS"
$env:COLMAP_TOOLS_NATIVE_EXPORT_GUI_RESULT="$PWD\build\native_texture_export_success.json"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --enable-event-simulate "C:\Users\gksru\Desktop\Untitled.blend" --python-use-system-env --python ".\tests\projection_paint\gui_native_texture_export_progress.py"

$env:COLMAP_TOOLS_NATIVE_EXPORT_GUI_MODE="CANCEL"
$env:COLMAP_TOOLS_NATIVE_EXPORT_CANCEL_STAGE="COLOR"
$env:COLMAP_TOOLS_NATIVE_EXPORT_GUI_RESULT="$PWD\build\native_texture_export_cancel_color.json"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --enable-event-simulate "C:\Users\gksru\Desktop\Untitled.blend" --python-use-system-env --python ".\tests\projection_paint\gui_native_texture_export_progress.py"

$env:COLMAP_TOOLS_NATIVE_EXPORT_CANCEL_STAGE="ALPHA"
$env:COLMAP_TOOLS_NATIVE_EXPORT_GUI_RESULT="$PWD\build\native_texture_export_cancel_alpha.json"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --enable-event-simulate "C:\Users\gksru\Desktop\Untitled.blend" --python-use-system-env --python ".\tests\projection_paint\gui_native_texture_export_progress.py"

$env:COLMAP_TOOLS_PROGRESS_OVERLAY_RESULT="$PWD\build\progress_overlay_text_bounds.json"
$env:COLMAP_TOOLS_PROGRESS_OVERLAY_SCREENSHOT="$PWD\build\progress_overlay_text_bounds.png"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup --python-use-system-env --python ".\tests\projection_paint\gui_progress_overlay_text_bounds.py"
```

Composite Layer를 미리 만들지 않은 일반 Layer Graph와 기존 Composite Graph를 모두 검사합니다. 눈이 켜진 Layer만 Color·Alpha 임시 Bake에 포함되고, Layer·Adjustment Mask와 보정값이 실제 출력 Pixel에 반영되어야 합니다. Export 성공·취소·실패 뒤에는 Layer 눈, 선택·활성 대상, 단독 보기, Composite 구조, Image Pixel·Dirty 상태와 임시 Image·Node 수가 그대로 유지되어야 합니다.

GUI 회귀는 실제 설치본과 저장 프로젝트를 사용합니다. 원본 `.blend`는 저장하지 않으며, 실제 Texture 파일 생성과 상태 불변성을 확인한 뒤 검사용 출력 폴더를 제거하고 `build/visible_layer_texture_export_gui.json`에 결과만 기록합니다.

Texture Export 파일 브라우저는 숨겨진 Blender `check_existing` 기본 확인을 사용하지 않습니다. UI에 표시된 `Overwrite Existing`만 권한의 단일 기준이며, 이 옵션을 켠 경우 기존 SINGLE·UDIM 묶음을 Backup으로 옮긴 뒤 새 파일 전체를 원자 확정해야 합니다.

파일 선택을 마치고 Export가 시작되면 Operator는 `BLOCKING` Modal로 동작합니다. `Esc` 취소를 제외한 마우스·키보드 입력은 Export 단계로 통과시키지 않아야 합니다. 이 입력 차단을 전제로 시작·확정 경계의 전체 Image Pixel SHA-256 검사는 수행하지 않습니다. 정적 회귀는 `sha256`, `pixels.foreach_get`, 구형 Export Snapshot 함수가 제품 코드에 남지 않는지 확인합니다.

Native GUI 회귀는 Color·Alpha Bake가 각각 Blender `OBJECT_BAKE` Job으로 실행되는 동안 Timer와 `Process.` 활동 문구가 계속 갱신되는지 확인합니다. 성공 경로는 Color → Alpha → Combining → Writing → Staging → Committing 순서, 실제 출력과 상태 불변성을 검사합니다. 취소 경로는 Color와 Alpha에서 실제 `Esc`를 각각 전송하고 출력 없음, 임시 Image 제거와 Layer·Paint 상태 복구를 검사합니다.

진행 안내창 GUI 회귀는 긴 Device 정보와 Image·Bake 정보가 실제 BLF 글꼴 픽셀 폭 기준으로 창 내부에 들어오는지 검사하고 Screenshot을 남깁니다.

## Projection Paint Diffuse Color View GUI 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup --python-use-system-env --python ".\tests\projection_paint\gui_base_color_paint_view.py"
```

검사는 Painting Mode 진입 후 전용 3D View가 `MATERIAL` Shading의 `DIFFUSE_COLOR` Render Pass를 사용하고, 진입 전 Combined 화면에서 캡처한 `VIEWPORT` 회색 배경을 그대로 유지하는지 확인합니다. 종료 시 원래 `SOLID`·`COMBINED` 상태와 배경 이미지 표시도 복원되어야 합니다.

## Projection Paint Blender 기본 Paint 도구 호환 회귀

```powershell
python -m unittest tests.projection_paint.test_architecture -v
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_paint_tool_bridge.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_stencil_resources.py"
```

정적 회귀는 Clone 전용 RNA 접근 제거, 단일 `use_clone_layer=False`, Tool ID 쓰기 금지와 Brush 자산 활성화 단일 진입점을 검사합니다. Background 회귀는 동일 Tool ID 무간섭, 호환 사용자 Brush 보존, Clone Edge와 프로젝트 전용 Draw Brush 생성·재사용을 검사합니다. Projection Paint 작업공간 복귀에서는 Paint Tool Edge Timer와 Camera View Timer가 모두 재등록되고, Clone이 이미 활성인 기준선에서도 같은 활성 Layer 모드가 확정되어야 합니다.

## Projection Paint Composite 직접 편집 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_composite_layer.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_layer_shader.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_layer_ui.py"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-use-system-env --python ".\tests\projection_paint\blender_background\test_sessionless_state.py"

$env:COLMAP_TOOLS_INSTALLED_MODULE="bl_ext.user_default.colmap_tools"
$env:COLMAP_TOOLS_CLONE_GUI_RESULT="$PWD\build\composite_clone_gui_0.3.18.json"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --enable-event-simulate --python-use-system-env --python ".\tests\projection_paint\gui_composite_clone_actual.py"
```

Background 회귀는 Composite Image가 실제 Paint Target인 계약, 독립 NORMAL Layer 보존, 명시한 재합성 Source 소비와 Release의 삭제 전 Rollback·삭제 후 정리 경고를 확인합니다. 정적 회귀는 Composite 직접 편집 자동 이전 식별자와 구형 Clone Snapshot·파생 Layer 저장 필드가 제품 코드에 다시 들어오지 않는지 확인합니다. GUI 회귀는 설치본의 실제 Blender Essentials Clone 도구로 `Ctrl+LMB` Source 지정, Source·Destination Pixel 복제와 Blender Undo Hash 원복을 확인하며 애드온의 `clone_offset` 직접 쓰기와 가짜 Brush 생성을 허용하지 않습니다.

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

## Align 진입 Object Mode 자동 전환 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\align\blender\test_object_mode_entry.py"

$process = Start-Process `
  -FilePath "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" `
  -ArgumentList @(
    "--factory-startup",
    "--python-use-system-env",
    "--python",
    ".\tests\align\blender\test_object_mode_workspace_gui.py"
  ) `
  -WindowStyle Hidden `
  -PassThru
$process.WaitForExit()
```

첫 검사는 Edit·Sculpt·Texture Paint에서 Blender 기본 Object Mode 전환이 완료되는지 확인합니다. 두 번째 검사는 Edit Mode 상태로 실제 Align 진입 Operator를 실행해 기존 모드 전환이 끝난 다음에만 Align 작업공간과 세션이 활성화되는지 확인합니다.

## Modifier 고정 COLMAP Fixture GUI 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup `
  --enable-event-simulate `
  --python-use-system-env `
  --python ".\tests\modifier\blender_gui\probe_modifier_workspace_workflow.py"
```

단일 Camera·Image·Point3D TXT Fixture를 실제 Import하고 Frame All, Camera Review, Point Edit, Modifier 전용 `Ctrl+Z`·`Ctrl+Shift+Z`, BIN Export와 Workspace 재진입을 검증합니다. 이 입력이 Blender Global Undo를 호출하지 않고 독립 Runtime Journal만 변경하는지 확인합니다. Blender의 합성 입력 허용 옵션으로 Modifier Camera View에 실제 F7 PRESS·RELEASE 이벤트를 보내 관리 Background Image가 OFF → ON으로 전환되는지도 확인합니다. 종료 뒤에는 Modifier 관리 Object 데이터 블록이 삭제되지 않은 채 일반 View Layer에서 모두 제외되고, 재진입하면 Modifier 전용 View Layer가 활성화되어 관리 Collection이 다시 포함되는지 확인합니다. Pending Camera의 개별 숨김 상태와 Pending Undo도 그대로 유지되어야 합니다.

## Modifier 독립 Runtime·전용 Undo/Redo 회귀

```powershell
python -m unittest discover -s ".\tests\modifier\core" -p "test_*.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_point_selection_background.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_undo_domain_reconcile_background.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_camera_review_load_rehydrate_background.py"
```

Core 검사는 변경 범위 Delta, Undo·Redo 분기, Reset 되돌리기, Domain 저장 Codec과 Active/Staging Text 원자 저장을 확인합니다. Blender 검사는 Global Undo 뒤 Runtime 객체와 Journal이 보존되는지, 저장·재실행 때 Source·Operation으로 Undo Command가 다시 구성되고 Redo가 비워지는지, Projection Painting Snapshot이 Domain 복합 Identity로만 수동 갱신되는지 확인합니다. `0.3.33` 이하에서 저장한 Modifier 데이터는 자동 이전하지 않으므로 새 버전에서 한 번 재Import한 뒤 `.blend`를 저장해야 합니다.

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

Projection Paint의 F7 배경 토글은 현재 Window가 해당 Scene의 Projection Paint Workspace를 표시하고 PP 독립 Snapshot이 `READY`일 때만 허용되어야 합니다. Modifier 또는 일반 Blender Workspace의 Camera View에서는 Projection Paint Operator가 입력을 소비하지 않아야 하며, Modifier F7은 기존 Modifier 세션·Workspace·Camera Review 조건을 독립적으로 검사합니다.

표시 파라미터 회귀는 Modifier 원본 파일이 Import 이후 변경된 경우에도 Workspace가 `ACTIVE`로 완료되고 Point·Camera 표시 설정을 사용할 수 있는지 확인합니다. Projection Paint는 다른 Workspace에서 패널이 노출되지 않는지, 재진입 시 PP 전용 View Layer에서 PP Snapshot Root만 표시되는지, Point Scale·Frustum·Axis 변경이 Modifier와 PP에서 서로 독립적으로 적용되는지 확인합니다.

## Projection Paint 독립 Snapshot 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_shared_dataset_consumer.py"
```

이 검사는 Modifier Import → 수동 PP 업데이트 → Modifier의 다른 Source Import → PP 불변 → 수동 재업데이트 순서를 실행합니다. PP 전용 Snapshot 소유 ID, Modifier와 독립된 Point Scale, Paint Target·Layer·Texture pixel 보존, Source token 변경 실패 Rollback과 기존 Camera Runtime 복원을 확인합니다.

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_snapshot_undo_isolation.py"
```

이 검사는 비활성 Modifier의 full reconcile 0회와 비활성 Projection Painting의 lazy recovery 기록을 확인합니다.

## Modifier 지연 Runtime·Projection Paint COLD 업데이트 회귀

빠른 Source 복원의 순수 Python 계약은 다음 명령으로 검사합니다.

```powershell
python -m unittest `
  tests.modifier.core.test_runtime_restore_job `
  tests.modifier.core.test_runtime_state_fast_restore -v
```

이 검사는 명시적 Import의 전체 무결성 검사를 유지하면서 동일 Fingerprint Runtime 복원에서는 전체 검사를 호출하지 않는지, Source SHA-256 1회, Parse 전후 파일 Signature, 결과 동등성, History 적용 전 실패와 Scene Token 원자 Commit을 확인합니다.

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_camera_review_load_rehydrate_background.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_shared_dataset_consumer.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_import_progress_background.py"
```

첫 검사는 `.blend` 로드 직후 `ensure_runtime`·`load_reconstruction` 호출과 자동 복원 Timer가 없고, Modifier Workspace를 활성화한 뒤에만 증분 복원과 Review 재연결이 시작되는지 확인합니다. 두 번째 검사는 COLD Modifier Runtime에서 수동 Projection Paint 업데이트가 Snapshot을 생성하고, 취소·실패·Source token 변경 시 기존 READY Snapshot과 Paint 자산·pixel을 보존하는지 확인합니다. 세 번째 검사는 진행창이 Runtime 복원 0%부터 Scene Commit 100%까지 단조 증가하는지 확인합니다.

실데이터 GUI 최종 QC에서는 저장하지 않은 복사본으로 다음 항목을 확인합니다.

1. 파일을 연 직후 일반 Blender·Projection Paint Workspace가 즉시 조작 가능하고 Modifier Runtime 복원 메시지가 없어야 합니다.
2. 기존 READY Snapshot으로 Painting Mode에 진입할 때 Modifier 원본을 다시 읽지 않아야 합니다.
3. `COLMAP 데이터 업데이트`를 누르면 진행창이 먼저 나타난 뒤 Runtime 복원·전송값·Scene Staging 순으로 진행해야 합니다.
4. 업데이트 중 `Esc`를 누르면 기존 Snapshot, Paint Target, Layer와 Texture pixel이 바뀌지 않아야 합니다.
5. Modifier Workspace 진입 중에도 N 패널은 유지되고 Edit·Review·Export는 Runtime 준비 완료 전까지 비활성 상태여야 합니다.

실제 설치본 COLD 복원과 Source 구간 비교는 다음 전용 Probe로 검사합니다. 환경 변수의 경로는 검사할 저장 프로젝트와 COLMAP 원본으로 지정합니다.

```powershell
$env:COLMAP_TOOLS_INSTALLED_MODULE = "bl_ext.user_default.colmap_tools"
$env:COLMAP_TOOLS_FAST_RESTORE_RESULT = "$PWD\build\modifier_fast_restore_actual.json"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" `
  "C:\path\to\project.blend" `
  --python-use-system-env `
  --python ".\tests\modifier\blender_gui\probe_fast_runtime_restore_actual.py"

$env:COLMAP_TOOLS_PROJECT_ROOT = "$PWD"
$env:COLMAP_TOOLS_SOURCE_BENCHMARK_RESULT = "$PWD\build\modifier_source_restore_benchmark.json"
$env:COLMAP_TOOLS_DATASET_ROOT = "C:\path\to\dataset"
$env:COLMAP_TOOLS_MODEL_DIRECTORY = "C:\path\to\dataset\sparse\0"
$env:COLMAP_TOOLS_IMAGES_DIRECTORY = "C:\path\to\dataset\images"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" `
  --background --factory-startup --python-use-system-env `
  --python ".\tests\modifier\blender_gui\benchmark_runtime_source_restore_actual.py"
```

첫 결과는 설치 버전, COLD Runtime 단계 시간, `Reconstruction 무결성 검사` 문구 부재, Camera·Image·Point3D 수와 편집 Revision을 기록합니다. 두 번째 결과는 같은 Source에서 전체 Import와 빠른 복원의 시간과 Reconstruction 수량 동등성을 기록합니다. Probe는 원본 `.blend`를 저장하지 않고 종료합니다.

## Projection Paint 레이어 선택 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_layer_ui.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_lifecycle_registry.py"
```

첫 검사는 모든 레이어 트리 행의 일반·`Ctrl`·`Shift` 선택, 첫 클릭의 선택 전용 처리, 같은 항목 두 번째 클릭의 Paint Image 활성화, 활성 기호, 선택 기반 단일·복수 삭제와 복수 선택 스트로크 차단 조건을 확인합니다. 두 번째 검사는 `Del` 선택 삭제와 `Image Paint` 입력 차단 키맵이 정확히 한 번씩 등록되고 Extension 해제 시 모두 제거되며, Load·Undo·Redo에서 더블클릭 후보가 폐기되는지 확인합니다.

실제 GUI 최종 QC는 설치본 0.3.23을 사용해 `tests/projection_paint/gui_layer_selection_actual.py`에 현재 창의 Layers 패널과 행 버튼 좌표를 환경 변수로 전달한 뒤 실행합니다. 한 번 클릭은 선택 배경만 바꾸고, 같은 행의 두 번째 클릭에서만 활성 기호와 Paint Target이 이동해야 합니다. 레이어 트리 위 `Del`은 선택 항목을 삭제하고 3D Viewport·Outliner·다른 작업공간의 `Del`은 Blender 기본 입력으로 남아야 합니다.

## Projection Paint 레이어 개인 단독 보기·마스크 단독 보기 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_layer_shader.py"
```

이 검사는 Layer 개인 단독 보기에서 검정 Base 위에 선택 Layer의 현재 Color·Adjustment·Adjustment Mask·Layer Mask·Image Alpha만 적용되고 위·아래 다른 Layer와 Original이 제외되는지 확인합니다. Layer 개인 보기, Layer Mask 흑백 보기와 Adjustment Mask 흑백 보기는 하나의 대체 출력으로 배타 전환되며, 활성 버튼을 다시 누르면 전체 최상단 Layer 합성으로 복귀해야 합니다.

## Projection Paint Blender 권위 Stencil·저장 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_stencil_resources.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_sessionless_state.py"
```

첫 검사는 명시적 Reconnect가 현재 Draw 설정을 복제한 프로젝트 전용 Brush를 한 번 만들고 이후 재사용하며, Camera Texture 원색 투영을 위해 전용 Brush 색상만 흰색으로 맞추고 실패 시 호출 전 색상을 복구하는지 확인합니다. 두 번째 검사는 Projection Paint가 관리하는 Material·Overlay Pointer와 프로젝트 전용 Draw Brush가 `.blend` 저장·재열기 뒤 유지되는지 확인합니다. Camera View 안의 이동·확대 중에는 위치·크기를 현재 Camera Frame에 다시 맞추며 Tool 전환·저장·로드 경계에서는 장기 Brush Snapshot 복구를 실행하지 않습니다.

## Projection Paint Mask·Camera Stencil 연결 제한 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_paint_runtime_mask_stencil.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_stencil_resources.py"
```

첫 검사는 Mask 선택 즉시 Stencil 연결이 해제되고 Camera·Workspace·Frame Callback과 명시적 Reconnect가 다시 연결하지 않는지 확인합니다. 일반 Layer에서는 Camera View 이동·확대가 같은 Draw Brush의 Stencil 위치·크기만 갱신하고, Camera View 이탈은 Frame Source 최종 대기 중에도 Texture 연결을 먼저 해제해야 합니다. Camera View 복귀는 다른 도구를 선택하지 않고 같은 Draw Brush만 복구하며, 일반 Layer 복귀와 같은 작업공간의 일시적인 활성 Target 전환 뒤에도 Camera View Timer가 살아 있어야 합니다. 두 번째 검사는 일반 Layer의 Stencil 연결에 유효한 Scene Camera가 필요하고 Source·Brush 영구 상태는 유지되는지 확인합니다.

Stencil 연결 상태는 최상단 `Status`의 기존 한 행에서 상태 코드와 짧은 원인을 함께 표시합니다. `Paint Setup` 아래에는 같은 상태와 긴 안내 행을 반복하지 않아야 하며, `BRUSH_CHANGED`는 `Stencil: BRUSH_CHANGED · 다른 Brush 활성` 한 행으로 표시되어 전체 Status 행 수를 늘리지 않아야 합니다.

## Projection Paint Stencil 수동 재연결·도구 독립 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_stencil_resources.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_paint_runtime_mask_stencil.py"
```

첫 검사는 Paint 진입·Camera 복귀·Layer 복귀·Undo·Redo·Load가 임의 Brush를 만들지 않고, 최초 `Reconnect Stencil Texture`만 프로젝트 전용 Draw Brush를 생성한 뒤 같은 Brush를 재사용하는지 확인합니다. Blender 5.2의 `builtin.brush` 범용 도구와 실제 `DRAW` Brush 조합은 허용하고, Clone·Smear·Blur·Fill·Mask 도구 또는 종류가 맞지 않는 Brush는 상태 변경 없이 거부합니다. 두 번째 검사는 최근접 Camera Reconnect의 단계별 Rollback, 실패 시 신규 Brush 제거와 Clone Source·Offset·Material Paint Slot 불변을 확인합니다.

`F6`은 3D View의 `projection_paint.reconnect_stencil`에 정확히 한 번 등록되어야 합니다. Projection Paint 작업공간의 실제 활성 Paint Runtime에서만 실행 가능하며, Mask 편집·준비되지 않은 Snapshot·다른 작업공간에서는 Poll이 실패해 Blender의 다른 F6 입력을 소비하지 않아야 합니다.

저장된 실데이터 GUI 검증은 원본 파일을 저장하지 않고 다음 명령으로 실행합니다.

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --python-use-system-env `
  "C:\Users\gksru\Desktop\Untitled.blend" `
  --python ".\tests\projection_paint\gui_stencil_tool_independence.py"
```

이 검사는 `Brush 도구 + Draw Brush`에서 명시적으로 Stencil을 연결한 뒤 Camera 이동·확대 추적, Camera View 이탈 해제, Camera View 복귀 연결과 Camera Texture 실제 Stroke를 검증합니다. 전용 Draw Brush는 흰색이어야 하며 실제 Layer Pixel이 검은색이 아닌 Source 색으로 바뀌어야 합니다. 이어서 `Clone 도구 + Clone Brush`를 포함한 Blender 기본 도구를 전환해 Tool·Brush가 각 도구와 일치하고, 같은 Draw Brush로 돌아오면 Stencil 연결을 그대로 이어가는지 확인합니다.

같은 GUI 검사는 Timeline Camera Frame을 20회 빠르게 변경한 뒤 0.30초 전에는 기존 Source Image가 유지되고, 마지막 입력 이후 0.30초가 지나면 최종 Frame의 Camera·Marker·IMAGE_ID·Stencil Source만 연결되는지도 확인합니다. 정상 Frame 이동은 Camera Guard 복구 횟수를 증가시키지 않아야 하며 Tool·Brush·Paint Target·활성 Layer는 전후 동일해야 합니다. `--enable-event-simulate` 옵션을 사용하면 Blender 기본 키맵 환경에서도 Projection Paint의 `Alt+우클릭` 드래그가 실제 Camera View 확대값을 바꾸고 Tool·Brush를 유지하는지 추가로 검증합니다.

## Projection Paint Blender 도구·브러시 활성화 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_paint_tool_bridge.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" `
  "C:\Users\gksru\Desktop\Untitled.blend" `
  --python-use-system-env `
  --python ".\tests\projection_paint\gui_stencil_tool_independence.py"
```

Background 검사는 여섯 Blender 도구의 Brush 종류·Essentials 자산 경로, 비지원 도구 무변경과 최초 Reconnect 이외의 Brush 생성·복제 금지를 확인합니다. GUI 검사는 Essentials Draw에서 프로젝트 전용 Local Brush를 정확히 하나 생성한 뒤 Camera 이동·확대·이탈·복귀와 실제 Camera Texture Stroke를 먼저 검증합니다. Layout 작업공간을 왕복한 뒤 저장 파일의 `use_clone_layer=True` 상태를 의도적으로 재현하고, Clone Tool 전환이 이를 끄는지도 확인합니다. 이후 툴바만 전환해 실제 `Blur`, `Smear`, `Clone`, `Fill`, `Mask` 자산이 활성화되는지 확인하며 파일을 저장하지 않고 Blender를 종료합니다. Draw 복귀는 이전에 연결한 동일 프로젝트 전용 `DRAW` Brush와 Camera Stencil 상태를 유지해야 합니다.

## Projection Paint Original 외부 직접 참조 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_original_layer.py"
```

이 검사는 외부 Source와 실제 Original Layer가 같은 Image를 참조하고, 원본이 Pack·소유 태그 변경 없이 유지되며, 읽기 전용 선택과 Mask 페인팅이 분리되는지 확인합니다. 기존 Schema 1 복제 Original의 자동 이관, 원본 파일 누락 시 비파괴 중단, `.blend` 저장·재열기 뒤 외부 픽셀 복원도 함께 검증합니다.

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

## Modifier·Projection Painting Camera 세션 격리 회귀

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\projection_paint\blender_background\test_camera_review_background.py"

& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup `
  --python-use-system-env `
  --python ".\tests\modifier\blender_background\test_camera_review_background.py"
```

실제 Window가 필요한 교대 검사는 사용자 화면을 가리지 않도록 숨김 창에서 실행합니다.

```powershell
$process = Start-Process `
  -FilePath "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" `
  -ArgumentList @(
    "--factory-startup",
    "--python-use-system-env",
    "--python",
    ".\tests\blender_gui\test_camera_marker_handoff.py"
  ) `
  -WindowStyle Hidden `
  -PassThru
$process.WaitForExit()
```

검사는 작업공간 열기 직후 Camera 세션이 아직 비활성인지, 화면 구성이 `READY`가 된 다음 UI Tick에서만 활성화되는지 확인합니다. 이어 Modifier → Projection Painting → Modifier → 일반 Workspace를 이동하며 비활성 도구의 Camera·Background·Frame Timer가 현재 도구를 덮어쓰지 않고 양쪽 Object와 사용자 Marker가 보존되는지 검증합니다.

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
