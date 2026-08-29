# 통합 구현 현황

## 완료

- 루트 Git 저장소와 단일 Blender Extension Manifest 구성
- `shell/`, `tools/`, `shared/`, `tests/`, `docs/`, `build_tools/` 경계 생성
- 원본 세 저장소를 수정하지 않고 Runtime Source와 테스트를 통합 경계로 복사
- Root Shell 단독 `Window → COLMAP Tools` 메뉴 소유
- Align, Modifier, Projection Painting의 독립 등록·해제 유지
- 기존 Operator ID와 Scene Property 이름 유지
- 도구 사이 직접 import 금지 및 공개 식별자 충돌 검사
- 등록 실패 원자적 Rollback과 중복 등록·해제 검사
- Blender 5.2 Manifest 검증, ZIP 생성과 격리 설치본 Smoke Test
- Align `small_gicp` Native Runtime의 Build Staging 포함과 설치본 합성 GICP 검증
- 세 도구의 활성 Workspace 복제 제거와 전용 Blender 5.2 Seed Append 전환
- Tool ID·UUID·Workspace/Seed Schema·Screen Owner 기반 공통 Workspace Factory
- Align 전용 필터 비활성, Modifier·Projection Paint 필터 활성과 Local View 해제 계약
- Legacy 및 CREATING·FAILED Workspace의 소유권·다중 Window 기반 안전 교체
- Source와 격리 설치 ZIP의 전체 Workspace 실행 순서 GUI 행렬 검증
- 다른 COLMAP 도구 진입 및 수동 탭 전환 시 이전 도구의 정식 종료 완료 후 다음 도구를 여는 공용 Workspace Handoff
- 일반 Blender Workspace 탭으로 이동할 때도 직전 COLMAP 도구를 정식 종료하고 사용자가 선택한 탭으로 복귀하는 자유 이동 계약
- Align·Modifier의 기존 강제 Workspace 복귀 비활성화와 Projection Paint 준비 중 강제 복귀 제거
- Align·Modifier 세션의 현재 탭이 아닌 소유 Window 기준 종료와 Projection Paint 준비·세션 종료 후 READY Workspace 유지·재사용
- Projection Paint 준비 중 이탈 시 불완전한 `CREATING` Workspace만 정리하고 `READY` Workspace는 보존하는 상태별 종료
- 이전 실행의 비활성 CREATING·FAILED 대상 Workspace를 공식 삭제하고 Source 복귀를 확인한 뒤 재생성하는 자동 복구
- 고정 COLMAP Fixture의 Modifier 실제 Import·Frame All·Edit·Undo·Export GUI 회귀
- Modifier 관리 Collection을 전용 View Layer에만 포함하고, 전용 View Layer에서는 일반 Scene 데이터를 제외하며, 모든 일반 View Layer에서는 관리 데이터를 제외하는 양방향 가시성 격리
- Modifier 종료·재진입에서 데이터 블록, Modifier 편집 결과, Camera Review Pending·Undo 상태를 보존하는 GUI 회귀
- 이전 일반 Workspace 복귀 확인이 남아 있는 동안의 빠른 Modifier 재진입 요청을 공용 Handoff 대상으로 병합
- Modifier Camera View에서 관리 Background Image를 실제 F7 입력으로 켜고 끄는 전용 Operator·키맵 수명주기
- Modifier와 Projection Paint의 F7 배경 토글을 활성 도구 세션 소유권으로 분리해 다른 도구가 입력을 소비하지 않는 실행 조건
- Scene별 Camera Marker 단일 사용권과 같은 Scene·다른 Window의 동시 점유 차단
- Modifier `review_marker_manifest_json` 기반 Marker Attach·Detach·Rollback과 비활성 Load·Undo 무생성 계약
- Projection Paint `pp_camera_manifest` 기반 Marker Attach·Detach·Rollback과 활성 lease 전용 Camera Guard
- Modifier와 Projection Paint 종료 시 정확 소유 Marker만 제거하고 Camera Object, 편집 결과, Pending·Undo와 Paint 데이터를 보존하는 세션 격리
- Modifier → Projection Paint → Modifier의 동일 Frame 1 실제 Import, Marker 교대, 사용자 Marker와 양쪽 Object 보존 GUI 회귀
- 수동 Projection Paint Workspace 재진입에서도 실제 Runtime 사용권을 다시 획득하는 공용 Handoff 연결
- Extension 해제 시 모든 Scene의 검증된 도구 Marker와 Runtime lease를 정리하는 생명주기
- Align 세션은 Workspace 전환 시 자동 종료하고 N 패널의 중복 수동 종료 버튼은 노출하지 않는 UI 계약
- READY Align Workspace 재진입 시 세션 재연결 전에도 N 패널을 유지하고 입력만 일시 잠그는 UI 계약
- `.blend` 파일 로드로 초기화되는 Workspace Message Bus를 `load_post`에서 다시 구독해 수동 탭 전환을 복구하는 계약
- Modifier와 Projection Paint의 Point Display·Camera Display 설정을 하나의 UI Box로 묶은 공통 배치
- 통합 표시 설정 Box의 Point·Camera 사이 구분선과 추가 세로 여백 제거
- Align의 마커 정렬용 대표 삼각형은 ICP 완료 결과 미리보기부터 숨기고, 점군·마커·승인 흐름은 유지
- Align OBJ의 `vt`와 face-corner UV 인덱스를 Runtime에 불변 보존하고, 정렬 결과 Export·Reflection·왕복 검증·Blender UV Layer 재수입까지 연결
- Align OBJ의 MTL diffuse 재질과 이미지 경로를 Runtime에 보존하고, `COLMAP RGB` 텍스처 표시·생성 MTL·원본 이미지 바이트 복사·SHA-256 왕복 검증까지 연결
- Align OBJ Export를 모델 표시 이름의 OBJ·MTL과 원본 이름 이미지가 한 폴더에 놓이는 구조로 변경하고, 중복 파일명과 Windows 260자 경로 제한을 생성 전에 명확한 오류로 보고

