# COLMAP Tools 0.3.4 Projection Paint Original 외부 직접 참조

## 변경 이유

기존 구조는 외부 Source Texture를 관리 `Original Layer` Image로 복제하고 Pack했습니다. 이때 이미 Pack된 빈 관리 Image에 원본 픽셀을 복사한 뒤 다시 Pack해도 Blender가 기존 Packed payload를 교체하지 않아, `.blend` 재열기 뒤 Original 픽셀이 투명하게 복원될 수 있었습니다.

## 변경된 구조

- 외부 `FILE`·`TILED` Source는 별도 Image를 만들지 않고 `Original Layer.image`와 `Scene.projection_paint.original_source_image`가 같은 Blender Image를 참조합니다.
- 원본 Image의 경로·색 공간·Alpha 설정·소유 태그·Packed 상태를 변경하지 않습니다.
- 외부 Original 본체는 읽기 전용입니다. 선택과 이름 변경, 눈 표시, 단독 보기, Mask, Adjustment, Composite 참여는 유지합니다.
- 일반 Paint 스트로크는 상위 관리 Layer에서만 허용합니다. Original의 Mask를 선택하면 Mask Image만 Paint 대상으로 사용합니다.
- 페인트 해상도와 Mask·Composite Bake 해상도는 외부 Original이 아니라 관리 Layer를 기준으로 유지합니다. 따라서 8K 원본과 2K Paint Layer를 함께 사용할 수 있습니다.

## 기존 파일 이관

Schema 1의 관리 복제 Original이 있고 유효한 외부 Source 경로가 남아 있으면 다음 Layer Graph 재구성에서 직접 참조로 원자 이관합니다. Graph 검증이 성공한 뒤에만 과거 관리 복제본을 정리하며, 실패하면 기존 Image Pointer와 Schema를 복구합니다.

외부 파일이 없거나 Source에 저장되지 않은 변경이 있으면 원본이나 Layer Stack을 교체하지 않고 명확한 오류로 중단합니다. 경로 없는 Packed Image, Generated Image, Material Socket Source는 외부 파일을 다시 읽을 수 없으므로 기존 관리 Image 방식으로 유지합니다.

## 검증

- 외부 Source 직접 참조·비소유·비Packed 계약
- 외부 Original 선택 시 Paint Image 비활성화와 Mask Paint 분리
- Schema 1 관리 복제본 자동 이관과 과거 복제본 정리
- 외부 파일 누락 시 비파괴 실패
- `.blend` 저장·재열기 뒤 경로·Image Pointer·원본 픽셀 유지
- SINGLE·UDIM Layer, Mask, Composite 생성·해제 회귀

Production 패키지는 `build/colmap_tools-0.3.4.zip`이며 SHA-256은 `94CF3EB052BB9565FD52F2ED224609144E2B93AC78E716E4DBBB1DD9F74B1802`입니다. 격리 설치본 검사와 실제 Blender `user_default` 설치본 검증을 통과했습니다. 설치 중 열려 있던 Blender GUI는 재시작 뒤 0.3.4를 로드합니다.
