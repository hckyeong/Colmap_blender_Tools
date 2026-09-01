# Projection Paint Texture Export Native Bake·진행률 UI 0.3.31

## 원인

Texture Export는 `BLOCKING` Modal Operator였지만 첫 Timer에서 Color·Alpha Bake를 `EXEC_DEFAULT`로 연속 실행했습니다. 두 Bake가 끝날 때까지 Blender 이벤트 루프가 Export에 돌아오지 않아 `Preparing 0.0%` 화면이 고정되고 창이 멈춘 것처럼 보였습니다.

## 변경 사항

- GUI Color·Alpha Bake를 각각 `INVOKE_DEFAULT` Native `OBJECT_BAKE` Job으로 실행합니다.
- Modal Timer가 Blender Job 실행 상태와 `object_bake_pre`, `object_bake_complete`, `object_bake_cancel` 결과를 함께 확인합니다.
- Color → Alpha → Combining → Writing → Staging → Committing을 독립 `TextureExportJob` 상태로 관리합니다.
- Bake 중 Composite 생성과 같은 공통 반투명 진행창에서 `Process.` 애니메이션, 단계, 장치, 해상도, EMIT, Samples 1과 경과 시간을 표시합니다.
- Bake 종료 확인 전용 틱에서도 활동형 화면을 유지해 가짜 백분율이 잠깐 나타나지 않게 했습니다.
- 일반 입력은 계속 차단하고 `Esc`만 Blender Native Bake 취소로 전달합니다.
- Bake Job이 완전히 멈춘 뒤에만 임시 Node·Image, 렌더·선택 상태와 파일 Transaction을 정리합니다.
- 종료 과정에서 Blender가 Modal Operator를 먼저 제거해도 남은 참조를 안전하게 폐기합니다.

## 유지되는 계약

- 눈이 켜진 실제 Layer Graph와 Mask·Adjustment·Alpha 결과
- Layer 눈, 선택, 활성 Layer, Paint Target과 Brush 상태 불변
- Background·테스트의 동기 `EXEC_DEFAULT` Export
- 임시 Staging, 기존 출력 Backup, 원자 Commit과 실패 Rollback
- Pixel Hash와 저장 직전 전체 Snapshot 재검사 미사용
- Bake 엔진, EMIT, Samples 1, Margin과 출력 픽셀 결과

## 검증

- Texture Export Background: 33개 통과
- Composite: 61개 통과
- Lifecycle: 14개 통과
- SINGLE Composite Export: 4개 통과
- Production 검사: 220개 통과
- Small GICP 내장 Runtime과 격리 설치본 검사 통과

사용자 `Untitled.blend`의 8192×8192 SINGLE 데이터에서 성공 Export, Color Bake `Esc` 취소와 Alpha Bake `Esc` 취소를 실제 GUI로 검증했습니다. 성공 경로는 38.41초 동안 Native Bake 진행 Timer가 계속 갱신됐고 78,810,870바이트 PNG를 생성했습니다. 두 취소 경로는 출력 없이 Layer·Paint 상태와 임시 Image를 복구했습니다. 원본 `.blend`는 저장하지 않았습니다.

Production 패키지는 `build/colmap_tools-0.3.31.zip`이며 SHA-256은 `A5E4E9AF34EB89E19F921D758BCC3C7C7162C1DCFD63250658516E2B990F30A3`, 크기는 28,683,362바이트입니다. 실제 Blender `user_default`에 재설치했습니다. 이미 열려 있던 Blender는 재시작한 뒤 0.3.31 Runtime을 사용합니다.
