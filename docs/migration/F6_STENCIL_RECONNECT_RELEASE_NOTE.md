# Projection Paint F6 Stencil 재연결 0.3.26

## 변경 결과

- Projection Paint 3D View에서 `F6`을 누르면 `Reconnect Stencil Texture`를 실행합니다.
- 키맵은 `3D View`·`VIEW_3D`·`WINDOW` 문맥에 한 번만 등록됩니다.
- Projection Paint 작업공간이 아니면 Reconnect Operator가 실행되지 않습니다.

## 실행 조건

- 현재 Window가 Scene의 Projection Paint 작업공간을 표시 중
- Paint 상태가 `ACTIVE`
- 실제 Paint Runtime 존재
- Projection Paint Mask 편집 상태가 아님
- COLMAP Snapshot이 `READY`

조건을 만족하지 않으면 상태를 변경하지 않고 Poll 단계에서 입력을 넘깁니다.

## 검증

- 중복 등록 후에도 F6 키맵 정확히 1개 유지
- Projection Paint 작업공간에서 Reconnect Poll 성공
- 다른 작업공간에서 Reconnect Poll 실패
- 기존 Mask·Runtime·Snapshot 제한 유지
- 수명주기 14개와 Stencil 자원 25개 회귀 통과

Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사를 통과했습니다. `build/colmap_tools-0.3.26.zip`을 실제 Blender `user_default`에 재설치하고 별도 Blender 프로세스에서 버전 0.3.26, F6 키맵 1개와 작업공간 제한을 확인했습니다. ZIP SHA-256은 `4A7448AB818B8D97DD1F983E79E98300484C771E2D50A4ABCE37C36EC50803AD`, 크기는 28,677,569바이트입니다.
