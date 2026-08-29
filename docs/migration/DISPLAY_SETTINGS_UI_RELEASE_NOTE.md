# COLMAP Tools 0.1.12 Point·Camera 표시 UI 통합 Release Note

## 변경

- Modifier의 `Point Display`와 `Camera Display`를 하나의 Box 안에 배치합니다.
- Projection Paint에도 같은 배치를 적용합니다.
- Point Scale과 Camera Frustum Size 사이에는 구분선만 표시합니다.
- 속성 이름, 기본값, 갱신 Callback과 데이터 동기화 동작은 변경하지 않습니다.

## 검증

두 Import 패널이 `point_size`와 `camera_frustum_size`를 같은 `display_box`에 그리는지 확인하고, 기존 Import 및 Lifecycle 회귀를 실행합니다.

## 배포물

`build/colmap_tools-0.1.12.zip`은 `SmallGicp` Production 프로필이며 SHA-256은 `EA842D461E33E539433FEE172BA630142DDB9A595C2C36F1F0FECE212E481304`입니다.
