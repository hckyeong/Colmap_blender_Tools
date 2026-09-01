# COLMAP Tools 0.3.3 Projection Paint 단독 보기 하위 합성

Projection Paint의 Layer 단독 보기가 선택한 Layer까지의 합성 결과를 표시하도록 변경했습니다.

- 선택 Layer 자체의 Mask와 Adjustment 효과를 적용합니다.
- 선택 Layer 아래에 있는 모든 Layer의 색상·Mask·Adjustment 합성을 유지합니다.
- 선택 Layer 위에 있는 Layer만 화면에서 제외합니다.
- Mask 단독 보기의 흑백 출력과 단독 보기 해제 시 전체 합성 복구는 기존 동작을 유지합니다.

기존 구현은 선택 Layer의 원색 입력을 Principled Base Color에 직접 연결해 Mask와 하위 Layer 합성을 우회했습니다. 이제 선택 Layer의 Mix 출력에 연결해 Layer Stack의 실제 합성 순서를 그대로 사용합니다.

Production 패키지는 `build/colmap_tools-0.3.3.zip`이며 SHA-256은 `B0BFEF38EB028CF4622174FB7170DA4D12C5A09978D1B0F1D6038FED227AEF3E`입니다. 격리 설치본 검사와 실제 Blender `user_default` 설치본 검증을 통과했습니다. 설치 중 열려 있던 Blender GUI는 재시작 뒤 0.3.3을 로드합니다.
