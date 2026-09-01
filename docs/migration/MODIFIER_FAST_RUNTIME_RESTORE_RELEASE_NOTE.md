# Modifier 빠른 Runtime 복원 0.3.33 Release Note

## 변경 내용

- 명시적 COLMAP Import는 기존처럼 Source SHA-256 2회, Reconstruction 전체 무결성 검사와 이미지 경로 검사를 수행한다.
- 저장된 Modifier Runtime의 COLD 복원은 전용 `RuntimeSourceRestoreJob`을 사용한다.
- COLD 복원은 저장된 Source Fingerprint와 현재 Source SHA-256을 한 번 비교한 뒤 COLMAP 원본을 읽는다.
- 원본을 읽는 동안 파일 경로·크기·수정 시각·파일 식별 정보가 달라지면 편집 기록을 적용하지 않고 다시 Import하도록 안내한다.
- 동일 Source에서는 `ReconstructionValidationJob`과 두 번째 전체 SHA-256을 반복하지 않는다.
- 이미지 경로 Manifest, Modifier 관계 인덱스, 저장된 편집 기록, Point3D 순서와 마지막 원자 Commit은 유지한다.
- 저장된 Source Schema 또는 Fingerprint가 없거나 현재 값과 다르면 자동 이전하지 않고 다시 Import하도록 안내한다.
- Projection Painting의 수동 `COLMAP 데이터 업데이트`도 동일한 COLD Runtime 복원 Job을 공유하며 기존 Snapshot은 성공 전까지 유지한다.

## 사용자 경험

처음 데이터를 Import할 때는 기존과 같이 `Reconstruction 무결성 검사 중`이 표시된다. Blender를 다시 열고 같은 원본의 Modifier Workspace에 들어가면 다음 순서로 복원된다.

```text
저장된 원본 확인
→ COLMAP 데이터 읽기
→ 편집 관계 준비
→ 저장된 편집 내용 적용
→ Point3D 순서 준비
→ Modifier Runtime 복원 완료
```

같은 Blender 실행에서 Modifier Workspace에 다시 들어갈 때 Scene Token과 Runtime Cache가 같으면 파일을 다시 읽지 않는다. 원본 COLMAP 파일을 외부에서 바꾸면 이전 편집 기록은 적용하지 않고 다시 Import하라는 원인을 표시한다.

## 검증 결과

- Core 빠른 복원·Scene 연결 신규 검사 12개 통과
- Modifier Core 전체 169개 통과
- Architecture 12개, Coexistence 11개 통과
- Modifier Import·진행률·Export·Camera Review·Point Edit Background 회귀 통과
- Projection Painting 공유 Snapshot·업데이트 진행률 Background 회귀 통과
- 동일 실데이터 Source에서 전체 Import 17.530807초, 빠른 복원 9.553817초로 Source 구간 45.503% 단축
- 두 Loader의 Camera 593, Image 593, Point3D 709,048, Rig 593, Frame 593 결과 일치
- 사용자 `Untitled.blend` COLD 복원에서 `Reconstruction 무결성 검사` 표시 0회, 편집 Revision 1과 Operation 1개 복원 확인
- 편집 적용 뒤 Runtime은 Camera 593, Image 593, Point3D 708,541로 확인

실제 COLD Runtime 전체 121.573661초 중 Source 확인·읽기는 33.138628초, 관계 인덱스 구축은 78.409795초, 편집 기록 재생은 0.051201초, Point3D 순서 준비는 9.236016초였다. 이번 변경의 Source 구간 단축 기준은 통과했으며, 전체 시간의 다음 병목은 중복 무결성 검사가 아니라 관계 인덱스 재구축이다.

## 배포

- Production ZIP: `build/colmap_tools-0.3.33.zip`
- SHA-256: `F0784206037EDCAD91078FDD3D7F441468EA241B406CE02322494F81183212FE`
- 크기: 28,689,146바이트
- 사용자 `user_default` Extension 저장소 재설치 완료
- 격리 설치본 등록·해제, Manifest 버전과 내장 Small GICP Runtime 검사 통과
