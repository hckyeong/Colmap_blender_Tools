// SPDX-License-Identifier: GPL-3.0-or-later
#include "mesh_bvh.h"
#include <DistanceComputationTools.h>
#include <GenericProgressCallback.h>
#include <PointCloud.h>
#include <SimpleMesh.h>
#include <SimpleTriangle.h>
#include <cmath>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <thread>
#include <vector>

using Cloud = CCCoreLib::PointCloud;
using Mesh = CCCoreLib::SimpleMesh;
using Distances = CCCoreLib::DistanceComputationTools;
constexpr std::uint64_t Budget = 512ull * 1024 * 1024;

// 최적화 빌드에서도 검증 조건 실패를 명확히 보고합니다.
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

struct Progress : CCCoreLib::GenericProgressCallback {
    float stopAt = 101.0f;
    float percent = 0;
    bool cancel = false;
    // 지정한 진행률에서 취소 상태로 전환합니다.
    void update(float value) override { percent = value; if (value >= stopAt) cancel = true; }
    // 시험에서는 작업 제목을 보관하지 않습니다.
    void setMethodTitle(const char*) override {}
    // 시험에서는 부가 설명을 보관하지 않습니다.
    void setInfo(const char*) override {}
    // 시험 진행 시작 콜백을 제공합니다.
    void start() override {}
    // 시험 진행 종료 콜백을 제공합니다.
    void stop() override {}
    // 시험에서 설정한 취소 여부를 반환합니다.
    bool isCancelRequested() override { return cancel; }
};

struct CountingMesh final : Mesh {
    mutable std::atomic<std::uint64_t> reads{0};
    bool delayReads = false;
    // 취소 시험용 메쉬가 실제로 조회된 횟수를 추적합니다.
    explicit CountingMesh(Cloud* cloud) : Mesh(cloud) {}
    // 원자적 조회 횟수를 누적하고 요청한 경우 시간 취소를 검증할 읽기 지연을 줍니다.
    void getTriangleVertices(unsigned index, CCVector3& a, CCVector3& b, CCVector3& c) const override {
        const auto count = reads.fetch_add(1, std::memory_order_relaxed);
        if (delayReads && count % 64 == 0) std::this_thread::sleep_for(std::chrono::milliseconds(2));
        Mesh::getTriangleVertices(index, a, b, c);
    }
};

struct TimedProgress final : Progress {
    std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now();
    std::thread::id caller = std::this_thread::get_id();
    bool throwOnUpdate = false;
    std::atomic<bool> wrongThread{false};
    // 콜백이 호출 스레드에 한정되는지 검사하고 예외 정리 경로도 검증합니다.
    void update(float value) override {
        if (std::this_thread::get_id() != caller) wrongThread.store(true, std::memory_order_relaxed);
        require(std::this_thread::get_id() == caller, "진행 콜백 병렬 접근");
        percent = value;
        if (throwOnUpdate && std::chrono::steady_clock::now() - started >= std::chrono::milliseconds(50))
            throw std::runtime_error("시험용 콜백 예외");
    }
    // 선행 취소와 구분하여 실제 검색 시작 후 50밀리초에 취소합니다.
    bool isCancelRequested() override {
        if (std::this_thread::get_id() != caller) wrongThread.store(true, std::memory_order_relaxed);
        require(std::this_thread::get_id() == caller, "취소 콜백 병렬 접근");
        return !throwOnUpdate && std::chrono::steady_clock::now() - started >= std::chrono::milliseconds(50);
    }
};

// 겹치는 비퇴화 면의 긴 순회가 취소 및 콜백 예외에 즉시 종료되는지 검사합니다.
void checkTraversalCancellation() {
    Cloud vertices, query;
    require(vertices.reserve(3) && query.reserve(1024), "취소 시험 점 예약 실패");
    vertices.addPoint(CCVector3(0, 0, 0));
    vertices.addPoint(CCVector3(1, 0, 0));
    vertices.addPoint(CCVector3(0, 1, 0));
    CountingMesh mesh(&vertices);
    constexpr unsigned Triangles = 200000;
    require(mesh.reserve(Triangles), "취소 시험 면 예약 실패");
    for (unsigned i = 0; i < Triangles; ++i) mesh.addTriangle(0, 1, 2);
    for (unsigned i = 0; i < 1024; ++i) query.addPoint(CCVector3(0.25f, 0.25f, 1));
    ccalign::MeshBvh tree;
    require(tree.build(mesh, vertices, Budget), "취소 시험 트리 생성 실패");
    for (unsigned threads : {1u, 4u}) {
        mesh.reads.store(0, std::memory_order_relaxed);
        TimedProgress progress;
        require(tree.computeDistances(query, nullptr, threads, &progress) == Distances::CANCELED_BY_USER,
                "겹치는 면 순회 중 취소 실패");
        const auto elapsed = std::chrono::steady_clock::now() - progress.started;
        require(elapsed < std::chrono::seconds(3), "순회 중 취소 응답 지연");
        const auto reads = mesh.reads.load(std::memory_order_relaxed);
        require(reads > 0 && reads < std::uint64_t(Triangles) * query.size(), "검색 시작 전 또는 전체 완료 후 취소");
        require(!query.isScalarFieldEnabled(), "취소 후 불완전 거리 기록");
    }
    TimedProgress exception;
    exception.throwOnUpdate = true;
    require(tree.computeDistances(query, nullptr, 1, &exception) == Distances::ERROR_INTERNAL,
            "진행 콜백 예외 정리 실패");
}

