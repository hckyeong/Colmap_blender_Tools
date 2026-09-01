# COLMAP Tools 0.3.7 Projection Paint COLMAP 데이터 업데이트 진행률

## 원인

`COLMAP 데이터 업데이트`는 Point3D와 Camera 생성을 이미 Modal Timer로 분할하고 실제 완료 작업량도 계산하고 있었지만, 그 값을 화면에 연결하지 않았습니다. 따라서 대형 COLMAP Dataset을 업데이트할 때 작업은 진행 중이어도 사용자는 멈춘 것으로 보였습니다.

## 변경 내용

- 기존 `CameraImportJob.progress_units()`의 실제 완료 작업량을 중앙 진행창에 연결합니다.
- Point3D 버퍼 준비, 메시 생성, 색상 기록, Camera Data와 Camera Object 생성, Scene Commit 단계를 표시합니다.
- 처리량, Camera·Point3D 수, Modifier Revision과 경과 시간을 함께 표시합니다.
- N 패널 상태에도 현재 백분율을 갱신합니다.
- 완료 시 100% 화면을 한 Event Tick 표시한 뒤 진행 UI를 정리합니다.
- 실패나 취소 시 임시 Staging 데이터, Event Timer와 Draw Handler를 함께 제거하고 기존 성공 Snapshot을 유지합니다.

## 검증

Blender 5.2 Background에서 단계별 진행률의 단조 증가, 100% 완료 표시, 독립 Snapshot 최초 생성·재업데이트·Rollback과 기존 Paint 자산 보존을 검증합니다.

Snapshot 진행률 대상 회귀 3개, 독립 Snapshot 최초 생성·재업데이트·Rollback 회귀, Production 소스 회귀 219개와 Blender 등록 검사를 통과했습니다. `build/colmap_tools-0.3.7.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `05A0B450D3B0F9E4971BEBE37CBF01801B913B36FE662A79212AB84614D19379`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.7이 적용됩니다.
