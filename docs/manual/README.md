# COLMAP Tools 사용자 매뉴얼

**COLMAP Tools 0.3.41 · Windows x64 · Blender 5.2.0 이상**

기능별 매뉴얼을 **Align, Modifier, Projection Paint** 세 문서로 나누었습니다. 각 문서의 기능 찾기에서 원하는 기능으로 바로 이동할 수 있으며, 기능마다 용도·버튼 위치·사용 방법·결과와 필요한 조건을 설명합니다.

작성 기준일은 2026-09-09이며 실행 확인 환경은 Blender 5.2.0 LTS입니다. 기본 메뉴 표시는 Blender 언어 설정에 따라 다를 수 있습니다.

> **편집 상태: 기능별 본문 작성, 사용자 캡처 삽입 대기**
>
> 자동 캡처는 중단했으며 문서에는 이미지를 넣을 위치만 표시했습니다. 캡처 준비 상태와 예정 파일명은 각 기능 옆에 있습니다.

<a id="manuals"></a>

## 기능별 매뉴얼

| 문서 | 주요 기능 | 출력 결과 |
|---|---|---|
| **[Align](ALIGN.md)** | 데이터·레이어·축 설정, 마커·3점 정렬, CC 정밀 정렬, 결과 승인 | 정렬한 COLMAP·OBJ |
| **[Modifier](MODIFIER.md)** | 점·카메라 선택과 제거, 카메라 검토·필터, 편집 이력·백업 | 편집을 반영한 COLMAP |
| **[Projection Paint](PROJECTION_PAINT.md)** | 페인트 대상·레이어, 스텐실·투영, 마스크·색 보정·Composite | 보정한 텍스처 이미지 |

Projection Paint의 실제 메뉴 이름은 **Projection Painting**입니다. 이 페이지에는 공통 설치·실행·화면 탐색과 캡처 삽입 안내를 모았습니다. 저장·복원·내보내기·문제 해결은 각 도구 매뉴얼에서 확인합니다.

## 공통 안내 찾기

