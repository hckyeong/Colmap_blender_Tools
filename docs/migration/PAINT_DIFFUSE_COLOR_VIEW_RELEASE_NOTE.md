# Projection Paint Diffuse Color View 0.3.27

## 변경 결과

- Painting Mode 진입 시 전용 3D View를 Material Preview로 유지하면서 Render Pass를 `Diffuse Color`로 설정합니다.
- 전환 직전 Combined 화면에서 캡처한 배경 종류와 RGB 색상을 전용 3D View에 그대로 적용합니다.
- 따라서 조명 결과를 제외한 재질 색상을 보면서도 Combined와 같은 회색 배경을 유지합니다.

## 복구 계약

- Painting Mode 종료 시 진입 전 Shading 종류와 Render Pass를 복원합니다.
- 종료 시점에도 애드온이 설정한 `MATERIAL`·`DIFFUSE_COLOR` 상태일 때만 복원합니다.
- Painting Mode 안에서 사용자가 Shading을 직접 변경했다면 해당 변경을 덮어쓰지 않습니다.
- 배경 이미지 표시는 Painting Mode에서 끄며, 원래 작업공간 복귀 시 기존 표시 상태가 유지됩니다.

## 검증

- 실제 Blender GUI에서 `MATERIAL`·`DIFFUSE_COLOR` 적용 확인
- Combined 화면에서 캡처한 `VIEWPORT`·RGB 0.18 회색 배경 승계 확인
- Painting Mode 종료 후 원래 `SOLID`·`COMBINED`와 배경 이미지 표시 복원 확인
- Paint 자원 43개와 Paint 작업공간 12개 회귀 통과

Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사를 통과했습니다. `build/colmap_tools-0.3.27.zip`을 실제 Blender `user_default`에 재설치하고 별도 Blender 프로세스에서 버전 0.3.27과 Diffuse Color·Combined 배경 승계·복구 코드를 확인했습니다. ZIP SHA-256은 `567E73D18BD7AFB5BA2BE14C3744205E8FEFB704329A898EB5E33633FD475BC9`, 크기는 28,677,581바이트입니다.
