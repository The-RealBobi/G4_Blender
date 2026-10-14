from mathutils import Matrix


def blender_from_g4_matrix() -> Matrix:
    return Matrix((
        (0.0, 0.0, 1.0, 0.0),
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))


def g4_from_blender_matrix() -> Matrix:
    return blender_from_g4_matrix().inverted()
