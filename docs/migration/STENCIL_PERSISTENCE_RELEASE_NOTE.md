# COLMAP Tools 0.3.1 Projection Paint Source Texture 저장·복원

## 원인

Projection Paint 세션 종료 시 Runtime Source Image, Stencil Texture와 Brush 연결은 정상적으로 정리되지만, 현재 Source IMAGE_ID와 사용자가 조절한 Stencil 위치·크기·회전을 별도의 영구 상태로 보존하지 않았습니다. 따라서 종료된 세션 상태로 `.blend`를 저장한 뒤 다시 열면 원본 이미지 경로가 존재해도 이전 Stencil 배치를 복원할 수 없었습니다.

## 변경

- Source IMAGE_ID·이름·절대 경로와 Dataset 식별자를 Scene 영구 속성에 저장합니다.
- Brush의 `stencil_pos`, `stencil_dimension`, Texture Slot 회전을 저장합니다.
- Blender 저장 직전과 Projection Paint 세션 종료 직전에 영구 스냅샷을 갱신합니다.
- 파일 로드 또는 Paint 세션 재진입 뒤 같은 Dataset·Source 경로·IMAGE_ID일 때만 배치를 한 번 복원합니다.
- Runtime Image·Texture·Brush 포인터는 기존처럼 세션 종료 시 정리해 Workspace와 Undo 격리를 유지합니다.

## 검증

- Blender 5.2 Stencil 세션 종료·재진입 회귀
- `.blend` 저장·재열기 영구 속성 회귀
- Mask Stencil, Lifecycle Registry와 Projection Paint Workspace 회귀
- 통합 Extension 구조 검사와 격리 설치본 검사

기존 파일에서 이미 사라진 수동 Stencil 배치는 역으로 복구할 수 없습니다. 0.3.1 설치 후 Source를 연결하고 위치를 조절한 다음 `.blend`를 저장하면 이후 재실행부터 복원됩니다.

Production 패키지는 `build/colmap_tools-0.3.1.zip`이며 SHA-256은 `8E9C029A8121DABCFF6E4DA2BCC9298180EABF08741AFAAC91AAEBF792703B01`입니다. 격리 설치본 전체 검사와 실제 Blender `user_default`의 버전·영구 속성·Save Handler 등록을 확인했습니다.