- [설치](#installation)
- [도구 실행](#launch)
- [화면 탐색](#navigation)
- [도구 사이 데이터 전달](#data-transfer)
- [캡처 위치와 삽입 방법](#captures)

<a id="installation"></a>

## 설치

- Windows x64 컴퓨터와 Blender 5.2.0 이상.
- 정식 설치 파일 **`colmap_tools-0.3.41.zip`**.
- 사용할 COLMAP 모델, 촬영 이미지, 필요에 따라 OBJ와 텍스처.

정식 설치 ZIP에는 CC 정렬 실행 환경이 포함되어 있습니다. 일반 사용자는 별도 Python이나 CloudCompare 프로그램을 설치할 필요가 없습니다. 소스 저장소의 일반 압축 파일과 정식 설치 ZIP을 구분하십시오.

1. Blender를 실행합니다.
2. 상단 **Edit → Preferences**를 엽니다.
3. **Add-ons** 또는 **Get Extensions** 화면에서 오른쪽 위 드롭다운 메뉴의 **Install from Disk…**를 선택합니다.
4. `colmap_tools-0.3.41.zip`을 선택하여 설치합니다. ZIP을 미리 풀지 않습니다.
5. 설치 목록에서 **COLMAP Tools**가 활성화되어 있는지 확인합니다.
6. 업데이트 설치였다면 작업을 저장하고 Blender를 완전히 종료한 뒤 다시 실행합니다.



Blender 기본 확장 설치 방법은 [공식 확장 설치 안내](https://docs.blender.org/manual/en/5.1/editors/preferences/extensions.html)를 참고할 수 있습니다. 이 링크는 Blender 공통 설치 절차이며 COLMAP Tools의 지원 버전은 위 준비물을 따릅니다.

<a id="capture-01"></a>

> **[캡처 01] 확장 설치 메뉴**
>
> - 준비: Preferences에서 설치 드롭다운 메뉴를 연 상태.
> - 필수 표시: Add-ons 또는 Get Extensions 위치, `Install from Disk…` 항목.
> - 예정 파일: `images/01-install-from-disk.png`

CC 정렬 환경 검사와 CPU 스레드 설정은 [Align 설정](ALIGN.md#cpu)을 참고하십시오.

<a id="launch"></a>

## 도구 실행

상단 **Window → COLMAP Tools**에서 원하는 도구를 선택합니다.

- **COLMAP Align**
- **COLMAP Modifier**
- **Projection Painting**

선택한 도구의 전용 작업공간이 열립니다. 메뉴가 보이지 않으면 확장 활성화 상태를 확인하고 Blender를 재시작하십시오.

<a id="capture-03"></a>

> **[캡처 03] 도구 실행 메뉴**
>
> - 준비: `Window → COLMAP Tools` 메뉴를 연 상태.
> - 필수 표시: 세 도구의 메뉴 항목.
> - 예정 파일: `images/03-tools-menu.png`

이미 열린 도구는 상단 작업공간 탭으로 다시 이동할 수 있습니다. 불러오기·정렬·복원·출력이 진행 중이라면 해당 도구의 상태 안내를 확인하고 완료된 뒤 다음 작업을 시작합니다.

<a id="navigation"></a>

## 화면 탐색

마우스를 조작할 3D 화면 위에 두고 입력합니다. 입력창이나 레이어 목록 위에 있을 때와 동작이 다를 수 있습니다.

| 조작 | Blender 기본 키맵 | Industry Compatible 키맵 |
|---|---|---|
| 회전 | 가운데 버튼 드래그 | `Alt` + 왼쪽 버튼 드래그 |
| 화면 이동 | `Shift` + 가운데 버튼 드래그 | `Alt` + 가운데 버튼 드래그 |
| 확대·축소 | 휠 또는 `Ctrl` + 가운데 버튼 드래그 | `Alt` + 오른쪽 버튼 드래그 |

키맵은 **Edit → Preferences → Keymap**에서 확인합니다. 사용자 지정 단축키가 있다면 그 설정을 우선합니다. Industry Compatible의 공통 조작은 [Blender 공식 키맵 안내](https://docs.blender.org/manual/en/4.1/interface/keymap/industry_compatible.html)에서도 확인할 수 있습니다.

패널이 닫혀 있다면 해당 3D 화면의 **View → Sidebar**를 사용합니다. 한쪽 화면의 설정이 다른 쪽 화면에도 적용된다고 가정하지 마십시오.

점·카메라 편집, 마커 입력, 페인트 레이어 활성화에 쓰는 전용 조작은 해당 도구 매뉴얼의 마지막 참고표에서 확인합니다.

<a id="data-transfer"></a>

## 도구 사이 데이터 전달

세 도구를 반드시 순서대로 사용할 필요는 없습니다. 목적에 맞는 도구를 선택하십시오.

| 전달 방향 | 사용할 기능 |
|---|---|
| Align → Modifier | Align에서 [COLMAP Export](ALIGN.md#colmap-export)한 폴더를 Modifier로 [Import](MODIFIER.md#import) |
| Align 메시 → Projection Paint | [OBJ Export](ALIGN.md#obj-export) 후 일반 Blender Mesh로 불러와 [Paint Target](PROJECTION_PAINT.md#paint-target)에 지정 |
| Modifier → Projection Paint | Painting의 [COLMAP 데이터 업데이트](PROJECTION_PAINT.md#dataset)로 현재 데이터 복사 |

Align 데이터는 다른 도구에 자동으로 나타나지 않습니다. Painting으로 복사한 COLMAP 데이터도 이후 Modifier 편집을 자동 반영하지 않으므로 필요할 때 다시 업데이트합니다.

Blender **File → Save / Save As**는 작업을 재개할 `.blend`를 저장하고, 각 도구의 Export는 외부 프로그램에서 사용할 결과를 기록합니다. Modifier Export는 불러온 sparse 파일을 수정하므로 [출력 범위와 Backup](MODIFIER.md#export)을 확인하십시오.

<a id="captures"></a>

## 캡처 위치와 삽입 방법

이 절은 매뉴얼 편집자를 위한 안내입니다. 기존 캡처 번호 **01–22**와 예정 파일명을 유지했습니다. 번호를 누락 없이 옮겼으며, 문서별로 읽을 때 번호가 연속되지 않는 것은 공통 목록의 번호를 사용하기 때문입니다.

### 캡처 목록

| 번호 | 촬영할 화면 | 삽입 위치 |
|---|---|---|
| 01 | 확장 설치 메뉴 | [공통 안내의 캡처 위치](README.md#capture-01) |
| 02 | COLMAP Tools 환경설정 | [Align의 캡처 위치](ALIGN.md#capture-02) |
| 03 | 도구 실행 메뉴 | [공통 안내의 캡처 위치](README.md#capture-03) |
| 04 | 입력 폴더 구조 | [Align의 캡처 위치](ALIGN.md#capture-04) |
| 05 | Align 전체 화면 | [Align의 캡처 위치](ALIGN.md#capture-05) |
| 06 | COLMAP 불러오기 옵션 | [Align의 캡처 위치](ALIGN.md#capture-06) |
| 07 | 레이어 아이콘 | [Align의 캡처 위치](ALIGN.md#capture-07) |
| 08 | 데이터셋 축 설정 | [Align의 캡처 위치](ALIGN.md#capture-08) |
| 09 | 마커 입력과 세 쌍 | [Align의 캡처 위치](ALIGN.md#capture-09) |
| 10 | 3점 정렬 미리보기 | [Align의 캡처 위치](ALIGN.md#capture-10) |
| 11 | CC 결과 검토 | [Align의 캡처 위치](ALIGN.md#capture-11) |
| 12 | Align 내보내기 | [Align의 캡처 위치](ALIGN.md#capture-12) |
| 13 | Modifier 전체 화면과 불러오기 | [Modifier의 캡처 위치](MODIFIER.md#capture-13) |
| 14 | 점·카메라 제거 대상 | [Modifier의 캡처 위치](MODIFIER.md#capture-14) |
| 15 | 카메라 검토와 대기 필터 | [Modifier의 캡처 위치](MODIFIER.md#capture-15) |
| 16 | Modifier Export 확인 | [Modifier의 캡처 위치](MODIFIER.md#capture-16) |
| 17 | Painting 데이터 준비와 전체 화면 | [Projection Paint의 캡처 위치](PROJECTION_PAINT.md#capture-17) |
| 18 | 레이어 선택과 활성 대상 | [Projection Paint의 캡처 위치](PROJECTION_PAINT.md#capture-18) |
| 19 | 카메라 스텐실 연결 | [Projection Paint의 캡처 위치](PROJECTION_PAINT.md#capture-19) |
| 20 | 마스크와 Composite | [Projection Paint의 캡처 위치](PROJECTION_PAINT.md#capture-20) |
| 21 | 텍스처 출력 옵션 | [Projection Paint의 캡처 위치](PROJECTION_PAINT.md#capture-21) |
| 22 | 저장 후 작업 재개 | [Align의 캡처 위치](ALIGN.md#capture-22) |

### 이미지 삽입

1. 캡처 위치 링크를 열고 상자의 준비 상태와 필수 표시 항목을 확인합니다.
2. 정식 버전 0.3.41에서 해당 화면을 직접 촬영합니다. 여러 장이 필요하면 파일명에 `-a`, `-b`를 붙일 수 있습니다.
3. 패널 글씨가 잘리지 않게 촬영하고 배포에 사용할 예시 데이터·파일명을 사용합니다.
4. `docs/manual/images/` 폴더를 만들고 상자의 예정 파일명으로 PNG를 저장합니다.
5. 해당 기능 옆 캡처 상자를 이미지와 설명으로 교체합니다. `capture-번호` 링크 위치는 남기면 목록에서 계속 이동할 수 있습니다.

예를 들어 Align의 캡처 07은 `docs/manual/images/07-align-layer-icons.png`에 저장한 뒤 `ALIGN.md`의 해당 위치에 아래처럼 넣습니다.

```markdown
<a id="capture-07"></a>

![Align 레이어의 점군·표면·축·눈 아이콘](images/07-align-layer-icons.png)

그림 7. 점군·표면 아이콘은 정렬 입력을, 눈 아이콘은 화면 표시를 정합니다.
```

이미지가 없는 현재 문서에는 실제 이미지 링크를 만들지 않았습니다. 폴더·파일명 예시는 설명용이며 별도의 예제 데이터가 첨부되어 있지는 않습니다. 촬영 후 실제 화면의 버튼명·동작과 본문을 최종 대조하고, 배포할 때 편집 상태와 캡처 상자·목록을 정리하십시오.
