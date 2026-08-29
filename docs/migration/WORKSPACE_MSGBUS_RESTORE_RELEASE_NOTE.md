# COLMAP Tools 0.1.11 파일 로드 후 Workspace 전환 복구 Release Note

## 원인

Blender는 `.blend` 파일을 불러올 때 모든 Message Bus 구독을 초기화합니다. 공용 Workspace Handoff는 `load_post`에서 Window와 Workspace 포인터만 다시 기록하고 구독을 복원하지 않아, 파일 로드 이후의 수동 Workspace 탭 이동을 감지하지 못했습니다.

그 결과 Align Workspace 화면은 열렸지만 이전 Modifier 세션은 `ACTIVE`로 남고, Align 세션은 시작되지 않아 N 패널에 `Align 세션 연결 중`만 계속 표시될 수 있었습니다.

## 변경

- 공용 Workspace 변경 구독을 독립 함수로 분리했습니다.
- Extension 최초 등록과 모든 `load_post`에서 같은 구독 함수를 실행합니다.
- 파일 로드 후 새 Window·Workspace 포인터를 기록하기 전에 Message Bus를 다시 구독합니다.
- 전환 실패 시 Align N 패널에 일반 연결 문구 대신 실제 오류 내용을 표시합니다.

## 검증

GUI 회귀에서 활성 Modifier 세션 상태로 Message Bus 구독을 명시적으로 초기화하고 `load_post` 복구를 실행합니다. 이후 기존 READY Align Workspace 탭을 직접 선택해 Modifier 세션이 종료되고 Align 세션이 자동 활성화되는지 확인합니다.

Blender의 Message Bus 파일 로드 제한은 [Blender Python API 문서](https://docs.blender.org/api/main/bpy.msgbus.html)에 정의돼 있습니다.

## 배포물

`build/colmap_tools-0.1.11.zip`은 `SmallGicp` Production 프로필이며 SHA-256은 `8B36C04D8DC76299D38183796C1D1442051D843192F4EBF32A8CAB1E99283DCD`입니다.
