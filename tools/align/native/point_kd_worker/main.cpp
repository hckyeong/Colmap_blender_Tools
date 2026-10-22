#include "point_kd.h"
#include "relative_delta.h"
#include "shared_input.h"
#include <Eigen/Geometry>
#include <Eigen/SVD>
#include <oneapi/tbb/global_control.h>
#include <oneapi/tbb/parallel_for.h>
#include <json.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <numeric>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#ifdef _WIN32
#include <windows.h>
#endif

namespace fs = std::filesystem;
using json = nlohmann::json;
using point_kd::Vec3;
using Mat3 = Eigen::Matrix3d;
using Mat4 = Eigen::Matrix4d;
using Clock = std::chrono::steady_clock;
constexpr const char* protocol = "point-kd-native-v1";
constexpr std::size_t block_size = 2048;
constexpr auto no_index = std::numeric_limits<std::size_t>::max();
struct Cancelled : std::runtime_error {
    // 취소 상태를 일반적인 입력·수치 실패와 구분합니다.
    Cancelled() : std::runtime_error("정렬이 취소되었습니다.") {}
};
struct Cloud {
    point_kd::PointBuffer positions, normals;
    bool has_normals = false;
    std::size_t valid_points = 0, valid_normals = 0;
};
struct Pair {
    Vec3 query = Vec3::Zero();
    std::size_t target = no_index;
    double d2 = 0;
    bool active = false;
};
struct SearchStats {
    std::size_t count = 0, distance_rejected = 0, normal_rejected = 0, invalid_rejected = 0, trimmed = 0;
    double squared_error = 0;
};
struct Job {
    fs::path root, result_path, progress_path, cancel_path;
    json request, result, timings = json::object();
    Clock::time_point started = Clock::now();
    Mat4 total = Mat4::Identity();
    int max_iterations = 30, iteration = 0;
    bool refine_scale = false;
    double initial_rms = 0, current_rms = 0, current_objective_rms = 0;
    std::size_t correspondence_count = 0;
    mutable double reported_progress = 0;
    mutable std::atomic<std::int64_t> next_cancel_check{0};
    mutable std::atomic<bool> cancellation_latched{false};
    // 모든 스레드가 취소를 공유하되 파일 조회는 25ms마다 한 스레드만 수행합니다.
    void check_cancel(bool force = false) const {
        if (cancellation_latched.load(std::memory_order_relaxed)) throw Cancelled();
        if (cancel_path.empty()) return;
        const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
        auto expected = next_cancel_check.load(std::memory_order_relaxed);
        if (!force && (now < expected || !next_cancel_check.compare_exchange_strong(expected, now + 25, std::memory_order_relaxed))) return;
        if (fs::exists(cancel_path)) { cancellation_latched.store(true, std::memory_order_relaxed); throw Cancelled(); }
    }
    // 작업 폴더 밖이나 심볼릭 링크로 우회한 경로를 거부합니다.
    fs::path confined(const std::string& raw) const {
        const fs::path path = fs::u8path(raw);
        const fs::path candidate = fs::weakly_canonical(path.is_absolute() ? path : root / path);
        auto expected = root.begin(), actual = candidate.begin();
        for (; expected != root.end(); ++expected, ++actual) {
            if (actual == candidate.end()) throw std::runtime_error("작업 폴더 외부 경로입니다.");
            auto a = expected->native(), b = actual->native();
#ifdef _WIN32
            std::transform(a.begin(), a.end(), a.begin(), ::towlower);
            std::transform(b.begin(), b.end(), b.begin(), ::towlower);
#endif
            if (a != b) throw std::runtime_error("작업 폴더 외부 경로입니다.");
        }
        if (actual == candidate.end()) throw std::runtime_error("파일 경로가 필요합니다.");
        return candidate;
    }
    // JSON 임시 파일을 완성한 다음 원자적으로 교체합니다.
    void write(const fs::path& destination, const json& value) const {
        const fs::path temporary = destination.native() + fs::path(".tmp").native();
        {
            std::ofstream stream(temporary, std::ios::binary | std::ios::trunc);
            const auto bytes = value.dump(2);
            stream.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
            stream.flush();
            if (!stream) throw std::runtime_error("결과 파일을 기록하지 못했습니다.");
        }
#ifdef _WIN32
        const auto deadline = Clock::now() + std::chrono::seconds(1);
        while (!MoveFileExW(temporary.c_str(), destination.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            const auto error = GetLastError();
            if ((error != ERROR_ACCESS_DENIED && error != ERROR_SHARING_VIOLATION) || Clock::now() >= deadline)
                throw std::runtime_error("결과 파일을 교체하지 못했습니다. Windows 오류: " + std::to_string(error));
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
#else
        fs::rename(temporary, destination);
#endif
    }
    // 외부 런타임에 단계와 현재까지 승인된 변환을 전달합니다.
    void progress(const std::string& stage, double fraction) const;
    // 단계별 벽시계 시간을 누적합니다.
    void elapsed(const std::string& stage, Clock::time_point start) {
        timings[stage] = timings.value(stage, 0.0) + std::chrono::duration<double>(Clock::now() - start).count();
    }
};

// 행렬을 JSON의 행 우선 중첩 배열로 변환합니다.
json matrix_json(const Mat4& matrix) {
    json rows = json::array();
    for (int row = 0; row < 4; ++row) rows.push_back({matrix(row, 0), matrix(row, 1), matrix(row, 2), matrix(row, 3)});
    return rows;
}

// 진행 파일에는 결과에 반영된 유한 변환만 기록합니다.
void Job::progress(const std::string& stage, double fraction) const {
    if (progress_path.empty()) return;
    reported_progress = std::max(reported_progress, fraction);
    write(progress_path, {{"protocol_version", protocol}, {"stage", stage}, {"progress", reported_progress},
        {"iteration", iteration}, {"max_iterations", max_iterations}, {"delta_4x4", matrix_json(total)},
        {"initial_rms", initial_rms}, {"current_rms", current_rms}, {"current_objective_rms", current_objective_rms}, {"correspondence_count", correspondence_count}});
}

// 바이너리 길이를 확인한 뒤 고정 크기 배열을 취소 가능하게 읽습니다.
template<class T> std::vector<T> read_array(Job& job, const std::string& path, std::size_t count) {
    static_assert(std::is_trivially_copyable_v<T>, "바이너리 입력에는 단순 값 배열만 사용할 수 있습니다.");
    const auto resolved = job.confined(path);
    if (count > static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max()) / sizeof(T) ||
        !fs::is_regular_file(resolved) || fs::file_size(resolved) != count * sizeof(T))
        throw std::runtime_error("입력 바이너리의 원소 수 또는 길이가 잘못되었습니다.");
    std::vector<T> values(count);
    std::ifstream stream(resolved, std::ios::binary);
    constexpr std::size_t chunk = 4 * 1024 * 1024;
    auto* output = reinterpret_cast<char*>(values.data());
    const auto bytes = count * sizeof(T);
    for (std::size_t offset = 0; offset < bytes; offset += chunk) {
        job.check_cancel();
        stream.read(output + offset, static_cast<std::streamsize>(std::min(chunk, bytes - offset)));
        if (!stream) throw std::runtime_error("입력 바이너리를 읽지 못했습니다.");
    }
    return values;
}

// 파일 또는 검증한 공유 입력을 계산용 소유 배열 하나로 취소 가능하게 읽습니다.
template<class T> std::vector<T> read_input_array(Job& job, const json& descriptor, const std::string& field, std::size_t count) {
    static_assert(std::is_trivially_copyable_v<T>, "공유 입력은 단순 값 배열이어야 합니다.");
    const auto file = field + "_file", mapping = field + "_mapping";
    if (descriptor.contains(file) == descriptor.contains(mapping))
        throw std::runtime_error("파일과 공유 입력 중 하나만 지정해야 합니다.");
    if (descriptor.contains(file)) return read_array<T>(job, descriptor.at(file), count);
    if (count > static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max()) / sizeof(T))
        throw std::runtime_error("공유 입력 크기가 지원 범위를 넘습니다.");
    const auto& entry = descriptor.at(mapping);
    const auto bytes = count * sizeof(T);
    if (!entry.is_object() || entry.size() != 2 || !entry.contains("name") || !entry.contains("byte_count") ||
        !entry.at("byte_count").is_number_unsigned() || entry.at("byte_count").get<std::uint64_t>() != bytes)
        throw std::runtime_error("공유 입력 크기와 원소 수가 일치하지 않습니다.");
    const point_kd::SharedInput input(entry.at("name").get<std::string>(), bytes);
    std::vector<T> values(count);
    auto* output = reinterpret_cast<char*>(values.data());
    constexpr std::size_t chunk = 4 * 1024 * 1024;
    for (std::size_t offset = 0; offset < bytes; offset += chunk) {
        job.check_cancel();
        std::memcpy(output + offset, input.data() + offset, std::min(chunk, bytes - offset));
    }
    return values;
}