struct Expected { ScalarType squared; CCVector3 point; unsigned triangle; };

// 모든 원본 삼각형을 전수 조사해 독립적인 최근접 기준값을 구합니다.
Expected exhaustive(const CCVector3& point, Mesh& mesh) {
    Expected result{std::numeric_limits<ScalarType>::infinity(), CCVector3(0, 0, 0), UINT32_MAX};
    for (unsigned i = 0; i < mesh.size(); ++i) {
        CCCoreLib::SimpleTriangle triangle;
        mesh.getTriangleVertices(i, triangle.A, triangle.B, triangle.C);
        CCVector3 closest;
        const auto squared = Distances::computePoint2TriangleDistance(&point, &triangle, false, &closest);
        if (squared < result.squared) result = {squared, closest, i};
    }
    return result;
}

// 점·면·모서리·상자 밖·동거리와 무작위 점을 여러 좌표 크기에서 비교합니다.
void checkScene(float scale, float offset) {
    Cloud vertices, query;
    Mesh mesh(&vertices);
    constexpr unsigned Triangles = 350;
    require(vertices.reserve(Triangles * 3), "정점 예약 실패");
    require(mesh.reserve(Triangles), "면 예약 실패");
    require(query.reserve(1500), "조회 예약 실패");
    std::mt19937 generator(73491);
    std::uniform_real_distribution<float> random(-15.0f, 15.0f);
    // 입력 변환을 한 번만 적용해 각 시험의 단정도 원본을 고정합니다.
    const auto point = [scale, offset](float x, float y, float z) {
        return CCVector3(offset + scale * x, offset + scale * y, offset + scale * z);
    };
    for (unsigned i = 0; i < Triangles; ++i) {
        const float x = i < 2 ? 0 : random(generator);
        const float y = i < 2 ? 0 : random(generator);
        const float z = i < 2 ? 0 : random(generator);
        vertices.addPoint(point(x, y, z));
        vertices.addPoint(point(x + 0.75f, y, z));
        vertices.addPoint(point(x, y + 1.25f, z + (i % 3 == 2 ? 0.1f : 0.0f)));
        mesh.addTriangle(i * 3, i * 3 + 1, i * 3 + 2);
    }
    query.addPoint(point(0, 0, 0));
    query.addPoint(point(0.25f, 0.25f, 0));
    query.addPoint(point(0.375f, 0, 0));
    query.addPoint(point(0.25f, 0.25f, 0.25f));
    query.addPoint(point(100000, -100000, 50000));
    query.addPoint(point(-100000, 100000, -50000));
    for (unsigned i = 6; i < 1500; ++i) query.addPoint(point(random(generator), random(generator), random(generator)));
    std::vector<Expected> expected;
    for (unsigned i = 0; i < query.size(); ++i) expected.push_back(exhaustive(*query.getPoint(i), mesh));
    ccalign::MeshBvh tree;
    require(!tree.build(mesh, vertices, ccalign::MeshBvh::estimatePeakBuildBytes(mesh.size()) - 1), "생성 예산 초과 허용");
    require(tree.build(mesh, vertices, Budget), "트리 생성 실패");
    require(tree.residentBytes() <= tree.peakBuildBytes(), "상주 메모리 산정 오류");
    for (unsigned threads : {1u, 4u}) {
        Cloud cp;
        require(tree.computeDistances(query, &cp, threads) == 0, "거리 계산 실패");
        require(cp.size() == query.size(), "최근접점 크기 오류");
        for (unsigned i = 0; i < query.size(); ++i) {
            const auto& exact = expected[i];
            const float stored = static_cast<float>(std::sqrt(static_cast<double>(static_cast<float>(exact.squared))));
            require(query.getPointScalarValue(i) == stored, "전수 기준 거리 불일치");
            require(cp.getPointScalarValue(i) == exact.triangle, "전수 기준 삼각형 번호 불일치");
            const auto* actual = cp.getPoint(i);
            require(actual->x == exact.point.x && actual->y == exact.point.y && actual->z == exact.point.z,
                    "전수 기준 최근접 좌표 불일치");
        }
        require(cp.getPointScalarValue(0) == 0, "동거리 최저 원본 번호 선택 오류");
    }
    require(tree.computeDistances(query, nullptr, 1) == 0, "최근접점 없는 거리 계산 실패");
    require(tree.computeDistances(query, nullptr, 1, nullptr, tree.residentBytes()) == Distances::ERROR_OUT_OF_MEMORY,
            "조회 예산 초과 허용");
    Progress canceled;
    canceled.cancel = true;
    require(tree.computeDistances(query, nullptr, 1, &canceled) == Distances::CANCELED_BY_USER, "조회 취소 실패");
    Progress during;
    during.stopAt = 1;
    require(tree.computeDistances(query, nullptr, 4, &during) == Distances::CANCELED_BY_USER, "실행 중 취소 실패");
    ccalign::MeshBvh canceledTree;
    require(!canceledTree.build(mesh, vertices, Budget, &canceled), "생성 취소 실패");
    Cloud invalid;
    require(invalid.resize(1), "잘못된 좌표 시험 준비 실패");
    // addPoint는 NaN을 원점으로 치환하므로 저장 좌표에 직접 잘못된 값을 주입합니다.
    *const_cast<CCVector3*>(invalid.getPoint(0)) = CCVector3(std::numeric_limits<float>::quiet_NaN(), 0, 0);
    require(std::isnan(invalid.getPoint(0)->x), "NaN 시험 입력 준비 오류");
    const int invalidResult = tree.computeDistances(invalid, nullptr, 1);
    require(invalidResult == Distances::INVALID_INPUT,
        (std::string("유한하지 않은 좌표 반환 코드 불일치: 실제=") + std::to_string(invalidResult)
            + ", 기대=" + std::to_string(Distances::INVALID_INPUT)).c_str());
}

