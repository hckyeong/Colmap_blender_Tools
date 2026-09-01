# COLMAP Tools 0.3.6 Projection Paint Stencil Mask·Camera 연결 제한

## 원인

Mask 선택은 `paint_mask_mode`와 Paint Image만 변경하고 기존 Brush의 관리 Stencil Texture 연결을 해제하지 않았습니다. Camera View, Workspace, Frame 변경 Callback도 Mask 상태를 연결 제한 조건으로 사용하지 않아 Mask를 선택한 뒤에도 Stencil Overlay가 남거나 다시 연결됐습니다.

## 변경 내용

- Layer Mask와 Adjustment Mask를 선택하면 관리 Brush의 Stencil Texture 연결을 즉시 해제합니다.
- Mask 활성 중에는 Camera View 복귀, Workspace 복귀, Frame Source 갱신과 Viewport Timer가 Stencil을 다시 연결하지 않습니다.
- `Reconnect Stencil Texture`와 `Project Stencil to Target`은 Mask 활성 중 비활성화합니다.
- 저수준 재연결도 Mask 활성 상태 또는 유효한 Scene Camera가 없는 상태를 거부합니다.
- 일반 Layer를 다시 선택하면 현재 Source가 유효하고 실제 Camera View인 경우에만 Stencil을 복구합니다.
- Source Image와 Stencil 위치·크기·회전 영구값은 연결 해제 중에도 유지합니다.

## 검증

Blender 5.2 Background에서 Mask 즉시 해제, Camera·Workspace·Frame Callback 재연결 차단, 수동 재연결 차단, Scene Camera 필수 조건, 일반 Layer 복귀와 Sessionless 저장 상태를 검증합니다.

Mask·Stencil·Layer·Sessionless·Workspace·Camera Review 대상 회귀 95개와 Production 빌드 검사 219개를 통과했습니다. `build/colmap_tools-0.3.6.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `45DB739C3CCE632B1555A0D0C69EC8223FE48B6DEC91605D53835BDABF90B9F1`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.6이 적용됩니다.
