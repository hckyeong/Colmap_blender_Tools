# COLMAP Tools 0.1.4 Workspace 자유 이동 Release Note

## 변경 내용

- Window별 직전 Workspace를 공통 Handoff가 기억한다.
- 활성 COLMAP 도구에서 일반 Blender Workspace를 선택하면 이전 도구의 정식 종료가 끝난 뒤 사용자가 선택한 Workspace를 다시 표시한다.
- 다른 COLMAP Workspace 탭을 직접 선택하면 이전 도구를 먼저 종료하고 선택한 도구를 시작한다.
- Align과 Modifier의 기존 Workspace 강제 복귀 호출을 비활성화했다.
- Projection Paint 준비 Timer가 사용자의 Workspace 선택을 덮어쓰지 않도록 강제 복귀를 제거했다.
- 정상 종료한 `READY` Workspace와 Import 데이터·Modifier·Scene 데이터는 보존한다.
- 준비 중 이탈한 Projection Paint의 `CREATING` Workspace만 공식 삭제 경로로 정리한다.
- Message Bus 전환 콜백은 Blender Undo 명령을 실행하거나 Undo Stack을 초기화하지 않는다.

## 검증

- 정적 구조 11개, 공존 식별자 3개, Modifier Core 155개, Projection Paint Core 40개 통과
- 통합 Extension 등록·해제 검사 통과
- Projection Paint Workspace Background 10개와 Lifecycle Registry 10개 통과
- Source Blender GUI에서 COLMAP 도구 간 전환, 일반 Workspace 직접 이동, READY 재사용과 FAILED 자동 교체 통과
- 격리 설치본에서 Native Runtime·등록·Seed·전체 Workspace GUI 회귀 통과
- 실제 사용자 Blender에서 `bl_ext.user_default.colmap_tools` 버전 `0.1.4`, 활성화 상태 확인
- 실제 사용자 설치본에서 일반 Workspace 자유 이동을 포함한 전체 Workspace GUI 회귀 통과

## 배포 파일

`build/colmap_tools-0.1.4.zip`의 SHA-256은 `006F34BFD980D9F00EA1FFBE72587E0388B67D942DC1677E861A62B0239E3039`이다.
