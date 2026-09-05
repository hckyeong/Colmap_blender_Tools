# 통합 구현 현황

## 0.3.36 Align 진입 Object Mode 자동 전환

- Align 진입 시 Edit·Sculpt·Texture Paint를 포함한 비 Object Mode를 Blender 기본 Object Mode로 자동 전환합니다.
- 다른 COLMAP 도구에서 넘어오면 기존 세션 종료를 완료한 뒤 Align 작업공간 생성 직전에 전환합니다.
- 현재 Window·Screen·View Layer·활성 Object를 검증하고 실패 시 부분 Align 세션을 만들지 않습니다.

Blender Background에서 Edit·Sculpt·Texture Paint 전환을 통과했고, Source와 실제 설치본의 숨김 GUI에서 Edit Mode → 실제 Align 작업공간 → 활성 세션 진입을 검증했습니다. Production 전체 검사와 격리 설치본 검사를 통과했고 실제 Blender `user_default` 설치 버전 `0.3.36`도 확인했습니다.

Production ZIP은 `build/colmap_tools-0.3.36.zip`이며 SHA-256은 `22F4CC3907759029181DE52A5A26D92B1A19D863A8DA8D5849704C5082C45542`, 크기는 28,731,889바이트입니다.

## 0.3.35 Modifier·Projection Painting Camera 세션 격리

- Modifier와 Projection Painting의 Camera 쓰기 권한을 현재 Window·Workspace·Scene Marker 사용권으로 제한했습니다.
- 비활성 도구의 Frame Handler, Debounce와 Repair Timer는 `scene.camera`, Render 해상도와 Background Image를 변경하지 않습니다.
- 작업공간 생성과 Camera 연결을 분리해 Workspace와 화면 구성이 `READY`가 된 다음 UI Tick에서만 Camera 세션을 연결합니다.
- 작업공간 준비 실패 시 Camera·Marker·View Layer 권한을 모두 반납하고 실패 Workspace를 정리해 삭제된 Workspace 참조를 다시 사용하지 않습니다.
- Modifier Camera 권한 검사를 Timeline과 Viewport의 공통 독립 경계로 분리해 제품 모듈의 순환 의존성을 제거했습니다.

Modifier Core 216개, Architecture 12개, Coexistence 11개, Projection Core 40개를 통과했습니다. Projection Painting Camera Background 19개와 Modifier Camera Background 전체를 통과했고, Source와 실제 설치본의 숨김 Blender GUI에서 Modifier → Projection Painting → Modifier → 일반 Workspace Camera·Marker 교대를 검증했습니다. 격리 설치본 검사와 실제 Blender `user_default` 설치 버전 `0.3.35` 확인도 완료했습니다.

Production ZIP은 `build/colmap_tools-0.3.35.zip`이며 SHA-256은 `60AB1B837F680D8999DF6C485A0794FEDADC80C336F196E5E95884A9069C1D58`, 크기는 28,731,259바이트입니다.

## 0.3.34 Modifier 독립 Runtime·전용 Undo/Redo

- Modifier의 현재 Reconstruction과 확정 편집 이력을 독립 Python Runtime Domain의 권위로 전환했습니다.
- 변경 범위 Forward/Inverse Delta를 가진 전용 Journal이 Point·Camera 제거, Pending Filter 적용, Reset의 Undo·Redo를 관리합니다.
- Modifier Workspace의 `Ctrl+Z`, `Ctrl+Shift+Z`, `Ctrl+Y`만 전용 Journal로 라우팅하고 빈 Journal과 `Ctrl+Alt+Z`도 Blender Global Undo로 넘기지 않습니다.
- 다른 Workspace의 Global Undo는 Domain·Journal을 보존하고 이전 Blender 참조만 폐기한 뒤 Runtime 기준으로 Point·Camera·Marker 표시를 다시 동기화합니다.
- Import는 Loader 결과를 Domain에 직접 설치하고 Export 성공은 새 Baseline과 빈 Journal로 전환합니다.
- Source Metadata, Domain Identity와 확정 Operation만 Active/Staging Text에 원자 저장하며, 재실행 때 Source·Operation으로 Runtime과 Undo Command를 자동 재구축합니다. Redo Stack은 비워집니다.
- Projection Painting 수동 업데이트는 Domain UUID·Generation·Revision·History Hash가 포함된 불변 Snapshot을 복사하며 Modifier Runtime·Journal을 공유하지 않습니다.
- 자동 마이그레이션은 제공하지 않습니다. `0.3.33` 이하에서 저장한 기존 Modifier 데이터는 한 번 재Import한 뒤 `.blend`를 다시 저장해야 합니다.

Modifier Core 216개, Architecture 12개, Coexistence 11개, Projection Core 40개와 Shared Transfer 8개를 통과했습니다. Modifier Blender Background 전체와 실제 GUI `Ctrl+Z → Ctrl+Shift+Z → Ctrl+Z`, F7, BIN Export, Workspace 재진입을 Source와 설치본 양쪽에서 검증했습니다. 격리 설치본 검사와 실제 Blender `user_default` 설치 버전 `0.3.34` 확인도 완료했습니다.

Production ZIP은 `build/colmap_tools-0.3.34.zip`이며 SHA-256은 `AE85BE8034C49567180B79462E7FC157CD8CCDDDE4931ABD8F1E6A80C925DAC7`, 크기는 28,725,664바이트입니다.

## 0.3.33 Modifier 빠른 Runtime 복원

- 신규·수동 Import의 전체 무결성 검사는 유지하고, 저장된 동일 Source의 COLD Runtime 복원만 전용 `RuntimeSourceRestoreJob`으로 분리했습니다.
- COLD 복원은 전체 SHA-256을 한 번만 계산하고 저장 Fingerprint와 비교합니다. Parse 완료 뒤에는 파일 Signature를 비교해 두 번째 전체 Hash 없이 읽기 중 원본 변경을 차단합니다.
- 동일 Source 복원에서는 `ReconstructionValidationJob`을 반복하지 않으며 Image Manifest, 관계 인덱스, 편집 History, Point3D 순서와 원자 Commit은 유지합니다.
- 저장 Schema·Fingerprint가 없거나 다르면 자동 이전하지 않고 기존 편집 기록을 적용하지 않은 채 명시적 재Import를 안내합니다.
- Projection Painting의 수동 업데이트도 같은 COLD Runtime 복원 경로를 사용하고 성공 전 기존 Snapshot을 유지합니다.

Core 빠른 복원 신규 검사 12개, Modifier Core 전체 169개, Architecture 12개와 Coexistence 11개를 통과했습니다. Modifier와 Projection Painting의 관련 Blender Background 회귀도 통과했습니다. 동일 실데이터 Source에서는 전체 Import 17.530807초, 빠른 복원 9.553817초로 45.503% 단축됐고 Camera 593, Image 593, Point3D 709,048, Rig 593, Frame 593 결과가 일치했습니다.

사용자 `Untitled.blend`를 실제 설치본으로 저장하지 않고 열어 COLD 복원을 검사했습니다. `Reconstruction 무결성 검사` 문구는 나타나지 않았고 편집 Revision 1·Operation 1개가 적용된 Camera 593, Image 593, Point3D 708,541 Runtime이 복원됐습니다. 전체 121.573661초 중 Source 확인·읽기 33.138628초, 관계 인덱스 구축 78.409795초, History 0.051201초, Point3D 순서 9.236016초였습니다. 다음 성능 병목은 관계 인덱스 재구축으로 확인했습니다.

Production ZIP은 `build/colmap_tools-0.3.33.zip`이며 SHA-256은 `F0784206037EDCAD91078FDD3D7F441468EA241B406CE02322494F81183212FE`, 크기는 28,689,146바이트입니다. Blender `user_default` 재설치와 격리 설치본 검사를 통과했습니다.

## 0.3.32 Projection Paint 진행 안내창 문자열 경계

- Native Bake 안내의 Device 정보와 Image·버퍼·Bake·Samples·경과 정보를 두 줄로 분리했습니다.
- 공통 진행 안내창의 제목·단계·상세 문구를 BLF 실제 픽셀 폭으로 측정하고, 창 내부 폭을 넘는 경우에만 말줄임표로 축약합니다.
- Texture Export뿐 아니라 같은 공통 Renderer를 사용하는 Composite·Painting 진입·Camera Import·Dataset 업데이트 안내도 화면 경계를 넘지 않습니다.
- 진행창 크기, 투명도, 최대 상세 줄 수와 Native Bake 상태 머신은 변경하지 않았습니다.