// 적재 버퍼를 그대로 소유하며 비유한 입력을 검증합니다.
Cloud load_cloud(Job& job, const json& descriptor) {
    const auto count = descriptor.at("point_count").get<std::size_t>();
    if (count < 3) throw std::runtime_error("세 점 이상의 원본점이 필요합니다.");
    Cloud cloud;
    cloud.positions = point_kd::PointBuffer(read_input_array<point_kd::Coordinates>(job, descriptor, "positions", count));
    for (std::size_t i = 0; i < count; ++i) {
        if ((i & 4095) == 0) job.check_cancel();
        if (cloud.positions[i].allFinite()) ++cloud.valid_points;
    }
    if (cloud.valid_points != count) throw std::runtime_error("입력 원본점에 비유한 좌표가 있습니다.");
    return cloud;
}

// 명시 법선은 적재 버퍼를 재사용하고 삼각형 법선은 같은 소유 버퍼에 누적합니다.
void make_normals(Job& job, Cloud& cloud, const json& descriptor) {
    const auto count = cloud.positions.size();
    const bool explicit_normals = descriptor.contains("normals_file") || descriptor.contains("normals_mapping");
    if (explicit_normals) {
        cloud.normals = point_kd::PointBuffer(read_input_array<point_kd::Coordinates>(job, descriptor, "normals", count));
        cloud.has_normals = true;
    } else cloud.normals = point_kd::PointBuffer(count);
    if (!explicit_normals && (descriptor.contains("triangles_file") || descriptor.contains("triangles_mapping"))) {
        const auto triangles = read_input_array<std::array<std::uint32_t, 3>>(job, descriptor, "triangles", descriptor.at("triangle_count"));
        cloud.has_normals = true;
        for (std::size_t i = 0; i < triangles.size(); ++i) {
            if ((i & 4095) == 0) job.check_cancel();
            const auto& t = triangles[i];
            if (t[0] >= count || t[1] >= count || t[2] >= count) throw std::runtime_error("삼각형의 정점 번호가 범위를 벗어났습니다.");
            const Vec3 normal = (cloud.positions[t[1]] - cloud.positions[t[0]]).cross(cloud.positions[t[2]] - cloud.positions[t[0]]);
            if (!normal.allFinite()) continue;
            cloud.normals[t[0]] += normal; cloud.normals[t[1]] += normal; cloud.normals[t[2]] += normal;
        }
    }
    for (std::size_t i = 0; i < count; ++i) {
        if ((i & 4095) == 0) job.check_cancel();
        const double length = cloud.normals[i].norm();
        if (cloud.positions[i].allFinite() && std::isfinite(length) && length > 1e-30) {
            cloud.normals[i] /= length; ++cloud.valid_normals;
        } else cloud.normals[i].setZero();
    }
}

