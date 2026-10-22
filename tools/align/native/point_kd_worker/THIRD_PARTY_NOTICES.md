# 제삼자 라이선스

이 워커의 원문은 COLMAP Tools 저장소의 라이선스를 따릅니다. 다음 고정 의존성을 사용합니다. CCCoreLib나 nanoflann을 링크하거나 그 구현을 복제하지 않습니다.

| 의존성 | 버전 | 라이선스 | 원문 |
|---|---|---|---|
| Eigen | 3.4.0 | MPL-2.0 | https://gitlab.com/libeigen/eigen/-/tree/3.4.0 |
| nlohmann JSON | 3.11.3 | MIT | https://github.com/nlohmann/json/tree/9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03 |
| oneTBB | 2022.0.0 | Apache-2.0 | https://github.com/uxlfoundation/oneTBB/tree/v2022.0.0 |

Eigen의 `COPYING.MPL2`, JSON의 `LICENSE.MIT`, oneTBB의 `LICENSE.txt`와 `third-party-programs.txt` 원문을 런타임과 재빌드 묶음의 `licenses/`에 포함합니다. Eigen 및 JSON 헤더는 수정하지 않습니다. 원문 다운로드 URL과 검증 해시는 `dependencies.lock.json`에 보존합니다.

병렬 루프와 고정 합산 순서 구현의 참고 자료는 [oneTBB 병렬 루프](https://uxlfoundation.github.io/oneTBB/main/tbb_userguide/parallel_for_os.html), [결정적 합산 규약](https://uxlfoundation.github.io/oneTBB/main/specification/source/algorithms/functions/parallel_deterministic_reduce_func.html), SVD 풀이의 참고 자료는 [Eigen JacobiSVD](https://eigen.tuxfamily.org/dox/classEigen_1_1JacobiSVD.html)입니다.
