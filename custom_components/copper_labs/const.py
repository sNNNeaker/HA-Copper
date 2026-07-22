"""Constants and unit helpers for the Copper Labs integration.

Kept dependency-free (no Home Assistant imports) so it can also be imported by
plain scripts/tests.
"""

# Integration domain — the folder name, the config-entry key, and the prefix for
# entity unique_ids. Must match manifest.json's "domain".
DOMAIN = "copper_labs"

# Config-entry / options dictionary keys. Centralised so a typo can't silently
# create a mismatched key in one file vs another.
CONF_REFRESH_TOKEN = "refresh_token"   # the long-lived Auth0 token the user pastes in
CONF_PREMISE_ID = "premise_id"         # which premise this entry represents
CONF_UNITS = "units"                   # per-meter display-unit choices (options flow)
CONF_DEBUG = "debug_logging"           # persistent debug-log toggle (options flow)

# The data API sets Cache-Control: max-age=900 (15 min), i.e. it only recomputes
# every 15 minutes. Polling faster just returns cached data, so match that cadence.
SCAN_INTERVAL_MINUTES = 15

# What the API actually reports per meter type (confirmed against the Copper app:
# gas in CCF, water in US gallons). Readings are converted FROM these into the
# user's chosen display unit. This is the "source of truth" side of conversion.
SOURCE_UNITS = {
    "gas": "CCF",
    "water_indoor": "gal",
    "water_outdoor": "gal",
    "electric": "kWh",
}

# Default *display* unit per meter type. Set equal to the source units so the
# out-of-the-box numbers are exact (no conversion, no rounding). Users can pick
# a different unit per meter in the options flow (e.g. m³ / L).
DEFAULT_UNITS = dict(SOURCE_UNITS)

# Cubic metres per unit — the single table that lets us convert any volume unit
# to any other by going via m³. Electric (kWh) is energy, not volume, so it isn't
# listed and is intentionally left unconverted.
_M3_PER_UNIT = {
    "m³": 1.0,
    "L": 0.001,
    "gal": 0.00378541,   # US gallon
    "ft³": 0.0283168,
    "CCF": 2.83168,      # 100 cubic feet
}


def convert_volume(value, src, dst):
    """Convert a volume reading from unit `src` to unit `dst`.

    Returns the value unchanged when it's None, the units already match, or
    either unit isn't a known volume (e.g. electric kWh) — so callers can call it
    unconditionally without special-casing those situations.
    """
    if value is None or src == dst:
        # Nothing to do: no reading yet, or already in the target unit.
        return value
    f_src = _M3_PER_UNIT.get(src)
    f_dst = _M3_PER_UNIT.get(dst)
    if not f_src or not f_dst:
        # Unknown/non-volume unit on either side -> leave the number as-is rather
        # than produce a nonsensical conversion.
        return value
    # value (in src) -> cubic metres -> value (in dst).
    return value * f_src / f_dst