// 합집합 중심과 두 입력 중 작은 유효 경계상자 대각선을 계산합니다.
std::pair<Vec3, double> normalization(Job& job, const Cloud& source, const Cloud& target) {
    Vec3 low = Vec3::Constant(std::numeric_limits<double>::infinity()), high = -low;
    double diameter = std::numeric_limits<double>::infinity();
    for (const Cloud* cloud : {&source, &target}) {
        Vec3 cloud_low = Vec3::Constant(std::numeric_limits<double>::infinity()), cloud_high = -cloud_low;
        for (std::size_t i = 0; i < cloud->positions.size(); ++i) {
            if ((i & 8191) == 0) job.check_cancel();
            if (cloud->positions[i].allFinite()) {
                cloud_low = cloud_low.cwiseMin(cloud->positions[i]); cloud_high = cloud_high.cwiseMax(cloud->positions[i]);
            }
        }
        low = low.cwiseMin(cloud_low); high = high.cwiseMax(cloud_high);
        diameter = std::min(diameter, (cloud_high - cloud_low).norm());
    }
    if (!std::isfinite(diameter) || diameter <= 0) throw std::runtime_error("입력 경계상자가 유한하지 않거나 크기가 없습니다.");
    return {low + 0.5 * (high - low), diameter};
}

// 최대 4096개의 결정적 원본 번호로 대상점의 최근접 간격 중앙값을 구합니다.
double spacing(Job& job, const point_kd::Tree& tree, const Cloud& target) {
    const std::size_t probes = std::min<std::size_t>(4096, target.positions.size());
    std::vector<double> distances(probes, -1);
    oneapi::tbb::parallel_for(std::size_t(0), probes, [&](std::size_t k) {
        job.check_cancel();
        const auto i = k * target.positions.size() / probes;
        if (!target.positions[i].allFinite()) return;
        const auto nearest = tree.nearest(target.positions[i], std::numeric_limits<double>::infinity(), [](std::size_t) { return true; }, i);
        if (nearest.index != no_index) distances[k] = std::sqrt(nearest.squared_distance);
    });
    distances.erase(std::remove_if(distances.begin(), distances.end(), [](double d) { return d < 0 || !std::isfinite(d); }), distances.end());
    if (distances.empty()) throw std::runtime_error("점 간격을 측정하지 못했습니다.");
    std::sort(distances.begin(), distances.end());
    return distances[distances.size() / 2];
}

// 독립 질의를 병렬 검색하며 Sim(3) 법선에는 균일 배율을 제외한 회전만 적용합니다.
SearchStats correspondences(Job& job, const Cloud& source, const Cloud& target, const point_kd::Tree& tree,
    const Mat4& transform, double radius, double cosine, bool normal_gate, bool plane, double trim_fraction, std::vector<Pair>& pairs) {
    const auto start = Clock::now();
    const auto blocks = (source.positions.size() + block_size - 1) / block_size;
    std::vector<SearchStats> stats(blocks);
    pairs.resize(source.positions.size());
    const Mat3 rotation = transform.topLeftCorner<3, 3>();
    const Mat3 normal_rotation = job.refine_scale ? Mat3(rotation / std::cbrt(rotation.determinant())) : rotation;
    const Vec3 translation = transform.topRightCorner<3, 1>();
    oneapi::tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) {
        job.check_cancel();
        auto& subtotal = stats[block];
        for (auto i = block * block_size; i < std::min(source.positions.size(), (block + 1) * block_size); ++i) {
            if ((i & 255) == 0) job.check_cancel();
            Pair pair;
            if (!source.positions[i].allFinite()) { ++subtotal.invalid_rejected; pairs[i] = pair; continue; }
            pair.query = rotation * source.positions[i] + translation;
            if (!pair.query.allFinite()) throw std::runtime_error("변환한 질의점이 유한하지 않습니다.");
            const Vec3 normal = normal_rotation * source.normals[i];
            if (normal_gate && normal.squaredNorm() < 0.5) { ++subtotal.normal_rejected; pairs[i] = pair; continue; }
            const auto found = tree.nearest(pair.query, radius * radius, [&](std::size_t j) {
                if ((plane || normal_gate) && target.normals[j].squaredNorm() < 0.5) return false;
                return !normal_gate || std::abs(normal.dot(target.normals[j])) + 1e-15 >= cosine;
            });
            if (found.index == no_index) {
                if (found.spatial_candidate) ++subtotal.normal_rejected; else ++subtotal.distance_rejected;
            } else { pair.target = found.index; pair.d2 = found.squared_distance; pair.active = true; ++subtotal.count; }
            pairs[i] = pair;
        }
    });
    SearchStats result;
    for (const auto& item : stats) {
        result.count += item.count; result.distance_rejected += item.distance_rejected;
        result.normal_rejected += item.normal_rejected; result.invalid_rejected += item.invalid_rejected;
    }
    job.elapsed("search", start);
    job.progress("TRIM", 0.2 + 0.75 * job.iteration / job.max_iterations);
    const auto trim_start = Clock::now();
    std::vector<std::size_t> ordered;
    ordered.reserve(result.count);
    for (std::size_t i = 0; i < pairs.size(); ++i) {
        if ((i & 4095) == 0) job.check_cancel();
        if (pairs[i].active) ordered.push_back(i);
    }
    // 같은 거리에서는 원본 질의 번호가 큰 쪽부터 제거합니다.
    std::size_t comparisons = 0;
    std::sort(ordered.begin(), ordered.end(), [&](std::size_t a, std::size_t b) {
        if ((++comparisons & 65535) == 0) job.check_cancel();
        return pairs[a].d2 < pairs[b].d2 || (pairs[a].d2 == pairs[b].d2 && a < b);
    });
    job.check_cancel();
    result.trimmed = static_cast<std::size_t>(std::floor(static_cast<double>(ordered.size()) * trim_fraction));
    for (std::size_t i = ordered.size() - result.trimmed; i < ordered.size(); ++i) pairs[ordered[i]].active = false;
    result.count -= result.trimmed;
    for (std::size_t i = 0; i < pairs.size(); ++i) {
        if ((i & 4095) == 0) job.check_cancel();
        if (pairs[i].active) result.squared_error += pairs[i].d2;
    }
    job.elapsed("trim", trim_start);
    return result;
}

