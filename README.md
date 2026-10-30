# COLMAP Tools

**[최신 설치 ZIP 다운로드](https://github.com/hckyeong/Colmap_blender_Tools/raw/refs/heads/main/colmap_tools.zip)** · 현재 버전 **0.3.83** · Windows x64 / Blender 5.2 이상

다운로드한 `colmap_tools.zip`을 Blender의 **환경설정 → 애드온 → 디스크에서 설치**에서 선택하세요. 실행 메뉴는 **Window → COLMAP Tools**입니다. 저장소 최상위에는 최신 설치본 하나만 유지합니다.

세 도구는 독립적으로 사용합니다. Painter의 Dataset에서 **COLMAP 불러오기 / OBJ 불러오기**로 파일을 직접 가져옵니다. Modifier 편집 결과는 COLMAP으로 내보낸 뒤 불러옵니다.



## 사용자 매뉴얼

각 도구의 사용 방법은 **[Blender COLMAP 매뉴얼 · Notion](https://burnt-sphere-603.notion.site/Blender-COLMAP-3d7426a1abfa81debd23df307a775913)**에서 확인하세요. 이미지와 영상, 자세한 사용 절차를 Notion에서 제공합니다.



## Align

여러 COLMAP 점군·카메라와 OBJ 메시를 **3점 마커 및 정밀 정렬**로 맞춥니다. 데이터셋의 축·이동·회전·스케일을 조절하고 정렬 결과를 COLMAP·OBJ로 내보냅니다. [Align 매뉴얼 · Notion](https://burnt-sphere-603.notion.site/COLMAP-Align-3d7426a1abfa818585a9de2fec9f244f)

| 버전 | 주요 업데이트 |
|---|---|
| 0.3.81 | 기즈모 중심 지정·물체 원점 이동을 각각 즉시 실행하고 개별 실행 취소 지원. |
| 0.3.80 | 축 설정에 기즈모의 바운딩 박스 중심 지정과 현재 기즈모를 기준으로 한 물체 원점 이동 추가. |
| 0.3.72 | 원본 축은 개별 보정하고 이동·회전·스케일은 공통 UI로 조절. 축 설정 중 COLMAP·OBJ 동시 표시. |
| 0.3.63–0.3.68 | 대용량 메시의 정밀 정렬·전처리 성능 개선. |
| 0.3.37–0.3.41 | CCCoreLib 정밀 정렬 도입, 겹침 영역 우선 정렬, OBJ 원본 면·UV 보존과 프로젝트 내장. |



## Modifier

COLMAP 점·카메라를 **선택·제거·필터링**하고 전체 데이터셋을 이동·회전·균일 확대합니다. 전용 Undo/Redo, 카메라 이미지 검토, COLMAP 출력·백업을 지원합니다. [Modifier 매뉴얼 · Notion](https://burnt-sphere-603.notion.site/COLMAP-Modifier-3ed426a1abfa8113b6d1f8271ab7f409)

| 버전 | 주요 업데이트 |
|---|---|
| 0.3.50 | 데이터셋 배율과 점·카메라 표시 크기 분리, 1–10m 눈금 안내 추가. |
| 0.3.42 | 전체 점군·카메라 변환 기즈모와 저장·출력 연계. |
| 0.3.34–0.3.35 | 독립 편집 상태와 전용 Undo/Redo, 저장한 편집의 자동 복원. |



## Painter · Projection Painting

촬영 이미지를 모델 표면에 **투영하여 텍스처를 페인팅·보정**합니다. 레이어·마스크·색 보정·합성과 PNG·JPEG 및 UDIM·Atlas 출력을 지원합니다. [Painter 매뉴얼 · Notion](https://burnt-sphere-603.notion.site/Projection-Painting-3f1426a1abfa81edaf4fc21248045841)

| 버전 | 주요 업데이트 |
|---|---|
| 0.3.74 | 연결된 스텐실에서 Ctrl을 누르는 동안 Erase Soft 사용, 해제 시 원래 브러시 복귀. |
| 0.3.73 | COLMAP·OBJ 직접 가져오기, Align과 파일 읽기 코드 공유, Modifier 업데이트 제거, 페인트 진입 설정·재진입 해상도 수정. |
| 0.3.28–0.3.32 | 가시 레이어를 합성한 텍스처 출력, 취소 가능한 베이크와 진행 표시. |
| 0.3.21–0.3.27 | 레이어 선택·활성화 분리, 카메라 스텐실 연결과 화면 탐색 개선. |



## 문서와 빌드

[사용자 매뉴얼 · Notion](https://burnt-sphere-603.notion.site/Blender-COLMAP-3d7426a1abfa81debd23df307a775913) · [상세 기능·구조·검증 기록](docs/README.md)

```powershell
.\build_tools\build_extension.ps1
```

빌드 결과는 최상위 `colmap_tools.zip`입니다. 기본 정렬 실행 환경은 ZIP에 포함되며, 네이티브 실행 환경을 처음 빌드할 때는 Visual Studio 2022 C++ 빌드 도구가 필요합니다. [빌드 상세 안내](docs/README.md#build)

독립 테스트·벤치마크 코드는 저장소에서 제거했습니다. 빌드는 실행 환경·매니페스트·파일 무결성을 검사하며, 과거 테스트는 [정리 전 Git 이력](https://github.com/hckyeong/Colmap_blender_Tools/tree/61a764f300dccfdef09311ddf4a553673dfa2716/tests)에서 확인할 수 있습니다.