Texture Export Background 34개와 Composite 61개 회귀를 통과했습니다. 실제 설치본과 사용자 `Untitled.blend`에서 8192×8192, NVIDIA GeForce RTX 4090 Device 문구로 Screenshot을 촬영했습니다. 사용 가능 내부 폭 476픽셀에서 가장 긴 상세 문구는 381픽셀이었고 모든 문자열이 안내창 안에 표시됐습니다. 원본 `.blend`는 저장하지 않았습니다.

Production ZIP은 `build/colmap_tools-0.3.32.zip`이며 SHA-256은 `B64BC89951D83D94E7325C9800CC6687EFD4B2E6069893B5C0FF2233D8F41E65`, 크기는 28,683,726바이트입니다. Blender `user_default` 재설치와 격리 설치본 검사를 통과했습니다.

## 0.3.31 Projection Paint Texture Export Native Bake·진행률 UI

- GUI Texture Export의 Color·Alpha Bake를 동기 `EXEC_DEFAULT`에서 Blender Native `INVOKE_DEFAULT` `OBJECT_BAKE` Job으로 전환했습니다.
- 독립 `TextureExportJob`이 Color → Alpha → 결합 → 기록 → Staging → Commit과 성공·취소·실패 정리를 관리합니다.
- Bake 중에는 Composite 생성과 같은 공통 `Process.` 활동형 진행창에 단계, 장치, 해상도, EMIT, Samples 1과 경과 시간을 표시합니다.
- Export 중 일반 입력 차단은 유지하고 `Esc`만 Native Bake 취소로 전달합니다. Bake Job 종료 전에는 임시 Node·Image와 렌더 상태를 정리하지 않습니다.
- 기존 가시 Layer Graph, 동기 Background Export, 파일 Staging·Backup·원자 Commit·Rollback과 Pixel Hash 미사용 계약을 유지했습니다.
- Blender 종료가 Modal Operator보다 먼저 진행된 경우에도 제거된 Operator 참조를 안전하게 폐기합니다.

Texture Export Background 33개, Composite 61개, Lifecycle 14개와 SINGLE Composite Export 4개를 통과했습니다. Production 검사 220개, Extension 등록·해제, Small GICP 내장 Runtime 합성과 격리 설치본 검사도 통과했습니다.

실제 `user_default` 설치본과 사용자 `Untitled.blend`의 8192×8192 SINGLE 데이터로 GUI 회귀를 실행했습니다. 성공 Export는 Color Bake 4.46초, Alpha Bake 19.26초, Combining 33.53초, Writing 34.22초, Staging 38.24초, Committing 38.31초 순서로 관측됐고 38.41초에 78,810,870바이트 PNG를 생성했습니다. Native Bake 중 Timer 351회와 실제 `OBJECT_BAKE` 실행 349회를 관측했으며 Layer·Paint 상태 불변과 임시 Image 제거를 확인했습니다. Color `Esc` 취소는 10.37초, Alpha `Esc` 취소는 24.88초에 완료됐고 두 경우 모두 출력 파일 없이 상태와 임시 자원을 복구했습니다. 원본 `.blend`는 저장하지 않았습니다.

Production ZIP은 `build/colmap_tools-0.3.31.zip`이며 SHA-256은 `A5E4E9AF34EB89E19F921D758BCC3C7C7162C1DCFD63250658516E2B990F30A3`, 크기는 28,683,362바이트입니다. 실제 Blender `user_default`에 0.3.31을 재설치하고 별도 프로세스에서 설치 버전을 확인했습니다.

## 0.3.30 Projection Paint Texture Export 입력 차단·Pixel Hash 제거

- 파일 선택을 마친 Texture Export는 `BLOCKING` Modal로 실행됩니다.
- `Esc` 취소를 제외한 마우스·키보드 입력을 Export가 끝날 때까지 소비합니다.
- 시작·확정 경계에서 모든 Image Pixel을 복사해 SHA-256으로 검사하던 Export Snapshot을 제거했습니다.
- 임시 파일 Staging, 기존 출력 Backup, 전체 묶음 원자 확정과 실패 Rollback은 유지했습니다.

Texture Export Blender Background 회귀 24개와 SINGLE Composite Export 회귀 4개, Production 검사 220개를 통과했습니다. `build/colmap_tools-0.3.30.zip`을 실제 Blender `user_default`에 설치하고 버전 0.3.30, `BLOCKING` Operator와 Pixel Hash 코드 부재를 확인했습니다.

사용자 `Untitled.blend`의 8192×8192 SINGLE `Layer 1`과 `Original Layer · Texture`를 설치본으로 다시 내보냈습니다. 전체 시간은 60.03초에서 36.74초로 23.28초 단축됐으며 초기 준비는 11.71초에서 0.0003초로 줄었습니다. 검사용 78,810,870바이트 PNG는 확인 뒤 삭제했고 원본 `.blend`는 저장하지 않았습니다. ZIP SHA-256은 `C53DA7763D88C12DD71A450531EE4E0733C0D3C0433D2C08F9643516BB6ED786`, 크기는 28,677,794바이트입니다.

## 0.3.29 Projection Paint Texture Export 덮어쓰기 권한 통합

- Blender 파일 브라우저의 숨겨진 `check_existing` 확인을 비활성화했습니다.
- UI에 표시되는 `Overwrite Existing`만 기존 출력 교체 권한으로 사용합니다.
- 옵션이 꺼져 있으면 기존과 같이 파일 생성 전에 중단하고, 켜져 있으면 기존 SINGLE·UDIM 파일 묶음을 Backup한 뒤 새 묶음을 원자 확정합니다.
- 교체 도중 실패하면 이미 확정한 새 파일을 제거하고 이전 파일 묶음을 복원합니다.

Texture Export Blender Background 회귀 24개와 Production 검사 220개를 통과했습니다. Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사도 완료했습니다. `build/colmap_tools-0.3.29.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.29, `check_existing=False`·숨김과 `Overwrite Existing=False`·표시 상태를 확인했습니다. ZIP SHA-256은 `608BACE711D89CEAD4BCA3D3314750F5D8A7DAA1D0E66C36AC7F56E046C03BF7`, 크기는 28,679,508바이트입니다.

## 0.3.28 Projection Paint 가시 Layer Texture Export

- 영구 Composite Layer가 없어도 준비된 Paint Layer Graph에서 `Export Texture`를 실행할 수 있습니다.
- 실행 시점의 실제 Base Color Graph에서 눈이 켜진 Layer만 임시 RGBA Image로 합성합니다.
- 기존 활성 Composite는 Graph 입력 하나로 사용하고 잠긴 Source 자식은 다시 합성하지 않습니다.
- Layer·Mask 단독 보기는 화면 확인 상태로만 유지하며 Export Source를 바꾸지 않습니다.
- 가시 Layer가 없으면 파일 탐색기를 열기 전에 구체적인 오류를 표시합니다.
- 성공·취소·실패 뒤 Layer 눈, 선택·활성 대상, 단독 보기, Composite 구조와 Paint 상태를 변경하지 않습니다.

Texture Export 23개, SINGLE Composite Export 4개, Layer Shader 47개와 Composite 61개 등 대상 Blender 회귀 135개를 통과했습니다. Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사도 통과했습니다. `build/colmap_tools-0.3.28.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.28을 확인했습니다. 사용자 `Untitled.blend`의 8192×8192 SINGLE `Layer 1`과 `Original Layer · Texture`를 Composite 생성 없이 83,821,267바이트 PNG 한 장으로 내보냈고, 58.19초 뒤 Layer Tree·선택·단독 보기·Composite 상태 불변과 임시 출력 정리를 확인했습니다. ZIP SHA-256은 `066768971DB78FFBEB426562276CC48AFDA207DDBF17CD4F61F1807B77D1C3EF`, 크기는 28,679,434바이트입니다.

## 0.3.27 Projection Paint Diffuse Color View

- Painting Mode 진입 시 전용 3D View를 Material Preview의 `Diffuse Color` Render Pass로 전환합니다.
- 진입 전 Combined 화면에서 캡처한 배경 종류와 색상을 그대로 적용해 같은 회색 배경을 유지합니다.
- 사용자 배경 이미지 표시는 기존 계약대로 Painting Mode에서 끄고, 원래 작업공간으로 복귀하면 기존 상태를 복원합니다.
- Painting Mode 안에서 사용자가 Shading 상태를 직접 바꿨다면 종료 시 그 변경을 덮어쓰지 않습니다.

