# Projection Paint Blender 기본 Paint 도구 호환 0.3.17

Projection Paint가 Blender 5.2의 Draw, Blur, Smear, Clone, Fill, Mask 및 사용자 Brush를 기본 동작 그대로 사용합니다.

- Clone Tool 전환 시 Paint Slot Clone을 끄고 활성 Layer Image를 Source와 Target으로 공유합니다. Clone 전용 Image, Slot, Offset은 읽거나 쓰지 않습니다.
- Tool Bridge는 지원 Tool ID가 바뀐 순간에만 동작합니다. 호환 사용자 Brush를 유지하고 기록된 Brush가 없을 때만 Essentials 자산을 한 번 활성화합니다.
- Camera Stencil은 명시적 Reconnect에서 현재 편집 가능한 Draw Brush에 직접 연결합니다. Brush 복사본과 장기 복구 상태는 만들지 않습니다.
- 구형 Stencil 위치·크기·회전 영구 복원 필드는 제거했으며 저장·재열기는 Blender Brush 상태를 그대로 따릅니다.
- 다른 Tool, Workspace, 저장과 로드 왕복은 연결된 Draw Brush의 Stencil 및 사용자 Brush 설정을 변경하지 않습니다.
- 3D Viewport의 Clone `Ctrl+LMB` Source 지정은 Blender에 전달하고 N 패널의 Ctrl/Shift Layer 복수 선택과 분리합니다.
- Projection Paint가 등록하던 3D View와 Image Paint의 Alt+RMB Zoom Keymap을 제거하고 F7 Camera Review 전환은 유지합니다.

## 검증 결과

- Blender Background 대상 회귀 198개와 Production 검사 220개를 통과했습니다.
- 실제 설치본에서 Draw·Blur·Smear·Clone·Fill·Mask의 Tool ID와 실제 Brush 기능을 확인했습니다.
- 사용자 파일을 저장하지 않고 도구 왕복 뒤 동일 사용자 Draw Brush와 Camera Stencil 연결 유지를 확인했습니다.
- 같은 이미지 Clone의 Viewport `Ctrl+LMB` Source 지정, 실제 Pixel 복제와 Blender Undo 복원을 확인했습니다.
- 읽기 전용 Essentials Draw Brush의 Reconnect 거부와 편집 가능한 Brush 선택 안내를 확인했습니다.
- 설치 산출물은 `build/colmap_tools-0.3.17.zip`이며 SHA-256은 `A1F9277B2EBFAF9BC33ED78B1546F5C062DEA3B5C9AB359D58341AE6B7F15201`입니다.
