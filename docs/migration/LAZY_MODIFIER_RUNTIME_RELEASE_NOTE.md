# COLMAP Tools 0.3.9 Modifier 지연 Runtime·Projection Paint 독립 재진입

## 변경 내용

- `.blend` 로드에서는 Modifier의 대용량 COLMAP 원본을 자동으로 다시 해석하지 않습니다.
- Modifier Runtime은 Modifier Workspace 진입 또는 Projection Paint의 `COLMAP 데이터 업데이트` 요청에서만 복원합니다.
- Modifier Workspace UI와 Projection Paint 업데이트 진행창을 먼저 표시하고, 원본 검사·해석·검증·관계 인덱스·편집 이력·전송값 생성을 UI Tick 단위로 진행합니다.
- 같은 Scene의 Modifier Workspace와 Projection Paint 업데이트는 동일한 Runtime 복원 Job을 공유합니다.
- Projection Paint는 기존 READY Snapshot으로 독립 재진입하며, 사용자가 업데이트 버튼을 누를 때만 Modifier 값을 새 Blender ID로 복사합니다.
- 업데이트 취소·실패·Source token 변경 시 기존 Snapshot, Paint Target, Layer, Texture pixel을 그대로 유지합니다.
- 저장된 Point·Image 제거 이력은 전체 관계 인덱스를 다시 만들지 않고 영향받은 항목만 copy-on-write로 갱신합니다.
- 70만 Point Snapshot 지문은 기존 SHA-256 결과를 유지하면서 레코드 단위로 증분 계산합니다.
- Properties의 `OBJECT` context가 없는 Blender 5.2 환경, Paint 진입 Timer의 `TypeError`, 활성 Brush가 없는 상태와 실제 Runtime 없는 재접속을 정식 오류·복구 경로로 처리합니다.

## 실제 데이터 검증

사용자 `Untitled.blend`의 Camera 593개, Image 593개, Point3D 708,541개와 Point 제거 이력 1건을 사용했습니다.

- 파일 로드: 약 0.69초
- 로드 직후 Modifier Runtime Cache: 없음
- 로드 직후 자동 Runtime 복원 Timer: 없음
- COLD Modifier Runtime → 값 Snapshot 준비: 약 28.58초 동안 진행률과 함께 분할
- COLD Runtime → Projection Paint Scene Commit 전체 업데이트: 약 32.41초
- 최장 단일 처리 Tick: 기존 약 7.78초에서 약 1.10초로 감소

남은 약 1.10초 최대치는 Python 대용량 객체의 세대별 메모리 회수 구간입니다. 원본 해석, 관계 인덱스, 편집 이력과 Snapshot 지문은 작업 진행 중 여러 Tick으로 나뉩니다.

## 호환성

- 기존 Modifier 공개 제거 API와 편집 History Schema를 유지합니다.
- 기존 Projection Paint READY Snapshot과 저장된 Paint Layer 자원을 그대로 읽습니다.
- Align과 Small GICP Runtime, OBJ Import·Export 동작은 변경하지 않습니다.

## 검증

- Modifier Core 157개
- Projection Paint Core 40개
- Shared 계약 8개
- Architecture 12개
- 실제 Blender의 비활성 load 무복원, Modifier 지연 복원, COLD Projection Paint 업데이트, 취소·오류 Rollback, Undo 격리, Point·Camera 편집 회귀

## 배포 검증

- Production ZIP: `build/colmap_tools-0.3.9.zip`
- ZIP SHA-256: `61E44BE0B101A95A34D33DA4AE9E55CB769AFA8E675CB5A3CC21CDBD7F9E3065`
- ZIP 크기: 28,666,881바이트
- 격리 설치본: Extension 등록, Snapshot Operator, Small GICP Runtime과 OBJ UV 왕복 검증 통과
- 실제 `user_default` 설치본: 0.3.9 재설치와 설치본 계약 검증 통과
- 실제 `Untitled.blend` GUI: 기존 Paint Target·Layer 3개를 유지한 Painting Mode 재진입과 Stencil Texture 재연결 통과(약 2.72초)