실제 Blender GUI에서 `MATERIAL`·`DIFFUSE_COLOR`·`VIEWPORT`·RGB 0.18 회색 적용과 종료 시 원래 `SOLID`·`COMBINED` 복원을 확인했습니다. Paint 자원 43개와 Paint 작업공간 12개 회귀, Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사도 통과했습니다. `build/colmap_tools-0.3.27.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.27과 Diffuse Color·Combined 배경 승계·복구 코드를 확인했습니다. SHA-256은 `567E73D18BD7AFB5BA2BE14C3744205E8FEFB704329A898EB5E33633FD475BC9`, 크기는 28,677,581바이트입니다.

## 0.3.26 Projection Paint F6 Stencil 재연결

- Projection Paint 3D View에서 `F6`을 `Reconnect Stencil Texture` 단축키로 등록했습니다.
- Add-on Keymap의 선두에 등록해 활성 Projection Paint 작업공간에서 우선 처리합니다.
- 다른 작업공간에서는 Reconnect Operator Poll이 실패하므로 Blender의 기존 F6 입력을 방해하지 않습니다.
- Mask 편집, 비활성 Paint Runtime과 준비되지 않은 Snapshot에서는 기존과 같이 재연결을 차단합니다.

수명주기 14개와 Stencil 자원 25개 회귀를 통과했습니다. Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사도 통과했습니다. `build/colmap_tools-0.3.26.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.26, F6 키맵 1개와 작업공간 제한을 확인했습니다. SHA-256은 `4A7448AB818B8D97DD1F983E79E98300484C771E2D50A4ABCE37C36EC50803AD`, 크기는 28,677,569바이트입니다.

## 0.3.25 Projection Paint Stencil 상태 한 줄 통합

- `Paint Setup` 아래에 반복되던 Stencil 상태와 여러 안내 행을 제거했습니다.
- 최상단 `Status`의 기존 Stencil 한 행에 상태 코드와 짧은 원인을 함께 표시합니다.
- `BRUSH_CHANGED`는 `Stencil: BRUSH_CHANGED · 다른 Brush 활성`로 표시하며 Status 행 수는 늘어나지 않습니다.
- Stencil 연결 판정과 상세 원인은 기존 Runtime 상태에 그대로 유지하고 UI 표현만 압축했습니다.

Layer UI 42개와 수명주기 14개 회귀를 통과했습니다. Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사도 통과했습니다. `build/colmap_tools-0.3.25.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.25, 한 줄 Status 문구와 Paint Setup 중복 제거를 확인했습니다. SHA-256은 `F7DAF389289ED85E66F98B65E97A6F02895E71291C32E90933779E8E89C94D50`, 크기는 28,677,516바이트입니다.

## 0.3.24 Projection Paint Camera View 이탈 Stencil 보장

- Camera View 이탈 검사를 Frame Source 최종 대기보다 먼저 실행해 빠른 Frame 이동 중에도 Stencil Texture 연결을 즉시 해제합니다.
- Camera View 밖에서는 View Token 변경 여부와 무관하게 해제 상태를 다시 확정합니다.
- 같은 Projection Paint 작업공간에서 활성 Target이 일시적으로 바뀌거나 Mask·수동 해제 상태가 되어도 Camera View Timer를 저빈도로 유지합니다.
- 일반 Layer를 선택하면 Stencil을 자동 연결하지 않고 Camera View 추적만 다시 등록합니다.
- Source Image, Paint Target, 활성 Layer와 Brush의 비Stencil 설정은 변경하지 않습니다.

Paint Runtime Mask·Stencil 26개, Camera Review 16개, Layer Shader 47개, Paint Tool Bridge 8개와 Stencil 자원 회귀를 통과했습니다. Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사도 통과했습니다. `build/colmap_tools-0.3.24.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.24와 설치 경로를 확인했습니다. SHA-256은 `73D4C9F9A7F2002074885ECDB40AC9E3514E09AC1EAB98CEEC9E52309D7ABFDA`, 크기는 28,677,329바이트입니다.

## 0.3.23 Projection Paint 레이어 선택·활성화·삭제 UX

- Layer·Mask·Adjustment 행의 한 번 클릭은 UI 선택만 바꾸고, 같은 항목의 두 번째 수정키 없는 클릭에서 실제 Paint Target을 활성화합니다.
- 파란 배경은 선택 상태만 나타내며 실제 활성 편집 대상은 별도 `RADIOBUT_ON` 기호로 표시합니다.
- `Ctrl`·`Shift` 선택과 트리 외부 클릭 해제는 실제 Paint Target을 변경하지 않습니다.
- `-` 버튼과 레이어 트리 위 `Del`은 선택된 항목을 UUID 기준으로 삭제하며 부모·자식 중복을 정규화합니다.
- 보호 대상이 섞인 복수 삭제는 실제 변경 전에 전체 중단하고, 성공한 복수 삭제의 Shader Graph는 마지막에 한 번만 재조정합니다.
- 다른 Editor·3D Viewport·작업공간의 `Del`은 Blender 기본 입력으로 통과합니다.
- 파일 Load·Undo·Redo·작업공간 전환·Extension 해제에서 더블클릭 Runtime 후보를 제거합니다.

