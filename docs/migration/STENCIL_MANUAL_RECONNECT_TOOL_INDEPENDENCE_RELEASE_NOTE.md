# Projection Paint Stencil 수동 재연결·Blender 도구 독립 0.3.13 Release Note

## 변경 결과

- Paint 진입, Camera View·Workspace·Layer 복귀, Frame 변경, Undo·Redo와 파일 Load는 Stencil 자원과 기존 연결만 갱신하며 새 Brush를 복사하거나 활성화하지 않습니다.
- 새 Camera Stencil Brush 채택은 사용자가 누르는 `Reconnect Stencil Texture` Operator에만 허용합니다.
- Blender 5.2의 범용 `builtin.brush` Tool과 실제 `DRAW` Brush 조합을 정상 Draw로 인정합니다.
- Clone·Smear·Blur·Fill·Mask Tool 또는 Tool과 Brush 종류가 맞지 않는 상태에서는 Reconnect를 변경 없이 거부합니다.
- 다른 Brush로 전환하면 이전 연결 Brush의 Texture, Mapping, Angle, Color, Stencil 배치와 Unified Color를 복구하고 현재 Tool과 Brush는 건드리지 않습니다.
- Stencil 상태를 `DISCONNECTED`, `CONNECTED`, `CAMERA_SUSPENDED`, `MASK_SUSPENDED`, `BRUSH_CHANGED`, `MANUAL_DISABLED`, `ERROR`로 분리해 N 패널에 원인을 표시합니다.
- Reconnect 실패 시 Camera, Frame, Viewport, Tool, Brush, Source, Clone 설정과 Material Paint Slot을 Transaction 단위로 복구합니다.
- Legacy 자동 Brush 승계를 모든 Scene에서 끄며, Extension 활성화 제한 문맥의 Scene 이전은 안전하게 `load_post`로 넘깁니다.

## 검증

- Stencil·Mask Runtime·Sessionless·Layer Shader·Paint Resource·Lifecycle·Architecture 대상 회귀 153개 통과
- Production 빌드의 Architecture·Coexistence·Modifier Core·Projection Core 220개와 통합 등록·해제 검사 통과
- Small GICP 내장 Runtime Python 3.12.10, NumPy 1.26.4, small_gicp 1.0.1 합성 연산 통과
- Production ZIP 격리 설치본 등록, UV OBJ 왕복과 Workspace·Camera Marker GUI 회귀 통과
- 실제 `user_default` 설치본 `0.3.13`으로 `C:\Users\gksru\Desktop\Untitled.blend`를 저장하지 않고 GUI 검증 통과
- `IMAGE_ID 593`에서 Draw Reconnect → Clone Tool·Clone Brush 전환 → Draw 복귀 시 자동 재연결 없음 → 명시적 Reconnect로 최종 `CONNECTED` 확인

## 패키지

- 파일: `build/colmap_tools-0.3.13.zip`
- 크기: 28,673,684바이트
- SHA-256: `C6253BF13F453A97E6B1C56744B5009DB24D681F3B81FFDA84992A366475594C`
- 실제 Blender `user_default` 저장소 재설치 완료

설치 중 이미 열려 있던 Blender GUI는 메모리에 로드한 이전 Runtime을 유지하므로, 사용자 작업을 저장한 뒤 Blender를 재시작해야 `0.3.13`이 적용됩니다.