// 병렬 경계 집계와 홀수 분할을 포함해 생성 스레드 수·반복에 무관한 정확한 검색 결과를 검사합니다.
void checkParallelBuildScenes() {
    for (unsigned scene = 0; scene < 4; ++scene) {
        const unsigned Triangles = scene == 0 ? 262145u : 65536u;
        Cloud vertices, query;
        Mesh mesh(&vertices);
        require(vertices.reserve(Triangles * 3) && mesh.reserve(Triangles) && query.reserve(6),
                "대형 생성 시험 예약 실패");
        for (unsigned i = 0; i < Triangles; ++i) {
            const float x = static_cast<float>((i * 73u) % 256u);
            const float y = static_cast<float>(((i * 73u) / 256u) % 256u);
            const float z = scene == 3 ? 0.0f : static_cast<float>(i % 17u);
            if (scene == 1) {
                const float size = 1.0f + static_cast<float>(i % 127u) / 128.0f;
                vertices.addPoint(CCVector3(-size, -size, 0));
                vertices.addPoint(CCVector3(size, -size, 0));
                vertices.addPoint(CCVector3(0, size, 0));
            } else if (scene == 2) {
                vertices.addPoint(CCVector3(0, 0, i % 2 ? -1.0f : 1.0f));
                vertices.addPoint(CCVector3(1, 0, i % 2 ? -1.0f : 1.0f));
                vertices.addPoint(CCVector3(0, 1, i % 2 ? -1.0f : 1.0f));
            } else {
                vertices.addPoint(CCVector3(x, y, z));
                vertices.addPoint(CCVector3(x + 0.75f, y, z));
                vertices.addPoint(CCVector3(x, y + 0.5f, z));
            }
            mesh.addTriangle(i * 3, i * 3 + 1, i * 3 + 2);
        }
        query.addPoint(CCVector3(0.25f, 0.25f, 0));
        query.addPoint(CCVector3(0, 0, 2));
        query.addPoint(CCVector3(17.5f, 25.25f, 3.5f));
        query.addPoint(CCVector3(128, 128, -4));
        query.addPoint(CCVector3(-1000, 2000, 1000));
        query.addPoint(CCVector3(255, 255, 16));
        std::vector<Expected> expected;
        for (unsigned i = 0; i < query.size(); ++i) expected.push_back(exhaustive(*query.getPoint(i), mesh));
        ccalign::MeshBvh tree;
        const auto emptyBytes = tree.residentBytes();
        for (unsigned threads : {1u, 2u, 4u, 0u}) {
            const auto exactBudget = ccalign::MeshBvh::estimatePeakBuildBytes(Triangles, threads);
            require(exactBudget > 0 && exactBudget <= Budget, "대형 생성 예상 예산 오류");
            for (unsigned repeat = 0; repeat < 2; ++repeat) {
                require(tree.build(mesh, vertices, exactBudget, nullptr, threads), "정확 예산 대형 생성 실패");
                require(tree.peakBuildBytes() == exactBudget, "사전 예산과 실제 생성 예산 불일치");
                require(tree.residentBytes() <= exactBudget, "생성 후 상주 예산 초과");
                Cloud cp;
                require(tree.computeDistances(query, &cp, 1, nullptr, Budget) == 0, "대형 생성 후 조회 실패");
                require(cp.size() == query.size(), "대형 생성 최근접점 크기 불일치");
                for (unsigned i = 0; i < query.size(); ++i) {
                    const auto& exact = expected[i];
                    const float distance = static_cast<float>(std::sqrt(static_cast<double>(static_cast<float>(exact.squared))));
                    require(query.getPointScalarValue(i) == distance, "생성 병렬화 전수 거리 불일치");
                    require(cp.getPointScalarValue(i) == exact.triangle, "생성 병렬화 원본 면 번호 불일치");
                    const auto* actual = cp.getPoint(i);
                    require(actual->x == exact.point.x && actual->y == exact.point.y && actual->z == exact.point.z,
                            "생성 병렬화 최근접 좌표 불일치");
                }
            }
            require(!tree.build(mesh, vertices, exactBudget - 1, nullptr, threads), "생성 예산 경계 초과 허용");
            require(tree.residentBytes() == emptyBytes, "예산 부족 후 기존 배열 미해제");
            require(!tree.lastError().empty(), "예산 부족 진단 누락");
            require(tree.build(mesh, vertices, exactBudget, nullptr, threads), "예산 부족 후 재생성 실패");
        }
    }
}

