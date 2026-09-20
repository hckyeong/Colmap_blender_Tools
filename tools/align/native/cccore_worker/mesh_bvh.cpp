// SPDX-License-Identifier: GPL-3.0-or-later
#include "mesh_bvh.h"
#include <DistanceComputationTools.h>
#include <GenericIndexedCloudPersist.h>
#include <GenericIndexedMesh.h>
#include <GenericProgressCallback.h>
#include <PointCloud.h>
#include <SimpleTriangle.h>
#include <tbb/blocked_range.h>
#include <tbb/parallel_for.h>
#include <tbb/parallel_invoke.h>
#include <tbb/parallel_reduce.h>
#include <tbb/task_arena.h>
#include <tbb/info.h>
#include <tbb/global_control.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <chrono>
#include <condition_variable>
#include <exception>
#include <limits>
#include <mutex>
#include <new>

namespace ccalign {
using DistanceTools = CCCoreLib::DistanceComputationTools;
namespace {
constexpr unsigned LeafSize = 8;
constexpr std::uint64_t FixedAllowance = 64 * 1024;
constexpr std::uint64_t ThreadAllowance = 1024 * 1024;
// 병렬 실행 수와 메모리 계산에 같은 스레드 상한을 적용합니다.
unsigned threadCount(unsigned requested) {
    const unsigned available = static_cast<unsigned>((std::max)(1, tbb::info::default_concurrency()));
    return requested ? (std::min)(requested, available) : available;
}
}

// 중앙 분할의 같은 크기 하위 구간은 한 번만 계산합니다.
std::uint64_t MeshBvh::nodeCount(unsigned count) {
    if (count <= LeafSize) return count ? 1 : 0;
    const unsigned half = count / 2;
    const auto left = nodeCount(half);
    return 1 + left + (count % 2 ? nodeCount(half + 1) : left);
}

// 임시·최종 배열과 실제 작업자 스택 및 태스크 여유를 함께 집계합니다.
std::uint64_t MeshBvh::estimatePeakBuildBytes(unsigned count, unsigned maxThreads) {
    const auto stackBytes = static_cast<std::uint64_t>(
        tbb::global_control::active_value(tbb::global_control::thread_stack_size));
    const auto workerBytes = (std::max)(ThreadAllowance, stackBytes) + FixedAllowance;
    return sizeof(MeshBvh) + FixedAllowance + std::uint64_t(threadCount(maxThreads)) * workerBytes
        + nodeCount(count) * sizeof(Node)
        + std::uint64_t(count) * (sizeof(Primitive) + sizeof(std::uint32_t));
}

// 출력 좌표·스칼라 신규 할당과 스레드별 스택 여유를 포함합니다.
std::uint64_t MeshBvh::estimateQueryBytes(unsigned count, unsigned threads, bool cp) {
    const auto stackBytes = static_cast<std::uint64_t>(
        tbb::global_control::active_value(tbb::global_control::thread_stack_size));
    const auto workerBytes = (std::max)(ThreadAllowance, stackBytes) + FixedAllowance;
    return FixedAllowance + std::uint64_t(threadCount(threads)) * workerBytes
        + std::uint64_t(count) * (sizeof(Hit) + sizeof(float)
            + (cp ? sizeof(CCVector3) + sizeof(float) : 0));
}

// 용량을 기준으로 계산하여 사용하지 않는 예약 영역도 집계합니다.
std::uint64_t MeshBvh::residentBytes() const {
    return sizeof(MeshBvh) + nodes_.capacity() * sizeof(Node) + ids_.capacity() * sizeof(std::uint32_t);
}

// 경계와 하위 트리를 병렬 생성하고 콜백은 호출 스레드에서만 처리합니다.
bool MeshBvh::build(CCCoreLib::GenericIndexedMesh& mesh, CCCoreLib::GenericIndexedCloudPersist& vertices,
                    std::uint64_t budget, CCCoreLib::GenericProgressCallback* progress, unsigned maxThreads) {
    mesh_ = nullptr;
    std::vector<Node>().swap(nodes_);
    std::vector<std::uint32_t>().swap(ids_);
    error_.clear();
    budgetBytes_ = budget;
    peakBytes_ = estimatePeakBuildBytes(mesh.size(), maxThreads);
    if (!mesh.size() || !vertices.size()) { error_ = "BVH 대상 기하가 비어 있습니다."; return false; }
    if (peakBytes_ > budget) { error_ = "BVH 생성 메모리 예산이 부족합니다."; return false; }
    try {
        if (progress && progress->isCancelRequested()) { error_ = "BVH 생성이 취소되었습니다."; return false; }
        std::vector<Primitive> primitives(mesh.size());
        ids_.resize(mesh.size());
        nodes_.resize(static_cast<std::size_t>(nodeCount(mesh.size())));
        tbb::task_arena arena(static_cast<int>(threadCount(maxThreads)), 0);
        std::atomic<bool> canceled{false}, invalid{false};
        std::atomic<unsigned> prepared{0}, completed{0};
        // 삼각형별 독립 칸에 경계를 기록한 뒤 서로 겹치지 않는 노드 구간을 생성합니다.
        const auto construct = [&] {
            tbb::parallel_for(tbb::blocked_range<unsigned>(0, mesh.size(), 4096), [&](const auto& range) {
                unsigned done = 0;
                for (unsigned i = range.begin(); i < range.end(); ++i) {
                    if (canceled.load(std::memory_order_relaxed)) break;
                    CCVector3 a, b, c;
                    mesh.getTriangleVertices(i, a, b, c);
                    auto& primitive = primitives[i];
                    primitive.id = i;
                    for (unsigned axis = 0; axis < 3; ++axis) {
                        if (!std::isfinite(a.u[axis]) || !std::isfinite(b.u[axis]) || !std::isfinite(c.u[axis])) {
                            invalid.store(true, std::memory_order_relaxed);
                            canceled.store(true, std::memory_order_relaxed);
                            break;
                        }
                        primitive.lo[axis] = (std::min)({a.u[axis], b.u[axis], c.u[axis]});
                        primitive.hi[axis] = (std::max)({a.u[axis], b.u[axis], c.u[axis]});
                    }
                    ++done;
                }
                prepared.fetch_add(done, std::memory_order_relaxed);
            });
            if (!canceled.load(std::memory_order_relaxed)
                && buildNode(primitives, 0, mesh.size(), 0, 1, canceled, completed))
                completed.store(mesh.size(), std::memory_order_relaxed);
        };
        if (!progress) {
            arena.execute(construct);
        } else {
            std::mutex completionMutex;
            std::condition_variable completion;
            bool finished = false;
            std::exception_ptr workerError;
            std::unique_lock<std::mutex> lock(completionMutex);
            // 예외와 취소가 발생해도 작업 완료 통지까지 모든 참조의 수명을 유지합니다.
            arena.enqueue([&] {
                try { construct(); } catch (...) { workerError = std::current_exception(); }
                std::lock_guard<std::mutex> guard(completionMutex);
                finished = true;
                completion.notify_one();
            });
            std::exception_ptr callbackError;
            while (!finished) {
                completion.wait_for(lock, std::chrono::milliseconds(10));
                if (finished) break;
                lock.unlock();
                try {
                    if (!callbackError) {
                        progress->update((30.0f * prepared.load(std::memory_order_relaxed)
                            + 70.0f * completed.load(std::memory_order_relaxed)) / mesh.size());
                        if (progress->isCancelRequested()) canceled.store(true, std::memory_order_relaxed);
                    }
                } catch (...) {
                    callbackError = std::current_exception();
                    canceled.store(true, std::memory_order_relaxed);
                }
                lock.lock();
            }
            lock.unlock();
            if (callbackError) std::rethrow_exception(callbackError);
            if (workerError) std::rethrow_exception(workerError);
            if (progress->isCancelRequested()) canceled.store(true, std::memory_order_relaxed);
        }
        if (invalid.load(std::memory_order_relaxed)) error_ = "BVH 대상 좌표가 유한하지 않습니다.";
        else if (canceled.load(std::memory_order_relaxed)) error_ = "BVH 생성이 취소되었습니다.";
        else {
            if (progress) {
                progress->update(100.0f);
                if (progress->isCancelRequested()) error_ = "BVH 생성이 취소되었습니다.";
            }
            if (error_.empty()) { mesh_ = &mesh; return true; }
        }
    } catch (const std::bad_alloc&) { error_ = "BVH 메모리 할당에 실패했습니다."; }
    catch (...) { error_ = "BVH 생성 중 내부 오류가 발생했습니다."; }
    std::vector<Node>().swap(nodes_);
    std::vector<std::uint32_t>().swap(ids_);
    return false;
}

// 동일 중심은 원본 번호로 정렬하여 분할과 동거리 결과를 재현합니다.
bool MeshBvh::buildNode(std::vector<Primitive>& primitives, unsigned first, unsigned count, unsigned index,
                        unsigned nextFree, std::atomic<bool>& canceled, std::atomic<unsigned>& completed) {
    if (canceled.load(std::memory_order_relaxed)) return false;
    struct Bounds {
        PointCoordinateType lo[3], hi[3];
        double centerLo[3], centerHi[3];
    };
    Bounds initial{};
    for (unsigned axis = 0; axis < 3; ++axis) {
        initial.lo[axis] = std::numeric_limits<PointCoordinateType>::infinity();
        initial.hi[axis] = -initial.lo[axis];
        initial.centerLo[axis] = std::numeric_limits<double>::infinity();
        initial.centerHi[axis] = -initial.centerLo[axis];
    }
    // 원본 순열 구간의 상자와 배정도 중심 극값을 같은 연산으로 계산합니다.
    const auto accumulate = [&](const tbb::blocked_range<unsigned>& range, Bounds bounds) {
        for (unsigned i = range.begin(); i < range.end(); ++i) {
            if ((i % 4096 == 0) && canceled.load(std::memory_order_relaxed)) break;
            const auto& p = primitives[i];
            for (unsigned axis = 0; axis < 3; ++axis) {
                bounds.lo[axis] = (std::min)(bounds.lo[axis], p.lo[axis]);
                bounds.hi[axis] = (std::max)(bounds.hi[axis], p.hi[axis]);
                const double center = double(p.lo[axis]) + double(p.hi[axis]);
                bounds.centerLo[axis] = (std::min)(bounds.centerLo[axis], center);
                bounds.centerHi[axis] = (std::max)(bounds.centerHi[axis], center);
            }
        }
        return bounds;
    };
    // 왼쪽 구간을 먼저 합쳐 같은 극값의 선택 순서도 원본 순회와 일치시킵니다.
    const auto combine = [](Bounds left, const Bounds& right) {
        for (unsigned axis = 0; axis < 3; ++axis) {
            left.lo[axis] = (std::min)(left.lo[axis], right.lo[axis]);
            left.hi[axis] = (std::max)(left.hi[axis], right.hi[axis]);
            left.centerLo[axis] = (std::min)(left.centerLo[axis], right.centerLo[axis]);
            left.centerHi[axis] = (std::max)(left.centerHi[axis], right.centerHi[axis]);
        }
        return left;
    };
    const tbb::blocked_range<unsigned> range(first, first + count, 16384);
    const Bounds bounds = count >= 262144
        ? tbb::parallel_reduce(range, initial, accumulate, combine)
        : accumulate(range, initial);
    if (canceled.load(std::memory_order_relaxed)) return false;
    Node node{};
    for (unsigned axis = 0; axis < 3; ++axis) {
        node.lo[axis] = bounds.lo[axis];
        node.hi[axis] = bounds.hi[axis];
    }
    // 배정도 보간 오차와 단정도 저장 반올림을 모두 바깥쪽 여유로 흡수합니다.
    for (unsigned axis = 0; axis < 3; ++axis) {
        const double low = node.lo[axis], high = node.hi[axis];
        const double padding = 64.0 * std::numeric_limits<double>::epsilon()
            * (std::max)({std::abs(low), std::abs(high), high - low});
        node.lo[axis] = std::nextafter(static_cast<PointCoordinateType>(low - padding),
            -std::numeric_limits<PointCoordinateType>::infinity());
        node.hi[axis] = std::nextafter(static_cast<PointCoordinateType>(high + padding),
            std::numeric_limits<PointCoordinateType>::infinity());
    }
    if (count <= LeafSize) {
        node.first = first;
        node.count = count;
        for (unsigned i = first; i < first + count; ++i) ids_[i] = primitives[i].id;
        nodes_[index] = node;
        return true;
    }
    unsigned axis = 0;
    for (unsigned i = 1; i < 3; ++i)
        if (bounds.centerHi[i] - bounds.centerLo[i] > bounds.centerHi[axis] - bounds.centerLo[axis]) axis = i;
    const unsigned half = count / 2;
    // 중심 좌표와 원본 번호를 합쳐 엄격한 순서를 유지합니다.
    const auto less = [axis](const Primitive& a, const Primitive& b) {
        const double ac = double(a.lo[axis]) + double(a.hi[axis]);
        const double bc = double(b.lo[axis]) + double(b.hi[axis]);
        return ac < bc || (ac == bc && a.id < b.id);
    };
    std::nth_element(primitives.begin() + first, primitives.begin() + first + half,
                     primitives.begin() + first + count, less);
    node.first = nextFree;
    nodes_[index] = node;
    const unsigned leftDescendants = nextFree + 2;
    const unsigned rightDescendants = nextFree + 1 + static_cast<unsigned>(nodeCount(half));
    bool leftOk = false, rightOk = false;
    // 직접 자식은 연속 배치하고 각 자손은 독립된 선할당 구간에 기록합니다.
    const auto left = [&] {
        leftOk = buildNode(primitives, first, half, nextFree, leftDescendants, canceled, completed);
        if (leftOk && count >= 65536 && half < 65536)
            completed.fetch_add(half, std::memory_order_relaxed);
    };
    // 직렬 처리한 하위 구간마다 진행량을 합쳐 리프별 원자 연산 경합을 피합니다.
    const auto right = [&] {
        rightOk = buildNode(primitives, first + half, count - half, nextFree + 1,
            rightDescendants, canceled, completed);
        if (rightOk && count >= 65536 && count - half < 65536)
            completed.fetch_add(count - half, std::memory_order_relaxed);
    };
    if (count >= 65536) tbb::parallel_invoke(left, right);
    else { left(); right(); }
    return leftOk && rightOk;
}

// 보수적인 상자에서 배정도로 하한을 구하고 마지막 반올림도 낮춥니다.
double MeshBvh::lowerBound(const Node& node, const CCVector3& point) {
    double squared = 0;
    for (unsigned axis = 0; axis < 3; ++axis) {
        const double distance = (std::max)({double(node.lo[axis]) - point.u[axis],
                                            double(point.u[axis]) - node.hi[axis], 0.0});
        squared += distance * distance;
    }
    return squared * (1.0 - 16.0 * std::numeric_limits<double>::epsilon());
}

// 균등 중앙 분할의 최대 깊이는 32 미만이므로 고정 스택이면 충분합니다.
MeshBvh::Hit MeshBvh::nearest(const CCVector3& point, const std::atomic<bool>& canceled) const {
    Hit hit{std::numeric_limits<ScalarType>::infinity(), CCVector3(0, 0, 0), UINT32_MAX};
    struct Entry { unsigned node; double lower; };
    std::array<Entry, 64> stack{};
    unsigned size = 1;
    unsigned visited = 0;
    stack[0] = {0, 0};
    while (size) {
        if ((++visited % 256 == 0) && canceled.load(std::memory_order_relaxed)) return hit;
        const auto current = stack[--size];
        if (current.lower > hit.squaredDistance) continue;
        const auto& node = nodes_[current.node];
        if (node.count) {
            for (unsigned i = node.first; i < node.first + node.count; ++i) {
                if ((++visited % 256 == 0) && canceled.load(std::memory_order_relaxed)) return hit;
                CCCoreLib::SimpleTriangle triangle;
                mesh_->getTriangleVertices(ids_[i], triangle.A, triangle.B, triangle.C);
                CCVector3 closest;
                const auto distance = CCCoreLib::DistanceComputationTools::computePoint2TriangleDistance(
                    &point, &triangle, false, &closest);
                if (std::isfinite(distance) && (distance < hit.squaredDistance
                    || (distance == hit.squaredDistance && ids_[i] < hit.triangle)))
                    hit = {distance, closest, ids_[i]};
            }
        } else {
            Entry left{node.first, lowerBound(nodes_[node.first], point)};
            Entry right{node.first + 1, lowerBound(nodes_[node.first + 1], point)};
            if (left.lower > right.lower) std::swap(left, right);
            if (right.lower <= hit.squaredDistance) stack[size++] = right;
            if (left.lower <= hit.squaredDistance) stack[size++] = left;
        }
    }
    return hit;
}

// 콜백과 스칼라 오프셋 변경은 호출 스레드에서만 수행합니다.
int MeshBvh::computeDistances(CCCoreLib::GenericIndexedCloudPersist& query, CCCoreLib::PointCloud* cpSet,
                              unsigned maxThreads, CCCoreLib::GenericProgressCallback* progress,
                              std::uint64_t budgetBytes) const {
    if (!mesh_) return DistanceTools::ERROR_NULL_REFERENCEMESH;
    if (!query.size()) return DistanceTools::ERROR_EMPTY_COMPAREDCLOUD;
    if (progress && progress->isCancelRequested()) return DistanceTools::CANCELED_BY_USER;
    const auto extra = estimateQueryBytes(query.size(), maxThreads, cpSet != nullptr);
    const auto budget = budgetBytes ? budgetBytes : budgetBytes_;
    if (residentBytes() > budget || extra > budget - residentBytes()) return DistanceTools::ERROR_OUT_OF_MEMORY;
    try {
        std::vector<Hit> hits(query.size());
        // 호출 스레드는 콜백을 처리하므로 단일 스레드에서도 작업자 슬롯을 확보합니다.
        tbb::task_arena arena(static_cast<int>(threadCount(maxThreads)), 0);
        std::atomic<bool> canceled{false};
        std::atomic<unsigned> completed{0};
        // 작업마다 별도 결과 칸을 기록하며 순회 내부에서도 취소 신호를 확인합니다.
        const auto search = [&] {
            tbb::parallel_for(tbb::blocked_range<unsigned>(0, query.size(), 32), [&](const auto& range) {
                for (unsigned i = range.begin(); i < range.end(); ++i) {
                    if (canceled.load(std::memory_order_relaxed)) break;
                    CCVector3 point;
                    query.getPoint(i, point);
                    if (!std::isfinite(point.x) || !std::isfinite(point.y) || !std::isfinite(point.z))
                        hits[i] = {std::numeric_limits<ScalarType>::infinity(), CCVector3(0, 0, 0), UINT32_MAX};
                    else hits[i] = nearest(point, canceled);
                    completed.fetch_add(1, std::memory_order_relaxed);
                }
            });
        };
        if (!progress) {
            arena.execute(search);
        } else {
            std::mutex completionMutex;
            std::condition_variable completion;
            bool finished = false;
            std::exception_ptr workerError;
            std::unique_lock<std::mutex> lock(completionMutex);
            // 모든 예외를 호출 스레드로 전달하고 완료 통지까지 지역 참조 수명을 보장합니다.
            arena.enqueue([&] {
                try { search(); } catch (...) { workerError = std::current_exception(); }
                {
                    std::lock_guard<std::mutex> guard(completionMutex);
                    finished = true;
                    completion.notify_one();
                }
            });
            std::exception_ptr callbackError;
            while (!finished) {
                completion.wait_for(lock, std::chrono::milliseconds(10));
                if (finished) break;
                lock.unlock();
                try {
                    if (!callbackError) {
                        progress->update(100.0f * static_cast<float>(completed.load(std::memory_order_relaxed)) / query.size());
                        if (progress->isCancelRequested()) canceled.store(true, std::memory_order_relaxed);
                    }
                } catch (...) {
                    callbackError = std::current_exception();
                    canceled.store(true, std::memory_order_relaxed);
                }
                lock.lock();
            }
            lock.unlock();
            if (callbackError) std::rethrow_exception(callbackError);
            if (workerError) std::rethrow_exception(workerError);
            if (canceled.load(std::memory_order_relaxed)) return DistanceTools::CANCELED_BY_USER;
            progress->update(100.0f);
        }
        if (progress && progress->isCancelRequested()) return DistanceTools::CANCELED_BY_USER;
        for (const auto& hit : hits) if (hit.triangle == UINT32_MAX) return DistanceTools::INVALID_INPUT;
        if (!query.enableScalarField()) return DistanceTools::ERROR_ENABLE_SCALAR_FIELD_FAILURE;
        if (cpSet && !cpSet->resize(query.size())) return DistanceTools::ERROR_OUT_OF_MEMORY;
        if (cpSet && !cpSet->enableScalarField()) return DistanceTools::ERROR_ENABLE_SCALAR_FIELD_FAILURE;
        query.setPointScalarValues(CCCoreLib::NAN_VALUE);
        query.setPointScalarValue(0, 0);
        // CCCoreLib과 동일하게 CPSet의 삼각형 번호 오프셋을 0으로 고정합니다.
        if (cpSet) cpSet->getCurrentInScalarField()->fill(0);
        for (unsigned i = 0; i < query.size(); ++i) {
            // 기존 CC 경로의 제곱거리 스칼라 저장 후 제곱근 변환 순서를 유지합니다.
            query.setPointScalarValue(i, hits[i].squaredDistance);
            query.setPointScalarValue(i, std::sqrt(std::abs(query.getPointScalarValue(i))));
            if (cpSet) {
                *const_cast<CCVector3*>(cpSet->getPoint(i)) = hits[i].point;
                cpSet->setPointScalarValue(i, static_cast<ScalarType>(hits[i].triangle));
            }
        }
        return DistanceTools::SUCCESS;
    } catch (const std::bad_alloc&) { return DistanceTools::ERROR_OUT_OF_MEMORY; }
    catch (...) { return DistanceTools::ERROR_INTERNAL; }
}
}
