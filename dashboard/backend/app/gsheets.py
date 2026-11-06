"""Google Sheets API with a service account: READ the sales team's sheet, WRITE our own results sheet.

No Google library: the service account's key signs a JWT (RS256, `cryptography`), which Google swaps for a 1-hour
access token (https://developers.google.com/identity/protocols/oauth2/service-account#httprest).

Key file: GOOGLE_SERVICE_ACCOUNT_FILE, default google_service_account.json next to .env
(server: /srv/dialmind/secrets/google_service_account.json, permission 600). Its private key and the tokens are never
logged, shown or stored anywhere else; only the account's email (client_email) is shown, so people know whom to share with.

Two tokens, two scopes, so the sales sheet can't be changed by us even by mistake:
  read_grid / tab_titles  token with spreadsheets.readonly, GET only     (the sales sheet)
  values_* / ensure_tab   token with spreadsheets, only for the RESULTS sheet: _check_writable refuses any other ID
                          and refuses outright when it's the sales sheet.
Every request goes through _http (tests replace it with a fake Google).
"""

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from env_utils import ENV_PATH  # shared/env_utils.py

logger = logging.getLogger("dashboard.gsheets")

API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_ROWS = 5000  # per tab
LAST_COL = "Z"
_tokens: dict[str, tuple[str, float]] = {}  # scope -> (token, expires at); in memory only


class SheetsError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# ---- the key -----------------------------------------------------------------------------

def key_path() -> Path:
    return Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or ENV_PATH.with_name("google_service_account.json"))


def _load_key() -> dict:
    p = key_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SheetsError(f"No Google key file yet ({p}). See the README (Running it for real).") from None
    except (OSError, ValueError):
        raise SheetsError(f"The Google key file ({p.name}) can't be read or isn't JSON.") from None
    if not isinstance(data, dict) or data.get("type") != "service_account" or not data.get("private_key") \
            or not data.get("client_email"):
        raise SheetsError(f"{p.name} isn't a service account key (download a JSON key for the service account).")
    return data


def key_info() -> dict:
    """For the dashboard: is a key there, and which email to share the sheets with. Never the key itself."""
    try:
        k = _load_key()
    except SheetsError as e:
        return {"ok": False, "client_email": "", "detail": str(e)}
    return {"ok": True, "client_email": k["client_email"], "detail": ""}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(key: dict, scope: str, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(now or time.time())
    header = {"alg": "RS256", "typ": "JWT", **({"kid": key["private_key_id"]} if key.get("private_key_id") else {})}
    claims = {"iss": key["client_email"], "scope": scope, "aud": key.get("token_uri") or TOKEN_URL,
              "iat": now, "exp": now + 3600}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}".encode()
    try:
        private = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    except (ValueError, TypeError):
        raise SheetsError("The private key in the Google key file can't be read.") from None
    return f"{signing_input.decode()}.{_b64(private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))}"


async def _token(scope: str) -> str:
    tok, exp = _tokens.get(scope, ("", 0.0))
    if tok and exp - 60 > time.time():
        return tok
    key = _load_key()
    status, body = await _http("POST", key.get("token_uri") or TOKEN_URL,
                               form={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                     "assertion": make_jwt(key, scope)})
    if status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        why = body.get("error_description") or body.get("error") if isinstance(body, dict) else ""
        raise SheetsError(f"Google refused the service account key ({why or f'HTTP {status}'}). "
                          "Is the key still active in Google Cloud?", status)
    _tokens[scope] = (body["access_token"], time.time() + int(body.get("expires_in") or 3600))
    return body["access_token"]


# ---- HTTP ----------------------------------------------------------------------------------

