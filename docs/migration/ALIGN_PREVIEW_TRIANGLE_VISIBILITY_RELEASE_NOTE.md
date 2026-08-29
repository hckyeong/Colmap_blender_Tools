# COLMAP Tools 0.1.14 Align 완료 후 대표 삼각형 숨김 Release Note

## 변경

- 마커 정렬 미리보기 단계에서는 A/P 대표 삼각형을 기존과 같이 표시합니다.
- Align 계산이 완료되어 ICP 결과 미리보기로 전환되면 대표 삼각형만 숨깁니다.
- 정렬된 점군, P1/P2/P3 마커, 결과 버리기와 정렬 승인 흐름은 변경하지 않습니다.
- 결과를 버려 마커 미리보기 단계로 돌아오면 대표 삼각형도 다시 표시합니다.

## 검증

- 마커 미리보기에서 A/P 삼각형 2개가 렌더링되는지 확인했습니다.
- ICP 완료 결과 미리보기에서 삼각형 렌더링 제출이 0건인지 확인했습니다.
- `SmallGicp` Production 패키지의 격리 설치, Native Runtime 검사와 전체 Workspace GUI 회귀를 통과했습니다.
- 실제 Blender `user_default` 설치본의 버전, 변경 코드, Workspace Seed와 Native Runtime 합성 연산을 확인했습니다.

## 배포물

`build/colmap_tools-0.1.14.zip`의 SHA-256은 `28D6A34050B0C85DA9C2D2365EAF099CA5DA7D048C3617F3D6F501AA5F1A0356`입니다.
