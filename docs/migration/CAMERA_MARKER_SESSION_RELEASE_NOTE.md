# COLMAP Tools 0.1.8 Camera Marker 세션 격리 Release Note

## 해결한 문제

Modifier의 Camera Review Marker와 Projection Paint의 Camera Marker는 Workspace 데이터가 아니라 `Scene.timeline_markers`에 저장된다. 따라서 Modifier Workspace를 종료해도 Marker가 Scene에 남았고, Projection Paint가 같은 Frame 1부터 Camera를 Import할 때 외부 Camera Marker 충돌로 중단됐다.

## 변경 내용

- Scene별 Camera Marker 사용권을 추가해 Modifier와 Projection Paint가 동시에 Marker를 올리지 못하게 한다.
- Modifier를 열면 `review_marker_manifest_json`에서 Modifier Marker를 복원하고, 종료하면 정확히 일치하는 Marker만 제거한다.
- Projection Paint를 열면 `pp_camera_manifest`에서 Projection Marker를 복원하고, 종료하면 정확히 일치하는 Marker만 제거한다.
- 일반 Blender Workspace에서는 두 도구의 실제 Camera Marker를 모두 내린 상태로 유지한다.
- Camera Object, Point Object, Paint Layer, 편집 결과, Pending·Undo와 두 Marker Manifest는 삭제하지 않는다.
- 비활성 도구의 Load·Undo·Camera Guard는 Marker를 다시 만들지 않는다.
- 사용자 Marker와 소유권이 모호한 Marker는 자동으로 삭제하지 않고 충돌을 명확히 보고한다.
- 같은 Scene을 표시하는 다른 Blender Window가 Marker 사용권을 동시에 얻는 것을 차단한다.
- 파일 Load와 Extension 해제 시 Runtime 사용권과 검증된 도구 Marker를 정리한다.

## Workspace 전환 결과

| 전환 | Scene에 남는 Camera Marker |
| --- | --- |
| Modifier → Projection Paint | Projection Paint Marker만 존재 |
| Projection Paint → Modifier | Modifier Marker만 존재 |
| Modifier → 일반 Workspace | 두 도구 Marker 없음 |
| Projection Paint → 일반 Workspace | 두 도구 Marker 없음 |

## 데이터 보존

실제 Timeline Marker는 활성 세션의 실행 사본으로 취급한다. 영구 원본은 기존 Manifest이므로 Workspace를 다시 열면 IMAGE_ID, Frame과 Camera 연결을 그대로 복원한다. Marker를 내리는 동작은 COLMAP 데이터나 편집 결과를 삭제하는 동작이 아니다.

## 회귀 검증

- 공용 Scene Marker 사용권의 단일 소유, 멱등 획득·반납과 다중 Window 차단
- Modifier Marker Attach·Detach·Rollback, 비활성 Load·Undo 차단과 GUI Pending Undo 재진입
- Projection Paint Marker Attach·Detach·Rollback, 비활성 Guard 차단과 Extension 해제
- Align → Modifier → Projection Paint → Align 및 수동 Workspace 탭 전환의 사용권 순서
- 사용자 Camera Marker 충돌의 Fail Closed 유지
- Production ZIP의 통합 Source, Workspace Seed와 Small GICP 내장 Runtime 검사

## 배포 파일

`build/colmap_tools-0.1.8.zip`은 `SmallGicp` Production 프로필이다. SHA-256은 `7DE16D82199AA5DA6A1ABF838A03F70003C5CE572EA8B7CF23982BE4818A5547`이다.