async def _http(method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None,
                form: dict | None = None) -> tuple[int, object]:
    """The one place that talks to Google. (status, parsed JSON or text). Tests replace this."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as http:
            async with http.request(method, url, headers=headers, json=json_body, data=form) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = (await resp.text())[:400]
                return resp.status, body
    except aiohttp.ClientError as e:
        raise SheetsError(f"Couldn't reach Google ({type(e).__name__})") from None
    except TimeoutError:
        raise SheetsError("Google did not answer in time") from None


def _google_error(body: object) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message") or "")[:300]
    return str(body)[:200]


async def _api(method: str, url: str, scope: str, json_body: dict | None = None) -> dict:
    if scope == READ_SCOPE and method != "GET":
        raise SheetsError("Read-only: refused to change the sales sheet.")  # can't happen; belt and braces
    status, body = await _http(method, url, headers={"Authorization": f"Bearer {await _token(scope)}"}, json_body=json_body)
    if status == 401:
        _tokens.pop(scope, None)
    if status in (403, 404):
        email = key_info().get("client_email") or "the service account"
        what = "found" if status == 404 else "opened"
        raise SheetsError(f"The sheet can't be {what} with our Google account. Check the link, and that the sheet is "
                          f"shared with {email}.", status)
    if status >= 400:
        raise SheetsError(f"Google Sheets: HTTP {status}: {_google_error(body)}", status)
    return body if isinstance(body, dict) else {}


def sheet_id_from(link: str) -> str:
    """A sheet link (https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0) or the ID itself -> the ID, or ""."""
    s = (link or "").strip()
    if m := re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", s):
        return m[1]
    return s if re.fullmatch(r"[A-Za-z0-9_-]{20,}", s) else ""


def a1(tab: str, cells: str = "") -> str:
    """'My tab'!A1:Z: quotes doubled, as Sheets wants."""
    name = "'" + tab.replace("'", "''") + "'"
    return f"{name}!{cells}" if cells else name


# ---- reading (the sales sheet: read-only scope, GET only) ------------------------------------

async def tab_titles(sheet_id: str, scope: str = READ_SCOPE) -> list[str]:
    body = await _api("GET", f"{API}/{sheet_id}?fields=sheets.properties.title", scope)
    return [((s.get("properties") or {}).get("title") or "") for s in body.get("sheets") or []]


async def read_grid(sheet_id: str, tabs: list[str], meta: dict | None = None) -> dict[str, list[list[dict]]]:
    """The tabs' cells with their background colour: {tab: [[{"text", "rgb"}...] per row]}. Read-only.
    meta (optional, filled in): {tab: {"conditional": n, "banded": n}}, the conditional-formatting rules and alternating
    colour ranges, whose colours Google doesn't report per cell (checked 2026-11-05: the sales sheet has none)."""
    fields = ("sheets(properties(title),conditionalFormats(ranges),bandedRanges(range),data(rowData(values(formattedValue,"
              "effectiveFormat(backgroundColor,backgroundColorStyle))))),properties(spreadsheetTheme)")
    ranges = "&".join("ranges=" + quote(a1(t, f"A1:{LAST_COL}{MAX_ROWS}"), safe="") for t in tabs)
    body = await _api("GET", f"{API}/{sheet_id}?includeGridData=true&{ranges}&fields={quote(fields, safe='(),/')}", READ_SCOPE)
    theme = _theme_colors(body)
    out: dict[str, list[list[dict]]] = {}
    for sh in body.get("sheets") or []:
        title = (sh.get("properties") or {}).get("title") or ""
        if meta is not None:
            meta[title] = {"conditional": len(sh.get("conditionalFormats") or []), "banded": len(sh.get("bandedRanges") or [])}
        rows = []
        for d in sh.get("data") or []:
            for r in d.get("rowData") or []:
                rows.append([{"text": str(v.get("formattedValue") or ""), "rgb": cell_rgb(v, theme)}
                             for v in r.get("values") or []])
        out[title] = rows
    return out


def _rgb(c: dict | None) -> tuple[float, float, float] | None:
    if not isinstance(c, dict):
        return None
    return (float(c.get("red", 0)), float(c.get("green", 0)), float(c.get("blue", 0)))  # Google leaves out zeros


