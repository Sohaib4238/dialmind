"""A lead's time zone, for calling hours in the lead's own local time.

Order: zip code in the location (first 3 digits) -> US state in the location -> phone area code.
Nothing found -> unknown, and the dialer skips the lead until the location is fixed.
Split states use their main zone, with the zip prefixes of the other zone listed below.
"""

import re

from app.area_codes import tz_for_phone

ET, CT, MT, PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
AZ, AK, HI, PR = "America/Phoenix", "America/Anchorage", "Pacific/Honolulu", "America/Puerto_Rico"

# State -> main zone
STATE_TZ = {
    "AL": CT, "AK": AK, "AZ": AZ, "AR": CT, "CA": PT, "CO": MT, "CT": ET, "DE": ET, "DC": ET, "FL": ET, "GA": ET,
    "HI": HI, "ID": MT, "IL": CT, "IN": ET, "IA": CT, "KS": CT, "KY": ET, "LA": CT, "ME": ET, "MD": ET, "MA": ET,
    "MI": ET, "MN": CT, "MS": CT, "MO": CT, "MT": MT, "NE": CT, "NV": PT, "NH": ET, "NJ": ET, "NM": MT, "NY": ET,
    "NC": ET, "ND": CT, "OH": ET, "OK": CT, "OR": PT, "PA": ET, "RI": ET, "SC": ET, "SD": CT, "TN": CT, "TX": CT,
    "UT": MT, "VT": ET, "VA": ET, "WA": PT, "WV": ET, "WI": CT, "WY": MT, "PR": PR,
}
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}

# Zip prefix ranges (first 3 digits, inclusive) -> state
_ZIP3_RANGES = [
    (5, 5, "NY"), (6, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"), (50, 54, "VT"),
    (55, 55, "MA"), (56, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"), (150, 196, "PA"),
    (197, 199, "DE"), (200, 200, "DC"), (201, 201, "VA"), (202, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"),
    (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"), (320, 349, "FL"), (350, 369, "AL"),
    (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"), (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"),
    (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"), (569, 569, "DC"), (570, 577, "SD"),
    (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"), (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"),
    (700, 714, "LA"), (716, 729, "AR"), (730, 749, "OK"), (750, 799, "TX"), (800, 816, "CO"), (820, 831, "WY"),
    (832, 838, "ID"), (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"), (885, 885, "TX"), (889, 898, "NV"),
    (900, 961, "CA"), (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
]
# Zip prefixes in the other zone of a split state
_ZIP3_TZ = {
    **dict.fromkeys(("324", "325"), CT),  # Florida panhandle (Panama City, Pensacola)
    **dict.fromkeys(("373", "374", "376", "377", "378", "379"), ET),  # East Tennessee (Chattanooga, Knoxville)
    **dict.fromkeys(("420", "421", "422", "423", "424"), CT),  # West Kentucky (Paducah, Bowling Green, Owensboro)
    **dict.fromkeys(("463", "464", "476", "477"), CT),  # NW Indiana (Gary), SW Indiana (Evansville)
    **dict.fromkeys(("798", "799", "885"), MT),  # El Paso
    "693": MT,  # western Nebraska (Scottsbluff)
    "577": MT,  # western South Dakota (Rapid City)
    "586": MT,  # SW North Dakota (Dickinson)
    **dict.fromkeys(("835", "838"), PT),  # north Idaho (Lewiston, Coeur d'Alene)
    "979": MT,  # eastern Oregon (Ontario)
}

ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_ABBR_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(STATE_TZ) + r")(?![A-Za-z])")
_NAME_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted(map(re.escape, STATE_NAMES), key=len, reverse=True)) + r")(?![a-z])")

SOURCE_LABEL = {"zip": "from zip code", "state": "from state", "area_code": "from area code", "unknown": "unknown"}


def zip_state(zip5: str) -> str | None:
    n = int(zip5[:3])
    return next((st for lo, hi, st in _ZIP3_RANGES if lo <= n <= hi), None)


def tz_for_zip(zip5: str) -> str | None:
    st = zip_state(zip5)
    return (_ZIP3_TZ.get(zip5[:3]) or STATE_TZ[st]) if st else None


def state_in(location: str) -> str | None:
    """A US state named in free text: "Austin, TX", "austin, tx", "Denver Colorado"."""
    text = location or ""
    abbrs = _ABBR_RE.findall(text)  # upper case only, so "in", "or", "me" in normal words don't count
    after_comma = re.findall(r",\s*([A-Za-z]{2})(?![A-Za-z])", text)
    for cand in reversed(abbrs + [a.upper() for a in after_comma]):
        if cand in STATE_TZ:
            return cand
    names = _NAME_RE.findall(text.lower())
    if names:
        return STATE_NAMES[names[-1]]
    return None


def lead_timezone(location: str, phone: str) -> tuple[str | None, str]:
    """(IANA zone or None, source: zip / state / area_code / unknown)."""
    for z in reversed(ZIP_RE.findall(location or "")):  # the zip is usually last in an address
        if tz := tz_for_zip(z):
            return tz, "zip"
    if st := state_in(location):
        return STATE_TZ[st], "state"
    tz, known = tz_for_phone(phone or "")
    return (tz, "area_code") if known else (None, "unknown")
