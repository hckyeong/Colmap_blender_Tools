# Projection Paint 진행 안내창 문자열 경계 0.3.32

## 원인

공통 진행 안내창의 폭은 최대 520픽셀이지만 BLF 문자열을 그릴 때 사용할 수 있는 내부 폭을 검사하지 않았습니다. Texture Export Native Bake가 Device·Image·버퍼 정보를 한 줄에 표시하면서 오른쪽 경계를 넘어갔습니다.

## 변경

- Device 정보와 Image·버퍼·Bake·Samples·경과 정보를 별도 줄로 재배치했습니다.
- 제목, 단계와 상세 문구를 그리기 전에 Blender BLF의 실제 픽셀 폭을 측정합니다.
- 내부 폭을 넘는 문자열만 말줄임표로 축약하고 짧은 문자열은 그대로 유지합니다.
- 3D Viewport가 좁아져 안내창 폭이 줄어드는 경우에도 같은 경계 검사를 적용합니다.

## 유지한 계약

- 안내창 최대 폭과 높이, 배경 투명도, 글꼴 크기와 상세 줄 수를 변경하지 않았습니다.
- Texture Export Native Bake 상태 전이, 입력 차단, `Esc` 취소와 Layer·Paint 상태 보존 계약을 변경하지 않았습니다.
- 공통 진행창을 사용하는 다른 작업의 단계와 상세 정보 생성 구조를 변경하지 않았습니다.

## 검증

- Texture Export Blender Background 34개
- Composite Blender Background 61개
- 긴 Device 문구의 BLF 픽셀 폭과 말줄임표 회귀
- 설치본 GUI의 실제 안내창 Screenshot과 창 내부 문자열 경계 검사

사용자 `Untitled.blend`에서 8192×8192, NVIDIA GeForce RTX 4090 Device 문구를 사용했습니다. 안내창 내부 가용 폭은 476픽셀이고 가장 긴 상세 문구는 381픽셀로 측정되어 모든 줄이 경계 안에 표시됐습니다. 원본 `.blend`는 저장하지 않았습니다.

Production 패키지는 `build/colmap_tools-0.3.32.zip`이며 SHA-256은 `B64BC89951D83D94E7325C9800CC6687EFD4B2E6069893B5C0FF2233D8F41E65`, 크기는 28,683,726바이트입니다. 실제 Blender `user_default` 재설치와 격리 설치본 검사를 통과했습니다.