// 고정 대응에서의 유클리드 또는 점평면 목적함수를 계산합니다.
double objective(Job& job, const std::vector<Pair>& pairs, const Cloud& target, const Mat4& delta, bool plane, double diameter) {
    const auto blocks = (pairs.size() + block_size - 1) / block_size;
    std::vector<double> sums(blocks, 0.0);
    const Mat3 rotation = delta.topLeftCorner<3, 3>();
    const Vec3 translation = delta.topRightCorner<3, 1>();
    oneapi::tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) {
        job.check_cancel();
        for (auto i = block * block_size; i < std::min(pairs.size(), (block + 1) * block_size); ++i) {
            const auto& p = pairs[i];
            if (!p.active) continue;
            const Vec3 residual = (rotation * p.query + translation - target.positions[p.target]) / diameter;
            sums[block] += plane ? std::pow(residual.dot(target.normals[p.target]), 2) : residual.squaredNorm();
        }
    });
    double sum = 0;
    for (double value : sums) sum += value;
    return sum;
}

// 정규화된 대응점의 SVD와 선택적 분산 비율로 강체 또는 Sim(3) 갱신을 구합니다.
Mat4 solve_point(Job& job, const std::vector<Pair>& pairs, const Cloud& target, const Vec3& center, double diameter, std::size_t count) {
    const auto blocks = (pairs.size() + block_size - 1) / block_size;
    struct Acc { Vec3 source = Vec3::Zero(), target = Vec3::Zero(); Mat3 cross = Mat3::Zero(); double variance = 0; };
    std::vector<Acc> acc(blocks);
    oneapi::tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) {
        job.check_cancel();
        for (auto i = block * block_size; i < std::min(pairs.size(), (block + 1) * block_size); ++i) {
            if (!pairs[i].active) continue;
            acc[block].source += (pairs[i].query - center) / diameter;
            acc[block].target += (target.positions[pairs[i].target] - center) / diameter;
        }
    });
    Vec3 a = Vec3::Zero(), b = Vec3::Zero();
    for (const auto& value : acc) { a += value.source; b += value.target; }
    a /= static_cast<double>(count); b /= static_cast<double>(count);
    oneapi::tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) {
        job.check_cancel();
        for (auto i = block * block_size; i < std::min(pairs.size(), (block + 1) * block_size); ++i) {
            if (pairs[i].active) acc[block].cross += (((pairs[i].query - center) / diameter) - a) * (((target.positions[pairs[i].target] - center) / diameter) - b).transpose();
            if (job.refine_scale && pairs[i].active) acc[block].variance += (((pairs[i].query - center) / diameter) - a).squaredNorm();
        }
    });
    Mat3 covariance = Mat3::Zero();
    for (const auto& value : acc) covariance += value.cross;
    if (!covariance.allFinite()) throw std::runtime_error("점대점 공분산이 유한하지 않습니다.");
    Eigen::JacobiSVD<Mat3> svd(covariance, Eigen::ComputeFullU | Eigen::ComputeFullV);
    const auto singular = svd.singularValues();
    if (!singular.allFinite() || singular[0] <= 0 || singular[1] <= singular[0] * 1e-12) throw std::runtime_error("점대점 대응의 회전 계수가 부족합니다.");
    Mat3 correction = Mat3::Identity();
    correction(2, 2) = (svd.matrixV() * svd.matrixU().transpose()).determinant() < 0 ? -1.0 : 1.0;
    const Mat3 rotation = svd.matrixV() * correction * svd.matrixU().transpose();
    double scale = 1;
    if (job.refine_scale) {
        double variance = 0;
        for (const auto& value : acc) variance += value.variance;
        if (!std::isfinite(variance) || variance <= 0) throw std::runtime_error("점대점 배율을 결정할 분산이 부족합니다.");
        scale = singular.dot(correction.diagonal()) / variance;
        if (!std::isfinite(scale) || scale <= 0) throw std::runtime_error("점대점 배율이 유효한 양수가 아닙니다.");
    }
    Mat4 delta = Mat4::Identity();
    delta.topLeftCorner<3, 3>() = scale * rotation;
    delta.topRightCorner<3, 1>() = center + diameter * b - scale * rotation * (center + diameter * a);
    return delta;
}

// 중심과 대각선으로 조건을 맞춘 6자유도 점평면 선형계를 풉니다.
Mat4 solve_plane(Job& job, const std::vector<Pair>& pairs, const Cloud& target, const Vec3& center, double diameter) {
    using Mat6 = Eigen::Matrix<double, 6, 6>;
    using Vec6 = Eigen::Matrix<double, 6, 1>;
    struct Acc { Mat6 matrix = Mat6::Zero(); Vec6 rhs = Vec6::Zero(); };
    const auto blocks = (pairs.size() + block_size - 1) / block_size;
    std::vector<Acc> acc(blocks);
    oneapi::tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) {
        job.check_cancel();
        for (auto i = block * block_size; i < std::min(pairs.size(), (block + 1) * block_size); ++i) {
            const auto& p = pairs[i];
            if (!p.active) continue;
            const Vec3 q = (p.query - center) / diameter;
            const Vec3 t = (target.positions[p.target] - center) / diameter;
            const Vec3 n = target.normals[p.target];
            Vec6 row;
            row.head<3>() = q.cross(n); row.tail<3>() = n;
            acc[block].matrix.noalias() += row * row.transpose();
            acc[block].rhs.noalias() += row * (-n.dot(q - t));
        }
    });
    Mat6 matrix = Mat6::Zero(); Vec6 rhs = Vec6::Zero();
    for (const auto& value : acc) { matrix += value.matrix; rhs += value.rhs; }
    Eigen::JacobiSVD<Mat6> svd(matrix, Eigen::ComputeFullU | Eigen::ComputeFullV);
    svd.setThreshold(1e-12);
    if (!matrix.allFinite() || !rhs.allFinite() || svd.rank() != 6) throw std::runtime_error("점평면 대응의 6자유도 계수가 부족합니다.");
    const Vec6 increment = svd.solve(rhs);
    if (!increment.allFinite()) throw std::runtime_error("점평면 갱신이 유한하지 않습니다.");
    const double angle = increment.head<3>().norm();
    const Mat3 rotation = angle > 0 ? Eigen::AngleAxisd(angle, increment.head<3>() / angle).toRotationMatrix() : Mat3::Identity();
    Mat4 delta = Mat4::Identity();
    delta.topLeftCorner<3, 3>() = rotation;
    delta.topRightCorner<3, 1>() = center + diameter * increment.tail<3>() - rotation * center;
    return delta;
}

