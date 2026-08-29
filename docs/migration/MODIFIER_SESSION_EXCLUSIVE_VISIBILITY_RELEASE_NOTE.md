# COLMAP Tools 0.1.15 Modifier 세션 관리 데이터 단독 표시 Release Note

## 변경

- Modifier 전용 View Layer에서는 COLMAP Modifier가 소유한 관리 Root Collection만 포함합니다.
- 일반 Scene Collection과 Scene Root에 직접 연결된 일반 Object는 Modifier 전용 View Layer에서만 숨깁니다.
- 일반 Blender View Layer에서는 기존처럼 Modifier 관리 데이터만 제외하고 사용자 데이터는 그대로 표시합니다.
- Object와 Collection 데이터 블록, 전역 `hide_viewport`와 일반 View Layer의 개별 표시 상태는 변경하지 않습니다.
- Modifier 세션 종료와 재진입, Import 교체, 파일 Load와 Undo·Redo 뒤에도 같은 가시성 경계를 다시 동기화합니다.

## 검증

- Background 회귀에서 Modifier 전용 View Layer의 관리 Point·Camera만 표시되고 사용자 Collection과 Scene Root Object는 표시되지 않는지 확인했습니다.
- 일반 View Layer와 새 사용자 View Layer에서는 사용자 Object가 계속 표시되고 Modifier 관리 데이터만 제외되는지 확인했습니다.
- 실제 소형 COLMAP Fixture의 Modifier 진입, Import, Camera Review, F7, Point Edit, Global Undo, BIN Export, 종료와 재진입 GUI 회귀를 통과했습니다.
- `SmallGicp` Production 패키지의 격리 설치, Native Runtime 검사와 전체 Workspace GUI 회귀를 통과했습니다.
- 실제 Blender `user_default` 설치본에서도 같은 Modifier 전체 GUI 회귀와 Native Runtime 검사를 통과했습니다.

## 배포물

`build/colmap_tools-0.1.15.zip`의 SHA-256은 `CEE251BF94B4E04DBE0B6E4A000490674CCC53EA8529862B854627E5B0042640`입니다.
