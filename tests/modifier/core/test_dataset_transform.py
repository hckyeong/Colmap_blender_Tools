"""데이터셋 유사 변환의 투영 및 리그 상대 자세를 검증합니다."""

import math
import unittest

from tools.modifier.core.dataset_transform import (
    IDENTITY_TRANSFORM, compose_transforms, inverse_transform, transform_frame,
    transform_image, transform_point, transform_rig, transformed_reconstruction,
    validate_transform, _rotation, TransformReconstructionJob,
)
from tools.modifier.core.model import (
    FrameRecord, ImageRecord, Point2DRecord, Point3DRecord, Reconstruction,
    RigRecord, SensorRecord, TrackElement,
)


class DatasetTransformTests(unittest.TestCase):
    """순수 수학 변환 계약을 확인합니다."""

    def assertVector(self, actual, expected):
        """벡터 원소가 수치 오차 범위 내에서 같은지 확인합니다."""
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b, places=10)

    def test_projection_and_record_preservation(self):
        """이미지 투영과 관측 및 트랙 메타데이터가 보존됩니다."""
        matrix = ((0., -2., 0., 4.), (2., 0., 0., -3.), (0., 0., 2., 7.), (0., 0., 0., 1.))
        image = ImageRecord(1, (math.sqrt(.5), 0., math.sqrt(.5), 0.), (1., 2., 3.), 1, 'a.jpg', (Point2DRecord(3., 4., 2),))
        point = Point3DRecord(2, (2., 4., 8.), (1, 2, 3), .4, (TrackElement(1, 0),))
        source = Reconstruction(images={1: image}, points3d={2: point})
        result = transformed_reconstruction(source, matrix)
        old_r, new_r = _rotation(image.qvec), _rotation(result.images[1].qvec)
        old_camera = tuple(sum(old_r[i][j] * point.xyz[j] for j in range(3)) + image.tvec[i] for i in range(3))
        new_camera = tuple(sum(new_r[i][j] * result.points3d[2].xyz[j] for j in range(3)) + result.images[1].tvec[i] for i in range(3))
        self.assertVector(new_camera, tuple(2*v for v in old_camera))
        self.assertVector(tuple(v/new_camera[2] for v in new_camera[:2]), tuple(v/old_camera[2] for v in old_camera[:2]))
        self.assertIs(result.images[1].points2d, image.points2d)
        self.assertIs(result.points3d[2].track, point.track)
        self.assertEqual(result.points3d[2].rgb, point.rgb)
        self.assertEqual(result.points3d[2].error, point.error)
        self.assertIs(result.cameras, source.cameras)
        self.assertIs(transformed_reconstruction(source, IDENTITY_TRANSFORM), source)

    def test_frame_sensor_composition(self):
        """변환된 리그와 프레임의 합성 자세가 이미지 자세와 일치합니다."""
        matrix = ((-3., 0., 0., 2.), (0., -3., 0., 4.), (0., 0., 3., 1.), (0., 0., 0., 1.))
        frame = FrameRecord(1, 1, (1., 0., 0., 0.), (2., 3., 4.), ())
        sensor = SensorRecord(0, 2, True, (0., 1., 0., 0.), (1., 2., 3.))
        rig = RigRecord(1, 0, 1, (sensor,))
        image = ImageRecord(1, sensor.qvec, (3., -1., -1.), 2, 'b.jpg', ())
        transformed_frame = transform_frame(matrix, frame)
        transformed_sensor = transform_rig(matrix, rig).sensors[0]
        transformed_image = transform_image(matrix, image)
        sensor_r = _rotation(transformed_sensor.qvec)
        composed_t = tuple(sum(sensor_r[i][j] * transformed_frame.tvec[j] for j in range(3)) + transformed_sensor.tvec[i] for i in range(3))
        self.assertVector(composed_t, transformed_image.tvec)
        composed_r = tuple(tuple(sum(sensor_r[i][k] * _rotation(transformed_frame.qvec)[k][j] for k in range(3)) for j in range(3)) for i in range(3))
        for actual, expected in zip(composed_r, _rotation(transformed_image.qvec)):
            self.assertVector(actual, expected)
        self.assertEqual(transformed_sensor.qvec, sensor.qvec)

    def test_inverse_and_half_turn_quaternions(self):
        """역변환과 반회전의 사원수 변환이 안정적으로 동작합니다."""
        for axis in range(3):
            matrix = tuple(tuple((2. if i == axis else -2.) if i == j else (float(i+1) if j == 3 else 0.) for j in range(4)) for i in range(3)) + ((0., 0., 0., 1.),)
            self.assertVector(transform_point(compose_transforms(inverse_transform(matrix), matrix), (1., 2., 3.)), (1., 2., 3.))
            image = ImageRecord(1, (1., 0., 0., 0.), (0., 0., 0.), 1, '', ())
            self.assertAlmostEqual(math.hypot(*transform_image(matrix, image).qvec), 1.)

    def test_invalid_transforms(self):
        """반사와 비균일 배율 및 비아핀과 비유한 행렬을 거부합니다."""
        for i, j, value in ((0, 0, -1.), (0, 0, 2.), (0, 1, .2), (3, 0, .01), (0, 3, math.inf), (0, 0, 0.)):
            matrix = [list(row) for row in IDENTITY_TRANSFORM]
            matrix[i][j] = value
            with self.subTest(i=i, j=j, value=value), self.assertRaises(ValueError):
                validate_transform(matrix)

    def test_incremental_job_budget_and_identity(self):
        """점 변환이 작업 예산에 따라 나뉘고 원본은 보존됩니다."""
        point = Point3DRecord(1, (1., 2., 3.), (0, 0, 0), 0., ())
        source = Reconstruction(points3d={i: point for i in range(4100)})
        matrix = ((2., 0., 0., 0.), (0., 2., 0., 0.), (0., 0., 2., 0.), (0., 0., 0., 1.))
        job = TransformReconstructionJob(source, matrix)
        job.advance(work_budget_ms=0)
        self.assertEqual(job.completed_units, 1)
        job.advance()
        self.assertEqual(job.completed_units, 4097)
        self.assertFalse(job.done)
        job.advance()
        self.assertTrue(job.done)
        self.assertEqual(job.total_units, 4100)
        self.assertEqual(job.result.points3d[0].xyz, (2., 4., 6.))
        self.assertEqual(source.points3d[0].xyz, (1., 2., 3.))
        identity = TransformReconstructionJob(source, IDENTITY_TRANSFORM)
        self.assertTrue(identity.done)
        self.assertIs(identity.result, source)

    def test_finite_inputs_cannot_emit_overflowed_coordinates(self):
        """유한 입력의 곱셈이 넘치면 점과 자세 및 상대 이동을 거부합니다."""
        matrix = ((2., 0., 0., 0.), (0., 2., 0., 0.), (0., 0., 2., 0.), (0., 0., 0., 1.))
        with self.assertRaises(ValueError):
            transform_point(matrix, (1e308, 0., 0.))
        image = ImageRecord(1, (1., 0., 0., 0.), (1e308, 0., 0.), 1, '', ())
        with self.assertRaises(ValueError):
            transform_image(matrix, image)
        rig = RigRecord(1, 0, 1, (SensorRecord(0, 2, True, (1., 0., 0., 0.), (1e308, 0., 0.)),))
        with self.assertRaises(ValueError):
            transform_rig(matrix, rig)
        source = Reconstruction(points3d={1: Point3DRecord(1, (1e308, 0., 0.), (0, 0, 0), 0., ())})
        with self.assertRaises(ValueError):
            transformed_reconstruction(source, matrix)


if __name__ == '__main__':
    unittest.main()
