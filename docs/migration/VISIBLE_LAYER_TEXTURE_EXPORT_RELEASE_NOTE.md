# Projection Paint 가시 Layer Texture Export 0.3.28

## 변경 사항

- `Export Texture`의 활성 Composite 선행 조건을 제거했습니다.
- 현재 Base Color Graph에서 눈이 켜진 Layer만 Color와 Alpha 두 채널로 임시 Bake합니다.
- Layer Mask, Adjustment, Adjustment Mask와 Layer 순서를 실제 출력 Pixel에 반영합니다.
- 활성 Composite가 있으면 Composite를 Graph 입력 하나로 취급하고 잠긴 Source 자식은 중복 합성하지 않습니다.
- Layer·Mask 단독 보기는 Export 결과 선택에 사용하지 않으며 Export 전 상태를 그대로 유지합니다.
- 가시 Layer가 하나도 없으면 파일 탐색기 진입 전에 `내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.`를 표시합니다.

## 상태 보존 계약

Export는 영구 Composite Layer를 만들거나 Layer 눈을 변경하지 않습니다. 출력 확정 직전에 Layer UUID·순서·눈 상태, Image Pixel·Dirty 상태, Mask·Adjustment, Composite Metadata, 선택·활성 Layer와 단독 보기 상태를 다시 검증합니다. 실행 중 상태가 바뀌면 기존 출력 파일을 건드리지 않고 임시 결과만 폐기합니다.

## 검증

- Texture Export Blender Background: 23개 통과
- SINGLE Composite Export Blender Background: 4개 통과
- Layer Shader Blender Background: 47개 통과
- Composite Blender Background: 61개 통과
- 합계: 135개 통과

실제 출력 Pixel로 일반 Layer, 기존 Composite, 눈 상태 필터, Color·Alpha, Layer·Adjustment Mask, 단독 보기, 취소·실패 정리와 Snapshot 불일치를 검증했습니다.

Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사를 통과했습니다. `build/colmap_tools-0.3.28.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.28과 `REGISTER` 전용 Export Operator를 확인했습니다.

사용자 `Untitled.blend`를 저장하지 않고 설치본 GUI로 열어 8192×8192 SINGLE `Layer 1`과 `Original Layer · Texture`를 Composite 생성 없이 내보냈습니다. 58.19초 동안 83,821,267바이트 PNG 한 장을 생성했으며 Layer Tree·눈·선택·활성 대상·단독 보기·Composite 상태가 전후 동일했습니다. 검사용 출력은 확인 뒤 자동 삭제했습니다.

Production ZIP SHA-256은 `066768971DB78FFBEB426562276CC48AFDA207DDBF17CD4F61F1807B77D1C3EF`, 크기는 28,679,434바이트입니다.