// 로그 배율 미분을 포함한 7자유도 점평면 계를 풀어 양수 균일 배율로 갱신합니다.
Mat4 solve_similarity_plane(Job& job, const std::vector<Pair>& pairs, const Cloud& target, const Vec3& center, double diameter) {
    using Mat7 = Eigen::Matrix<double, 7, 7>;
    using Vec7 = Eigen::Matrix<double, 7, 1>;
    struct Acc { Mat7 matrix = Mat7::Zero(); Vec7 rhs = Vec7::Zero(); };
    const auto blocks = (pairs.size() + block_size - 1) / block_size;
    std::vector<Acc> acc(blocks);
    oneapi::tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) {
        job.check_cancel();
        for (auto i = block * block_size; i < std::min(pairs.size(), (block + 1) * block_size); ++i) {
            const auto& p = pairs[i];
            if (!p.active) continue;
            const Vec3 q = (p.query - center) / diameter;
            const Vec3 t = (target.positions[p.target] - center) / diameter;
            const Vec3 n = target.normals[p.target];
            Vec7 row;
            row.head<3>() = q.cross(n); row.segment<3>(3) = n; row[6] = q.dot(n);
            acc[block].matrix.noalias() += row * row.transpose();
            acc[block].rhs.noalias() += row * (-n.dot(q - t));
        }
    });
    Mat7 matrix = Mat7::Zero(); Vec7 rhs = Vec7::Zero();
    for (const auto& value : acc) { matrix += value.matrix; rhs += value.rhs; }
    if (!matrix.allFinite() || !rhs.allFinite()) throw std::runtime_error("Sim(3) 점평면 계수가 유한하지 않습니다.");
    Eigen::JacobiSVD<Mat7> svd(matrix, Eigen::ComputeFullU | Eigen::ComputeFullV);
    svd.setThreshold(1e-12);
    if (svd.rank() != 7) throw std::runtime_error("Sim(3) 점평면 대응의 7자유도 계수가 부족합니다.");
    const Vec7 increment = svd.solve(rhs);
    if (!increment.allFinite()) throw std::runtime_error("Sim(3) 점평면 갱신이 유한하지 않습니다.");
    const double scale = std::exp(increment[6]);
    if (!std::isfinite(scale) || scale <= 0) throw std::runtime_error("Sim(3) 점평면 배율이 유효한 양수가 아닙니다.");
    const double angle = increment.head<3>().norm();
    const Mat3 rotation = angle > 0 ? Eigen::AngleAxisd(angle, increment.head<3>() / angle).toRotationMatrix() : Mat3::Identity();
    Mat4 delta = Mat4::Identity();
    delta.topLeftCorner<3, 3>() = scale * rotation;
    delta.topRightCorner<3, 1>() = center + diameter * increment.segment<3>(3) - scale * rotation * center;
    return delta;
}

// 반사·전단·비유한 변환을 거부하고 유효한 양수 균일 배율만 반환합니다.
double similarity_scale(const Mat4& transform) {
    const double determinant = transform.topLeftCorner<3, 3>().determinant();
    if (!transform.allFinite() || !std::isfinite(determinant) || determinant <= 0)
        throw std::runtime_error("Sim(3) 변환의 배율이 유효하지 않습니다.");
    const double scale = std::cbrt(determinant);
    const Mat3 rotation = transform.topLeftCorner<3, 3>() / scale;
    if ((rotation.transpose() * rotation - Mat3::Identity()).cwiseAbs().maxCoeff() > 1e-8)
        throw std::runtime_error("Sim(3) 변환에 비균일 배율 또는 전단이 포함되었습니다.");
    return scale;
}

// 유한성과 허용 범위를 검사한 수치 옵션을 반환합니다.
double parameter(const json& parameters, const char* name, double fallback, double low, double high) {
    const double value = parameters.value(name, fallback);
    if (!std::isfinite(value) || value < low || value > high) throw std::runtime_error(std::string("정렬 옵션 범위가 잘못되었습니다: ") + name);
    return value;
}

