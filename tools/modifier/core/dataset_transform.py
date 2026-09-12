"""재구성의 투영을 보존하는 양의 균일 배율 유사 변환입니다."""

from dataclasses import replace
import math
import time

from .model import FrameRecord, ImageRecord, Reconstruction, RigRecord

Row4 = tuple[float, float, float, float]
Matrix4 = tuple[Row4, Row4, Row4, Row4]
IDENTITY_TRANSFORM: Matrix4 = tuple(
    tuple(float(i == j) for j in range(4)) for i in range(4)
)


def validate_transform(matrix) -> Matrix4:
    """유한한 아핀 유사 변환을 검사하고 불변 실수 행렬로 반환합니다."""
    try:
        result = tuple(tuple(float(value) for value in row) for row in matrix)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("변환 행렬은 4×4 실수 행렬이어야 합니다.") from exc
    if len(result) != 4 or any(len(row) != 4 for row in result):
        raise ValueError("변환 행렬은 4×4 행렬이어야 합니다.")
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError("변환 행렬에 유한하지 않은 값이 있습니다.")
    if result[3] != (0.0, 0.0, 0.0, 1.0):
        raise ValueError("변환 행렬은 아핀 행렬이어야 합니다.")
    scale = math.hypot(*result[0][:3])
    if scale <= 0 or not math.isfinite(scale):
        raise ValueError("배율은 유한한 양수여야 합니다.")
    rotation = tuple(tuple(value / scale for value in row[:3]) for row in result[:3])
    for i in range(3):
        for j in range(3):
            if not math.isclose(sum(rotation[i][k] * rotation[j][k] for k in range(3)), float(i == j), abs_tol=1e-6, rel_tol=1e-6):
                raise ValueError("균일 배율과 직교 회전만 허용합니다.")
    a, b, c = rotation
    determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if determinant <= 0:
        raise ValueError("반사 변환은 허용하지 않습니다.")
    return result


def transform_scale(matrix) -> float:
    """검증된 변환의 양의 균일 배율을 반환합니다."""
    return math.hypot(*validate_transform(matrix)[0][:3])


def transform_point(matrix, xyz) -> tuple[float, float, float]:
    """점 좌표에 균일 배율과 회전 및 이동을 적용합니다."""
    matrix = validate_transform(matrix)
    return _finite_vector(tuple(sum(matrix[i][j] * xyz[j] for j in range(3)) + matrix[i][3] for i in range(3)))


def _finite_vector(values):
    """변환 결과가 실수 표현 범위를 넘거나 비유한 값이면 거부합니다."""
    if not all(math.isfinite(value) for value in values):
        raise ValueError("변환 결과가 유한한 실수 범위를 벗어났습니다.")
    return values


def compose_transforms(left, right) -> Matrix4:
    """오른쪽 변환 다음에 왼쪽 변환을 적용하는 행렬을 반환합니다."""
    left, right = validate_transform(left), validate_transform(right)
    return validate_transform(tuple(tuple(sum(left[i][k] * right[k][j] for k in range(4)) for j in range(4)) for i in range(4)))


def inverse_transform(matrix) -> Matrix4:
    """유사 변환의 역행렬을 계산합니다."""
    matrix = validate_transform(matrix)
    scale = transform_scale(matrix)
    linear = tuple(tuple((matrix[j][i] / scale) / scale for j in range(3)) for i in range(3))
    return validate_transform(tuple(linear[i] + (-sum(linear[i][j] * matrix[j][3] for j in range(3)),) for i in range(3)) + ((0., 0., 0., 1.),))


def _rotation(qvec):
    """정규화된 실수부 우선 사원수에서 회전 행렬을 만듭니다."""
    norm = math.hypot(*qvec)
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("자세 사원수가 유효하지 않습니다.")
    w, x, y, z = (v / norm for v in qvec)
    return ((1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)),
            (2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)),
            (2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)))


def _quaternion(r):
    """대각 성분의 안정적인 분기를 사용해 단위 사원수를 계산합니다."""
    candidates = (1+r[0][0]+r[1][1]+r[2][2], 1+r[0][0]-r[1][1]-r[2][2],
                  1-r[0][0]+r[1][1]-r[2][2], 1-r[0][0]-r[1][1]+r[2][2])
    index = max(range(4), key=candidates.__getitem__)
    v = math.sqrt(candidates[index]) * 2
    if index == 0:
        q = (v/4, (r[2][1]-r[1][2])/v, (r[0][2]-r[2][0])/v, (r[1][0]-r[0][1])/v)
    elif index == 1:
        q = ((r[2][1]-r[1][2])/v, v/4, (r[0][1]+r[1][0])/v, (r[0][2]+r[2][0])/v)
    elif index == 2:
        q = ((r[0][2]-r[2][0])/v, (r[0][1]+r[1][0])/v, v/4, (r[1][2]+r[2][1])/v)
    else:
        q = ((r[1][0]-r[0][1])/v, (r[0][2]+r[2][0])/v, (r[1][2]+r[2][1])/v, v/4)
    norm = math.hypot(*q)
    return tuple(value / norm for value in q)


