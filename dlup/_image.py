# coding=utf-8
# Copyright (c) dlup contributors

"""Whole slide image access objects.

In this module we take care of abstracting the access to whole slide images.
The main workhorse is SlideImage which takes care of simplifying region extraction
of discrete-levels pyramidal images in a continuous way, validating relevant
properties and offering a future aggregated api for possibly multiple different backends
other than OpenSlide.
"""

import errno
import functools
import os
import pathlib
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple, Type, TypeVar, Union

import numpy as np  # type: ignore
import PIL
import PIL.Image  # type: ignore
from numpy.typing import ArrayLike

import openslide  # type: ignore
from dlup import DlupUnsupportedSlideError

from ._region import BoundaryMode, RegionView

_GenericNumber = Union[int, float]
_GenericNumberArray = Union[np.ndarray, Iterable[_GenericNumber]]
_GenericFloatArray = Union[np.ndarray, Iterable[float]]
_GenericIntArray = Union[np.ndarray, Iterable[int]]
_Box = Tuple[_GenericNumber, _GenericNumber, _GenericNumber, _GenericNumber]
_TSlideImage = TypeVar("_TSlideImage", bound="SlideImage")


class _SlideImageRegionView(RegionView):
    """Represents an image view tied to a slide image."""

    def __init__(self, wsi: _TSlideImage, scaling: _GenericNumber, boundary_mode: BoundaryMode = None):
        """Initialize with a slide image object and the scaling level."""
        # Always call the parent init
        super().__init__(boundary_mode=boundary_mode)
        self._wsi = wsi
        self._scaling = scaling

    @property
    def mpp(self) -> float:
        """Returns the level effective mpp."""
        return self._wsi.mpp / self._scaling

    @property
    def size(self) -> Tuple[int, ...]:
        """Size"""
        return self._wsi.get_scaled_size(self._scaling)

    def _read_region_impl(self, location: _GenericFloatArray, size: _GenericIntArray) -> PIL.Image.Image:
        """Returns a region of the level associated to the view."""
        x, y = location
        w, h = size
        return self._wsi.read_region((x, y), self._scaling, (w, h))


def _clip2size(a: np.ndarray, size: Tuple[_GenericNumber, _GenericNumber]) -> Sequence[_GenericNumber]:
    """Clip values from 0 to size boundaries."""
    return np.clip(a, (0, 0), size)


