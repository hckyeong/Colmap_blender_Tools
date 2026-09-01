# Projection Paint Texture Export 입력 차단·Pixel Hash 제거 0.3.30

## 원인

Texture Export는 실행 중 Layer 변경을 감지하기 위해 시작과 파일 확정 직전에 모든 Layer Image Pixel을 복사하고 SHA-256 서명을 계산했습니다. 8192×8192 RGBA Float Image 두 장에서는 한 번에 약 2 GiB를 읽었고 같은 검사를 두 번 반복해 Export 시작과 종료에 큰 지연이 발생했습니다.

기존 Modal Operator는 타이머가 아닌 입력을 `PASS_THROUGH`로 넘기고 있어 사용자가 Export 단계 사이에 Layer를 변경할 수 있었습니다.

## 변경 사항

- 파일 선택 이후 Texture Export Operator에 `BLOCKING` 계약을 적용했습니다.
- Export 중 `Esc` 취소를 제외한 마우스·키보드 입력을 Blender의 다른 Operator로 전달하지 않습니다.
- Modal Timer Event만 다음 Export 작업 단계를 진행합니다.
- 전체 Image Pixel SHA-256 생성과 파일 확정 직전 Snapshot 재검증을 제거했습니다.
- 임시 합성, Staging 저장, 기존 출력 Backup, 전체 파일 원자 확정과 실패 Rollback은 유지했습니다.

## 사용자 경험

1. 사용자가 Export 경로와 옵션을 선택합니다.
2. Export가 시작되면 진행 Overlay가 표시되고 다른 Blender 조작은 잠깁니다.
3. 사용자는 `Esc`로 Export를 취소할 수 있습니다.
4. 완료·취소·실패 후 입력 잠금과 진행 Overlay가 함께 해제됩니다.

## 검증

- Texture Export Blender Background 회귀 24개 통과
- SINGLE Composite Export Blender Background 회귀 4개 통과
- `BLOCKING` Operator 옵션과 비타이머 입력 소비 확인
- 제품 Export 코드의 `sha256`, `pixels.foreach_get`, 구형 Export Snapshot 함수 부재 확인

Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 실제 `user_default` 설치본 검사를 통과했습니다. 설치본에서 버전 0.3.30, `BLOCKING` Operator와 Pixel Hash 코드 부재를 확인했습니다.

사용자 `Untitled.blend`의 8192×8192 SINGLE `Layer 1`과 `Original Layer · Texture`를 실제 설치본으로 내보냈습니다. 전체 시간은 기존 60.03초에서 36.74초로 23.28초 단축됐고, 초기 준비는 11.71초에서 0.0003초로 줄었습니다. 검사용 78,810,870바이트 PNG는 확인 뒤 삭제했으며 원본 `.blend`는 저장하지 않았습니다.

Production ZIP은 `build/colmap_tools-0.3.30.zip`, SHA-256은 `C53DA7763D88C12DD71A450531EE4E0733C0D3C0433D2C08F9643516BB6ED786`, 크기는 28,677,794바이트입니다.
