"""Unit tests for render.has_pixel_data."""
from __future__ import annotations

from pydicom.dataset import Dataset

from deid_app.render import has_pixel_data


def test_has_pixel_data_true_for_pixel_data():
    ds = Dataset()
    ds.PixelData = b"\x00"
    assert has_pixel_data(ds)


def test_has_pixel_data_true_for_float_pixel_data():
    ds = Dataset()
    ds.FloatPixelData = b"\x00\x00\x00\x00"
    assert has_pixel_data(ds)


def test_has_pixel_data_true_for_double_float_pixel_data():
    ds = Dataset()
    ds.DoubleFloatPixelData = b"\x00" * 8
    assert has_pixel_data(ds)


def test_has_pixel_data_false_when_absent():
    ds = Dataset()
    assert not has_pixel_data(ds)