// 전체점을 재검색하며 강체 종료식 또는 배율 안정 조건을 더한 Sim(3) 종료식을 적용합니다.
void run(Job& job) {
    const auto& request = job.request;
    if (request.at("protocol_version") != protocol) throw std::runtime_error("지원하지 않는 네이티브 프로토콜입니다.");
    const auto& parameters = request.value("parameters", json::object());
    const auto scale_mode = parameters.value("scale_mode", std::string("FIXED"));
    if (scale_mode != "FIXED" && scale_mode != "SIM3") throw std::runtime_error("알 수 없는 배율 보정 모드입니다.");
    job.refine_scale = scale_mode == "SIM3";
    const double scale_tolerance = parameter(parameters, "scale_tolerance", 1e-6, std::numeric_limits<double>::min(), 1);
    job.result["scale_mode"] = scale_mode;
    const int threads = parameters.value("max_threads", 0);
    if (threads < 0 || threads > 1024) throw std::runtime_error("CPU 스레드 상한이 잘못되었습니다.");
    std::unique_ptr<oneapi::tbb::global_control> control;
    if (threads > 0) control = std::make_unique<oneapi::tbb::global_control>(oneapi::tbb::global_control::max_allowed_parallelism, threads);
    job.max_iterations = parameters.value("max_iterations", 30);
    if (job.max_iterations < 1 || job.max_iterations > 30) throw std::runtime_error("반복 상한은 1~30이어야 합니다.");
    const auto method = request.at("method").get<std::string>();
    const bool plane = method == "POINT_TO_PLANE";
    if (!plane && method != "POINT_TO_POINT") throw std::runtime_error("지원하지 않는 정렬 방식입니다.");
    const double trim_fraction = parameter(parameters, "trim_fraction", 0.05, 0, 0.5);
    const double normal_angle = parameter(parameters, "normal_angle_degrees", 45, 0, 90);
    const double relative_tolerance = parameter(parameters, "relative_rms_tolerance", point_kd::relative_delta_default_tolerance, 0, 1);
    const double translation_tolerance = parameter(parameters, "translation_tolerance", point_kd::relative_delta_default_tolerance, 0, 1);
    const double rotation_tolerance = parameter(parameters, "rotation_tolerance", point_kd::relative_delta_default_tolerance, 0, 1);
    job.result["termination_policy"] = job.refine_scale ? "SIM3_RELATIVE_DELTA_V1" : point_kd::termination_policy;
    job.result["termination_epsilon"] = point_kd::relative_delta_epsilon;
    job.result["termination_tolerances"] = {{"error", relative_tolerance}, {"translation", translation_tolerance}, {"rotation", rotation_tolerance}};
    if (job.refine_scale) job.result["termination_tolerances"]["log_scale"] = scale_tolerance;
    point_kd::RelativeDeltaState termination_state;
    job.check_cancel(true); job.progress("LOAD", 0.0);
    auto timer = Clock::now();
    Cloud source = load_cloud(job, request.at("source"));
    Cloud target = load_cloud(job, request.at("target"));
    job.elapsed("load", timer);
    job.result["source_count"] = source.positions.size(); job.result["target_count"] = target.positions.size();
    job.result["valid_point_count"] = {{"source", source.valid_points}, {"target", target.valid_points}};
    job.progress("NORMALS", 0.05); timer = Clock::now();
    make_normals(job, source, request.at("source")); make_normals(job, target, request.at("target"));
    job.elapsed("normals", timer);
    const bool normal_gate = source.has_normals && target.has_normals;
    job.result["normal_policy"] = normal_gate ? "BOTH_VALID_ABS_DOT" : (plane ? "TARGET_VALID_ONLY" : "DISABLED_MISSING_SIDE");
    job.result["normal_generation_policy"] = "TRANSFORMED_SURFACE_AREA_WEIGHTED";
    job.result["valid_normal_count"] = {{"source", source.valid_normals}, {"target", target.valid_normals}};
    if (plane && target.valid_normals < 6) throw std::runtime_error("점평면 대상의 유효 법선이 부족합니다.");
    const auto normalized = normalization(job, source, target);
    const Vec3 center = normalized.first;
    const double diameter = normalized.second;
    job.result["diagonal"] = diameter;
    job.progress("KD_BUILD", 0.1); timer = Clock::now();
    point_kd::Tree tree(target.positions, [&job]() { job.check_cancel(); });
    job.elapsed("kd_build", timer); job.result["target_kd_build_count"] = 1;
    job.progress("SPACING", 0.15); timer = Clock::now();
    const double h = spacing(job, tree, target);
    job.elapsed("spacing", timer); job.result["spacing"] = h;
    const double marker_rms = parameter(parameters, "marker_rms", 0, 0, std::numeric_limits<double>::max());
    const bool explicit_radius = parameters.contains("maximum_correspondence_distance");
    const double radius = explicit_radius ? parameter(parameters, "maximum_correspondence_distance", 0, std::numeric_limits<double>::min(), std::numeric_limits<double>::max()) : std::max({0.01 * diameter, 3 * h, 2 * marker_rms});
    if (!std::isfinite(radius) || !std::isfinite(radius * radius)) throw std::runtime_error("대응 검색 반경이 유한하지 않습니다.");
    job.result["maximum_correspondence_distance"] = radius;
    if (!explicit_radius && radius > 0.05 * diameter) throw std::runtime_error("자동 대응 반경이 경계상자 대각선의 5%를 초과합니다.");
    const double cosine = std::cos(normal_angle * std::acos(-1.0) / 180.0);
    std::vector<Pair> pairs;
    job.progress("SEARCH", 0.2);
    SearchStats stats = correspondences(job, source, target, tree, job.total, radius, cosine, normal_gate, plane, trim_fraction, pairs);
    const std::size_t minimum = plane ? (job.refine_scale ? 7 : 6) : 3;
    if (stats.count < minimum) throw std::runtime_error("유효한 대응점이 부족합니다.");
    job.initial_rms = job.current_rms = std::sqrt(stats.squared_error / static_cast<double>(stats.count));
    timer = Clock::now();
    job.current_objective_rms = plane ? diameter * std::sqrt(objective(job, pairs, target, Mat4::Identity(), true, diameter) / static_cast<double>(stats.count)) : job.current_rms;
    job.elapsed("objective", timer);
    job.result["initial_objective_rms"] = job.current_objective_rms;
    job.result["iteration_stats"] = json::array();
    std::string termination = "MAX_ITERATIONS", status = "REVIEW";
    for (int iteration = 0; iteration < job.max_iterations; ++iteration) {
        job.iteration = iteration + 1; job.correspondence_count = stats.count;
        job.check_cancel(); job.progress("SOLVE", 0.2 + 0.75 * iteration / job.max_iterations);
        timer = Clock::now();
        const Mat4 delta = plane ? (job.refine_scale ? solve_similarity_plane(job, pairs, target, center, diameter)
                                                    : solve_plane(job, pairs, target, center, diameter))
                                 : solve_point(job, pairs, target, center, diameter, stats.count);
        job.elapsed("solve", timer); job.check_cancel();
        const double increment_scale = job.refine_scale ? similarity_scale(delta) : 1.0;
        if (!job.refine_scale && (!delta.allFinite() || std::abs(delta.topLeftCorner<3, 3>().determinant() - 1) > 1e-8)) throw std::runtime_error("강체 갱신이 유효하지 않습니다.");
        timer = Clock::now();
        const double before = objective(job, pairs, target, Mat4::Identity(), plane, diameter);
        const double after = objective(job, pairs, target, delta, plane, diameter);
        job.elapsed("objective", timer);
        if (!std::isfinite(before) || !std::isfinite(after)) throw std::runtime_error("대응 목적함수가 유한하지 않습니다.");
        if (after > before + std::max(1e-28 * static_cast<double>(stats.count), before * 1e-12)) {
            termination = "OBJECTIVE_INCREASE"; status = "REVIEW";
            job.result["rejected_update_objective"] = {{"before", before}, {"after", after}};
            break;
        }
        timer = Clock::now(); job.progress("UPDATE", 0.2 + 0.75 * iteration / job.max_iterations);
        const double move = (delta.topLeftCorner<3, 3>() * center + delta.topRightCorner<3, 1>() - center).norm() / diameter;
        const double angle = Eigen::AngleAxisd(Mat3(delta.topLeftCorner<3, 3>() / increment_scale)).angle();
        const Mat4 candidate = delta * job.total;
        if (!candidate.allFinite()) throw std::runtime_error("누적 변환이 유한하지 않습니다.");
        if (job.refine_scale) similarity_scale(candidate);
        job.total = candidate;
        job.elapsed("update", timer); job.check_cancel();
        job.progress("SEARCH", 0.2 + 0.75 * (iteration + 1) / job.max_iterations);
        const double previous = job.current_rms;
        const double previous_objective = job.current_objective_rms;
        const auto previous_count = stats.count;
        stats = correspondences(job, source, target, tree, job.total, radius, cosine, normal_gate, plane, trim_fraction, pairs);
        if (stats.count < minimum) throw std::runtime_error("갱신 후 대응점이 부족합니다.");
        job.current_rms = std::sqrt(stats.squared_error / static_cast<double>(stats.count));
        timer = Clock::now();
        job.current_objective_rms = plane ? diameter * std::sqrt(objective(job, pairs, target, Mat4::Identity(), true, diameter) / static_cast<double>(stats.count)) : job.current_rms;
        job.elapsed("objective", timer);
        job.correspondence_count = stats.count;
        const double relative = std::abs(previous_objective - job.current_objective_rms) / (previous_objective + point_kd::relative_delta_epsilon);
        const double euclidean_relative = std::abs(previous - job.current_rms) / std::max(previous, diameter * 1e-15);
        // 대응 급감과 반복 상한은 수렴 성공과 분리하고 상한에서는 종료식을 호출하지 않습니다.
        const bool collapsed = stats.count <= previous_count / 2;
        const bool at_limit = iteration + 1 == job.max_iterations;
        point_kd::RelativeDeltaDecision decision;
        decision.branch = collapsed ? "NOT_CHECKED_COLLAPSE" : "NOT_CHECKED_MAX_ITERATIONS";
        if (!collapsed && !at_limit) {
            Mat4 rigid_increment = delta;
            if (job.refine_scale) rigid_increment.topLeftCorner<3, 3>() /= increment_scale;
            decision = termination_state.evaluate(job.current_objective_rms, previous_objective, rigid_increment,
                relative_tolerance, translation_tolerance, rotation_tolerance);
            if (job.refine_scale && decision.converged && std::abs(std::log(increment_scale)) >= scale_tolerance) {
                decision.converged = false; decision.branch = "SCALE_NOT_STABLE";
            }
        }
        job.result["iteration_stats"].push_back({{"iteration", iteration + 1}, {"rms", job.current_rms},
            {"objective_rms", job.current_objective_rms}, {"euclidean_relative_rms_change", euclidean_relative},
            {"previous_correspondence_count", previous_count},
            {"correspondence_count", stats.count}, {"rejected_distance_count", stats.distance_rejected},
            {"rejected_normal_count", stats.normal_rejected}, {"trimmed_count", stats.trimmed},
            {"relative_rms_change", relative}, {"normalized_translation", move}, {"rotation_radians", angle},
            {"relative_translation_change", decision.translation ? json(*decision.translation) : json(nullptr)},
            {"relative_rotation_change", decision.rotation ? json(*decision.rotation) : json(nullptr)},
            {"convergence_branch", decision.branch}, {"increment_4x4", matrix_json(delta)},
            {"fixed_objective_before", before}, {"fixed_objective_after", after}});
        if (job.refine_scale) {
            job.result["iteration_stats"].back()["increment_scale"] = increment_scale;
            job.result["iteration_stats"].back()["log_scale_change"] = std::abs(std::log(increment_scale));
        }
        // 한 갱신에서 대응의 절반 이상이 사라지면 수렴으로 간주하지 않습니다.
        if (collapsed) {
            termination = "CORRESPONDENCE_COLLAPSE"; status = "REVIEW"; break;
        }
        if (decision.converged) {
            termination = "CONVERGED"; status = "SUCCESS"; break;
        }
    }
    job.result["status"] = status; job.result["termination_reason"] = termination;
    job.result["iterations"] = job.result["iteration_stats"].size();
    job.result["correspondence_count"] = stats.count;
    job.result["rejected_distance_count"] = stats.distance_rejected; job.result["rejected_normal_count"] = stats.normal_rejected;
    job.result["invalid_source_count"] = stats.invalid_rejected; job.result["trimmed_count"] = stats.trimmed;
    job.result["final_objective_rms"] = job.current_objective_rms;
    job.result["additional_scale"] = job.refine_scale ? similarity_scale(job.total) : 1.0;
    if (plane) job.result["point_to_plane_rms"] = job.current_objective_rms;
    job.check_cancel(true); job.progress("COMPLETE", 1.0);
}

