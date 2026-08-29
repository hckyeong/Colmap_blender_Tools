# COLMAP Tools 0.1.2 Workspace 자동 전환 Release Note

## 해결한 문제

활성 COLMAP Modifier 세션을 종료하지 않고 Align을 열면 두 도구의 비동기 Workspace 전환 Timer가 같은 Window를 동시에 소유했다. Align 진입은 활성화 확인에 실패해 `FAILED` Workspace를 남겼고, 다음 시도에서는 비활성 실패 Workspace를 동기 삭제할 수 없어 `COLMAP Align 소유 작업공간을 안전하게 교체하지 못했습니다.` 오류가 발생했다.

## 변경 내용

- 공용 `Workspace Handoff`는 도구별 구현을 직접 Import하지 않고 각 도구가 등록한 시작·종료·완료 판정 계약만 조정한다.
- 다른 COLMAP 도구를 열면 현재 도구의 정식 종료를 먼저 한 번 요청하고, Source Workspace 복귀와 런타임 정리가 확인된 다음 UI Tick에 대상 도구를 시작한다.
- 활성 Workspace 탭을 직접 다른 COLMAP Workspace로 바꾼 경우에도 같은 자동 종료·진입 순서를 적용한다.
- 둘 이상의 이전 세션이 남은 손상 상태에서는 현재 활성 도구부터 하나씩 종료해 Window 전환 충돌을 방지한다.
- Align과 Modifier는 수동 탭 전환 뒤에도 현재 탭이 아니라 세션이 소유한 Window를 기준으로 안전하게 종료할 수 있다.
- Projection Paint는 준비 작업, 활성 Paint 세션, Source 복귀와 전용 Workspace 지연 삭제까지 완료된 뒤 다음 도구를 시작한다.
- 파일 로드 전에는 대기 중인 Window Pointer와 Handoff Timer를 폐기한다.
- 이전 버전에서 남은 비활성 `CREATING`·`FAILED` 대상 Workspace는 활성화, 공식 삭제, Source 복귀 완료를 UI Tick별로 확인한 뒤 새 Workspace를 생성한다.

## 회귀 검증

- 정적 구조·공존·Modifier Core·Projection Paint Core·Align 계약 검사 통과
- Blender Background 통합 등록·해제, Workspace Factory, Modifier Workspace와 Projection Paint Workspace 검사 통과
- Source GUI에서 종료 버튼 없는 Align → Modifier → Projection Paint → Align, 수동 Align → Modifier, Modifier → Align 전환과 최종 Source 복귀 통과
- 자동 전환 뒤 Align·Modifier 세션은 동시에 활성화되지 않고 `FAILED` COLMAP Workspace가 남지 않음을 확인
- 기존 Align Workspace를 의도적으로 `FAILED`로 만든 뒤 Modifier → Align 전환에서 잔여본 삭제 완료 후 새 Align이 생성됨을 확인

## 배포 파일

`build/colmap_tools-0.1.2.zip`의 SHA-256은 `6E95EAA94C30AB9345B8E126E05D14948EC8C0470A77553D896D87C479218D10`이다. 격리 설치본에서 Manifest, 세 진입 Operator, Scene 속성, Workspace Seed, 내장 Small GICP 합성 연산과 전체 자동 전환 GUI 회귀를 통과했다.

Blender 5.2의 공식 Append Workspace 삭제 중 출력되는 `SRLayout` 내부 진단은 0.1.1과 동일하며, 삭제 결과와 Workspace·Screen ID 제거는 정상이다. 사용자 수를 직접 조작하는 우회는 추가하지 않았다.
