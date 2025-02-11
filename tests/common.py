# coding=utf-8
# Copyright (c) dlup contributors

"""Utilities to simplify the mocking of SlideImages."""
from typing import Any, Dict, Optional, Sequence, Tuple, Type, Union

import numpy as np
import PIL
import pytest
from PIL.Image import Image
from pydantic import BaseModel, Field
from scipy import interpolate

import openslide  # type: ignore
from dlup import DlupUnsupportedSlideError, SlideImage


def get_sample_nonuniform_image(size: Tuple[int, int] = (256, 256)):
    """Generate a non-uniform sample image."""
    # Interpolate some simple function
    interp_args = ((0, 1), (0, 1), ((0, 1), (1, 0)))  # x  # y  # z
    f = interpolate.interp2d(*interp_args, kind="linear")
    if not (np.array(size) % 2 == 0).all():
        raise ValueError("Size should be a tuple of values divisible by two.")

    # Sample it
    width, height = size
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)
    z = f(x, y)

    # Interpret it as HSV, so we get funny colors
    im = np.zeros((height, width, 3))
    im[:, :, 0] = z.T
    im[:, :, 1] = 1

    # Set the value to a pixel-level checkerboard.
    # Maybe there's a faster way.
    im[height // 2, width // 2, 2] = 1
    im[:, :, 2] = np.sign(np.fft.ifft2(im[:, :, 2]).real)
    im = im * 255
    im = im.astype("uint8")
    im = PIL.Image.fromarray(im, mode="HSV")
    return im.convert(mode="RGBA")  # type: ignore


class SlideProperties(BaseModel):
    """Mock configuration properties."""

    mpp_x: Optional[float] = Field(1.0, alias=openslide.PROPERTY_NAME_MPP_X)
    mpp_y: Optional[float] = Field(1.0, alias=openslide.PROPERTY_NAME_MPP_Y)
    mag: Optional[int] = Field(40, alias=openslide.PROPERTY_NAME_OBJECTIVE_POWER)
    vendor: str = Field("dummy", alias=openslide.PROPERTY_NAME_VENDOR)

    class Config:
        allow_population_by_field_name = True


class SlideConfig(BaseModel):
    """Mock slide configuration."""

    image: Type[Image] = get_sample_nonuniform_image()
    properties: SlideProperties = SlideProperties()
    level_downsamples: Tuple[float, ...] = (1.0, 2.0)


class OpenSlideImageMock(openslide.ImageSlide):
    """Mock OpenSlide object also with layers.

    NOTE: read_region works a bit differently than the actual openslide.
    Openslide *does* project the float base layer values to the best layer
    and then performs different operations such as adding saturation and translations.
    https://github.com/openslide/openslide/blob/main/src/openslide.c#L488-L493
    """

    properties: Dict[Any, Any] = {}
    level_downsamples: Sequence[Union[float, int]] = (1.0,)

    def __init__(self, image: PIL.Image, properties: Dict, level_downsamples: Tuple):
        self.properties = properties
        self.image = image
        self.level_downsamples = sorted(level_downsamples)
        base_size = np.array((self.image.width, self.image.height))
        self._level_dimensions = tuple([tuple((base_size / d).astype(int)) for d in self.level_downsamples])
        super().__init__(image)

    def get_best_level_for_downsample(self, downsample):
        level_downsamples = np.array(self.level_downsamples)
        level = 0 if downsample < 1 else np.where(level_downsamples <= downsample)[0][-1]
        return level

    @property
    def level_dimensions(self):
        return self._level_dimensions

    def get_level_image(self, level):
        return self.image.resize(self.level_dimensions[level])

    def read_region(self, location, level, size):
        image = np.array(self.get_level_image(level))

        # Add a single pixel padding
        image = np.pad(image, [(0, 1), (0, 1), (0, 0)])
        image = PIL.Image.fromarray(image)
        location = np.asarray(location) / self.level_downsamples[level]
        return image.resize(size, resample=PIL.Image.LANCZOS, box=(*location, *(location + size)))

    @classmethod
    def from_slide_config(cls, slide_config):
        return cls(
            slide_config.image,
            slide_config.properties.dict(by_alias=True, exclude_none=True),
            slide_config.level_downsamples,
        )