// 정확 검색·SVD 자체 검사와 강체·Sim(3) 지원 모드를 반환합니다.
json environment() {
    const point_kd::PointBuffer positions(std::vector<point_kd::Coordinates>{{0, 0, 0}, {1, 0, 0}, {0, 2, 0}});
    point_kd::Tree tree(positions, []() {});
    const auto found = tree.nearest(Vec3(0.1, 0, 0), 4.0, [](std::size_t i) { return i != 0; });
    Eigen::JacobiSVD<Mat3> svd(Mat3::Identity(), Eigen::ComputeFullU | Eigen::ComputeFullV);
    const bool passed = found.index == 1 && (svd.matrixU() * svd.matrixV().transpose() - Mat3::Identity()).norm() < 1e-14;
    return {{"protocol_version", protocol}, {"engine", "POINT_KD"}, {"engine_version", "1.4.0"}, {"termination_policy", point_kd::termination_policy},
        {"scale_modes", {"FIXED", "SIM3"}},
        {"input_transports", {"FILES_V1", "WIN32_SHARED_MEMORY_V1"}},
        {"kd_build_policy", "LONGEST_AXIS_SPATIAL_MIDPOINT"}, {"input_buffer_policy", "OWNED_PACKED_F64_EIGEN_MAP"},
        {"status", passed ? "READY" : "FAIL"}, {"self_test", passed ? "passed" : "failed"}, {"point_scalar", "float64"}, {"triangle_scalar", "uint32"}, {"leaf_size", 16},
        {"tbb_version", "2022.0.0"}, {"eigen_version", "3.4.0"}, {"json_version", "3.11.3"},
        {"search", "EXACT_NEAREST_ELIGIBLE"}, {"methods", {"POINT_TO_POINT", "POINT_TO_PLANE"}}};
}

