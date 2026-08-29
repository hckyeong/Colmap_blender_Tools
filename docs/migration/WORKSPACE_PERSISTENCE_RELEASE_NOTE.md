# COLMAP Tools 0.1.3 Projection Paint Workspace 유지 Release Note

## 변경 내용

- Projection Paint 정상 종료 시 Paint 상태와 사용자 상태를 복구하고 Source Workspace로 돌아간다.
- 정상 종료한 Projection Paint Workspace는 삭제하지 않고 `READY` 상태와 Scene Pointer를 유지한다.
- 다음 Projection Paint 진입은 기존 Workspace와 Screen을 재사용하고 현재 Source 메타데이터를 갱신한다.
- 정상 종료 삭제 호출은 코드에 비활성화 사유와 함께 주석으로 보존한다.
- 생성 실패, 손상, 구버전, `CREATING`·`FAILED` 잔여 Workspace의 안전 삭제 계약은 유지한다.

## 검증

- 정적 구조 11개와 Projection Paint Core 40개 통과
- Projection Paint Workspace Background 9개와 Paint Resources 43개 통과
- 통합 Extension 등록·해제 검사 통과
- Source와 격리 설치본 및 실제 사용자 설치본에서 `Paint → Align → Paint` 동일 Workspace 재사용 GUI 회귀 통과
- 실제 사용자 Blender에서 `bl_ext.user_default.colmap_tools` 버전 `0.1.3`, 활성화 상태 확인

## 배포 파일

`build/colmap_tools-0.1.3.zip`의 SHA-256은 `7376B52764DD8572E47B4D4842D4AB3A7C77C46D2D2DAACF0B5293479D37EDB0`이다.
