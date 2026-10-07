"""Cameras: ``create_camera`` gives the real one (OpenCV); ``fake.FakeCamera`` stands in for tests and ``--fake``."""

from __future__ import annotations

from .base import Camera, CameraError, CameraFrame, CameraInfo

__all__ = ["Camera", "CameraError", "CameraFrame", "CameraInfo", "create_camera", "list_cameras"]


def create_camera(spec: str | None, *, width: int = 1280, height: int = 720, fps: int = 30) -> Camera:
    """A closed camera; ``open()`` opens it.

    ``spec``: None or "" for the first camera, a decimal index, or part of a
    camera's name (case-insensitive), as the ``--camera`` option takes it.
    """
    from .opencv_camera import OpenCVCamera

    return OpenCVCamera(spec, width=width, height=height, fps=fps)


def list_cameras() -> list[tuple[int, str]]:
    """(index, name) of the cameras the OS lists, without opening any; [] when they can't be listed."""
    from .opencv_camera import list_cameras as _list

    return _list()
