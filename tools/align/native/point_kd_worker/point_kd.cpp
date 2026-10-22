#include "point_kd.h"
#include <algorithm>
#include <cmath>
#include <numeric>
#include <stdexcept>

namespace point_kd {
// 유한한 원본점만 색인하고 점 배열 자체는 변경하지 않습니다.
Tree::Tree(const PointBuffer& positions, std::function<void()> cancellation)
    : positions_(positions), cancellation_(std::move(cancellation)) {
    indices_.reserve(positions.size());
    for (std::size_t i = 0; i < positions.size(); ++i) {
        if ((i & 4095) == 0) cancellation_();
        if (positions[i].allFinite()) indices_.push_back(i);
    }
    if (indices_.empty()) throw std::runtime_error("유효한 대상점이 없습니다.");
    nodes_.reserve(indices_.size() / 4 + 1);
    build(0, indices_.size());
}

// Artec에서 확인한 최장축 경계상자 중간 위치를 기준으로 번호만 양방향 분할합니다.
int Tree::build(std::size_t begin, std::size_t end) {
    cancellation_();
    Node node;
    node.begin = begin; node.end = end;
    node.low = node.high = positions_[indices_[begin]];
    for (std::size_t i = begin + 1; i < end; ++i) {
        if ((i & 8191) == 0) cancellation_();
        node.low = node.low.cwiseMin(positions_[indices_[i]]);
        node.high = node.high.cwiseMax(positions_[indices_[i]]);
    }
    const int index = static_cast<int>(nodes_.size());
    nodes_.push_back(node);
    if (end - begin > 16) {
        Eigen::Index axis = 0;
        (node.high - node.low).maxCoeff(&axis);
        // 배정도 극단값에서도 합의 오버플로 없이 공간 중간 위치를 계산합니다.
        const double midpoint = node.low[axis] * 0.5 + node.high[axis] * 0.5;
        auto middle = begin, upper = end;
        std::size_t scanned = 0;
        while (middle < upper) {
            while (middle < upper && positions_[indices_[middle]][axis] <= midpoint) {
                ++middle;
                if ((++scanned & 8191) == 0) cancellation_();
            }
            while (middle < upper && positions_[indices_[upper - 1]][axis] > midpoint) {
                --upper;
                if ((++scanned & 8191) == 0) cancellation_();
            }
            if (middle < upper) {
                std::swap(indices_[middle++], indices_[--upper]);
                if ((++scanned & 8191) == 0) cancellation_();
            }
        }
        if (middle == begin || middle == end) return index;
        const int left = build(begin, middle);
        const int right = build(middle, end);
        nodes_[index].left = left; nodes_[index].right = right;
    }
    return index;
}

// 적격점이 아직 없어도 공간 후보 존재 여부를 독립적으로 보존합니다.
Neighbor Tree::nearest(const Vec3& query, double squared_radius, const std::function<bool(std::size_t)>& eligible, std::size_t excluded) const {
    Neighbor best;
    best.squared_distance = squared_radius;
    std::size_t visited = 0;
    search(0, query, squared_radius, eligible, excluded, best, visited);
    return best;
}

// 노말 부적격점 이후도 정확하게 탐색하고 분리 불가능한 큰 잎에서도 취소를 확인합니다.
void Tree::search(int index, const Vec3& query, double radius, const std::function<bool(std::size_t)>& eligible, std::size_t excluded, Neighbor& best, std::size_t& visited) const {
    if ((++visited & 1023) == 0) cancellation_();
    const Node& node = nodes_[index];
    const Vec3 offset = (node.low - query).cwiseMax(Vec3::Zero()) + (query - node.high).cwiseMax(Vec3::Zero());
    if (offset.squaredNorm() > best.squared_distance) return;
    if (node.left < 0) {
        for (std::size_t i = node.begin; i < node.end; ++i) {
            if ((i & 4095) == 0) cancellation_();
            const auto candidate = indices_[i];
            if (candidate == excluded) continue;
            const double distance = (positions_[candidate] - query).squaredNorm();
            if (distance > radius) continue;
            best.spatial_candidate = true;
            if (distance <= best.squared_distance && eligible(candidate) &&
                (distance < best.squared_distance || candidate < best.index)) {
                best.index = candidate; best.squared_distance = distance;
            }
        }
        return;
    }
    const auto bound = [&](int child) {
        const Node& c = nodes_[child];
        return ((c.low - query).cwiseMax(Vec3::Zero()) + (query - c.high).cwiseMax(Vec3::Zero())).squaredNorm();
    };
    if (bound(node.left) <= bound(node.right)) {
        search(node.left, query, radius, eligible, excluded, best, visited);
        search(node.right, query, radius, eligible, excluded, best, visited);
    } else {
        search(node.right, query, radius, eligible, excluded, best, visited);
        search(node.left, query, radius, eligible, excluded, best, visited);
    }
}
}
