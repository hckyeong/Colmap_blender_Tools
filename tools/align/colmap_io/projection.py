"""COLMAP 공식 카메라 모델 수식으로 점 투영과 재투영 오차를 계산합니다."""

from __future__ import annotations

from typing import Final

import numpy as np

from .camera_models import camera_model_from_id
from .errors import ColmapParseError
from .types import CameraRecord


_DEPTH_EPSILON: Final[float] = float(np.finfo(np.float64).eps)
_FOV_EPSILON: Final[float] = 1.0e-4


class ProjectionError(ValueError):
    """카메라 모델·pose·점이 유효하게 투영되지 않을 때 발생합니다."""


def _finite_array(value: object, *, label: str, shape: tuple[int, ...]) -> np.ndarray:
    """유한한 float64 입력을 복사하고 형상을 확인합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.shape != shape:
        raise ProjectionError(f"{label} 형상이 올바르지 않습니다: {array.shape}")
    if not np.isfinite(array).all():
        raise ProjectionError(f"{label}에 NaN 또는 무한대가 있습니다.")
    return array


def _camera_params(camera: CameraRecord) -> tuple[str, np.ndarray]:
    """CameraRecord의 모델 이름·ID·파라미터 수가 서로 맞는지 확인합니다."""

    if not isinstance(camera, CameraRecord):
        raise TypeError("투영에는 CameraRecord가 필요합니다.")
    try:
        model = camera_model_from_id(camera.model_id)
    except ColmapParseError as error:
        raise ProjectionError(str(error)) from error
    if camera.model_name != model.name:
        raise ProjectionError(
            f"CAMERA_ID {camera.camera_id}의 카메라 모델 이름과 ID가 다릅니다."
        )
    params = _finite_array(
        camera.params_float64,
        label=f"CAMERA_ID {camera.camera_id} 파라미터",
        shape=(model.parameter_count,),
    )
    return model.name, params


def _require_perspective_depth(point_camera: np.ndarray, model_name: str) -> tuple[float, float]:
    """perspective·fisheye 모델의 COLMAP cheirality 조건을 확인합니다."""

    x, y, z = (float(component) for component in point_camera)
    if z < _DEPTH_EPSILON:
        raise ProjectionError(f"{model_name}은 카메라 앞쪽의 점만 투영할 수 있습니다.")
    return x, y


def _fisheye_coordinates(x: float, y: float, z: float) -> tuple[float, float]:
    """COLMAP fisheye 모델의 normal 좌표를 equidistant 좌표로 바꿉니다."""

    normal_x = x / z
    normal_y = y / z
    radius = float(np.hypot(normal_x, normal_y))
    if radius <= _DEPTH_EPSILON:
        return normal_x, normal_y
    factor = float(np.arctan(radius)) / radius
    return normal_x * factor, normal_y * factor


def _fov_distortion(u: float, v: float, omega: float) -> tuple[float, float]:
    """COLMAP FOV 모델의 안정화 분기까지 포함한 왜곡 좌표를 계산합니다."""

    radius_squared = u * u + v * v
    omega_squared = omega * omega
    if omega_squared < _FOV_EPSILON:
        factor = omega_squared * radius_squared / 3.0 - omega_squared / 12.0 + 1.0
    elif radius_squared < _FOV_EPSILON:
        tangent = float(np.tan(omega / 2.0))
        factor = (
            -2.0
            * tangent
            * (4.0 * radius_squared * tangent * tangent - 3.0)
            / (3.0 * omega)
        )
    else:
        radius = float(np.sqrt(radius_squared))
        factor = float(np.arctan(radius * 2.0 * np.tan(omega / 2.0))) / (radius * omega)
    return u * factor, v * factor


def _radial_distortion(
    u: float,
    v: float,
    coefficients: np.ndarray,
) -> tuple[float, float]:
    """반경 다항식 계수의 additive radial distortion을 계산합니다."""

    radius_squared = u * u + v * v
    radial = 0.0
    radius_power = radius_squared
    for coefficient in coefficients:
        radial += float(coefficient) * radius_power
        radius_power *= radius_squared
    return u * radial, v * radial


def _thin_prism_fisheye_distortion(
    u: float,
    v: float,
    extra_params: np.ndarray,
) -> tuple[float, float]:
    """THIN_PRISM_FISHEYE의 공식 radial·tangential·prism 왜곡을 계산합니다."""

    k1, k2, p1, p2, k3, k4, sx1, sy1 = (float(value) for value in extra_params)
    u_squared = u * u
    v_squared = v * v
    uv = u * v
    radius_squared = u_squared + v_squared
    radius_fourth = radius_squared * radius_squared
    radius_sixth = radius_fourth * radius_squared
    radius_eighth = radius_sixth * radius_squared
    radial = (
        k1 * radius_squared
        + k2 * radius_fourth
        + k3 * radius_sixth
        + k4 * radius_eighth
    )
    return (
        u * radial
        + 2.0 * p1 * uv
        + p2 * (radius_squared + 2.0 * u_squared)
        + sx1 * radius_squared,
        v * radial
        + 2.0 * p2 * uv
        + p1 * (radius_squared + 2.0 * v_squared)
        + sy1 * radius_squared,
    )


def _rad_tan_thin_prism_fisheye_distortion(
    u: float,
    v: float,
    extra_params: np.ndarray,
) -> tuple[float, float]:
    """RAD_TAN_THIN_PRISM_FISHEYE의 공식 왜곡 보정량을 계산합니다."""

    radial_coefficients = extra_params[:6]
    p0, p1, s0, s1, s2, s3 = (float(value) for value in extra_params[6:])
    theta_squared = u * u + v * v
    radial_scale = 1.0
    theta_power = 1.0
    for coefficient in radial_coefficients:
        theta_power *= theta_squared
        radial_scale += float(coefficient) * theta_power
    undistorted_x = radial_scale * u
    undistorted_y = radial_scale * v
    x_squared = undistorted_x * undistorted_x
    y_squared = undistorted_y * undistorted_y
    xy = undistorted_x * undistorted_y
    radius_squared = x_squared + y_squared
    radius_fourth = radius_squared * radius_squared
    tangential_x = 2.0 * p1 * xy + p0 * (radius_squared + 2.0 * x_squared)
    tangential_y = 2.0 * p0 * xy + p1 * (radius_squared + 2.0 * y_squared)
    prism_x = s0 * radius_squared + s1 * radius_fourth
    prism_y = s2 * radius_squared + s3 * radius_fourth
    return (
        undistorted_x + tangential_x + prism_x - u,
        undistorted_y + tangential_y + prism_y - v,
    )


def _projection_result(x: float, y: float, model_name: str) -> np.ndarray:
    """투영 수식의 마지막 유한성 검사를 거쳐 독립 pixel 좌표를 반환합니다."""

    result = np.asarray((x, y), dtype=np.float64)
    if not np.isfinite(result).all():
        raise ProjectionError(f"{model_name} 투영 결과에 NaN 또는 무한대가 있습니다.")
    return result


def project_camera_point(camera: CameraRecord, point_camera: object) -> np.ndarray:
    """camera-frame 3D 점을 해당 COLMAP 카메라 모델의 pixel 좌표로 투영합니다."""

    model_name, params = _camera_params(camera)
    point = _finite_array(point_camera, label="camera 좌표", shape=(3,))
    x, y, z = (float(component) for component in point)

    if model_name == "EQUIRECTANGULAR":
        width, height = (float(value) for value in params)
        horizontal = float(np.hypot(x, z))
        if horizontal + abs(y) < _DEPTH_EPSILON:
            raise ProjectionError("EQUIRECTANGULAR은 영(0) 방향 벡터를 투영할 수 없습니다.")
        if width <= 0.0 or height <= 0.0:
            raise ProjectionError("EQUIRECTANGULAR의 width와 height는 양수여야 합니다.")
        theta = float(np.arctan2(x, z))
        phi = float(np.arctan2(-y, horizontal))
        return _projection_result(
            (theta / (2.0 * np.pi) + 0.5) * width,
            (0.5 - phi / np.pi) * height,
            model_name,
        )

    if model_name in {"SIMPLE_DIVISION", "DIVISION"}:
        if model_name == "SIMPLE_DIVISION":
            focal_x = focal_y = float(params[0])
            center_x, center_y, coefficient = (float(value) for value in params[1:])
        else:
            focal_x, focal_y, center_x, center_y, coefficient = (
                float(value) for value in params
            )
        rho = float(np.hypot(x, y))
        discriminant_squared = z * z - 4.0 * rho * rho * coefficient
        if discriminant_squared < 0.0:
            raise ProjectionError(f"{model_name}의 division 판별식이 음수입니다.")
        denominator = z + float(np.sqrt(discriminant_squared))
        if abs(denominator) < _DEPTH_EPSILON:
            raise ProjectionError(f"{model_name}의 division 분모가 0에 가깝습니다.")
        ratio = 2.0 / denominator
        return _projection_result(
            focal_x * ratio * x + center_x,
            focal_y * ratio * y + center_y,
            model_name,
        )

    perspective_x, perspective_y = _require_perspective_depth(point, model_name)
    normal_x = perspective_x / z
    normal_y = perspective_y / z

    if model_name == "SIMPLE_PINHOLE":
        focal, center_x, center_y = (float(value) for value in params)
        return _projection_result(
            focal * normal_x + center_x,
            focal * normal_y + center_y,
            model_name,
        )
    if model_name == "PINHOLE":
        focal_x, focal_y, center_x, center_y = (float(value) for value in params)
        return _projection_result(
            focal_x * normal_x + center_x,
            focal_y * normal_y + center_y,
            model_name,
        )
    if model_name == "SIMPLE_RADIAL":
        focal, center_x, center_y, coefficient = (float(value) for value in params)
        delta_x, delta_y = _radial_distortion(
            normal_x,
            normal_y,
            np.asarray((coefficient,), dtype=np.float64),
        )
        return _projection_result(
            focal * (normal_x + delta_x) + center_x,
            focal * (normal_y + delta_y) + center_y,
            model_name,
        )
    if model_name == "RADIAL":
        focal, center_x, center_y, coefficient_one, coefficient_two = (
            float(value) for value in params
        )
        delta_x, delta_y = _radial_distortion(
            normal_x,
            normal_y,
            np.asarray((coefficient_one, coefficient_two), dtype=np.float64),
        )
        return _projection_result(
            focal * (normal_x + delta_x) + center_x,
            focal * (normal_y + delta_y) + center_y,
            model_name,
        )
    if model_name == "OPENCV":
        focal_x, focal_y, center_x, center_y, k1, k2, p1, p2 = (
            float(value) for value in params
        )
        radius_squared = normal_x * normal_x + normal_y * normal_y
        radial = k1 * radius_squared + k2 * radius_squared * radius_squared
        delta_x = (
            normal_x * radial
            + 2.0 * p1 * normal_x * normal_y
            + p2 * (radius_squared + 2.0 * normal_x * normal_x)
        )
        delta_y = (
            normal_y * radial
            + 2.0 * p2 * normal_x * normal_y
            + p1 * (radius_squared + 2.0 * normal_y * normal_y)
        )
        return _projection_result(
            focal_x * (normal_x + delta_x) + center_x,
            focal_y * (normal_y + delta_y) + center_y,
            model_name,
        )
    if model_name == "FULL_OPENCV":
        focal_x, focal_y, center_x, center_y, k1, k2, p1, p2, k3, k4, k5, k6 = (
            float(value) for value in params
        )
        radius_squared = normal_x * normal_x + normal_y * normal_y
        radius_fourth = radius_squared * radius_squared
        radius_sixth = radius_fourth * radius_squared
        radial_denominator = 1.0 + k4 * radius_squared + k5 * radius_fourth + k6 * radius_sixth
        if abs(radial_denominator) < _DEPTH_EPSILON:
            raise ProjectionError("FULL_OPENCV의 radial 분모가 0에 가깝습니다.")
        radial = (
            1.0 + k1 * radius_squared + k2 * radius_fourth + k3 * radius_sixth
        ) / radial_denominator
        distorted_x = (
            normal_x * radial
            + 2.0 * p1 * normal_x * normal_y
            + p2 * (radius_squared + 2.0 * normal_x * normal_x)
        )
        distorted_y = (
            normal_y * radial
            + 2.0 * p2 * normal_x * normal_y
            + p1 * (radius_squared + 2.0 * normal_y * normal_y)
        )
        return _projection_result(
            focal_x * distorted_x + center_x,
            focal_y * distorted_y + center_y,
            model_name,
        )
    if model_name == "FOV":
        focal_x, focal_y, center_x, center_y, omega = (float(value) for value in params)
        distorted_x, distorted_y = _fov_distortion(normal_x, normal_y, omega)
        return _projection_result(
            focal_x * distorted_x + center_x,
            focal_y * distorted_y + center_y,
            model_name,
        )
    if model_name in {
        "SIMPLE_RADIAL_FISHEYE",
        "RADIAL_FISHEYE",
        "OPENCV_FISHEYE",
        "THIN_PRISM_FISHEYE",
        "RAD_TAN_THIN_PRISM_FISHEYE",
        "SIMPLE_FISHEYE",
        "FISHEYE",
    }:
        fisheye_x, fisheye_y = _fisheye_coordinates(perspective_x, perspective_y, z)
        if model_name == "SIMPLE_RADIAL_FISHEYE":
            focal, center_x, center_y, coefficient = (float(value) for value in params)
            delta_x, delta_y = _radial_distortion(
                fisheye_x,
                fisheye_y,
                np.asarray((coefficient,), dtype=np.float64),
            )
            return _projection_result(
                focal * (fisheye_x + delta_x) + center_x,
                focal * (fisheye_y + delta_y) + center_y,
                model_name,
            )
        if model_name == "RADIAL_FISHEYE":
            focal, center_x, center_y, coefficient_one, coefficient_two = (
                float(value) for value in params
            )
            delta_x, delta_y = _radial_distortion(
                fisheye_x,
                fisheye_y,
                np.asarray((coefficient_one, coefficient_two), dtype=np.float64),
            )
            return _projection_result(
                focal * (fisheye_x + delta_x) + center_x,
                focal * (fisheye_y + delta_y) + center_y,
                model_name,
            )
        if model_name == "OPENCV_FISHEYE":
            focal_x, focal_y, center_x, center_y = (float(value) for value in params[:4])
            delta_x, delta_y = _radial_distortion(fisheye_x, fisheye_y, params[4:])
            return _projection_result(
                focal_x * (fisheye_x + delta_x) + center_x,
                focal_y * (fisheye_y + delta_y) + center_y,
                model_name,
            )
        if model_name == "THIN_PRISM_FISHEYE":
            focal_x, focal_y, center_x, center_y = (float(value) for value in params[:4])
            delta_x, delta_y = _thin_prism_fisheye_distortion(
                fisheye_x,
                fisheye_y,
                params[4:],
            )
            return _projection_result(
                focal_x * (fisheye_x + delta_x) + center_x,
                focal_y * (fisheye_y + delta_y) + center_y,
                model_name,
            )
        if model_name == "RAD_TAN_THIN_PRISM_FISHEYE":
            focal_x, focal_y, center_x, center_y = (float(value) for value in params[:4])
            delta_x, delta_y = _rad_tan_thin_prism_fisheye_distortion(
                fisheye_x,
                fisheye_y,
                params[4:],
            )
            return _projection_result(
                focal_x * (fisheye_x + delta_x) + center_x,
                focal_y * (fisheye_y + delta_y) + center_y,
                model_name,
            )
        if model_name == "SIMPLE_FISHEYE":
            focal, center_x, center_y = (float(value) for value in params)
            return _projection_result(
                focal * fisheye_x + center_x,
                focal * fisheye_y + center_y,
                model_name,
            )
        focal_x, focal_y, center_x, center_y = (float(value) for value in params)
        return _projection_result(
            focal_x * fisheye_x + center_x,
            focal_y * fisheye_y + center_y,
            model_name,
        )
    if model_name == "EUCM":
        focal_x, focal_y, center_x, center_y, alpha, beta = (float(value) for value in params)
        rho_squared = beta * (perspective_x * perspective_x + perspective_y * perspective_y) + z * z
        if rho_squared < 0.0:
            raise ProjectionError("EUCM의 rho 제곱이 음수입니다.")
        denominator = alpha * float(np.sqrt(rho_squared)) + (1.0 - alpha) * z
        if denominator < _DEPTH_EPSILON:
            raise ProjectionError("EUCM의 투영 분모가 0에 가깝거나 카메라 뒤쪽입니다.")
        return _projection_result(
            focal_x * perspective_x / denominator + center_x,
            focal_y * perspective_y / denominator + center_y,
            model_name,
        )
    raise ProjectionError(f"지원하지 않는 COLMAP 카메라 모델입니다: {model_name}")


def _rotation_from_quaternion(quaternion_wxyz: object) -> np.ndarray:
    """재투영 계산용으로 Hamilton 쿼터니언을 proper 회전으로 바꿉니다."""

    quaternion = _finite_array(
        quaternion_wxyz,
        label="pose 쿼터니언",
        shape=(4,),
    )
    norm = float(np.linalg.norm(quaternion))
    if norm <= _DEPTH_EPSILON:
        raise ProjectionError("pose 쿼터니언 크기가 0에 가깝습니다.")
    qw, qx, qy, qz = quaternion / norm
    return np.asarray(
        (
            (
                1.0 - 2.0 * (qy * qy + qz * qz),
                2.0 * (qx * qy - qz * qw),
                2.0 * (qx * qz + qy * qw),
            ),
            (
                2.0 * (qx * qy + qz * qw),
                1.0 - 2.0 * (qx * qx + qz * qz),
                2.0 * (qy * qz - qx * qw),
            ),
            (
                2.0 * (qx * qz - qy * qw),
                2.0 * (qy * qz + qx * qw),
                1.0 - 2.0 * (qx * qx + qy * qy),
            ),
        ),
        dtype=np.float64,
    )


def project_world_point(
    camera: CameraRecord,
    cam_from_world: object,
    point_world: object,
) -> np.ndarray:
    """world→camera pose와 카메라 모델로 world 점을 pixel 좌표로 투영합니다."""

    pose = _finite_array(cam_from_world, label="cam_from_world", shape=(7,))
    point = _finite_array(point_world, label="world 좌표", shape=(3,))
    rotation = _rotation_from_quaternion(pose[:4])
    point_camera = rotation @ point + pose[4:]
    return project_camera_point(camera, point_camera)


def reprojection_error_pixels(
    camera: CameraRecord,
    cam_from_world: object,
    point_world: object,
    observed_xy: object,
) -> float:
    """관측 points2D와 Project World Point3D 사이의 pixel Euclidean 오차를 반환합니다."""

    observed = _finite_array(observed_xy, label="관측 points2D", shape=(2,))
    projected = project_world_point(camera, cam_from_world, point_world)
    error = float(np.linalg.norm(projected - observed))
    if not np.isfinite(error):
        raise ProjectionError("재투영 오차에 NaN 또는 무한대가 있습니다.")
    return error


__all__ = (
    "ProjectionError",
    "project_camera_point",
    "project_world_point",
    "reprojection_error_pixels",
)
