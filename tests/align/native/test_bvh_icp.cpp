// SPDX-License-Identifier: GPL-3.0-or-later
#include "mesh_bvh.h"
#include <DistanceComputationTools.h>
#include <GenericProgressCallback.h>
#include <PointCloud.h>
#include <RegistrationTools.h>
#include <SimpleMesh.h>
#include <SimpleTriangle.h>
#include <json.hpp>
#include <cmath>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>

using Cloud = CCCoreLib::PointCloud;
using Mesh = CCCoreLib::SimpleMesh;
using Distances = CCCoreLib::DistanceComputationTools;
using Registration = CCCoreLib::ICPRegistrationTools;
using Json = nlohmann::json;
constexpr std::uint64_t Budget = 512ull * 1024 * 1024;

// 최적화 빌드에서도 엄격한 검증 실패를 종료 코드로 전달합니다.
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

// 공간 가지치기 없이 모든 원본 삼각형의 배정도 거리와 최근접 점을 직접 비교합니다.
int exhaustiveDistances(void* opaque, CCCoreLib::GenericIndexedCloudPersist* query,
                        Cloud* cpSet, unsigned, CCCoreLib::GenericProgressCallback* progress) {
    auto& mesh = *static_cast<Mesh*>(opaque);
    require(query->enableScalarField(), "전수검색 거리 저장소 생성 실패");
    require(cpSet && cpSet->resize(query->size()) && cpSet->enableScalarField(), "전수검색 최근접 점 저장소 생성 실패");
    query->setPointScalarValues(CCCoreLib::NAN_VALUE);
    query->setPointScalarValue(0, 0);
    cpSet->getCurrentInScalarField()->fill(0);
    for (unsigned i = 0; i < query->size(); ++i) {
        if (progress && progress->isCancelRequested()) return Distances::CANCELED_BY_USER;
        double minimum = std::numeric_limits<double>::infinity();
        unsigned triangleId = UINT32_MAX;
        CCVector3 closest;
        for (unsigned t = 0; t < mesh.size(); ++t) {
            CCCoreLib::SimpleTriangle triangle;
            mesh.getTriangleVertices(t, triangle.A, triangle.B, triangle.C);
            CCVector3 candidate;
            const double squared = Distances::computePoint2TriangleDistance(query->getPoint(i), &triangle, false, &candidate);
            // 오름차순 원본 번호이므로 배정도 동거리에서는 가장 작은 번호가 유지됩니다.
            if (squared < minimum) {
                minimum = squared;
                closest = candidate;
                triangleId = t;
            }
        }
        require(triangleId != UINT32_MAX, "전수검색 최근접 삼각형 누락");
        // 코어 계약의 float 스칼라 저장 후 제곱근 변환을 그대로 적용합니다.
        query->setPointScalarValue(i, minimum);
        query->setPointScalarValue(i, std::sqrt(std::abs(query->getPointScalarValue(i))));
        *const_cast<CCVector3*>(cpSet->getPoint(i)) = closest;
        cpSet->setPointScalarValue(i, triangleId);
    }
    return Distances::SUCCESS;
}

// 같은 공개 ICP 문맥에 실제 BVH 검색을 연결합니다.
int bvhDistances(void* opaque, CCCoreLib::GenericIndexedCloudPersist* query,
                 Cloud* cpSet, unsigned threads, CCCoreLib::GenericProgressCallback* progress) {
    return static_cast<ccalign::MeshBvh*>(opaque)->computeDistances(*query, cpSet, threads, progress);
}

struct Outcome {
    CCCoreLib::PointProjectionTools::Transformation transform;
    double rms = -1;
    unsigned count = 0;
    Registration::RESULT_TYPE code = Registration::ICP_ERROR;
};

