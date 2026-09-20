// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <CCGeom.h>
#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace CCCoreLib {
class GenericIndexedMesh;
class GenericIndexedCloudPersist;
class GenericProgressCallback;
class PointCloud;
}

namespace ccalign {
// 원본 메쉬를 소유하지 않는 정확 최근접 표면 검색 트리입니다.
class MeshBvh {
public:
    // 원본 메쉬 수명 동안 유지할 읽기 전용 트리를 예산 안에서 생성합니다.
    bool build(CCCoreLib::GenericIndexedMesh& mesh, CCCoreLib::GenericIndexedCloudPersist& vertices,
               std::uint64_t budgetBytes, CCCoreLib::GenericProgressCallback* progress = nullptr, unsigned maxThreads = 0);
    // 정확 거리를 기록하며 CCCoreLib의 거리 계산 성공·실패 코드를 반환합니다.
    int computeDistances(CCCoreLib::GenericIndexedCloudPersist& query, CCCoreLib::PointCloud* cpSet,
                         unsigned maxThreads, CCCoreLib::GenericProgressCallback* progress = nullptr,
                         std::uint64_t budgetBytes = 0) const;
    // 원본 기하를 제외한 빌드 최대 할당량을 사전에 계산합니다.
    static std::uint64_t estimatePeakBuildBytes(unsigned triangleCount, unsigned maxThreads = 0);
    // 병렬 검색과 출력 준비의 보수적인 추가 할당량을 계산합니다.
    static std::uint64_t estimateQueryBytes(unsigned pointCount, unsigned maxThreads, bool withCpSet);
    // 예약된 배열과 객체 자체가 차지하는 상주 바이트 수를 반환합니다.
    std::uint64_t residentBytes() const;
    // 마지막 빌드에 사용한 보수적 최대 바이트 수를 반환합니다.
    std::uint64_t peakBuildBytes() const { return peakBytes_; }
    // 마지막 빌드 실패 사유를 반환하며 조회 중에는 변경하지 않습니다.
    const std::string& lastError() const { return error_; }
private:
    struct Node {
        PointCoordinateType lo[3], hi[3];
        std::uint32_t first, count;
    };
    struct Primitive {
        PointCoordinateType lo[3], hi[3];
        std::uint32_t id;
    };
    struct Hit {
        ScalarType squaredDistance;
        CCVector3 point;
        std::uint32_t triangle;
    };
    // 고정된 중앙 분할에서 필요한 노드 수를 계산합니다.
    static std::uint64_t nodeCount(unsigned count);
    // 원본 삼각형 순열의 구간을 결정적인 중앙 분할로 구성합니다.
    bool buildNode(std::vector<Primitive>& primitives, unsigned first, unsigned count, unsigned node,
                   unsigned nextFree, std::atomic<bool>& canceled, std::atomic<unsigned>& completed);
    // 경계 상자 하한을 사용해 가까운 자식부터 모든 유효 후보를 확인합니다.
    Hit nearest(const CCVector3& point, const std::atomic<bool>& canceled) const;
    // 좌표 반올림을 보수적으로 고려한 상자 거리 하한을 계산합니다.
    static double lowerBound(const Node& node, const CCVector3& point);
    CCCoreLib::GenericIndexedMesh* mesh_ = nullptr;
    std::vector<Node> nodes_;
    std::vector<std::uint32_t> ids_;
    std::uint64_t budgetBytes_ = 0;
    std::uint64_t peakBytes_ = 0;
    std::string error_;
};
}