class SlideImage:
    """Utility class to simplify whole-slide pyramidal images management.

    This helper class furtherly abstracts openslide access to WSIs
    by validating some of the properties and giving access
    to a continuous pyramid. Layer values are interpolated from
    the closest high resolution layer.
    Each horizontal slices of the pyramid can be accessed using a scaling value
    z as index.

    Lifetime
    --------
    SlideImage is currently initialized and holds an openslide image object.
    The openslide wsi instance is automatically closed when gargbage collected.

    Examples
    --------
    >>> import dlup
    >>> wsi = dlup.SlideImage.from_file_path('path/to/slide.svs')
    """

    def __init__(self, wsi: openslide.AbstractSlide, identifier: Union[str, None] = None):
        """Initialize a whole slide image and validate its properties."""
        self._openslide_wsi = wsi
        self._identifier = identifier

        try:
            mpp_x = float(self._openslide_wsi.properties[openslide.PROPERTY_NAME_MPP_X])
            mpp_y = float(self._openslide_wsi.properties[openslide.PROPERTY_NAME_MPP_Y])
            mpp = np.array([mpp_y, mpp_x])
        except KeyError:
            raise DlupUnsupportedSlideError(f"slide property mpp is not available.", identifier)

        if not np.isclose(mpp[0], mpp[1], rtol=1.0e-2):
            raise DlupUnsupportedSlideError(f"cannot deal with slides having anisotropic mpps. Got {mpp}.", identifier)

        self._min_native_mpp = float(mpp[0])

    def close(self):
        """Close the underlying openslide image."""
        self._openslide_wsi.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    @classmethod
    def from_file_path(
        cls: Type[_TSlideImage], wsi_file_path: os.PathLike, identifier: Union[str, None] = None
    ) -> _TSlideImage:
        wsi_file_path = pathlib.Path(wsi_file_path)
        if not wsi_file_path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(wsi_file_path))
        try:
            wsi = openslide.open_slide(str(wsi_file_path))
        except (openslide.OpenSlideUnsupportedFormatError, PIL.UnidentifiedImageError):
            raise DlupUnsupportedSlideError(f"Unsupported file: {wsi_file_path}")

        return cls(wsi, str(wsi_file_path) if identifier is None else identifier)

    def read_region(
        self,
        location: Union[np.ndarray, Tuple[_GenericNumber, _GenericNumber]],
        scaling: float,
        size: Union[np.ndarray, Tuple[int, int]],
    ) -> PIL.Image.Image:
        """Return a region at a specific scaling level of the pyramid.

        A typical slide is made of several levels at different mpps.
        In normal cirmustances, it's not possible to retrieve an image of
        intermediate mpp between these levels. This method takes care of
        sumbsampling the closest high resolution level to extract a target
        region via interpolation.

        Once the best layer is selected, a native resolution region
        is extracted, with enough padding to include the samples necessary to downsample
        the final region (considering LANCZOS interpolation method basis functions).

        The steps are approximately the following:

        1. Map the region that we want to extract to the below layer.
        2. Add some extra values (left and right) to the native region we want to extract
           to take into account the interpolation samples at the border ("native_extra_pixels").
        3. Map the location to the level0 coordinates, floor it to add extra information
           on the left (level_zero_location_adapted).
        4. Re-map the integral level-0 location to the native_level.
        5. Compute the right bound of the region adding the native_size and extra pixels (native_size_adapted).
           The size is also clipped so that any extra pixel will fit within the native level.
        6. Since the native_size_adapted needs to be passed to openslide and has to be an integer, we ceil it
           to avoid problems with possible overflows of the right boundary of the target region being greater
           than the right boundary of the sample region
           (native_location + native_size > native_size_adapted + native_location_adapted).
        7. Crop the target region from within the sampled region by computing the relative
           coordinates (fractional_coordinates).

        Parameters
        ----------
        location :
            Location from the top left (x, y) in pixel coordinates given at the requested scaling.
        scaling :
            The scaling to be applied compared to level 0.
        size :
            Region size of the resulting region.

        Returns
        -------
        np.ndarray
            The extract region.

        Example
        -------
        The locations are defined at the requested scaling (with respect to level 0), so if we want to extract at
        location ``(location_x, location_y)`` of a scaling 0.5 (with respect to level 0), and have resulting tile size of
         ``(tile_size, tile_size)`` with a scaling factor of 0.5, we can use:
        >>>  wsi.read_region(location=(coordinate_x, coordinate_y), scaling=0.5, size=(tile_size, tile_size))
        """
        owsi = self._openslide_wsi
        location = np.asarray(location)
        size = np.asarray(size)
        level_size = np.array(self.get_scaled_size(scaling))

        if (size < 0).any():
            raise ValueError("Size values must be greater than zero.")

        if ((location < 0) | ((location + size) > level_size)).any():
            raise ValueError("Requested region is outside level boundaries.")

        # Compute values projected onto the best layer.
        native_level = owsi.get_best_level_for_downsample(1 / scaling)
        native_level_size = owsi.level_dimensions[native_level]
        native_level_downsample = owsi.level_downsamples[native_level]
        native_scaling = scaling * owsi.level_downsamples[native_level]
        native_location = location / native_scaling
        native_size = size / native_scaling

        # OpenSlide doesn't feature float coordinates to extract a region.
        # We need to extract enough pixels and let PIL do the interpolation.
        # In the borders, the basis functions of other samples contribute to the final value.
        # PIL lanczos uses 3 pixels as support.
        # See pillow: https://git.io/JG0QD
        native_extra_pixels = 3 if native_scaling > 1 else np.ceil(3 / native_scaling)

        # Compute the native location while counting the extra pixels.
        native_location_adapted = np.floor(native_location - native_extra_pixels).astype(int)
        native_location_adapted = _clip2size(native_location_adapted, native_level_size)

        # Unfortunately openslide requires the location in pixels from level 0.
        level_zero_location_adapted = np.floor(native_location_adapted * native_level_downsample).astype(int)
        native_location_adapted = level_zero_location_adapted / native_level_downsample
        native_size_adapted = np.ceil(native_location + native_size + native_extra_pixels).astype(int)
        native_size_adapted = _clip2size(native_size_adapted, native_level_size) - native_location_adapted

        # By casting to int we introduce a small error in the right boundary leading
        # to a smaller region which might lead to the target region to overflow from the sampled
        # region.
        native_size_adapted = np.ceil(native_size_adapted).astype(int)

        # We extract the region via openslide with the required extra border
        region = owsi.read_region(tuple(level_zero_location_adapted), native_level, tuple(native_size_adapted))

        # Within this region, there are a bunch of extra pixels, we interpolate to sample
        # the pixel in the right position to retain the right sample weight.
        fractional_coordinates = native_location - native_location_adapted
        box = (*fractional_coordinates, *(fractional_coordinates + native_size))
        return region.resize(size, resample=PIL.Image.LANCZOS, box=box)

    def get_scaled_size(self, scaling: _GenericNumber) -> Tuple[int, ...]:
        """Compute slide image size at specific scaling."""
        size = np.array(self.size) * scaling
        return tuple(size.astype(int))

    def get_mpp(self, scaling: float) -> float:
        """Returns the respective mpp from the scaling."""
        return self._min_native_mpp / scaling

    def get_scaling(self, mpp: float) -> float:
        """Inverse of get_mpp()."""
        return self._min_native_mpp / mpp

    def get_scaled_view(self, scaling: _GenericNumber) -> _SlideImageRegionView:
        """Returns a RegionView at a specific level."""
        return _SlideImageRegionView(self, scaling)

    def get_thumbnail(self, size: Tuple[int, int] = (512, 512)) -> PIL.Image.Image:
        """Returns an RGB numpy thumbnail for the current slide.

        Parameters
        ----------
        size :
            Maximum bounding box for the thumbnail expressed as (width, height).
        """
        return self._openslide_wsi.get_thumbnail(size)

    @property
    def thumbnail(self) -> PIL.Image.Image:
        """Returns the thumbnail."""
        return self.get_thumbnail()

    @property
    def identifier(self) -> Optional[str]:
        """Returns a user-defined identifier."""
        return self._identifier

    @property
    def properties(self) -> dict:
        """Returns any extra associated properties with the image."""
        return self._openslide_wsi.properties

    @property
    def vendor(self) -> str:
        """Returns the scanner vendor."""
        return self.properties["openslide.vendor"]

    @property
    def size(self) -> Tuple[int, int]:
        """Returns the highest resolution image size in pixels."""
        return self._openslide_wsi.dimensions

    @property
    def mpp(self) -> float:
        """Returns the microns per pixel of the high res image."""
        return self._min_native_mpp

    @property
    def magnification(self) -> int:
        """Returns the objective power at which the WSI was sampled."""
        return int(self._openslide_wsi.properties[openslide.PROPERTY_NAME_OBJECTIVE_POWER])

    @property
    def aspect_ratio(self) -> float:
        """Returns width / height."""
        width, height = self.size
        return width / height

    def __repr__(self) -> str:
        """Returns the SlideImage representation and some of its properties."""
        props = ("identifier", "vendor", "mpp", "magnification", "size")
        props_str = []
        for key in props:
            value = getattr(self, key)
            props_str.append(f"{key}={value}")
        return f"{self.__class__.__name__}({', '.join(props_str)})"
