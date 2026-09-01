# COLMAP Tools 0.3.12 Projection Paint Composite Clone Slot 연결

## 원인

Composite 이후 만든 일반 Layer를 선택할 때 `ImagePaint.clone_image`만 Composite로 바꾸고, Blender 3D Clone 브러시가 실제로 읽는 `Material.paint_clone_slot`은 기존 Original Texture 슬롯에 남아 있었습니다. 따라서 UI와 내부 Pointer는 Composite를 표시해도 실제 Stroke는 모든 Source Layer를 합성한 결과를 복제하지 못했습니다.

## 변경 내용

- 현재 편집 Layer는 기존처럼 `Material.paint_active_slot`에 유지합니다.
- Clone Source는 활성 Composite Image가 있는 `Material.paint_clone_slot`으로 별도 지정합니다.
- Object Mode처럼 `texture_paint_images`가 아직 준비되지 않은 시점에는 `clone_image`만 보존하고, Texture Paint 진입 및 의존 Layer 선택 시 실제 Clone Slot 연결을 완료합니다.
- 기존 0.3.11 `.blend`에서 활성 Composite를 이어서 사용할 때는 처음 Clone Slot을 연결하기 직전의 슬롯을 이전 값으로 저장합니다.
- Composite 생성 실패, 다시 생성 실패와 해제 시에는 기존 Clone Image, Clone 사용 여부와 Clone Slot을 함께 복구합니다.

## 동작 범위

Clone Source가 되는 Composite는 생성 당시 선택한 Source 범위 중 눈이 켜진 모든 Layer와 각 Layer의 Mask·Adjustment 결과를 평면화한 이미지입니다. Composite 이후 새로 만든 Clone Layer 자체는 재귀 복제를 막기 위해 Source 합성에서 제외됩니다. 기존 Source를 수정한 뒤 최신 결과를 Clone하려면 `Composite 다시 생성`을 실행해야 합니다.

## 검증

- Composite 생성·해제·재합성·실패 복구 회귀 60개
- Layer·Mask 선택과 Paint Slot 회귀 47개
- 실제 Texture Paint Mode에서 편집 슬롯과 Clone 슬롯이 서로 다른 Image를 가리키는지 검증
- Production 패키지 격리 설치본과 실제 Blender `user_default` 설치본 검사

사용자 `Untitled.blend`를 설치본으로 저장 없이 열어 `Layer 2` 편집 슬롯과 `Composite Layer 1` Clone 슬롯이 서로 다른 Image를 가리키는지 확인했습니다. `build/colmap_tools-0.3.12.zip`을 실제 Blender `user_default`에 재설치했으며 ZIP SHA-256은 `85D981891AA8D8DF8840201C432D54267A3B04CEF7E23B0EB297D0112AC578FC`, 크기는 28,667,834바이트입니다.
