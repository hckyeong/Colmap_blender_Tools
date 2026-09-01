# COLMAP Tools 0.3.8 Projection Paint 외부 Original·Composite 재진입

## 원인

외부 `FILE`·`TILED` Source를 직접 참조하는 `Original Layer` Image는 애드온 소유 태그와 Paint Target UUID를 붙이지 않는 것이 정상입니다. 하지만 Painting 재진입 검사가 이 비소유 Original Image까지 일반 관리 Image처럼 비교하여, 저장·재열기 뒤 활성 Composite를 다른 Target의 데이터로 잘못 판정했습니다.

## 변경 내용

- `Scene.projection_paint.original_source_image`와 같은 읽기 전용 외부 Original 본체만 Paint Target UUID 검사에서 제외합니다.
- 외부 Original에 연결된 관리 Mask는 계속 현재 Paint Target 소유인지 검사합니다.
- 일반 Layer·Mask, Adjustment·Mask, Material, Overlay와 Composite Image의 기존 소유권 검사는 유지합니다.
- 실제로 다른 Target의 활성 Composite가 남은 경우에는 기존처럼 Painting 진입을 차단합니다.

## 검증

- 외부 Original 직접 참조 `.blend` 저장·재열기 뒤 오탐 해제
- 사용자 `Untitled.blend`의 활성 Composite 상태에서 Painting 진입 성공
- 실제 다른 Paint Target의 활성 Composite 차단 유지
- 외부 Original 저장·재열기와 Layer Graph 회귀 16개 통과

Production 소스 회귀 219개와 Blender 등록 검사를 통과했습니다. `build/colmap_tools-0.3.8.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `D423EBD28C22750C63EFD3367C37A71722F92F0AA40B05E01DDBF2BD59EB1188`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.8이 적용됩니다.