Layer UI 41개, 수명주기 14개, Original Layer 16개, Layer Shader 47개, Paint Tool Bridge 8개, Paint Resources 43개와 Paint Runtime Mask·Stencil 회귀를 통과했습니다. Composite 전체 61개와 Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성 및 격리 설치본 검사도 통과했습니다. `build/colmap_tools-0.3.23.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.23과 설치 경로를 확인했습니다. SHA-256은 `4E86574658AD430246BDA61A364C13319454634B413A28E068CC85C62173F9DC`, 크기는 28,677,279바이트입니다. 실제 버튼 좌표를 사용하는 Layer GUI 입력 시뮬레이션은 최종 수동 QC 대상으로 남습니다.

## 0.3.22 Projection Paint Industry Compatible 뷰포트 탐색

- Projection Paint 작업공간에서 `Alt+좌클릭` Orbit, `Alt+가운데 버튼` Pan, `Alt+우클릭` Zoom을 지원합니다.
- 자체 카메라 조작을 구현하지 않고 Blender 기본 `view3d.rotate`, `view3d.move`, `view3d.zoom` 모달 연산자에 입력을 전달합니다.
- 3D Viewport Window Region과 저장된 Projection Paint 작업공간에서만 활성화해 다른 작업공간과 N 패널 입력은 변경하지 않습니다.
- Image Paint Keymap에도 같은 경로를 등록해 Painting Mode의 Paint Stroke보다 Industry Compatible 탐색 입력을 우선합니다.
- 복수 레이어 선택 중 스트로크 차단은 `Alt+좌클릭`을 소비하지 않고 Orbit 입력으로 통과시킵니다.
- Tool, Brush, Stencil Source, Layer와 Undo에는 개입하지 않습니다.

Blender Background 대상 회귀 51개와 Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성, 격리 설치본과 Workspace·Camera Marker GUI 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Blender 기본 키맵 상태에서 `Alt+우클릭` 드래그가 Camera View 확대값을 `-4.243720`에서 `1.280397`로 변경하는 것을 확인했습니다. 같은 Draw Tool·Brush Pointer와 Camera Stencil 연결, Frame Source 지연 처리와 실제 Camera Texture Stroke도 유지됐습니다. `build/colmap_tools-0.3.22.zip`을 Blender `user_default`에 설치했으며 SHA-256은 `9F2174C3199B3C82605061DAFDF87D2AA0F98164CEAACC8A1BB7F4DCCB7CDEE3`, 크기는 28,672,755바이트입니다. 이미 열려 있던 Blender 프로세스는 재시작한 뒤 0.3.22 Runtime을 사용합니다.

## 0.3.21 Camera Frame Source 최종 대기

- Timeline Camera Frame을 빠르게 이동하는 동안 마지막 Frame 요청만 Runtime에 유지합니다.
- 마지막 입력 후 0.30초 동안 추가 변경이 없을 때 최종 Camera와 Source Image를 정확히 한 번 연결합니다.
- 정상 Frame 처리를 Camera Guard 복구 횟수에서 분리해 빠른 이동이 반복 복구 상한 오류를 만들지 않습니다.
- Source 연결 성공 뒤에만 Pending을 제거하고, 실패 시 기존 Source Pointer·Metadata·Render 상태를 복구한 뒤 동일 최신 요청만 최대 2회 재시도합니다.
- 명시적 `Reconnect Stencil Texture`는 대기를 건너뛰고 현재 Frame Source를 즉시 연결합니다.
- Blender Paint Tool, Brush, Clone Source 위치, Paint Target, Layer와 Undo에는 개입하지 않습니다.

Blender Background 대상 회귀 91개와 Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성, 격리 설치본과 Workspace·Camera Marker GUI 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Frame 20회 빠른 이동 뒤 최종 Frame 20·IMAGE_ID 20 연결과 Camera Guard 복구 횟수 0을 확인했습니다. Tool·Brush·Paint Target·활성 Layer는 유지됐고 Camera View 이동·확대·이탈·복귀, Draw → Blur → Smear → Clone → Fill → Mask → Draw 왕복과 실제 Camera Texture Stroke도 통과했습니다. `build/colmap_tools-0.3.21.zip`을 Blender `user_default`에 설치했으며 SHA-256은 `F292251C8AF155104C66C885E3437DF81A3CD86DEE3D08A40A8BA2DD1756E403`, 크기는 28,671,284바이트입니다. 이미 열려 있던 Blender 프로세스는 재시작한 뒤 0.3.21 Runtime을 사용합니다.

## 0.3.20 Clone Source 위치 지정 작업공간 복귀

- Projection Paint 작업공간을 벗어날 때 종료된 Paint Tool Edge Timer를 작업공간 복귀 시 Camera View Timer와 함께 다시 등록합니다.
- 작업공간 복귀 시 Clone Tool이 이미 활성 상태여도 `use_clone_layer=False`를 확정해 활성 Layer Image가 Source이자 Target인 Blender 기본 같은 이미지 Clone 모드를 사용합니다.
- 이후 Draw·Blur·Smear·Clone·Fill·Mask 전환 Edge를 다시 감지하며 Clone 진입 시 저장 파일에 남은 `Clone from Paint Slot` 상태를 끕니다.
- Clone Brush, `clone_offset`, Source 위치와 Stroke는 직접 관리하지 않고 Blender 기본 `Ctrl+LMB`와 Stroke 동작에 맡깁니다.

대상 Blender Background 회귀 30개와 Production 검사 220개, Extension 등록·해제, Small GICP 내장 Runtime 합성과 격리 설치본 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 작업공간 왕복·Painting Mode 재진입 뒤 Clone Tool Edge가 `use_clone_layer=False`를 복구하고 Draw·Blur·Smear·Clone·Fill·Mask 전환과 Camera Stencil이 유지되는 것을 확인했습니다. 독립 GUI 회귀에서는 Blender 기본 `Ctrl+LMB` Source 지정, 실제 Source Pixel 복제와 Blender Undo Hash 원복을 확인했으며 제품 코드의 `clone_offset` 직접 쓰기와 가짜 Clone Brush 생성은 0회였습니다. `build/colmap_tools-0.3.20.zip`을 Blender `user_default`에 설치했으며 SHA-256은 `9A92FAF8D0237941AE77414C0A079E4AA28843E40A580C6788433EAA14A274D0`, 크기는 28,668,678바이트입니다. 이미 열려 있던 Blender 프로세스는 현재 실행 중인 Clone 상태만 안전하게 복구했으며, 재시작 뒤 0.3.20 Runtime을 사용합니다.

## 0.3.19 Camera View Stencil 추적·Camera Texture 원색 투영

- Camera View 안에서 이동·확대·영역 크기·Camera Frame이 바뀌면 연결된 프로젝트 전용 Draw Brush의 Stencil 위치와 크기만 다시 맞춥니다.
- Viewport 회전으로 Camera View를 벗어났을 때만 Brush Texture 연결을 해제하며 Source Image와 Layer Painting Mode는 유지합니다.
- Camera View로 돌아오면 같은 프로젝트 전용 Draw Brush가 활성인 경우에만 기존 Stencil 연결을 복구합니다. Clone·Blur·Smear·Fill·Mask와 사용자 정의 도구를 선택하거나 교체하지 않습니다.
- 프로젝트 전용 Draw Brush의 기본 색상을 흰색으로 고정해 Camera Texture RGB가 검은 Brush 색상과 곱해지지 않고 원색으로 Layer에 기록되게 했습니다.
- Reconnect 실패 Rollback은 전용 Draw Brush 색상도 호출 전 값으로 원자 복구합니다.

대상 Blender Background 회귀 113개와 Production 검사 220개, Extension 등록·해제, Small GICP 내장 Runtime 합성과 격리 설치본 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Camera 이동·확대 추적, Camera View 이탈 해제와 복귀 재연결, Draw → Blur → Smear → Clone → Fill → Mask → Draw 왕복을 확인했습니다. 실제 Camera Texture Stroke는 전용 Draw Brush 색상 `(1, 1, 1)`에서 검정이 아닌 RGBA `(0.294118, 0.243137, 0.164706, 1)`을 활성 Layer에 기록했습니다. `build/colmap_tools-0.3.19.zip`을 Blender `user_default`에 설치했으며 SHA-256은 `69373D4CF3DEAFDA96DD02AD9EB2491F342F853CF9C6D96BBD1A50A987096935`, 크기는 28,668,638바이트입니다. 이미 열려 있던 Blender 프로세스는 재시작 뒤 0.3.19 Runtime을 사용합니다.

## 0.3.18 프로젝트 전용 Stencil Draw Brush

- 최초 수동 Reconnect에서 현재 Draw 설정을 복제한 편집 가능한 프로젝트 전용 Draw Brush를 정확히 하나 생성합니다.
- 이후 Reconnect, Draw Tool 복귀와 `.blend` 저장·재열기는 저장된 동일 Brush를 재사용합니다.
- Clone, Blur, Smear, Fill, Mask는 복제하지 않고 Blender 기본 또는 사용자 Brush를 그대로 사용합니다.
- 최초 Reconnect 실패 시 원래 활성 Brush를 복구하고 해당 호출에서 생성한 전용 Brush만 제거합니다.
- Tool·Workspace·저장·종료 경계의 외부 Brush 임대·장기 복구 상태는 추가하지 않았습니다.

대상 Blender Background 회귀 262개와 Architecture 검사 10개, Production 검사 220개, Extension 등록·해제, Small GICP 내장 Runtime 합성과 격리 설치본 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Essentials `Paint Hard`에서 `Projection Paint Stencil Draw`가 한 번 생성되고 Draw → Blur → Smear → Clone → Fill → Mask → Draw 왕복 뒤 같은 Brush Pointer와 Camera Stencil이 유지되는 것을 확인했습니다. 독립 GUI 회귀에서는 Blender 기본 Clone의 Viewport `Ctrl+LMB` Source 지정, 실제 Pixel 복제와 Undo Hash 원복을 확인했으며 Clone Offset 직접 쓰기와 테스트 Brush 생성은 0회였습니다. `build/colmap_tools-0.3.18.zip`을 실제 Blender `user_default`에 설치했으며 SHA-256은 `BCD532B2BF6CF1FAA5ECA2F0D3A187D722C5A27DBEBA52E06C4FA5AEB4F03DC1`, 크기는 28,668,092바이트입니다.

## 0.3.17 Projection Paint Blender 기본 Paint 도구 호환

- 비파괴 Clone 상태와 Composite Clone 호환 계층을 제거하고 활성 Layer Image를 Blender 기본 Clone의 Source이자 Target으로 사용합니다.
- Tool Bridge는 지원 Paint Tool의 실제 전환 Edge에만 반응하며 동일 Tool ID, 호환 사용자 Brush와 지원 외 Tool에는 개입하지 않습니다.
- Camera Stencil은 명시적 Reconnect 때 현재 편집 가능한 Draw Brush에 직접 연결하며 Tool·Workspace·저장·로드 경계에서 자동 해제하거나 복구하지 않습니다.
- N 패널 Layer 선택과 3D Viewport 입력을 분리하고 복수 선택·Original 읽기 전용 상태에서 실제 Pixel/Mask Stroke만 차단합니다.
- 구형 Stencil 영구 복원 필드와 자동 복원 함수를 제거해 Blender Brush 저장 상태만 권위로 사용합니다.
- 최근접 Camera Reconnect는 Snapshot 이후 Camera·Brush 단계를 검증하고 Overlay 복구를 마지막 Commit으로 실행해 실패 시 전체 상태를 되돌립니다.

대상 Blender Background 회귀 198개와 Production 검사 220개, Extension 등록·해제, Small GICP 내장 Runtime 합성, 격리 설치본 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Draw → Blur → Smear → Clone → Fill → Mask → Draw 왕복 뒤 동일 사용자 Draw Brush와 Camera Stencil 연결이 유지되는 것을 확인했습니다. 독립 GUI 회귀에서는 같은 이미지 Clone의 Viewport `Ctrl+LMB` Source 지정, 실제 Pixel 복제와 Blender Undo Hash 원복을 확인했으며 Clone Offset 직접 쓰기와 가짜 Brush 생성은 0회였습니다. `build/colmap_tools-0.3.17.zip`을 실제 Blender `user_default`에 설치했으며 SHA-256은 `A1F9277B2EBFAF9BC33ED78B1546F5C062DEA3B5C9AB359D58341AE6B7F15201`, 크기는 28,666,594바이트입니다.

## 0.3.16 Projection Paint Composite 자동 이전 제거

- 파일 열기·등록·Undo·Redo·Save·Layer Graph 재구성 경계에서 실행되던 Composite 직접 편집 자동 이전을 제거했습니다.
- Direct Edit Schema, 구형 Clone Snapshot, `COMPOSITE_DERIVED`, Composite 의존 UUID 저장 필드를 제품 코드에서 제거했습니다.
- 구버전 데이터셋을 자동 변경하거나 실행·저장을 차단하지 않습니다. 초기화가 필요하면 코드가 결정하지 않고 검사 결과를 사용자에게 먼저 알린 뒤 명시적 승인으로 진행합니다.
- 정적 회귀에서 자동 이전 식별자가 제품 코드에 다시 추가되지 않는지 검사합니다.

Composite 61개, Layer Shader 48개, Layer UI 37개, 저장·재열기 4개와 Production 220개 검사를 통과했습니다. Small GICP Runtime 합성, 격리 설치본, Workspace·Camera Marker GUI도 통과했습니다. `build/colmap_tools-0.3.16.zip`을 실제 Blender `user_default`에 재설치했으며 SHA-256은 `E49C95C4BFCFCDE4E96858AF6A8B5F2CE0A12A033A6298623B433D419A749243`, 크기는 28,674,271바이트입니다. 사용자 `Untitled.blend`는 저장하지 않고 읽기 전용으로 열어 `ACTIVE` 상태가 유지되고 구형 Clone 설정이 자동 변경되지 않는 것을 확인했습니다.

## 0.3.15 Projection Paint Composite 직접 편집

- Composite Image를 별도 Clone Source가 아닌 Blender Texture Paint Target으로 직접 편집합니다.
- 활성 Composite 이후 추가한 일반 Layer는 독립 `NORMAL`로 유지하고, 해제 시 UUID·Image·Pixel·Mask·Adjustment를 보존합니다.
- 재합성 Source로 명시 선택한 일반 Layer는 기존 Bake 후 소비 계약을 따릅니다.
- `COMPOSITE_DIRECT_EDIT_SCHEMA_VERSION=1` 이전은 Legacy Clone 소유권을 `FULL_OWNED`·`DEFERRED_OWNED`·`FOREIGN`으로 구분하고, 검증된 Projection Paint 소유 상태만 정리합니다.
- Release는 준비·복구 Graph 검증·논리 Commit·고아 정리 순서이며, Commit 이후 정리 실패는 삭제 Pixel을 되살리는 거짓 Rollback 대신 정상 Graph와 경고를 남깁니다.
- 공개 `configure_composite_clone_source`와 `CompositeSession.dependents`·`derived`는 한 릴리스 동안 비활성 호환 shim으로 유지합니다.

대상 Blender Background 회귀는 Composite 62개, Layer Shader 48개, Layer UI 37개, Sessionless State 6개가 통과했습니다. Production 검사 220개, Small GICP 내장 Runtime 합성 검사, 격리 설치본과 Workspace·Camera Marker GUI도 통과했습니다. `build/colmap_tools-0.3.15.zip`을 실제 Blender `user_default`에 재설치했으며 SHA-256은 `A032329BF549EDC0856285046CE8BB96AA632811E87B1BB2CB369824EF35D28D`, 크기는 28,678,045바이트입니다. 사용자 `Untitled.blend`의 실제 설치본 GUI에서 Essentials Clone Tool·Brush와 Draw 수동 Stencil Reconnect를 저장 없이 검증했습니다. 실제 `Window.event_simulate` GUI에서도 `Ctrl+LMB` Source 지정, Source 빨강의 Destination 복제와 Blender Undo Pixel Hash 원복을 확인했으며 `clone_offset` 직접 쓰기와 가짜 Brush 생성은 0회였습니다.

## 0.3.14 Projection Paint Blender 도구·브러시 활성화

- Projection Paint 활성 Texture Paint 세션에서 Draw·Blur·Smear·Clone·Fill·Mask 툴바 선택과 실제 `Brush.image_brush_type`을 동기화합니다.
- 종류가 다를 때 Blender 5.2 Essentials의 실제 `Paint Hard`, `Blur`, `Smear`, `Clone`, `Fill`, `Mask` 자산만 활성화하며 Brush를 생성하거나 복제하지 않습니다.
- 같은 종류의 사용자 Brush는 유지하고, 지원하지 않는 Blender 도구와 다른 작업공간에는 개입하지 않습니다.
- 비Draw 전환 시 Camera Stencil만 해제하고 Draw 복귀 뒤에는 사용자의 명시적 Reconnect 규칙을 유지합니다.

매핑 자동 회귀 3개, Production 빌드 검사 220개, 격리 설치본·Workspace GUI·Camera Marker GUI와 사용자 `Untitled.blend` Source·실제 설치본 GUI 검증을 통과했습니다. 실제 GUI에서 여섯 도구의 Tool ID, Brush 종류와 Essentials 자산 경로 일치를 확인했고 검증용 가짜 Brush 생성은 0개였습니다. `build/colmap_tools-0.3.14.zip`을 실제 Blender `user_default`에 재설치했으며 SHA-256은 `0691EF192174175F096F64273F7FB8A34A801FAEDB30F02725587248225436C2`, 크기는 28,676,215바이트입니다. 열려 있던 Blender는 재시작 뒤 새 Runtime을 사용합니다.

## 0.3.13 Projection Paint Stencil 수동 재연결·Blender 도구 독립

- 내부 Paint 진입·Timer·Callback·Undo·Redo·Load 경로의 새 Brush 자동 채택을 제거했습니다.
- `Reconnect Stencil Texture`만 현재 Draw Brush를 Camera Stencil 대상으로 연결합니다.
- Clone·Smear·Blur·Fill·Mask Tool 전환 시 현재 Blender Tool과 Brush를 유지하고 이전 Stencil Brush만 원래 값으로 복구합니다.
- Blender 5.2 범용 `builtin.brush` Tool과 실제 `DRAW` Brush 조합을 지원하며 Tool·Brush 종류가 맞지 않으면 변경 없이 안내합니다.
- Reconnect 전체 Rollback, 명시적 Stencil 상태, Legacy 자동 승계 해제와 제한 등록 문맥 이전을 적용했습니다.

대상 회귀 153개, Production 빌드 검사 220개와 통합 등록, 격리 설치본·Workspace GUI·Camera Marker GUI를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`의 `IMAGE_ID 593` Draw → Clone → Draw 시나리오를 저장 없이 검증했으며 자동 재연결 없이 버튼으로만 최종 `CONNECTED`가 되는 것을 확인했습니다. `build/colmap_tools-0.3.13.zip`을 실제 Blender에 재설치했고 SHA-256은 `C6253BF13F453A97E6B1C56744B5009DB24D681F3B81FFDA84992A366475594C`, 크기는 28,673,684바이트입니다. 열려 있던 Blender는 재시작 뒤 새 Runtime을 사용합니다.

