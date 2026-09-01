# Projection Paint Industry Compatible 뷰포트 탐색 0.3.22

## 변경 사항

Projection Paint 전용 작업공간의 3D Viewport에서 다음 Industry Compatible 탐색 입력을 지원합니다.

- `Alt+좌클릭 드래그`: Orbit View
- `Alt+가운데 버튼 드래그`: Pan View
- `Alt+우클릭 드래그`: Zoom View

각 입력은 Projection Paint가 카메라 행렬을 직접 계산하지 않고 Blender의 기본 `view3d.rotate`, `view3d.move`, `view3d.zoom` 연산자를 시작합니다. 따라서 Blender가 제공하는 기존 탐색 감도와 방향 설정을 그대로 사용합니다.

## 적용 범위

- 현재 Window가 저장된 Projection Paint 작업공간을 사용 중일 때만 활성화됩니다.
- 3D Viewport의 `WINDOW` 영역에서만 활성화되며 N 패널과 다른 Editor에서는 작동하지 않습니다.
- 일반 Blender 작업공간, Modifier와 Align 작업공간의 키맵은 변경하지 않습니다.
- Blender 기본 키맵과 Industry Compatible 키맵 중 어느 것을 사용해도 Projection Paint 안에서는 같은 세 입력을 사용할 수 있습니다.

## 입력 독립성

- 실제 탐색은 Blender 기본 View3D 모달 연산자가 처리합니다.
- Paint Tool, Brush, Stencil Source, Layer와 Undo 상태는 변경하지 않습니다.
- 복수 레이어 선택 중 Paint 스트로크 차단은 `Alt+좌클릭`을 소비하지 않고 Orbit 입력으로 통과시킵니다.
- Extension 해제 시 추가한 여섯 Keymap 항목을 모두 제거합니다.

## 검증 범위

- 3D View와 Image Paint Keymap에 세 입력이 각각 한 개만 등록되는지 확인
- Projection Paint 작업공간·3D Viewport·Window Region 범위 확인
- 다른 작업공간과 N 패널에서 Operator Poll 거부 확인
- 세 입력이 Blender 기본 rotate·move·zoom에 그대로 위임되는지 확인
- 실제 사용자 프로젝트에서 `Alt+우클릭` 드래그로 Camera View 확대값 변경 확인
- 탐색 전후 Tool·Brush와 Camera Stencil 상태 유지 확인

Blender Background 대상 회귀 51개와 Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성, 격리 설치본과 Workspace·Camera Marker GUI 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Blender 기본 키맵 상태에서 `Alt+우클릭` PRESS·드래그·RELEASE를 주입했으며 Camera View 확대값이 `-4.243720`에서 `1.280397`로 변경됐습니다. 같은 Draw Tool·Brush Pointer와 Camera Stencil 연결, 기존 Frame Source 지연 처리와 Camera Texture Stroke도 유지됐습니다.

Production 패키지는 `build/colmap_tools-0.3.22.zip`이며 SHA-256은 `9F2174C3199B3C82605061DAFDF87D2AA0F98164CEAACC8A1BB7F4DCCB7CDEE3`, 크기는 28,672,755바이트입니다. 실제 Blender `user_default`에 0.3.22를 설치했고 이미 열려 있던 Blender 프로세스는 재시작한 뒤 새 Runtime을 사용합니다.