// 요청 파일의 경계에서 예외를 구조화하고 부분 결과를 실패로 명시합니다.
int execute_request(const fs::path& request_path) {
    Job job;
    try {
        job.root = fs::weakly_canonical(request_path).parent_path();
        std::ifstream stream(request_path, std::ios::binary);
        if (!stream) throw std::runtime_error("요청 파일을 읽지 못했습니다.");
        stream >> job.request;
        const auto result_path = job.confined(job.request.at("result_file"));
        const auto progress_path = job.confined(job.request.at("progress_file"));
        const auto cancel_path = job.confined(job.request.at("cancel_file"));
        if (result_path == progress_path || result_path == cancel_path || progress_path == cancel_path ||
            result_path == fs::weakly_canonical(request_path) || progress_path == fs::weakly_canonical(request_path) || cancel_path == fs::weakly_canonical(request_path))
            throw std::runtime_error("요청·진행·취소·결과 파일 경로가 충돌합니다.");
        job.result_path = result_path;
        // 원본 바이너리와 출력·취소 파일이 겹치면 어느 파일도 쓰지 않습니다.
        for (const auto* side : {"source", "target"}) {
            if (!job.request.contains(side)) continue;
            for (const auto* field : {"positions_file", "normals_file", "triangles_file"}) {
                if (!job.request.at(side).contains(field)) continue;
                const auto input = job.confined(job.request.at(side).at(field));
                if (input == result_path || input == progress_path || input == cancel_path) {
                    job.result_path.clear();
                    throw std::runtime_error("원본 입력과 출력·취소 경로가 충돌합니다.");
                }
            }
        }
        job.result_path = result_path; job.progress_path = progress_path; job.cancel_path = cancel_path;
        job.result = {{"protocol_version", protocol}, {"engine", "POINT_KD"}, {"status", "FAIL"}, {"iterations", 0}};
        run(job);
    } catch (const Cancelled& error) {
        job.result["status"] = "CANCELLED"; job.result["termination_reason"] = "CANCELLED"; job.result["message"] = error.what();
    } catch (const std::exception& error) {
        job.result["status"] = "FAIL"; job.result["termination_reason"] = "ERROR"; job.result["message"] = error.what();
    }
    job.result["protocol_version"] = protocol; job.result["engine"] = "POINT_KD";
    job.result["engine_version"] = "1.4.0";
    job.result["kd_build_policy"] = "LONGEST_AXIS_SPATIAL_MIDPOINT";
    job.result["input_buffer_policy"] = "OWNED_PACKED_F64_EIGEN_MAP";
    job.result["delta_4x4"] = matrix_json(job.total);
    job.result["initial_rms"] = job.initial_rms; job.result["final_rms"] = job.current_rms;
    job.elapsed("total", job.started);
    for (const auto* stage : {"load", "normals", "kd_build", "spacing", "search", "trim", "solve", "objective", "update"}) if (!job.timings.contains(stage)) job.timings[stage] = 0.0;
    job.result["timing_seconds"] = job.timings;
    try {
        if (!job.result_path.empty()) job.write(job.result_path, job.result);
        std::cout << job.result.dump() << '\n';
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 3; }
    return job.result["status"] == "FAIL" ? 2 : 0;
}

#ifdef _WIN32
// 유니코드 작업 폴더를 손실 없이 Windows 진입점으로 전달합니다.
int wmain(int argc, wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--check-environment") { std::cout << environment().dump() << '\n'; return 0; }
    if (argc == 3 && std::wstring(argv[1]) == L"--request") return execute_request(fs::path(argv[2]));
    std::cerr << "사용법: colmap_point_kd_worker --request 요청.json 또는 --check-environment\n"; return 2;
}
#else
// 표준 플랫폼에서 같은 독립 명령행 계약을 제공합니다.
int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--check-environment") { std::cout << environment().dump() << '\n'; return 0; }
    if (argc == 3 && std::string(argv[1]) == "--request") return execute_request(fs::u8path(argv[2]));
    std::cerr << "사용법: colmap_point_kd_worker --request 요청.json 또는 --check-environment\n"; return 2;
}
#endif