## 0.3.12 Projection Paint Composite Clone Slot 연결

- Composite 이후 만든 일반 Layer는 편집 대상 `paint_active_slot`과 Clone Source `paint_clone_slot`을 서로 다른 Image로 유지합니다.
- Clone Source는 Composite 생성 시 선택한 범위의 눈이 켜진 Layer·Mask·Adjustment 전체 결과입니다.
- Texture Paint Slot이 준비되지 않은 Object Mode에서는 연결을 지연하고 실제 Texture Paint 진입 시 확정합니다.
- 기존 0.3.11 활성 Composite의 최초 연결 이전과 생성 실패·해제 시 기존 Clone 설정을 복구합니다.
- Composite 회귀 60개와 Layer Shader 회귀 47개를 통과했습니다.

`build/colmap_tools-0.3.12.zip` Production 빌드, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 통과했습니다. 사용자 `Untitled.blend`에서 `Layer 2`는 편집 슬롯 5, `Composite Layer 1`은 Clone 슬롯 4로 분리된 것을 확인했습니다. ZIP SHA-256은 `85D981891AA8D8DF8840201C432D54267A3B04CEF7E23B0EB297D0112AC578FC`이고 크기는 28,667,834바이트입니다.

## 0.3.11 Projection Paint Camera View Stencil 복귀

