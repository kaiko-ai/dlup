# coding=utf-8
# Copyright (c) dlup contributors

import collections
import functools
from enum import Enum
from typing import Iterator, List, Sequence, Tuple, Type, TypeVar, Union

import numpy as np

from ._region import RegionView

_GenericNumber = Union[int, float]
_GenericNumberArray = Union[np.ndarray, Sequence[_GenericNumber]]


class TilingMode(str, Enum):
    """Type of tiling.

    Skip will skip the border tiles if they don't fit the region.
    Overflow counts as last tile even if it's overflowing.
    Fit will change the overlapping region between tiles to make them fit the region.
    The grid will then become non-uniform in case of integral values.
    """

    skip = "skip"
    overflow = "overflow"
    fit = "fit"


def _flattened_array(a: Union[_GenericNumberArray, _GenericNumber]) -> np.ndarray:
    """Converts any generic array in a flattened numpy array."""
    return np.asarray(a).flatten()


def indexed_ndmesh(bases: Sequence[_GenericNumberArray], indexing="ij") -> np.ndarray:
    """Converts a list of arrays into an n-dimensional indexed mesh.

    Example
    -------

    .. code-block:: python

        import dlup
        mesh = dlup.tiling.indexed_ndmesh(((1, 2, 3), (4, 5, 6)))
        assert mesh[0, 0] == (1, 4)
        assert mesh[0, 1] == (1, 5)
    """
    return np.ascontiguousarray(np.stack(tuple(reversed(np.meshgrid(*reversed(bases), indexing=indexing)))).T)


def tiles_grid_coordinates(
    size: _GenericNumberArray,
    tile_size: _GenericNumberArray,
    tile_overlap: Union[_GenericNumberArray, _GenericNumber] = 0,
    mode: TilingMode = TilingMode.skip,
) -> List[np.ndarray]:
    """Generate a list of coordinates for each dimension representing a tile location.

    The first tile has the corner located at (0, 0).
    """
    size = _flattened_array(size)
    tile_size = _flattened_array(tile_size)
    tile_overlap = _flattened_array(tile_overlap)

    if not (size.shape == tile_size.shape == tile_overlap.shape):
        raise ValueError("size, tile_size and tile_overlap " "should have the same dimensions.")

    if (size <= 0).any():
        raise ValueError("size should always be greater than zero.")

    if (tile_size <= 0).any():
        raise ValueError("tile size should always be greater than zero.")

    # Let's force it to a valid value.
    tile_overlap = np.remainder(tile_overlap, np.minimum(tile_size, size), casting="safe")

    # Get the striding
    stride = tile_size - tile_overlap

    # Same thing as computing the output shape of a convolution with padding zero and
    # specified stride.
    num_tiles = (size - tile_size) / stride + 1

    if mode == TilingMode.skip:
        num_tiles = np.floor(num_tiles).astype(int)
        overflow = np.zeros_like(size)
    else:
        num_tiles = np.ceil(num_tiles).astype(int)
        tiled_size = (num_tiles - 1) * stride + tile_size
        overflow = tiled_size - size

    # Let's create our indices list
    coordinates = []
    for n, dstride, dtile_size, doverflow, dsize in zip(num_tiles, stride, tile_size, overflow, size):
        tiles_locations = np.arange(n) * dstride

        if mode == TilingMode.fit:
            if n < 2:
                coordinates.append(np.array([]))
                continue

            # The location of the last tile
            # should stay fixed at the end
            tiles_locations[-1] = dsize - dtile_size
            distribute = doverflow / (n - 1)
            tiles_locations = tiles_locations.astype(float)
            tiles_locations[1:-1] -= distribute * (np.arange(n - 2) + 1)

        coordinates.append(tiles_locations)
    return coordinates


class Grid(collections.abc.Sequence):
    """Facilitates the access to the coordinates of an n-dimensional grid."""

    def __init__(self, coordinates: List[np.ndarray]):
        """Initialize a lattice given a set of basis vectors."""
        self.coordinates = coordinates

    @classmethod
    def from_tiling(
        cls,
        offset: _GenericNumberArray,
        size: _GenericNumberArray,
        tile_size: _GenericNumberArray,
        tile_overlap: Union[_GenericNumberArray, _GenericNumber] = 0,
        mode: TilingMode = TilingMode.skip,
    ):
        """Generate a grid from a set of tiling parameters."""
        coordinates = tiles_grid_coordinates(size, tile_size, tile_overlap, mode)
        coordinates = [c + o for c, o in zip(coordinates, offset)]
        return cls(coordinates)

    @property
    def size(self) -> Tuple[int, ...]:
        """Return the size of the generated lattice."""
        return tuple(len(x) for x in self.coordinates)

    def __getitem__(self, key):
        index = np.unravel_index(key, self.size)
        return np.array([c[i] for c, i in zip(self.coordinates, index)])

    def __len__(self) -> int:
        """Return the total number of points in the grid."""
        return functools.reduce(lambda value, size: value * size, self.size, 1)

    def __iter__(self) -> Iterator[np.ndarray]:
        """Iterate through every tile."""
        for i in range(len(self)):
            yield self[i]
