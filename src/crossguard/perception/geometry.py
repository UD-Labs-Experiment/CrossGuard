"""Flat-ground localization from calibrated CARLA RGB cameras (no actor truth).

Image size is (width, height), FOV is horizontal degrees, and the rigid 4x4
transform maps CARLA camera axes (X forward, Y right, Z up) to world axes.
The bottom-centre of a fully visible box is a ground contact estimate. For
vehicles this is an approximate visible footprint, not the vehicle centre;
occlusion, chassis clearance and extent introduce metres of systematic error.
"""
import math
import numpy as np

from crossguard.contracts import Detection


def ground_position(detection: Detection, image_size, horizontal_fov,
                    world_from_camera, ground_z=0.0, max_distance=100.0):
    """Return world (x,y), or None for an unusable box or ground ray.

    Bad calibration raises ValueError. Border-touching boxes are conservatively
    rejected because their ground contact may be truncated. Distance is the
    full 3D camera-to-contact distance, not the unnormalised ray parameter.
    """
    width, height = image_size
    if (not all(math.isfinite(v) for v in
                (width, height, horizontal_fov, ground_z, max_distance))
            or width <= 2 or height <= 2 or not 0 < horizontal_fov < 180
            or max_distance <= 0):
        raise ValueError("Invalid image dimensions, FOV, plane or distance")
    transform = np.asarray(world_from_camera, dtype=float)
    if (transform.shape != (4, 4) or not np.isfinite(transform).all()
            or not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-6)
            or not np.allclose(transform[:3, :3].T @ transform[:3, :3],
                               np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(transform[:3, :3]), 1, atol=1e-5)):
        raise ValueError("Camera transform must be a finite rigid 4x4 transform")
    box = np.asarray(detection.bbox, dtype=float)
    if box.shape != (4,) or not np.isfinite(box).all():
        return None
    x1, y1, x2, y2 = box
    if not (0 < x1 < x2 < width - 1 and 0 < y1 < y2 < height - 1):
        return None
    focal = width / (2 * math.tan(math.radians(horizontal_fov) / 2))
    ray = np.array([1.0, ((x1 + x2) / 2 - width / 2) / focal,
                    -(y2 - height / 2) / focal])
    direction = transform[:3, :3] @ ray
    origin = transform[:3, 3]
    # A valid camera must be above the plane and its ray point downwards.
    if origin[2] <= ground_z or direction[2] >= -1e-9:
        return None
    scale = (ground_z - origin[2]) / direction[2]
    point = origin + scale * direction
    if not np.isfinite(point).all() or np.linalg.norm(point - origin) > max_distance:
        return None
    return float(point[0]), float(point[1])