- Mask 편집 중 중단된 Camera View 감시를 일반 Layer 복귀 시 다시 등록합니다.
- 일반 Layer 선택 시점이 Camera View 밖이면 Stencil을 즉시 연결하지 않고 감시만 재개합니다.
- 이후 실제 Camera View에 들어가는 순간 기존 Frame Source와 관리 Brush Texture를 Stencil로 복구합니다.
- Mask가 계속 선택된 상태에서는 기존 규칙대로 Stencil을 표시하지 않습니다.
- Layer Shader 47개, Mask Runtime 13개와 Stencil 자원 16개 회귀를 통과했습니다.

`build/colmap_tools-0.3.11.zip` Production 빌드, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 통과했습니다. 사용자 `Untitled.blend`에서 Mask 편집 중 Stencil 해제, 원근 뷰 일반 Layer 선택 시 미복구, Camera View 진입 시 자동 복구를 실제 GUI로 확인했습니다. ZIP SHA-256은 `53646E273D5FE1D34858AC36DA88A529B1CA3C034E97E51995B1F6599DC069BE`이고 크기는 28,667,091바이트입니다.

## 0.3.10 Projection Paint Layer 편집 활성 상태

- 유효한 Texture Paint Target과 활성 Layer Image가 있는 세션은 Stencil 연결 여부와 관계없이 `paint_state=ACTIVE`를 유지합니다.
- Mask 선택에서는 Camera Stencil만 해제하고 Painting Mode와 Mask Paint 입력은 계속 활성화합니다.
- Camera View 이탈과 수동 Stencil 해제도 Painting Mode를 종료하지 않으며, Stencil 상태는 `stencil_outside_camera_view`와 `manual_stencil_disabled`로 분리합니다.
- 실제 Texture Paint Target이 아니거나 Paint 세션이 종료된 경우의 기존 `FROZEN`·`IDLE` 처리는 유지합니다.
- Mask Runtime 13개, Stencil 자원 16개, Paint 자원 44개와 Layer Shader 47개 회귀를 통과했습니다.

`build/colmap_tools-0.3.10.zip` Production 빌드와 격리 설치본 검사, 실제 Blender `user_default` 재설치 및 사용자 `Untitled.blend` Mask GUI 회귀를 통과했습니다. ZIP SHA-256은 `45969408DBAFFBD911AF5F40AB7CC1697D3CEDD12B95541675BAC3195E9A9092`이고 크기는 28,667,069바이트입니다.

## 0.3.9 Modifier 지연 Runtime·Projection Paint 독립 재진입

- 비활성 `.blend` 로드에서 Modifier Runtime 원본 해석과 자동 복원 Timer 제거
- Modifier Workspace와 Projection Paint 수동 업데이트에서만 공유 증분 Runtime 복원
- Projection Paint READY Snapshot 독립 재진입과 COLD Modifier 업데이트 지원
- 취소·실패·Source token 변경 시 기존 Snapshot·Paint 자산·pixel 원자 보존
- 저장 편집 이력의 영향 범위 인덱스 갱신과 대용량 Snapshot SHA-256 증분 계산
- Properties context, Paint 진입 rollback, 기본 Paint Hard Brush와 Reconnect 실제 Runtime 판정 보강

사용자 `Untitled.blend`의 Camera 593개, Image 593개, Point3D 708,541개와 Point 제거 이력 1건에서 파일 로드 약 0.69초, 자동 Runtime 복원 0회, COLD 값 Snapshot 준비 약 28.58초, Projection Paint Scene Commit 전체 약 32.41초와 최장 단일 Tick 약 1.10초를 확인했습니다. 기존 Paint Target과 Layer 3개를 유지한 실제 전체 업데이트와 설치본 GUI Painting 재진입·Stencil 재연결(약 2.72초), Core·Shared·Architecture 및 Blender 대상 회귀를 통과했습니다.

`build/colmap_tools-0.3.9.zip` 빌드, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 통과했습니다. ZIP SHA-256은 `61E44BE0B101A95A34D33DA4AE9E55CB769AFA8E675CB5A3CC21CDBD7F9E3065`이고 크기는 28,666,881바이트입니다.

## 0.3.8 Projection Paint 외부 Original·Composite 재진입

- 저장·재열기 뒤 외부 `Original Layer` 직접 참조를 다른 Paint Target 자원으로 오판하던 소유권 검사 수정
- `original_source_image`와 같은 읽기 전용 외부 Original 본체만 Target UUID 검사에서 제외
- Original Mask, 일반 Layer·Mask, Adjustment·Mask와 Composite Image의 기존 Target UUID 검증 유지
- 실제 다른 Target의 활성 Composite 차단과 기존 Scene·Layer·Image 보존 계약 유지

외부 Original 저장·재열기 회귀 16개, 다른 Target 활성 Composite 차단, 사용자 `Untitled.blend`의 Painting 진입, Production 소스 회귀 219개와 Blender 등록 검사를 통과했습니다. `build/colmap_tools-0.3.8.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `D423EBD28C22750C63EFD3367C37A71722F92F0AA40B05E01DDBF2BD59EB1188`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.8이 적용됩니다.

## 0.3.7 Projection Paint COLMAP 데이터 업데이트 진행률

- `COLMAP 데이터 업데이트`의 기존 증분 Snapshot Job 처리량을 중앙 진행창에 연결
- Point3D 버퍼·메시·색상, Camera Data·Object와 Scene Commit 단계를 실제 완료 단위로 표시
- N 패널 상태에 현재 백분율을 함께 표시하고 완료 시 100% 화면을 한 Event Tick 유지
- 성공·실패·사용자 취소·Blender 강제 취소에서 Timer, Draw Handler와 Staging 자원을 함께 정리
- 작업 완료 전까지 기존 READY Snapshot을 유지하는 원자 교체 계약 보존

Snapshot 진행률 대상 회귀 3개, 독립 Snapshot 최초 생성·재업데이트·Rollback 회귀, Production 소스 회귀 219개와 Blender 등록 검사를 통과했습니다. `build/colmap_tools-0.3.7.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `05A0B450D3B0F9E4971BEBE37CBF01801B913B36FE662A79212AB84614D19379`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.7이 적용됩니다.

## 0.3.6 Projection Paint Stencil Mask·Camera 연결 제한

- Layer Mask와 Adjustment Mask 선택 즉시 관리 Brush Stencil Texture 연결 해제
- Mask 활성 중 Camera View·Workspace·Frame 변경과 Viewport Timer의 자동 재연결 차단
- Mask 활성 중 Reconnect·Project 버튼과 저수준 재연결 차단
- 유효한 Scene Camera가 없는 Stencil 연결 차단
- 일반 Layer 복귀 시 현재 Source와 Camera View가 모두 유효할 때만 Stencil 복구
- 연결 해제 중에도 Source와 Stencil 배치 영구 상태 유지

Mask·Stencil·Layer·Sessionless·Workspace·Camera Review 대상 회귀 95개와 Production 빌드 검사 219개를 통과했습니다. `build/colmap_tools-0.3.6.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `45DB739C3CCE632B1555A0D0C69EC8223FE48B6DEC91605D53835BDABF90B9F1`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.6이 적용됩니다.

## 0.3.5 Projection Paint 레이어 개인 단독 보기·마스크 단독 보기

- 주 Layer 합성 Chain을 변경하지 않는 단일 애드온 소유 Layer Solo Preview Mix를 추가
- 검정 Base 위에 선택 Layer의 최종 처리 Color와 현재 Factor만 적용해 위·아래 Layer와 Original을 제외
- Layer의 표시 상태, Image Alpha, Layer Mask, Adjustment와 Adjustment Mask 상태를 개인 출력에 반영
- Layer 개인 보기, Layer Mask 흑백 보기와 Adjustment Mask 흑백 보기를 배타적으로 전환하고 해제 시 전체 합성으로 복귀
- Reconcile, Layer 추가·삭제, Mask·표시 갱신과 저장·재열기에서 유효한 Preview UUID를 재연결하고 사라진 대상을 정리
- 기존 Scene·Material Preview 식별 상태는 호환성을 위해 유지

Blender 5.2 Background 대상 회귀 99개를 통과했습니다. `build/colmap_tools-0.3.5.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `F628F29DCD56E797F73C18F13D58D81FFA093AE586FFFBF9E418EB5087B7E9B7`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.5가 적용됩니다.

