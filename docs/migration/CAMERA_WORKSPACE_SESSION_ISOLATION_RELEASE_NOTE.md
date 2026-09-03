# Modifier·Projection Painting Camera 세션 격리 0.3.35

## 변경 내용

- Modifier와 Projection Painting의 Camera 쓰기 권한을 현재 Window·Workspace·Marker 사용권·세대 Token으로 제한했습니다.
- 비활성 도구의 Frame Handler, Debounce와 Repair Timer는 `scene.camera`, Render 해상도와 Background Image를 변경하지 않습니다.
- 전용 Viewport는 도구별 Local Camera를 사용하며 Camera Object·Background Image·Runtime Cache는 작업공간을 나가도 보존합니다.
- 작업공간 생성과 Camera 연결을 분리했습니다. Workspace와 화면 구성이 `READY`가 된 다음 UI Tick에서만 Camera 세션을 연결합니다.
- 준비 실패 시 Camera·Marker·View Layer 권한을 반납하고 실패 Workspace를 정리해 삭제된 Workspace 참조의 반복 사용을 막습니다.

## 사용자 경험

1. Modifier에서 Projection Painting을 열면 Modifier Camera 세션이 먼저 종료됩니다.
2. Projection Painting 작업공간과 화면 구성이 준비되는 동안 상태에 전환 확인 중임을 표시합니다.
3. 작업공간 준비가 확인되면 현재 Frame의 Projection Painting Camera가 한 번 연결됩니다.
4. Modifier로 돌아가면 Projection Painting의 대기 중 Frame 요청은 폐기되고 Modifier Camera와 Background Image가 유지됩니다.
5. 일반 Blender 작업공간에서는 두 COLMAP Camera 세션 모두 Scene Camera를 변경하지 않습니다.

기존 Dataset 재Import와 자동 마이그레이션은 필요하지 않습니다.

## 검증

- Modifier Core 216개와 Architecture 12개
- Coexistence 11개와 Projection Core 40개
- Projection Painting Camera Background 검사 19개
- Modifier Camera Background 전체 검사
- Source와 실제 설치본의 숨김 Blender GUI에서 Modifier → Projection Painting → Modifier → 일반 Workspace Camera·Marker 교대
- 격리 설치본 검사와 실제 Blender `user_default` 설치 버전 `0.3.35` 확인

Production ZIP은 `build/colmap_tools-0.3.35.zip`이며 SHA-256은 `60AB1B837F680D8999DF6C485A0794FEDADC80C336F196E5E95884A9069C1D58`, 크기는 28,731,259바이트입니다.