// 고정 입력과 동일 매개변수를 사용해 한 검색 방식의 실제 ICP 결과를 얻습니다.
Outcome run(const Json& fixture, double scale, bool filtered, bool outlier, unsigned threads, bool bvh) {
    Cloud vertices, data;
    require(vertices.reserve(static_cast<unsigned>(fixture.at("vertices").size())), "모델 점 예약 실패");
    require(data.reserve(static_cast<unsigned>(fixture.at("points").size()) + unsigned(outlier)), "이동 점 예약 실패");
    const double c = std::cos(.012), s = std::sin(.012);
    for (const auto& point : fixture.at("vertices")) {
        const double x = point[0], y = point[1], z = point[2];
        vertices.addPoint(CCVector3(static_cast<float>(scale * (c*x-s*y) + .014),
                                   static_cast<float>(scale * (s*x+c*y) - .009),
                                   static_cast<float>(scale * z + .011)));
    }
    for (const auto& point : fixture.at("points"))
        data.addPoint(CCVector3(point[0].get<float>(), point[1].get<float>(), point[2].get<float>()));
    if (outlier) data.addPoint(CCVector3(100, 120, 150));
    Mesh mesh(&vertices);
    require(mesh.reserve(static_cast<unsigned>(fixture.at("faces").size())), "삼각형 예약 실패");
    for (const auto& face : fixture.at("faces")) mesh.addTriangle(face[0], face[1], face[2]);
    ccalign::MeshBvh tree;
    if (bvh) require(tree.build(mesh, vertices, Budget), "BVH 생성 실패");
    Registration::Parameters params;
    params.convType = Registration::MAX_ITER_CONVERGENCE;
    params.nbMaxIterations = 100;
    params.samplingLimit = data.size();
    params.adjustScale = scale != 1.0;
    params.minScale = .9;
    params.maxScale = 1.1;
    params.filterOutFarthestPoints = filtered;
    params.finalOverlapRatio = filtered || outlier ? .9 : 1.0;
    params.maxThreadCount = static_cast<int>(threads);
    params.useC2MSignedDistances = false;
    CCCoreLib::ICPMeshRegistrationContext context;
    context.exactMeshDistances = bvh ? bvhDistances : exhaustiveDistances;
    context.exactMeshDistancesData = bvh ? static_cast<void*>(&tree) : static_cast<void*>(&mesh);
    Outcome result;
    result.code = Registration::RegisterWithMeshContext(&vertices, &mesh, &data, params,
        result.transform, result.rms, result.count, context);
    require(result.code < Registration::ICP_ERROR, "ICP 실행 실패");
    return result;
}

// 전수검색과 BVH의 점수·배율·행렬·RMS를 허용오차 없이 비교합니다.
void check(const Outcome& actual, const Outcome& expected) {
    require(actual.code == expected.code, "ICP 종료 코드 불일치");
    require(actual.count == expected.count, "ICP 최종 점수 불일치");
    require(actual.rms == expected.rms, "ICP RMS 불일치");
    require(actual.transform.s == expected.transform.s, "ICP 배율 불일치");
    require(actual.transform.R.isValid() == expected.transform.R.isValid(), "ICP 회전행렬 상태 불일치");
    for (unsigned row = 0; row < 3; ++row) {
        require(actual.transform.T.u[row] == expected.transform.T.u[row], "ICP 이동 불일치");
        if (actual.transform.R.isValid())
            for (unsigned column = 0; column < 3; ++column)
                require(actual.transform.R.getValue(row, column) == expected.transform.R.getValue(row, column),
                        "ICP 회전행렬 불일치");
    }
}

// 강체·배율·필터·먼 점 및 단일·병렬 검색의 모든 조합을 고정 입력으로 검사합니다.
int main(int argc, char** argv) {
    try {
        require(argc == 2, "고정 입력 JSON 경로 인자가 필요합니다.");
        std::ifstream stream(argv[1]);
        require(bool(stream), "고정 입력 파일을 열지 못했습니다.");
        Json fixture;
        stream >> fixture;
        require(fixture.at("points").size() == 400 && fixture.at("faces").size() == 4, "고정 입력 크기 불일치");
        unsigned cases = 0;
        for (double scale : {1.0, 1.025})
            for (bool filtered : {false, true})
                for (bool outlier : {false, true}) {
                    const auto expected = run(fixture, scale, filtered, outlier, 1, false);
                    for (unsigned threads : {1u, 4u}) {
                        check(run(fixture, scale, filtered, outlier, threads, true), expected);
                        ++cases;
                    }
                }
        std::cout << "BVH와 독립 전수검색 ICP " << cases << "개 조합 엄격 일치\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
