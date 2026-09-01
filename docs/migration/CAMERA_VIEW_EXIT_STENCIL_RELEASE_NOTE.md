# Projection Paint Camera View 이탈 Stencil 보장 0.3.24

## 변경 이유

Camera Frame Source는 빠른 Timeline 이동에서 마지막 입력 후 0.30초를 기다립니다. 기존 View 추적은 이 대기 상태를 Camera View 이탈보다 먼저 검사해, 대기 중 Viewport를 회전하면 Stencil Texture 연결 해제를 건너뛸 수 있었습니다.

## 변경 내용

- Camera View 이탈 여부를 Frame Source 상태보다 먼저 확인합니다.
- Camera View 밖이면 기존 Source Image와 Paint 상태는 유지하고 Draw Brush의 Stencil Texture 연결만 해제합니다.
- 일반 Layer 복귀 시 자동 재연결 없이 Camera View Timer를 다시 등록합니다.
- 같은 Projection Paint 작업공간의 일시적인 활성 Target 전환과 Mask·수동 해제 상태에서는 Timer를 저빈도로 유지합니다.
- Projection Paint 작업공간을 실제로 벗어나면 기존과 같이 해당 Timer를 종료합니다.

## 보존되는 상태

- 활성 Layer와 Paint Target
- Frame Source Image와 IMAGE_ID
- Draw Brush의 비Stencil 설정
- Mask 편집 상태
- Blender 기본 Paint 도구 선택

## 검증

- Frame Source 대기 중 Camera View 이탈 즉시 Stencil 연결 해제
- 일반 Camera View 이탈·복귀와 이동·확대
- Mask 선택·복귀 중 자동 재연결 금지
- 일반 Layer 선택 뒤 Camera View Timer 재등록
- 같은 작업공간의 일시적인 활성 Target 전환 뒤 Timer 유지
- Camera Review, Layer Shader, Paint Tool Bridge와 Stencil 자원 회귀

Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사를 통과했습니다. `build/colmap_tools-0.3.24.zip`을 실제 Blender `user_default`에 재설치하고 별도 Blender 프로세스에서 설치 버전 0.3.24를 확인했습니다. ZIP SHA-256은 `73D4C9F9A7F2002074885ECDB40AC9E3514E09AC1EAB98CEEC9E52309D7ABFDA`, 크기는 28,677,329바이트입니다.
