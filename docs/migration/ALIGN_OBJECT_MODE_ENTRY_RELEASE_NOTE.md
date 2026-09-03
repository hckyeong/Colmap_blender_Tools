# Align 진입 Object Mode 자동 전환 0.3.36

## 변경 내용

- Align 진입 시 현재 Blender 모드가 Edit, Sculpt, Texture Paint 등 `OBJECT`가 아니면 Object Mode로 자동 전환합니다.
- Modifier 또는 Projection Painting에서 넘어오는 경우에는 기존 도구의 종료가 완료된 다음 Align 작업공간을 만들기 직전에 전환합니다.
- 현재 Window·Screen·View Layer와 활성 Object를 검증하고, 가능한 경우 같은 화면의 3D View 컨텍스트에서 Blender 기본 `object.mode_set`을 실행합니다.
- 활성 Object나 실행 가능한 컨텍스트가 없으면 Align 작업공간을 일부 생성하지 않고 현재 모드를 포함한 오류를 표시합니다.

## 사용자 경험

1. 사용자가 Edit Mode, Sculpt Mode 또는 Texture Paint Mode에서 `COLMAP Align 열기`를 실행합니다.
2. 활성 COLMAP 도구가 있다면 해당 도구의 세션 종료와 데이터 정리를 먼저 완료합니다.
3. Blender가 현재 활성 Object를 Object Mode로 전환합니다.
4. 전환 성공 뒤에만 Align 전용 작업공간과 세션을 준비합니다.

데이터, 선택 Mesh와 Paint Image는 삭제하지 않으며 Blender의 모드만 Object Mode로 변경합니다.

## 검증

- Blender Background에서 Edit·Sculpt·Texture Paint → Object Mode 전환
- Source와 실제 설치본의 숨김 Blender GUI에서 Edit Mode → 실제 Align 작업공간 생성 → 활성 세션 진입
- Production 전체 검사, 격리 설치본 검사와 실제 Blender `user_default` 설치 버전 `0.3.36` 확인

Production ZIP은 `build/colmap_tools-0.3.36.zip`이며 SHA-256은 `22F4CC3907759029181DE52A5A26D92B1A19D863A8DA8D5849704C5082C45542`, 크기는 28,731,889바이트입니다.