def _pose(matrix, record, scale=None):
    """세계 좌표 변환에 대응하는 카메라 또는 리그 자세를 계산합니다."""
    if scale is None:
        scale = transform_scale(matrix)
    old = _rotation(record.qvec)
    rotation = tuple(tuple(sum(old[i][k] * (matrix[j][k] / scale) for k in range(3)) for j in range(3)) for i in range(3))
    translation = tuple(scale * record.tvec[i] - sum(rotation[i][j] * matrix[j][3] for j in range(3)) for i in range(3))
    return replace(record, qvec=_quaternion(rotation), tvec=_finite_vector(translation))


def transform_image(matrix, image: ImageRecord) -> ImageRecord:
    """관측과 내부 파라미터 참조를 보존하며 이미지 자세를 변환합니다."""
    matrix = validate_transform(matrix)
    return image if matrix == IDENTITY_TRANSFORM else _pose(matrix, image)


def transform_frame(matrix, frame: FrameRecord) -> FrameRecord:
    """프레임의 세계 기준 리그 자세를 변환합니다."""
    matrix = validate_transform(matrix)
    return frame if matrix == IDENTITY_TRANSFORM else _pose(matrix, frame)


def transform_rig(matrix, rig: RigRecord) -> RigRecord:
    """센서의 상대 회전을 보존하며 상대 이동에 배율을 적용합니다."""
    scale = transform_scale(matrix)
    if scale == 1:
        return rig
    sensors = []
    for sensor in rig.sensors:
        if sensor.has_pose and (sensor.qvec is None or sensor.tvec is None):
            raise ValueError("센서의 상대 자세가 누락되었습니다.")
        sensors.append(replace(sensor, tvec=_finite_vector(tuple(scale * v for v in sensor.tvec))) if sensor.has_pose else sensor)
    return replace(rig, sensors=tuple(sensors))


def transformed_reconstruction(reconstruction: Reconstruction, matrix) -> Reconstruction:
    """원본 레코드의 비기하 데이터를 보존한 변환 재구성을 만듭니다."""
    job = TransformReconstructionJob(reconstruction, matrix)
    while not job.done:
        job.advance()
    return job.result


class TransformReconstructionJob:
    """제한된 레코드 수와 시간 안에서 내보낼 재구성을 변환합니다."""

    def __init__(self, reconstruction: Reconstruction, matrix):
        """원본을 보존하는 변환 출력과 순차 작업 상태를 준비합니다."""
        self.matrix = validate_transform(matrix)
        self._scale = math.hypot(*self.matrix[0][:3])
        self.completed_units = 0
        self.total_units = sum(len(getattr(reconstruction, name)) for name in ('images', 'points3d', 'rigs', 'frames'))
        self.done = self.matrix == IDENTITY_TRANSFORM
        self.result = reconstruction if self.done else replace(reconstruction, images={}, points3d={}, rigs={}, frames={})
        self._records = ((name, key, value) for name in ('images', 'points3d', 'rigs', 'frames') for key, value in getattr(reconstruction, name).items())
        if self.done:
            self.completed_units = self.total_units

    def advance(self, work_budget_ms=None) -> None:
        """한 번에 최대 4096개 레코드를 지정된 시간 예산 안에서 처리합니다."""
        if self.done:
            return
        deadline = None if work_budget_ms is None else time.perf_counter() + max(0., work_budget_ms) / 1000.
        for count in range(4096):
            if count and deadline is not None and time.perf_counter() >= deadline:
                break
            try:
                name, key, record = next(self._records)
            except StopIteration:
                self.done = True
                return
            if name == 'points3d':
                xyz = tuple(sum(self.matrix[i][j] * record.xyz[j] for j in range(3)) + self.matrix[i][3] for i in range(3))
                transformed = replace(record, xyz=_finite_vector(xyz))
            elif name == 'rigs':
                transformed = transform_rig(self.matrix, record)
            else:
                transformed = _pose(self.matrix, record, self._scale)
            getattr(self.result, name)[key] = transformed
            self.completed_units += 1
        if self.completed_units == self.total_units:
            self.done = True
