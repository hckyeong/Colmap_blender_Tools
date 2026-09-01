# COLMAP Tools 0.3.5 Projection Paint 레이어 개인 단독 보기·마스크 단독 보기

## 변경 이유

기존 Layer 단독 보기는 선택 Layer의 주 합성 Mix 출력을 Principled Base Color에 직접 연결했습니다. 이 출력에는 아래 Layer와 Original이 이미 누적되어 있어 선택 Layer만 확인할 수 없었습니다.

## 변경 내용

- 주 Layer Chain과 별도로 애드온 소유 단일 `Layer Solo Preview Mix`를 둡니다.
- Layer 개인 단독 보기는 검정 Base 위에 선택 Layer의 처리된 Color와 현재 Factor만 합성합니다.
- 선택 Layer의 표시 상태, Image Alpha, Layer Mask, Adjustment와 Adjustment Mask를 그대로 반영합니다.
- 위·아래 다른 Layer와 Original은 Layer 개인 단독 보기에서 제외합니다.
- Layer Mask와 Adjustment Mask 단독 보기는 기존처럼 각 Mask 원본을 흑백으로 표시합니다.
- Layer 개인 보기와 두 Mask 보기는 동시에 하나만 활성화되며 다른 버튼을 누르면 즉시 전환합니다.
- 활성 버튼을 다시 누르면 전체 최상단 Layer 합성으로 복귀합니다.
- 기존 `Scene.projection_paint.layer_preview_uuid`, `mask_preview_adjustment_uuid`, Material의 `pp_layer_preview_uuid`, `pp_mask_preview_uuid`는 저장 파일과 공개 상태 호환을 위해 유지합니다.

## 복구와 검증

Reconcile, Layer 추가·삭제·이동, Mask·표시·보정 갱신, Undo와 파일 로드에서 Preview UUID가 유효하면 전용 출력을 다시 연결합니다. 대상이 사라지면 Scene 상태와 Material 태그를 지우고 전체 합성으로 복귀합니다. 검증기는 전용 Preview Mix의 소유권, 선택 Layer UUID, 검정 Base, 처리 Color와 Factor 연결 및 Principled 출력 연결을 함께 확인합니다.

Blender 5.2 Background의 `test_layer_shader.py` 47개, `test_original_layer.py` 16개, `test_layer_ui.py` 36개 등 대상 회귀 99개를 통과했습니다. `build/colmap_tools-0.3.5.zip` Production 빌드, Small GICP Runtime 검증, 격리 설치본 검사와 실제 Blender `user_default` 재설치를 완료했습니다. ZIP SHA-256은 `F628F29DCD56E797F73C18F13D58D81FFA093AE586FFFBF9E418EB5087B7E9B7`입니다. 설치 중 열려 있던 Blender GUI는 기존 Runtime을 유지하므로 재시작 뒤 0.3.5가 적용됩니다.