## 0.3.4 Projection Paint Original 외부 직접 참조

- 외부 파일 기반 Source Image를 `Original Layer`가 복제·리샘플·Pack 없이 직접 참조
- 직접 참조 Original은 읽기 전용으로 유지하고 일반 Paint는 상위 관리 Layer에서만 수행
- Original의 Mask·Adjustment·단독 보기·Composite 합성 효과는 기존처럼 유지
- 기존 Schema 1 관리 복제 Original을 Source 직접 참조로 원자 이관하고 과거 복제본만 정리
- `.blend` 저장·재열기에서는 Blender Image의 원본 경로로 픽셀을 다시 읽고, 파일 누락·미저장 변경은 명확한 오류로 중단
- 경로 없는 Packed·Generated·Material Source는 기존 관리 Image 방식으로 유지

`build/colmap_tools-0.3.4.zip` 빌드, 격리 설치본 검사와 실제 Blender `user_default` 설치본 검증을 통과했습니다. ZIP SHA-256은 `94CF3EB052BB9565FD52F2ED224609144E2B93AC78E716E4DBBB1DD9F74B1802`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.4가 적용됩니다.

## 0.3.3 Projection Paint 단독 보기 하위 합성

- Layer 단독 보기를 선택 Layer의 원색 출력이 아닌 해당 Layer의 Mix 출력에 연결
- 선택 Layer의 Mask·Adjustment와 모든 하위 Layer 합성을 유지하고 상위 Layer만 제외
- 단독 보기 상태 검증도 같은 Mix 출력을 기준으로 통일
- Mask 단독 보기의 흑백 출력과 전체 합성 복귀 계약은 기존대로 유지

`build/colmap_tools-0.3.3.zip` 빌드, 격리 설치본 검사와 실제 Blender `user_default` 설치본 검증을 통과했습니다. ZIP SHA-256은 `B0BFEF38EB028CF4622174FB7170DA4D12C5A09978D1B0F1D6038FED227AEF3E`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.3이 적용됩니다.

## 0.3.2 Projection Paint 레이어 선택 통합

- Layer·Layer Mask·Adjustment·Adjustment Mask 행의 일반 클릭을 즉시 선택·활성화로 통합
- 기존 Layer 더블클릭 활성화 분기와 활성 체크 표시 제거
- 일반 클릭은 단일 선택, `Ctrl`은 항목 토글, `Shift`는 표시 순서 범위 선택으로 통일
- 복수 선택 중 Projection Paint 작업공간의 Viewport Paint 입력을 `LEFTMOUSE`·`PEN`·`ERASER` 단계에서 차단
- 복수 선택은 UI 상태에만 추가하고 Composite 합성 후보는 기존 최상위 Paint Layer 계약 유지
- Layer Reset·교체·Rollback에서 Mask·Adjustment 선택 상태를 함께 저장·복원

`build/colmap_tools-0.3.2.zip` 빌드, 격리 설치본 검사와 실제 Blender `user_default` 설치본 검증을 통과했습니다. ZIP SHA-256은 `19536B3F5924637C844D3CD18192C01C0EE7F78DEF63BE0C97B87FA2F48FA114`입니다. 설치 중 열려 있던 Blender GUI는 기존 0.3.1 Runtime을 유지하므로 재시작 뒤 0.3.2가 적용됩니다.

## 0.3.1 Source Texture 저장·복원

- Projection Paint Source IMAGE_ID·이름·경로와 Dataset 식별자를 Scene 영구 상태로 저장
- Stencil 위치·크기·회전을 저장하고 같은 Dataset·경로·IMAGE_ID에만 한 번 복원
- Blender 저장 직전과 Paint 세션 종료의 Runtime Source 해제 전에 영구 스냅샷 갱신
- 기존 Runtime Image·Texture·Brush 정리와 Workspace·Undo 격리 계약 유지
- Blender 5.2 세션 재진입, `.blend` 저장·재열기, Mask·Lifecycle·Workspace 회귀 통과

`build/colmap_tools-0.3.1.zip`과 격리 설치본 검증을 통과했고 실제 Blender `user_default`에 0.3.1을 재설치했습니다. ZIP SHA-256은 `8E9C029A8121DABCFF6E4DA2BCC9298180EABF08741AFAAC91AAEBF792703B01`입니다.

## 0.3.0 Source 변경

- `shared/colmap_transfer` 불변 값 계약과 Modifier Provider Registry 구현
- Projection Painting `COLMAP 데이터 업데이트` Modal Staging·Source 재검증·원자 Commit·Rollback 구현
- Projection Painting Dataset 패널에서 상태 정보와 Display Axis UI를 제거하고 업데이트·표시 크기만 유지
- PP 전용 Snapshot UUID·Revision·Source token·수량·상태와 Resource ownership tag 구현
- Modifier 재Import·표시 변경 전 PP Snapshot 불변, PP 표시 설정 독립화
- Paint Target·Layer·Texture pixel을 Snapshot 교체 대상에서 제외
- Projection Painting의 실시간 공용 Binding·Display Listener 등록 제거
- Modifier와 PP 독립 View Layer 포함·제외 정책 적용
- Painting Undo에서 비활성 Modifier full reconcile 미실행, 비활성 PP lazy recovery 적용
- `shared/colmap_dataset`은 기존 `0.2.0` 파일을 위한 Legacy 입력으로 보존

현재 Source 자동 회귀, `0.3.0` Production ZIP, 격리 설치본과 실제 Blender `user_default` 재설치는 통과했습니다. 593 Camera 및 8K 실데이터 GUI 성능과 기존 `0.2.0` 저장 파일 전체 마이그레이션 QC는 아직 수행하지 않았습니다.

## 완료

