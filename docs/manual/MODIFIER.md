# Modifier 기능 매뉴얼

COLMAP 희소 점군과 카메라를 검토하고, 불필요한 점·카메라를 제거하여 COLMAP 데이터에 반영하는 도구입니다.

- 기준: **COLMAP Tools 0.3.50 · Windows x64 · Blender 5.2.0 이상**.
- 실행: **Window → COLMAP Tools → COLMAP Modifier**.
- [설치·공통 조작](README.md) · [Align](ALIGN.md) · [Projection Paint](PROJECTION_PAINT.md).
- 캡처는 사용자가 직접 추가합니다. 각 기능 옆 상자에 준비 상태와 예정 파일명을 표시했습니다.
- `Dataset Transform`은 0.3.42부터 제공하며, 2026-09-12에 Blender 5.2 설치본 반영을 검증했습니다.

**Modifier의 Export는 불러온 sparse 파일을 덮어씁니다.** 원본을 유지하려면 데이터셋의 작업용 복사본을 Import하십시오. [Export와 Backup](#export)에서 적용 범위를 확인할 수 있습니다.

## 기능 찾기

| 기능 | 할 수 있는 일 |
|---|---|
| [COLMAP Import](#import) | 편집할 점군·카메라 불러오기 |
| [축·점·카메라 표시](#display) | 화면 방향과 표시 크기 조절 |
| [데이터셋 변환 기즈모](#dataset-transform) | 전체 점군·카메라를 함께 이동·회전·균일 확대 |
| [점 선택·제거](#points) | 희소 점을 선택하여 제거 |
| [관측 카메라 선택](#observing-cameras) | 선택한 점을 촬영한 카메라 찾기 |
| [카메라 선택·제거](#cameras) | 카메라와 관측 점 제거 |
| [카메라 이미지 검토](#camera-review) | 촬영 시점·이미지 확인 |
| [대기 카메라 필터](#filters) | 제외 후보를 모아 확인하고 일괄 적용 |
| [Undo·Redo·Reset Edits](#history) | 편집 취소·다시 실행·초기화 |
| [Export·Backup](#export) | 편집을 sparse 파일에 반영하고 백업 |
| [Painting으로 데이터 전달](#painting-transfer) | 현재 데이터를 Painting에 복사 |
| [저장·복원](#save) | 작업 파일에서 편집 상태 이어가기 |
| [문제 해결·단축키](#help) | 비활성 버튼·복원·편집 상태 확인 |

<a id="import"></a>

## 1. COLMAP Import

**용도:** 편집할 COLMAP의 희소 점군과 카메라를 불러옵니다.

**위치:** **COLMAP Import → Dataset Root 오른쪽 폴더 아이콘**. Import는 파일 선택창에서 실행합니다.

입력은 촬영 이미지와 sparse 모델을 함께 보관한 데이터셋입니다.

```text
scan_edit/
├─ images/
│  └─ image_001.jpg
└─ sparse/
   └─ 0/
      ├─ cameras.bin
      ├─ images.bin
      └─ points3D.bin
```

TXT 모델은 같은 위치의 `cameras.txt`, `images.txt`, `points3D.txt`를 사용합니다. 촬영 이미지의 이름은 COLMAP에 기록된 이름과 일치해야 합니다. 함께 제공된 추가 모델 파일도 그대로 보관합니다.

1. 작업용 COLMAP 복사본을 준비합니다.
2. **Window → COLMAP Tools → COLMAP Modifier**를 엽니다.
3. **COLMAP Import** 패널을 펼칩니다.
4. **Input Format**을 확인하고 **Dataset Root 경로 오른쪽 폴더 아이콘**을 누릅니다.
5. 파일 선택창에서 폴더와 오른쪽 **Display Axis**를 설정한 뒤 **Import**를 누릅니다. 바로 가져오기가 시작되며 상태가 완료로 바뀔 때까지 기다립니다.
6. Point Display와 Camera Display로 보기 좋은 크기를 조절합니다. 다른 축으로 다시 가져오려면 같은 폴더 아이콘을 사용합니다.

Display Axis는 화면 표시용입니다. Modifier Export는 COLMAP 원본 축을 유지합니다.

**결과:** COLMAP Status에 불러오기 상태가 표시되고 점·카메라 편집 기능을 사용할 수 있습니다. [Align 출력](ALIGN.md#colmap-export)을 편집하려면 해당 결과 폴더를 Dataset Root로 지정합니다. Align에 불러온 데이터가 자동으로 복사되지는 않습니다.

<a id="capture-13"></a>

> **[캡처 13] Modifier 전체 화면과 불러오기**
>
> - 준비: 점군과 여러 카메라를 불러온 상태.
> - 필수 표시: COLMAP Status, Dataset Root 경로·폴더 아이콘, Point Selection, Camera Review, COLMAP Export의 위치. 축 설정과 Import는 별도의 파일 선택창 캡처에 표시합니다.
> - 예정 파일: `images/13-modifier-overview.png`

<a id="display"></a>

## 2. 축·점·카메라 표시

**용도:** 점군의 화면 방향과 점·카메라 표시 크기를 조절합니다.

**위치:** 축 설정은 **COLMAP Import → 경로 오른쪽 폴더 아이콘 → 파일 탐색기 오른쪽 Display Axis**에 있습니다. Point Display와 Camera Display는 N패널의 **COLMAP Import**에 있습니다.

| 항목 | 기능 |
|---|---|
| Display Axis | 표시 축 프리셋 선택. 사용자 정의에서는 Forward·Up 조절 |
| Point Display | 화면에 그리는 희소 점 크기 조절 |
| Camera Display | 카메라 프러스텀의 표시 크기 조절 |

0.3.50부터 점의 표시 반지름과 카메라 절두체 크기는 **Dataset Transform의 배율과 독립적으로 설정값을 유지**합니다. 데이터를 확대하면 점 위치와 카메라 간격만 함께 벌어집니다. 화면을 확대·축소할 때 보이는 픽셀 크기는 원근과 줌에 따라 달라집니다. Edit Point 모드의 기본 편집 정점 크기는 Blender의 표시 설정을 따릅니다.

**결과:** 화면 표시만 바뀝니다. 원본 점 위치나 카메라 내부 파라미터를 편집하는 기능이 아니며, Export는 COLMAP 원본 축을 유지합니다.

파일 선택창을 취소하면 경로와 축은 바뀌지 않습니다. **Import**를 누르면 선택한 경로와 축을 적용하고 바로 가져옵니다. N패널에는 별도의 Import 버튼이 없습니다.

> **[캡처 필요] Dataset Root 폴더 선택창**
>
> - 필수 표시: 오른쪽 Display Axis의 프리셋, 사용자 정의 Forward·Up, 원본 축 유지 안내.

화면을 회전·이동하거나 패널을 다시 여는 방법은 [공통 화면 조작](README.md#navigation)을 참고하십시오.

<a id="dataset-transform"></a>

## 2.1. 데이터셋 변환 기즈모

**위치:** **COLMAP Import** 아래의 **Dataset Transform**.

점군과 모든 카메라를 하나의 데이터셋으로 함께 변환합니다. 점 또는 카메라 편집 모드를 종료하고 대기 필터를 Apply 또는 Clear All로 정리한 뒤 사용합니다.

1. **기즈모 표시**를 켭니다.
2. 축 화살표로 이동하고 회전 링으로 회전합니다. 중앙 핸들은 전체를 균일하게 확대·축소합니다.
3. 핸들은 드래그 후 마우스 버튼을 놓으면 확정됩니다. **이동·회전·배율** 버튼이나 뷰포트의 `G/R/S`로 시작했다면 왼쪽 클릭 또는 Enter로 확정합니다.
4. 이동·회전 중 `X/Y/Z`로 축을 제한합니다. `Esc` 또는 오른쪽 클릭으로 취소합니다.
5. 일반 탐색 상태에서 점을 더블클릭하면 변환 피벗과 화면 회전 중심이 그 점으로 이동합니다. **피벗 원점 초기화**로 되돌립니다.

한 번 확정한 변환은 전용 Undo 한 단계입니다. 점·카메라 삭제와 같은 이력에서 Undo·Redo하며, 저장·재실행과 Export 및 Painter 데이터 업데이트에도 반영됩니다. 기즈모 표시를 꺼도 버튼과 단축키는 사용할 수 있습니다.

**Display Axis**는 화면 방향만 바꾸는 설정이고, **Dataset Transform**은 실제 출력 좌표를 바꾸는 편집입니다. 비균일 배율과 좌우 반전은 지원하지 않습니다.

Modifier 바닥의 +X·+Y 방향에는 `1 m`부터 `10 m`까지 거리 눈금이 항상 배치됩니다. 데이터와 별도로 고정되어 확대·축소할 때 크기를 비교할 수 있습니다. 눈금은 선택되지 않으며 렌더·COLMAP 내보내기·Painter 전달에도 포함되지 않습니다.

눈금은 Scene 단위를 미터로 환산합니다. 단위가 None이면 1 Blender 단위를 1m로 봅니다. 눈금을 표시하는 것만으로 COLMAP 모델의 실제 크기가 보정되지는 않습니다. 실제 길이를 알고 있는 부분을 기준으로 데이터 배율을 조절하십시오.

> **[추가 캡처 필요] 배율과 독립적인 표시 크기·미터 눈금**
>
> - 준비: 점·카메라가 있는 데이터를 서로 다른 배율로 표시한 상태.
> - 필수 표시: 같은 Point Scale·Camera Frustum Size, 고정된 1~10m 눈금과 달라진 데이터 크기.

> **[추가 캡처 필요] Modifier 기즈모**
>
> - 준비: 점군·카메라를 가져오고 모든 편집 모드를 종료한 상태.
> - 필수 표시: Dataset Transform 패널, XYZ 이동 화살표·회전 링·중앙 배율 핸들.
> - 예정 파일: `images/modifier-dataset-transform.png`

<a id="points"></a>

## 3. 점 선택·제거

**용도:** 배경이나 불필요한 부위의 희소 점을 제거합니다.

**위치:** **Point Selection → Edit Point Mode → Remove**.

1. **Point Selection → Edit Point Mode**를 누릅니다.
2. 제거할 점을 선택합니다. 화면 선택 방식은 현재 Blender 키맵을 따릅니다.
3. **Selected Points**와 **Remove Target: Selected Points**를 확인합니다.
4. **Remove**를 누릅니다.
5. 결과를 확인하고, 잘못 제거했다면 전용 Undo로 되돌립니다.
6. 작업 후 **Exit Point Mode**로 점 편집 모드를 종료합니다.

**사용 조건:** 카메라 편집 모드는 종료되어 있어야 합니다. 대기 필터가 남았다면 먼저 Apply 또는 Clear All로 정리합니다.

**결과:** 선택한 점이 현재 편집 결과에서 제거됩니다. 잘못 제거한 점은 [Undo Last Edit](#history)로 되돌릴 수 있고, sparse 파일 반영은 [Export](#export)에서 수행합니다.

<a id="observing-cameras"></a>

## 4. 관측 카메라 선택

**용도:** 선택한 점들이 어느 촬영 이미지에서 관측되었는지 찾습니다.

**위치:** **Point Selection → Select Observing Cameras**.

1. 점 편집 모드에서 확인할 점을 선택합니다.
2. **Selected Points**에 선택한 점 수가 표시되는지 확인합니다.
3. **Select Observing Cameras**를 누릅니다.
4. **Observing Cameras**와 카메라 선택 결과를 확인합니다.

**결과:** 선택한 점을 관측한 카메라를 찾습니다. 선택만으로 점이나 카메라를 제거하지 않습니다. 제거하려면 [점 제거](#points) 또는 [카메라 제거](#cameras)의 모드와 Remove Target을 확인합니다.

<a id="cameras"></a>

## 5. 카메라 선택·제거

**용도:** 불필요한 카메라와 그 관측 점을 편집 결과에서 제거합니다.

**위치:** **Point Selection → Edit Camera Mode → Remove**.

점 편집 모드를 종료한 다음 **Edit Camera Mode**로 들어갑니다. 제거할 카메라를 선택하고 대상 수를 확인한 뒤 **Remove**합니다.

카메라 제거는 카메라 오브젝트만 숨기는 기능이 아닙니다. 관측 점에도 영향을 줄 수 있으며 화면의 **Cameras + Observed Points** 대상 표시를 확인해야 합니다. 일반 오브젝트가 섞인 선택이라면 카메라만 다시 선택하십시오.

점 편집 모드와 카메라 편집 모드는 동시에 사용하지 않습니다. 삭제 대상이 불분명하면 실행 전에 모드와 **Remove Target**을 다시 확인합니다.

**결과:** 선택 카메라와 연결된 관측 점을 대상으로 편집이 수행됩니다. 결과를 확인하고 필요하면 Undo Last Edit로 되돌린 뒤 **Exit Camera Mode**로 종료합니다.

<a id="capture-14"></a>

> **[캡처 14] 점·카메라 제거 대상**
>
> - 준비: 편집 모드에서 일부 점 또는 카메라를 선택한 상태. 두 모드는 별도 컷으로 촬영.
> - 필수 표시: 현재 편집 모드, 선택 수, Remove Target, Remove, Undo·Redo.
> - 예정 파일: `images/14-modifier-selection.png`

<a id="camera-review"></a>

## 6. 카메라 이미지 검토

**용도:** 촬영 시점에서 이미지와 재구성 데이터의 관계를 확인합니다.

**위치:** **Camera Review**와 타임라인.

1. 타임라인에서 확인할 카메라 프레임을 선택합니다.
2. Camera Review의 **Camera Index**, **IMAGE_ID**, 모델·이미지 정보를 확인합니다.
3. **Look Through Camera**로 촬영 시점에 들어갑니다.
4. **Show Background Image** 또는 `F7`로 촬영 이미지를 표시합니다.
5. **Background Alpha**로 배경 표시 강도를 조절합니다.

**결과:** 현재 촬영 이미지와 점군을 같은 시점에서 검토할 수 있습니다. 배경 표시를 끄는 것은 카메라를 삭제하거나 필터로 제외하는 작업과 다릅니다.

왜곡이 있는 카메라 모델 등에서 정확한 배경 투영을 지원하지 않는다는 안내가 나오면 배경과 점의 화면 일치만으로 데이터 정확도를 판단하지 않습니다. 제외할 카메라는 다음 [대기 필터](#filters)에 추가할 수 있습니다.

<a id="filters"></a>

## 7. 대기 카메라 필터

**용도:** 여러 카메라를 제외 후보로 모은 뒤 한 번에 편집에 반영합니다.

**위치:** **Camera Review → Pending Camera Filters**의 필터 버튼.

1. [Camera Review](#camera-review)에서 제외할 촬영 시점을 확인합니다.
2. **Filter Current Camera**로 현재 카메라를 제외 후보에 추가합니다.
3. 다른 카메라도 확인하여 필요한 후보를 모읍니다.
4. **Pending Cameras / Pending Point3D** 수를 확인합니다.
5. 후보가 맞으면 **Apply**로 편집을 확정합니다.

| 대기 필터 버튼 | 동작 |
|---|---|
| Filter Current Camera | 현재 카메라를 제외 후보로 지정 |
| Unfilter Current Camera | 현재 카메라의 제외 후보 지정 해제 |
| Undo Last Filter | 마지막 대기 필터 조작 취소 |
| Clear All | 아직 적용하지 않은 대기 필터 전체 해제 |
| Apply | 대기 필터를 실제 편집으로 확정 |

**Clear All은 이미 적용한 편집을 되돌리는 버튼이 아닙니다.** 적용한 편집은 전용 Undo를 사용합니다. 대기 필터가 남아 있으면 일반 편집이나 Export가 제한될 수 있으므로 Apply 또는 Clear All로 먼저 정리합니다.

<a id="capture-15"></a>

> **[캡처 15] 카메라 검토와 대기 필터**
>
> - 준비: 촬영 이미지가 배경에 보이고 한 카메라가 Pending 목록에 들어간 상태.
> - 필수 표시: IMAGE_ID, Look Through Camera, Background Alpha, Pending 수, Apply·Clear All·Undo Last Filter.
> - 예정 파일: `images/15-modifier-camera-filter.png`

<a id="history"></a>

## 8. Undo·Redo·Reset Edits

**용도:** 잘못 수행한 편집을 취소하거나 다시 실행하고 편집 전 상태로 되돌립니다.

**위치:** **Point Selection → Undo Last Edit / Redo Last Edit / Reset Edits**.

Modifier의 점·카메라 삭제와 데이터셋 변환은 같은 도구 전용 이력을 사용합니다. 기즈모 조작을 취소하면 이력에 추가하지 않습니다.

- **Undo Last Edit / `Ctrl+Z`**: 마지막 확정 편집 되돌리기.
- **Redo Last Edit / `Ctrl+Shift+Z` 또는 `Ctrl+Y`**: 되돌린 편집 다시 실행.
- **Reset Edits**: 현재 편집을 초기 상태로 되돌리는 기능. 마지막 한 단계만 취소하려면 Undo Last Edit를 사용합니다.

이 단축키는 Modifier 작업공간에서 사용합니다. 다른 작업공간의 Blender Undo와 같은 이력이라고 생각하지 마십시오. 대기 필터의 Undo Last Filter와 확정 편집의 Undo도 구분합니다.

**Reset Edits는 원본 sparse 파일을 직접 수정하지 않습니다.** 이미 Export한 파일을 내보내기 이전으로 돌릴 때는 저장한 백업이나 작업용 복사본을 사용해야 합니다.

<a id="export"></a>

## 9. Export·Backup

**용도:** 현재 편집 결과를 불러온 COLMAP 파일에 반영합니다.

**위치:** **COLMAP Export → Format / Backup / Export**.

**Modifier의 Export는 현재 불러온 원본 sparse 파일을 덮어씁니다.** Align처럼 별도 새 데이터셋을 지정하는 출력 창이 아닙니다. 원본을 유지하려면 처음부터 데이터셋의 작업용 복사본을 불러오십시오.

1. 점·카메라 편집 모드를 모두 종료합니다.
2. Pending Camera Filters를 적용하거나 해제합니다.
3. **COLMAP Export**에서 **Format**을 선택합니다. 원본 형식 유지 또는 제공되는 TXT/Binary 형식을 사용합니다.
4. **Backup**을 확인합니다. 켜면 먼저 `sparse_backup`에 원본 스냅샷을 보관합니다.
5. **Export**의 확인 내용을 읽고 실행합니다.
6. 완료 상태와 변경된 sparse 파일을 확인합니다.

Export가 성공하면 출력 상태가 새 기준이 되고 **전용 편집 이력이 비워집니다.** 내보낸 뒤 이전 편집 전체를 Undo로 되돌릴 수 있다고 기대하지 마십시오. 실패하거나 취소한 경우에는 기존 편집 상태를 유지합니다. 변경된 편집이 없거나 편집 모드·대기 필터가 남아 있으면 Export 버튼이 비활성일 수 있습니다.

<a id="capture-16"></a>

> **[캡처 16] Modifier Export 확인**
>
> - 준비: 편집을 하나 확정하고 편집 모드·대기 필터를 정리한 뒤 Export 확인 화면을 연 상태.
> - 필수 표시: Format, Backup, 실제 수정 대상 경로와 덮어쓰기 설명. 개인 경로 대신 배포용 예시 경로 사용.
> - 예정 파일: `images/16-modifier-export.png`

<a id="painting-transfer"></a>

## 10. Painting으로 데이터 전달

**용도:** 현재 준비된 점군·카메라 데이터를 Projection Paint에서 사용합니다.

**위치:** Projection Painting 작업공간의 **COLMAP Dataset → COLMAP 데이터 업데이트**.

1. Modifier에서 사용할 편집 결과를 확정합니다.
2. **Window → COLMAP Tools → Projection Painting**을 엽니다.
3. **COLMAP 데이터 업데이트**를 누릅니다.
4. Painting의 카메라·점과 상태 표시를 확인합니다.

**결과:** 업데이트 시점의 데이터가 Painting에 독립적으로 복사됩니다. 이후 Modifier를 편집해도 자동 반영되지 않습니다. 새 편집 결과를 전달하려면 다시 업데이트합니다. 기존 페인트 모델과 레이어는 이 버튼으로 자동 교체되지 않습니다.

페인트 모델 준비와 후속 기능은 [Projection Paint 매뉴얼](PROJECTION_PAINT.md)을 참고하십시오.

<a id="save"></a>

## 11. 저장·복원

**용도:** COLMAP 원본 참조와 확정 편집 기록을 보관하고 다음 작업에서 복원합니다.

**위치:** Blender **File → Save / Save As**, **COLMAP Modifier** 작업공간.

1. 편집 모드와 대기 필터를 정리하고 `.blend`를 저장합니다.
2. 같은 COLMAP 원본 모델과 촬영 이미지를 함께 보관합니다.
3. 파일을 다시 열고 Modifier의 복원 안내가 끝날 때까지 기다립니다.
4. 점·카메라·확정 편집 상태를 확인합니다.

정상 저장된 0.3.34 이후 데이터는 원본을 읽고 편집 기록을 자동 복원합니다. 복원된 Undo는 사용할 수 있지만 저장 당시의 Redo 분기는 복원하지 않습니다.

원본이 바뀌거나 누락되었거나 구형 저장 형식으로 인해 재Import 안내가 나오면 원본과 보존할 편집 결과를 먼저 확인합니다. `.blend` 저장은 sparse 파일에 편집을 쓰는 [Export](#export)와 별도입니다.

작업공간을 종료할 때는 상단의 **COLMAP Modifier 종료**를 사용할 수 있습니다. 종료 버튼이 저장이나 Export를 대신하지는 않습니다.

<a id="help"></a>

## 12. 문제 해결·단축키

| 증상 | 확인할 기능 |
|---|---|
| Import가 실패함 | Dataset Root, sparse 모델 파일, images 경로와 Input Format |
| 점·카메라가 선택되지 않음 | [편집 모드](#points), 복원 완료 여부, 대기 필터 상태 |
| Remove 대상이 예상과 다름 | 현재 모드와 Remove Target, 카메라 외 오브젝트 혼합 선택 여부 |
| 일반 편집·Export가 비활성 | [대기 필터](#filters)를 Apply 또는 Clear All로 정리 |
| Clear All을 눌러도 이전 편집이 남음 | Clear All은 미적용 후보 해제. 적용된 편집은 [Undo Last Edit](#history) |
| Export 버튼이 비활성 | 확정 편집 유무, 편집 모드 종료, 대기 필터 정리 여부 |
| Export 뒤 Undo가 비어 있음 | 출력이 새 기준으로 확정됨. 이전 파일은 [Backup](#export) 확인 |
| 모델이 누워 보임 | [Display Axis](#display). 출력은 원본 축 유지 |
| 복원 오류 또는 재Import 안내 | [원본 위치·변경 여부·저장 버전](#save) |
| Painting에 수정이 반영되지 않음 | Painting에서 [데이터 업데이트](#painting-transfer)를 다시 실행 |

| 입력 | Modifier에서의 동작 |
|---|---|
| Ctrl+Z | Undo Last Edit |
| Ctrl+Shift+Z 또는 Ctrl+Y | Redo Last Edit |
| F7 | 카메라 배경 이미지 표시 전환 |
| G / R / S | 일반 탐색 상태에서 데이터셋 전체 이동 / 회전 / 균일 배율 |
| X / Y / Z | 이동·회전 중 축 제한 |
| Esc / 오른쪽 클릭 | 진행 중인 데이터셋 변환 취소 |
| Exit Point Mode / Exit Camera Mode | 해당 편집 모드 종료 |

[공통 안내로 돌아가기](README.md) · [Align](ALIGN.md) · [Projection Paint](PROJECTION_PAINT.md)
