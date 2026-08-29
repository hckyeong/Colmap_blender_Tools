# COLMAP Tools 0.1.1 Workspace 격리 Release Note

## 해결한 문제

COLMAP Align Workspace에서 COLMAP Modifier를 열면 Align의 `show_object_viewport_* = False` 상태가 복제되어, Import는 성공했지만 Camera·Image·Point Mesh가 보이지 않던 문제를 구조적으로 제거했다.

## 변경 사항

- Align, Modifier, Projection Paint는 현재 Workspace를 복제하지 않고 각자 Blender 5.2 전용 Seed에서 독립 Workspace와 Screen을 Append한다.
- 이름 대신 Tool ID, UUID, Workspace Schema, Seed Schema와 Screen Owner로 소유권을 판정한다.
- Align의 Object 필터 비활성 정책은 Align 소유 Screen에만 적용된다.
- Modifier와 Projection Paint는 모든 지원 Object 타입 필터를 활성화하고 Local View를 해제한다.
- Legacy, CREATING, FAILED Workspace는 사용자·타 도구 소유권과 다중 Window 점유를 확인한 뒤에만 교체한다.
- Projection Paint 재진입 시 기존 전용 Workspace를 재사용하고 현재 진입 Source 메타데이터를 갱신한다.

## 검증

- 정적·순수 Python 회귀 218개 통과
- 세 Workspace Seed Validator 통과
- Source와 격리 설치 ZIP의 전체 Workspace 실행 순서 GUI 행렬 통과
- 고정 COLMAP Fixture 실제 Modifier Import·Frame All·Edit·Undo·Export 통과
- 설치본 Small GICP 1.0.1 합성 연산 통과

배포 파일은 `build/colmap_tools-0.1.1.zip`이며 SHA-256은 `ECE34B202799B3EA82CDA6BAE72C07699F0ED659A6A2F736A6371E172AF679CA`이다.

## 알려진 Blender 5.2 진단

Append된 Workspace를 공식 삭제할 때 Blender 5.2 Console에 `ID user decrement error: SRLayout`이 출력된다. 최소 공식 API 재현과 저장·재로드 뒤에도 동일하며, 삭제 결과는 `FINISHED`이고 Workspace와 Screen ID는 모두 제거된다. 고아 Screen을 만드는 사용자 수 조작은 적용하지 않았다.

## 최종 수동 승인

- 사용자 실데이터로 Align → Modifier Import 후 Camera 440개, Image 440개, Point3D 563,983개와 실제 Viewport 가시성 확인
- 사용자 실데이터 Projection Paint Target·Brush·Point 숨김 복구 확인
- 실제 다중 Window Legacy 점유와 `.blend` 저장·재로드 마이그레이션 확인