- 루트 Git 저장소와 단일 Blender Extension Manifest 구성
- `shell/`, `tools/`, `shared/`, `tests/`, `docs/`, `build_tools/` 경계 생성
- 원본 세 저장소를 수정하지 않고 Runtime Source와 테스트를 통합 경계로 복사
- Root Shell 단독 `Window → COLMAP Tools` 메뉴 소유
- Align, Modifier, Projection Painting의 독립 등록·해제 유지
- 기존 Operator ID와 Scene Property 이름 유지
- 도구 사이 직접 import 금지 및 공개 식별자 충돌 검사
- 등록 실패 원자적 Rollback과 중복 등록·해제 검사
- Blender 5.2 Manifest 검증, ZIP 생성과 격리 설치본 Smoke Test
- Align `small_gicp` Native Runtime의 Build Staging 포함과 설치본 합성 GICP 검증
- 세 도구의 활성 Workspace 복제 제거와 전용 Blender 5.2 Seed Append 전환
- Tool ID·UUID·Workspace/Seed Schema·Screen Owner 기반 공통 Workspace Factory
- Align 전용 필터 비활성, Modifier·Projection Paint 필터 활성과 Local View 해제 계약
- Legacy 및 CREATING·FAILED Workspace의 소유권·다중 Window 기반 안전 교체
- Source와 격리 설치 ZIP의 전체 Workspace 실행 순서 GUI 행렬 검증
- 다른 COLMAP 도구 진입 및 수동 탭 전환 시 이전 도구의 정식 종료 완료 후 다음 도구를 여는 공용 Workspace Handoff
- 일반 Blender Workspace 탭으로 이동할 때도 직전 COLMAP 도구를 정식 종료하고 사용자가 선택한 탭으로 복귀하는 자유 이동 계약
- Align·Modifier의 기존 강제 Workspace 복귀 비활성화와 Projection Paint 준비 중 강제 복귀 제거
- Align·Modifier 세션의 현재 탭이 아닌 소유 Window 기준 종료와 Projection Paint 준비·세션 종료 후 READY Workspace 유지·재사용
- Projection Paint 준비 중 이탈 시 불완전한 `CREATING` Workspace만 정리하고 `READY` Workspace는 보존하는 상태별 종료
- 이전 실행의 비활성 CREATING·FAILED 대상 Workspace를 공식 삭제하고 Source 복귀를 확인한 뒤 재생성하는 자동 복구
- 고정 COLMAP Fixture의 Modifier 실제 Import·Frame All·Edit·Undo·Export GUI 회귀
- Modifier 관리 Collection을 전용 View Layer에만 포함하고, 전용 View Layer에서는 일반 Scene 데이터를 제외하며, 모든 일반 View Layer에서는 관리 데이터를 제외하는 양방향 가시성 격리
- Modifier 종료·재진입에서 데이터 블록, Modifier 편집 결과, Camera Review Pending·Undo 상태를 보존하는 GUI 회귀
- 이전 일반 Workspace 복귀 확인이 남아 있는 동안의 빠른 Modifier 재진입 요청을 공용 Handoff 대상으로 병합
- Modifier Camera View에서 관리 Background Image를 실제 F7 입력으로 켜고 끄는 전용 Operator·키맵 수명주기
- Modifier와 Projection Paint의 F7 배경 토글을 활성 도구 세션 소유권으로 분리해 다른 도구가 입력을 소비하지 않는 실행 조건
- Scene별 Camera Marker 단일 사용권과 같은 Scene·다른 Window의 동시 점유 차단
- Modifier `review_marker_manifest_json` 기반 Marker Attach·Detach·Rollback과 비활성 Load·Undo 무생성 계약
- Projection Paint `pp_camera_manifest` 기반 Marker Attach·Detach·Rollback과 활성 lease 전용 Camera Guard
- Modifier와 Projection Paint 종료 시 정확 소유 Marker만 제거하고 Camera Object, 편집 결과, Pending·Undo와 Paint 데이터를 보존하는 세션 격리
- Modifier → Projection Paint → Modifier의 동일 Frame 1 실제 Import, Marker 교대, 사용자 Marker와 양쪽 Object 보존 GUI 회귀
- 수동 Projection Paint Workspace 재진입에서도 실제 Runtime 사용권을 다시 획득하는 공용 Handoff 연결
- Extension 해제 시 모든 Scene의 검증된 도구 Marker와 Runtime lease를 정리하는 생명주기
- Align 세션은 Workspace 전환 시 자동 종료하고 N 패널의 중복 수동 종료 버튼은 노출하지 않는 UI 계약
- READY Align Workspace 재진입 시 세션 재연결 전에도 N 패널을 유지하고 입력만 일시 잠그는 UI 계약
- `.blend` 파일 로드로 초기화되는 Workspace Message Bus를 `load_post`에서 다시 구독해 수동 탭 전환을 복구하는 계약
- Modifier와 Projection Paint의 Point Display·Camera Display 설정을 하나의 UI Box로 묶은 공통 배치
- 통합 표시 설정 Box의 Point·Camera 사이 구분선과 추가 세로 여백 제거
- Align의 마커 정렬용 대표 삼각형은 ICP 완료 결과 미리보기부터 숨기고, 점군·마커·승인 흐름은 유지
- Align OBJ의 `vt`와 face-corner UV 인덱스를 Runtime에 불변 보존하고, 정렬 결과 Export·Reflection·왕복 검증·Blender UV Layer 재수입까지 연결
- Align OBJ의 MTL diffuse 재질과 이미지 경로를 Runtime에 보존하고, `COLMAP RGB` 텍스처 표시·생성 MTL·원본 이미지 바이트 복사·SHA-256 왕복 검증까지 연결
- Align 독립·연결 OBJ를 UUID 기반 비연결 Fake User Mesh로 `.blend`에 내장하고, 원본 Polygon·Loop UV·재질은 Export에, `calc_loop_triangles()` 프록시는 표시·피킹·마커·CCCoreLib에만 사용하는 계약
- Align 프로젝트 schema v8에서 외부 OBJ SHA 의존성을 제거하고, 외부 OBJ 삭제 뒤 복원·Export, 구버전 일회성 내장 이전, 레이어 삭제·새 프로젝트·실패 rollback의 소유 Data Block 수명주기 검증
- Align OBJ Export를 모델 표시 이름의 OBJ·MTL과 원본 이름 이미지가 한 폴더에 놓이는 구조로 변경하고, 중복 파일명과 Windows 260자 경로 제한을 생성 전에 명확한 오류로 보고
- Modifier 원본 변경 경고 시 Workspace 활성 상태와 표시 설정은 유지하고, Projection Paint 재진입 시 소유 COLMAP Collection 표시와 Point Geometry 갱신을 복원하는 계약
- `Scene.colmap_dataset`과 중립 소유 키를 권위로 삼는 Scene별 단일 공용 COLMAP Dataset 계층
- Modifier를 Point·Camera·Axis·Collection의 유일 생산자로 유지하고 손실 없는 Reconstruction·편집·Undo·Export·`Scene.colmap_import` 계약을 보존
- Projection Paint의 별도 COLMAP Import Operator와 Import UI를 제거하고 공용 Snapshot·Resource를 읽기 전용으로 소비하는 Adapter로 전환
- PP Manifest·Runtime·Timeline Marker가 IMAGE_ID별 공용 Camera Object/Data 포인터를 직접 참조하고, UUID·Dataset Revision·Display Revision 변경을 재바인드하는 계약
- 공용 Root를 Modifier와 Projection Paint 전용 View Layer에서만 표시하고 일반·Align View Layer에서 제외하는 중립 가시성 계약
- Point Scale·Camera Frustum·Display Axis의 공용 단일 권위와 Modifier·PP 양방향 UI 동기화, 프리셋 축 묶음의 단일 Display Revision 갱신
- 같은 Source 재Import의 UUID 유지·Revision 증가, 다른 Source의 새 UUID, 살아 있는 Camera 기준 PP 재구성과 Paint Layer·Texture·Target·Legacy PP Resource 보존

## 현재 0.3.0 Build 성격

`build/colmap_tools-0.3.0.zip`은 Modifier 값 Provider와 Projection Painting 독립 Snapshot, 수동 업데이트, 원자 교체·Rollback, 독립 표시 설정과 비활성 Undo 지연 복구를 포함합니다. `SmallGicp` Production 프로필의 Native Runtime과 기존 Workspace·Align OBJ UV·텍스처 왕복 기능도 유지합니다.

- ZIP SHA-256: `1817922E749BDFD2EA3557E544F8A74C1085FB0F0F4461ACA2692C8FF4017EE7`
- 격리 설치본: 등록·Snapshot Operator·Scene 속성·Small GICP·OBJ UV 왕복 통과
- 실제 설치본: `bl_ext.user_default.colmap_tools` 재설치와 같은 공개 계약 통과

## 이전 0.2.0 Build 기록

`build/colmap_tools-0.2.0.zip`은 `SmallGicp` Production 프로필입니다. 기존 통합·Workspace·세션 격리와 Align OBJ UV·텍스처 왕복 계약에 더해 Modifier가 생산하고 Projection Painting이 읽는 중립 공용 COLMAP Dataset, 단일 Point·Camera Resource, Dataset Revision 재연결, 공용 표시 설정과 전용 View Layer 가시성 계약을 포함합니다. CPython 3.12.10, NumPy 1.26.4와 small_gicp 1.0.1은 `tools/align/runtime/python/`에 포함되며 원본 Repository, 테스트, 문서, Cache와 Build 도구는 ZIP에서 제외됩니다.

- ZIP SHA-256: `840BCFA5BBC4280F6A6797D81D81FE5291E3B84E188E57D08606BA26DBBD5F73`
- Align Seed SHA-256: `2C89AB4890253F52B3B3005794B9FCC8586589222B517F65940626487939A8D2`
- Modifier Seed SHA-256: `00E2D5559293ECAD65D0E225D87CE8655E9D0CE37F1E680DB7B0C0715B730FF3`
- Projection Paint Seed SHA-256: `9553632614C5E7CCAB35EB2115600CC6EED464093464F6A203DE8B53784AECA4`

## 후속 작업

- Open3D 개발 Profile의 별도 배포 여부와 외부 의존성 라이선스 정리
- 사용자 보유 실데이터로 Align → Modifier Import 수치와 실제 화면 가시성 최종 승인
- 사용자 보유 데이터로 Modifier Import → 편집 확정 → Projection Painting Camera Review·Stencil·Paint 전체 승인
- 실제 다중 Window Legacy 점유 차단과 기존 `.blend` 저장·재로드 전체 마이그레이션 승인
- Blender 5.2 Append Workspace 공식 삭제 시 발생하는 `SRLayout` 내부 진단의 후속 Blender Version 재검증
- 기존 개별 Extension과 통합 Extension의 동시 설치 정책 확정
- 원격 Repository, 원본 Git Bundle 보관 위치와 첫 통합 Commit 정책 확정

후속 작업은 Align을 공용 Dataset에 연결하거나 도구별 Runtime·Undo를 통합하는 작업을 의미하지 않습니다. Align 독립성과 Modifier·Projection Painting의 세션 경계는 계속 유지합니다.