// 실제 면 읽기 이후 시간 취소·콜백 예외·비유한 입력의 정리와 재생성을 검사합니다.
void checkBuildFailureRecovery() {
    constexpr unsigned Triangles = 65536;
    Cloud vertices;
    require(vertices.reserve(3), "생성 실패 시험 정점 예약 실패");
    vertices.addPoint(CCVector3(0, 0, 0));
    vertices.addPoint(CCVector3(1, 0, 0));
    vertices.addPoint(CCVector3(0, 1, 0));
    CountingMesh mesh(&vertices);
    require(mesh.reserve(Triangles), "생성 실패 시험 면 예약 실패");
    for (unsigned i = 0; i < Triangles; ++i) mesh.addTriangle(0, 1, 2);
    ccalign::MeshBvh tree;
    const auto emptyBytes = tree.residentBytes();
    for (unsigned threads : {1u, 2u, 4u, 0u}) {
        for (bool throws : {false, true}) {
            mesh.reads.store(0, std::memory_order_relaxed);
            mesh.delayReads = true;
            TimedProgress progress;
            progress.throwOnUpdate = throws;
            require(!tree.build(mesh, vertices, Budget, &progress, threads), "생성 중 취소 또는 콜백 예외 무시");
            require(!progress.wrongThread.load(std::memory_order_relaxed), "생성 콜백의 호출 스레드 위반");
            require(std::chrono::steady_clock::now() - progress.started < std::chrono::seconds(3),
                    "생성 중 취소 또는 예외 응답 지연");
            const auto reads = mesh.reads.load(std::memory_order_relaxed);
            require(reads > 0 && reads < Triangles, "실제 생성 시작 전 또는 전체 읽기 후 중단");
            require(tree.residentBytes() == emptyBytes, "생성 중단 후 배열 미해제");
            require(!tree.lastError().empty(), "생성 중단 진단 누락");
            mesh.delayReads = false;
            require(tree.build(mesh, vertices, Budget, nullptr, threads), "생성 중단 후 재생성 실패");
        }
        for (float invalid : {std::numeric_limits<float>::quiet_NaN(), std::numeric_limits<float>::infinity()}) {
            *const_cast<CCVector3*>(vertices.getPoint(0)) = CCVector3(invalid, 0, 0);
            require(!tree.build(mesh, vertices, Budget, nullptr, threads), "비유한 생성 입력 허용");
            require(tree.residentBytes() == emptyBytes, "비유한 입력 실패 후 배열 미해제");
            require(!tree.lastError().empty(), "비유한 입력 진단 누락");
            *const_cast<CCVector3*>(vertices.getPoint(0)) = CCVector3(0, 0, 0);
            require(tree.build(mesh, vertices, Budget, nullptr, threads), "비유한 입력 복구 후 재생성 실패");
        }
    }
}

// 부동소수점 크기·평면·중복·생성 병렬화·예산·취소를 한 번에 검증합니다.
int main() {
    try {
        checkScene(1.0f, 0.0f);
        checkScene(0.000001f, 0.0f);
        checkScene(1024.0f, 100000000.0f);
        checkTraversalCancellation();
        checkParallelBuildScenes();
        checkBuildFailureRecovery();
        std::cout << "BVH 전수 기준 단위시험 통과\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
