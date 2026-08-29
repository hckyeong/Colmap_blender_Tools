from __future__ import annotations

import io
import struct
import unittest
from dataclasses import replace

from tools.modifier.core import binary_writer
from tools.modifier.core.model import (
    CameraRecord,
    DataIdRecord,
    FrameRecord,
    ImageRecord,
    Point2DRecord,
    Point3DRecord,
    Reconstruction,
    RigRecord,
    SensorRecord,
    TrackElement,
)


def binary_writer_test_model() -> Reconstruction:
    camera_id = 4_000_000_000
    image_id = 3_000_000_000
    point3d_id = (1 << 63) + 19
    camera = CameraRecord(
        camera_id=camera_id,
        model_id=0,
        model_name="SIMPLE_PINHOLE",
        width=6000,
        height=4000,
        params=(4200.5, 3000.125, 2000.875),
    )
    image = ImageRecord(
        image_id=image_id,
        qvec=(1.0, -0.0, 0.125, -0.25),
        tvec=(1.0e-20, -2.0e20, 3.5),
        camera_id=camera_id,
        name="한글 촬영 image 01.jpg",
        points2d=(
            Point2DRecord(10.25, 20.5, None),
            Point2DRecord(30.75, 40.125, point3d_id),
        ),
    )
    point = Point3DRecord(
        point3d_id=point3d_id,
        xyz=(1.0 / 3.0, -2.5, 9.0e-100),
        rgb=(1, 127, 255),
        error=0.12345678901234566,
        track=(TrackElement(image_id, 1),),
    )
    rig = RigRecord(
        rig_id=3_000_000_001,
        ref_sensor_type=0,
        ref_sensor_id=camera_id,
        sensors=(
            SensorRecord(
                sensor_type=0,
                sensor_id=7,
                has_pose=True,
                qvec=(1.0, 0.25, -0.5, 0.75),
                tvec=(1.5, -2.5, 3.5),
            ),
            SensorRecord(sensor_type=0, sensor_id=8, has_pose=False),
        ),
    )
    frame = FrameRecord(
        frame_id=3_000_000_002,
        rig_id=rig.rig_id,
        qvec=(1.0, -0.125, 0.25, -0.5),
        tvec=(4.0, 5.0, -6.0),
        data_ids=(
            DataIdRecord(0, 7, image_id),
            DataIdRecord(0, 8, (1 << 63) + 23),
        ),
    )
    return Reconstruction(
        cameras={camera_id: camera},
        images={image_id: image},
        points3d={point3d_id: point},
        rigs={rig.rig_id: rig},
        frames={frame.frame_id: frame},
    )


class BinaryWriterSerializationTests(unittest.TestCase):
    def test_writer_uses_one_mib_buffer(self):
        self.assertEqual(binary_writer._WRITE_BUFFER_BYTES, 1024 * 1024)

    def test_all_serializers_match_colmap_layout(self):
        model = binary_writer_test_model()

        camera_stream = io.BytesIO()
        binary_writer._serialize_cameras(camera_stream, model)
        camera = next(iter(model.cameras.values()))
        expected_cameras = b"".join(
            (
                struct.pack("<Q", 1),
                struct.pack(
                    "<IiQQ",
                    camera.camera_id,
                    camera.model_id,
                    camera.width,
                    camera.height,
                ),
                struct.pack("<ddd", *camera.params),
            )
        )
        self.assertEqual(camera_stream.getvalue(), expected_cameras)

        image_stream = io.BytesIO()
        binary_writer._serialize_images(image_stream, model)
        image = next(iter(model.images.values()))
        expected_images = b"".join(
            (
                struct.pack("<Q", 1),
                struct.pack(
                    "<IdddddddI",
                    image.image_id,
                    *image.qvec,
                    *image.tvec,
                    image.camera_id,
                ),
                image.name.encode("utf-8"),
                b"\0",
                struct.pack("<Q", len(image.points2d)),
                b"".join(
                    struct.pack(
                        "<ddQ",
                        point.x,
                        point.y,
                        (1 << 64) - 1
                        if point.point3d_id is None
                        else point.point3d_id,
                    )
                    for point in image.points2d
                ),
            )
        )
        self.assertEqual(image_stream.getvalue(), expected_images)

        point_stream = io.BytesIO()
        binary_writer._serialize_points3d(point_stream, model)
        point = next(iter(model.points3d.values()))
        expected_points = b"".join(
            (
                struct.pack("<Q", 1),
                struct.pack(
                    "<QdddBBBd",
                    point.point3d_id,
                    *point.xyz,
                    *point.rgb,
                    point.error,
                ),
                struct.pack("<Q", len(point.track)),
                b"".join(
                    struct.pack("<II", track.image_id, track.point2d_idx)
                    for track in point.track
                ),
            )
        )
        self.assertEqual(point_stream.getvalue(), expected_points)

        rig_stream = io.BytesIO()
        binary_writer._serialize_rigs(rig_stream, model)
        rig = next(iter(model.rigs.values()))
        expected_rigs = [
            struct.pack("<Q", 1),
            struct.pack(
                "<IIiI",
                rig.rig_id,
                len(rig.sensors) + 1,
                rig.ref_sensor_type,
                rig.ref_sensor_id,
            ),
        ]
        for sensor in rig.sensors:
            expected_rigs.append(
                struct.pack(
                    "<iIB",
                    sensor.sensor_type,
                    sensor.sensor_id,
                    int(sensor.has_pose),
                )
            )
            if sensor.has_pose:
                expected_rigs.append(
                    struct.pack(
                        "<ddddddd",
                        *(sensor.qvec or ()),
                        *(sensor.tvec or ()),
                    )
                )
        self.assertEqual(rig_stream.getvalue(), b"".join(expected_rigs))

        frame_stream = io.BytesIO()
        binary_writer._serialize_frames(frame_stream, model)
        frame = next(iter(model.frames.values()))
        expected_frames = [
            struct.pack(
                "<QIIdddddddI",
                1,
                frame.frame_id,
                frame.rig_id,
                *frame.qvec,
                *frame.tvec,
                len(frame.data_ids),
            )
        ]
        expected_frames.extend(
            struct.pack(
                "<iIQ",
                data_id.sensor_type,
                data_id.sensor_id,
                data_id.data_id,
            )
            for data_id in frame.data_ids
        )
        self.assertEqual(frame_stream.getvalue(), b"".join(expected_frames))

    def test_serializers_preserve_dictionary_insertion_order(self):
        model = binary_writer_test_model()
        first_camera = next(iter(model.cameras.values()))
        second_camera = replace(first_camera, camera_id=7)
        model.cameras = {
            first_camera.camera_id: first_camera,
            second_camera.camera_id: second_camera,
        }

        stream = io.BytesIO()
        binary_writer._serialize_cameras(stream, model)

        first_record_offset = struct.calcsize("<Q")
        first_id = struct.unpack_from("<I", stream.getvalue(), first_record_offset)[0]
        self.assertEqual(first_id, first_camera.camera_id)


if __name__ == "__main__":
    unittest.main()
