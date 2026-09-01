# COLMAP Tools 0.3.10 Projection Paint Layer 편집 활성 상태

## 변경 내용

- Painting Mode의 활성 상태와 Camera Stencil 연결 상태를 분리했습니다.
- 유효한 Texture Paint Target에서 Layer 또는 Mask Image를 편집하는 동안 `paint_state`는 항상 `ACTIVE`입니다.
- Mask 선택, Camera View 이탈 또는 Stencil 수동 해제 시에도 Painting Mode 버튼은 활성 상태를 유지합니다.
- Mask에서는 기존 계약대로 Camera Stencil을 연결하지 않습니다.
- Camera Stencil 표시 여부는 `stencil_outside_camera_view`와 `manual_stencil_disabled`로 계속 관리합니다.
- 실제 Texture Paint Target이 아니거나 Paint 세션이 종료된 상태는 활성으로 오인하지 않습니다.

## 검증

- Mask Runtime·Stencil 연결 수명주기 13개
- Stencil 자원·Camera 이탈 16개
- Paint Target·Material·상태 복구 44개
- Layer·Mask Shader·선택·합성 47개

## 배포 검증

- Production ZIP: `build/colmap_tools-0.3.10.zip`
- ZIP SHA-256: `45969408DBAFFBD911AF5F40AB7CC1697D3CEDD12B95541675BAC3195E9A9092`
- ZIP 크기: 28,667,069바이트
- 격리 설치본과 실제 Blender `user_default` 재설치 검증 통과
- 사용자 `Untitled.blend`에서 편집 가능 Layer의 Mask 활성 후 `TEXTURE_PAINT`, `paint_state=ACTIVE`, Camera Stencil 해제를 확인했습니다.
