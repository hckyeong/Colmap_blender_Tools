#pragma once
#include <Eigen/Geometry>
#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>

namespace point_kd {
constexpr double relative_delta_epsilon = 1e-9;
constexpr double relative_delta_default_tolerance = static_cast<double>(0.001f);
constexpr const char* termination_policy = "ARTEC_RELATIVE_DELTA_V1";

// 한 번의 갱신 회전을 관측된 사원수 부호 규칙의 축×각 벡터로 바꿉니다.
inline Eigen::Vector3d rotation_vector(const Eigen::Matrix3d& rotation) {
    const Eigen::Quaterniond quaternion(rotation);
    const double length = quaternion.vec().norm();
    if (length <= std::numeric_limits<double>::epsilon()) return Eigen::Vector3d::Zero();
    const double angle = std::acos(std::clamp(quaternion.w() * quaternion.w() - quaternion.vec().squaredNorm(), -1.0, 1.0));
    return quaternion.vec() / std::copysign(length, quaternion.w()) * angle;
}

struct RelativeDeltaDecision {
    double error = 0;
    std::optional<double> translation, rotation;
    const char* branch = "CONTINUE";
    bool converged = false;
};

struct RelativeDeltaState {
    Eigen::Vector3d previous_translation = Eigen::Vector3d::Zero();
    Eigen::Vector3d previous_rotation = Eigen::Vector3d::Zero();

    // 오차를 먼저 검사하고 실패할 때만 갱신 벡터의 상대 변화와 이력을 계산합니다.
    RelativeDeltaDecision evaluate(double current_error, double previous_error,
            const Eigen::Matrix4d& increment, double error_tolerance,
            double translation_tolerance, double rotation_tolerance) {
        RelativeDeltaDecision result;
        result.error = std::abs(current_error - previous_error) / (previous_error + relative_delta_epsilon);
        if (result.error < error_tolerance) {
            result.branch = "ERROR_STABLE";
            result.converged = true;
            return result;
        }
        const Eigen::Vector3d translation = increment.topRightCorner<3, 1>();
        const Eigen::Vector3d rotation = rotation_vector(increment.topLeftCorner<3, 3>());
        result.translation = (translation - previous_translation).norm() / (previous_translation.norm() + relative_delta_epsilon);
        result.rotation = (rotation - previous_rotation).norm() / (previous_rotation.norm() + relative_delta_epsilon);
        previous_translation = translation;
        previous_rotation = rotation;
        if (*result.rotation < rotation_tolerance && *result.translation < translation_tolerance) {
            result.branch = "TRANSFORM_STABLE";
            result.converged = true;
        }
        return result;
    }
};
}