## 현재 Build 성격

`build/colmap_tools-0.1.19.zip`은 `SmallGicp` Production 프로필입니다. 기존 통합·Workspace·세션 격리와 Align OBJ UV·텍스처 왕복 계약에 더해 모델 표시 이름의 OBJ·MTL과 원본 이름 이미지를 같은 Export 폴더에 저장하는 구조, 파일명 충돌 차단과 Windows 경로 길이 사전 진단을 포함합니다. CPython 3.12.10, NumPy 1.26.4와 small_gicp 1.0.1은 `tools/align/runtime/python/`에 포함되며 원본 Repository, 테스트, 문서, Cache와 Build 도구는 ZIP에서 제외됩니다.

- ZIP SHA-256: `5F686587FBA709FFE79024F6DF329AD2B33D426C7B31B6B3CECF452A067781DE`
- Align Seed SHA-256: `2C89AB4890253F52B3B3005794B9FCC8586589222B517F65940626487939A8D2`
- Modifier Seed SHA-256: `00E2D5559293ECAD65D0E225D87CE8655E9D0CE37F1E680DB7B0C0715B730FF3`
- Projection Paint Seed SHA-256: `9553632614C5E7CCAB35EB2115600CC6EED464093464F6A203DE8B53784AECA4`

## 후속 작업

- Open3D 개발 Profile의 별도 배포 여부와 외부 의존성 라이선스 정리
- 사용자 보유 실데이터로 Align → Modifier Import 수치와 실제 화면 가시성 최종 승인
- 사용자 보유 데이터로 Projection Paint Target·Brush·Point 숨김 복구 최종 승인
- 실제 다중 Window Legacy 점유 차단과 `.blend` 저장·재로드 전체 마이그레이션 승인
- Blender 5.2 Append Workspace 공식 삭제 시 발생하는 `SRLayout` 내부 진단의 후속 Blender Version 재검증
- 기존 개별 Extension과 통합 Extension의 동시 설치 정책 확정
- 원격 Repository, 원본 Git Bundle 보관 위치와 첫 통합 Commit 정책 확정

후속 작업은 도구 간 파이프라인이나 공통 Core 도입을 의미하지 않습니다. 세 도구의 독립성은 계속 유지합니다.
