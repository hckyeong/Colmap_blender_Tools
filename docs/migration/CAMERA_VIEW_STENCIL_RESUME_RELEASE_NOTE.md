# COLMAP Tools 0.3.11 Projection Paint Camera View Stencil 복귀

## 변경 내용

- Stencil 복구 시점을 일반 Layer 선택이 아니라 실제 Camera View 진입으로 확정했습니다.
- Mask 편집 후 일반 Layer로 돌아오면 Camera View Message Bus와 추적 Timer를 다시 등록합니다.
- 원근 뷰에서는 Stencil을 연결하지 않고, 이후 Camera View 전환 Callback에서 복구합니다.
- 이미 Camera View인 상태에서 일반 Layer를 선택하면 즉시 복구한 뒤 감시를 유지합니다.
- Mask가 선택된 동안 Stencil을 숨기는 기존 계약은 유지합니다.

## 검증

- Layer·Mask Shader와 선택 수명주기 47개
- Mask Runtime·Camera 감시 제한 13개
- Stencil 자원·Camera 이탈 및 복귀 16개
- 격리 설치본 검사와 실제 Blender `user_default` 설치본 검사
- 사용자 `Untitled.blend`에서 Mask 선택, 원근 뷰 일반 Layer 복귀, Camera View 자동 Stencil 복구 GUI 회귀

`build/colmap_tools-0.3.11.zip` Production 빌드와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `53646E273D5FE1D34858AC36DA88A529B1CA3C034E97E51995B1F6599DC069BE`이고 크기는 28,667,091바이트입니다.