def _theme_colors(body: dict) -> dict[str, tuple]:
    theme = ((body.get("properties") or {}).get("spreadsheetTheme") or {}).get("themeColors") or []
    return {t.get("colorType"): _rgb((t.get("color") or {}).get("rgbColor")) for t in theme if t.get("colorType")}


def cell_rgb(v: dict, theme: dict | None = None) -> tuple[float, float, float] | None:
    """The cell's painted background (effectiveFormat; conditional formatting isn't included by Google)."""
    fmt = v.get("effectiveFormat") or {}
    style = fmt.get("backgroundColorStyle") or {}
    if style.get("rgbColor") is not None:
        return _rgb(style["rgbColor"])
    if style.get("themeColor") and theme:
        return theme.get(style["themeColor"])
    return _rgb(fmt.get("backgroundColor"))


def hex_of(rgb: tuple | None) -> str:
    """#rrggbb, or "" for no fill."""
    if not rgb:
        return ""
    return "#" + "".join(f"{round(min(1.0, max(0.0, x)) * 255):02x}" for x in rgb)


COLOR_NAMES = ("green", "yellow", "orange", "red", "pink", "purple", "blue", "cyan", "gray")


def color_name(rgb: tuple | None) -> str:
    """'none' (white / no fill) or a colour family. Any shade counts: light green 3 (#d9ead3) is green, the default
    yellow highlight and light yellow 3 (#fff2cc) are yellow."""
    if not rgb:
        return "none"
    r, g, b = (min(1.0, max(0.0, x)) for x in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    c = mx - mn
    if c < 0.05:
        return "none" if mx > 0.95 else "gray"
    if mx == r:
        hue = 60 * (((g - b) / c) % 6)
    elif mx == g:
        hue = 60 * ((b - r) / c + 2)
    else:
        hue = 60 * ((r - g) / c + 4)
    for limit, name in ((15, "red"), (40, "orange"), (70, "yellow"), (170, "green"), (200, "cyan"), (260, "blue"),
                        (290, "purple"), (345, "pink"), (361, "red")):
        if hue < limit:
            return name
    return "red"


# ---- writing (only the results sheet) ---------------------------------------------------------

def _check_writable(sheet_id: str) -> None:
    from app import sheet_sync

    cfg = sheet_sync.load()
    results, sales = sheet_id_from(cfg["results_sheet"]), sheet_id_from(cfg["sales_sheet"])
    if not sheet_id or sheet_id == sales:
        raise SheetsError("Refused: we never write to the sales team's sheet.")
    if sheet_id != results:
        raise SheetsError("Refused: we only write to the results sheet set on the Leads page.")


async def ensure_tab(sheet_id: str, tab: str) -> None:
    _check_writable(sheet_id)
    if tab in await tab_titles(sheet_id, WRITE_SCOPE):
        return
    await _api("POST", f"{API}/{sheet_id}:batchUpdate", WRITE_SCOPE,
               {"requests": [{"addSheet": {"properties": {"title": tab}}}]})


async def values_get(sheet_id: str, range_: str) -> list[list[str]]:
    _check_writable(sheet_id)
    body = await _api("GET", f"{API}/{sheet_id}/values/{quote(range_, safe='')}", WRITE_SCOPE)
    return body.get("values") or []


async def values_update(sheet_id: str, data: list[dict]) -> None:
    """[{"range", "values"}]: RAW, so nothing typed into a lead (like "=IMPORTXML(...)") becomes a formula."""
    _check_writable(sheet_id)
    if data:
        await _api("POST", f"{API}/{sheet_id}/values:batchUpdate", WRITE_SCOPE, {"valueInputOption": "RAW", "data": data})


async def values_append(sheet_id: str, range_: str, rows: list[list]) -> None:
    _check_writable(sheet_id)
    if rows:
        await _api("POST", f"{API}/{sheet_id}/values/{quote(range_, safe='')}:append?valueInputOption=RAW"
                   "&insertDataOption=INSERT_ROWS", WRITE_SCOPE, {"values": rows})
