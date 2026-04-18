"""Coordinate transforms between rotated-display space and original-frame space."""


def rotated_to_original(nx, ny, method):
    """Convert normalised coords in rotated-video space to original-frame space.

    Rotation methods match GStreamer videoflip:
        0: no rotation
        1: 90° CW
        2: 180°
        3: 270° CW
    """
    if method == 0: return nx, ny
    if method == 1: return ny, 1 - nx
    if method == 2: return 1 - nx, 1 - ny
    if method == 3: return 1 - ny, nx
    return nx, ny
