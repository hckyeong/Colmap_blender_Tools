# Projection Paint Composite 자동 이전 제거 0.3.16 Release Note

## 변경 사항

Composite Image를 Blender Texture Paint의 실제 Paint Target으로 사용하는 직접 편집 구조는 유지합니다. 활성 Composite의 Source Layer는 잠기지만 Composite 자체와 이후 추가한 독립 `NORMAL` Layer는 일반 Layer처럼 편집할 수 있습니다.

파일 열기·Extension 등록·Undo·Redo·Save·Layer Graph 재구성 중 실행되던 Composite 직접 편집 자동 이전을 제거했습니다. Direct Edit Schema, 구형 Clone Snapshot, `COMPOSITE_DERIVED`, Composite 의존 UUID 저장 필드도 제품 코드에서 제거했습니다.

## 데이터셋 정책

애드온은 구버전 데이터셋을 자동 변경하거나 `STALE`로 바꾸지 않으며, 실행이나 저장을 막지 않습니다. 기존 데이터셋의 초기화가 필요한지는 검사 결과로 사용자에게 먼저 알리고, 명시적 승인을 받은 경우에만 별도 초기화 작업을 수행합니다.

이 정책은 업데이트마다 자동 마이그레이션 코드가 누적되는 것을 막고, 테스트할 데이터셋 변경 시점을 사용자가 직접 결정하도록 합니다.

## 회귀 기준

- 제품 Python 코드에 Composite 직접 편집 자동 이전 진입점과 전용 저장 필드가 없어야 합니다.
- 새 Composite는 직접 Paint Target으로 동작해야 합니다.
- 활성 Composite의 Source Layer 계층과 잠금 표시가 정상이어야 합니다.
- 기존 데이터셋을 열었다는 이유만으로 상태나 Layer Metadata를 자동 수정하면 안 됩니다.

## 검증과 설치

Blender 5.2 Background에서 Composite 61개, Layer Shader 48개, Layer UI 37개, 저장·재열기 4개 회귀를 통과했습니다. Production 220개 검사, Small GICP Runtime 합성, 격리 설치본과 Workspace·Camera Marker GUI도 통과했습니다.

`build/colmap_tools-0.3.16.zip`을 실제 Blender `user_default`에 재설치했습니다. SHA-256은 `E49C95C4BFCFCDE4E96858AF6A8B5F2CE0A12A033A6298623B433D419A749243`이고 크기는 28,674,271바이트입니다. 설치 당시 열려 있던 Blender는 재시작한 뒤 0.3.16 Runtime을 사용합니다.
