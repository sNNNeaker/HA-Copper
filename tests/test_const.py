"""Unit tests for const.py: unit tables and convert_volume()."""

import pytest

import const


def test_default_units_is_a_copy():
    # DEFAULT_UNITS must be an independent dict; mutating it (e.g. in a test or
    # future code) must not silently change what the API reports (SOURCE_UNITS).
    assert const.DEFAULT_UNITS == const.SOURCE_UNITS
    assert const.DEFAULT_UNITS is not const.SOURCE_UNITS


def test_convert_none_passthrough():
    # No reading yet -> stays None so entities can go unavailable.
    assert const.convert_volume(None, "gal", "L") is None


def test_convert_same_unit_is_exact():
    # Default config is source unit == display unit: value must be untouched
    # (no float round-trip), so out-of-the-box numbers match the Copper app.
    assert const.convert_volume(123.456, "CCF", "CCF") == 123.456


def test_convert_unknown_unit_passthrough():
    # Electric is kWh (energy, not volume) -> deliberately left unconverted.
    assert const.convert_volume(42.0, "kWh", "m³") == 42.0
    assert const.convert_volume(42.0, "gal", "kWh") == 42.0


@pytest.mark.parametrize(
    ("value", "src", "dst", "expected"),
    [
        (1.0, "m³", "L", 1000.0),          # SI sanity
        (100.0, "gal", "L", 378.541),      # US gallon definition
        (1.0, "CCF", "ft³", 100.0),        # CCF = 100 cubic feet
        (1.0, "CCF", "m³", 2.83168),       # gas bill conversion
    ],
)
def test_convert_known_pairs(value, src, dst, expected):
    assert const.convert_volume(value, src, dst) == pytest.approx(expected, rel=1e-6)


def test_convert_round_trip():
    # gal -> m³ -> gal must return the original value (within float precision).
    there = const.convert_volume(250.0, "gal", "m³")
    back = const.convert_volume(there, "m³", "gal")
    assert back == pytest.approx(250.0, rel=1e-12)
