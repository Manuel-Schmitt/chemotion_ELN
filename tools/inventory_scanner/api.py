"""Bounded, fail-closed client for the existing Chemotion v1 API.

Audits require explicit collection IDs and full sample/SBMM detail access. Lists
use the server's normal collection lists (including its standard-sample
sample_or_startmat_or_products filter), never the SBMM chemical search.
Pagination headers are mandatory: guessing from a short page is unsafe because
the server resets out-of-range requests to page one. No write is ever replayed.
"""

from dataclasses import dataclass
from http.client import HTTPException
import json
import math
import re
import ssl
from urllib import error, parse, request


class ApiError(Exception):
    """A safe, operator-readable API failure."""


class AuthenticationError(ApiError):
    """The station token could not be authenticated."""


class PermissionDeniedError(ApiError):
    """The API explicitly refused access to a resource."""


class UnverifiedWrite(ApiError):
    """A write may have happened but its complete result could not be verified."""


KINDS = ("sample", "sequence_based_macromolecule_sample")


def _id(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _data(value):
    if not isinstance(value, list) or not value or any(type(x) is not dict for x in value):
        raise ApiError("Inventory data must be a nonempty array of objects.")
    return value


def _object(value, label):
    if type(value) is not dict:
        raise ApiError(f"Malformed {label} response.")
    return value


@dataclass(frozen=True)
class Ref:
    kind: str
    id: int

    def __post_init__(self):
        if self.kind not in KINDS or not _id(self.id):
            raise ApiError("Unsupported element type or invalid element ID.")

    @property
    def parent_key(self):
        return f"{self.kind}_id"

    @property
    def key(self):
        return f"{self.kind}:{self.id}"


@dataclass
class Inventory:
    ref: Ref
    id: int
    data: list[dict]

    def __post_init__(self):
        if not isinstance(self.ref, Ref) or not _id(self.id):
            raise ApiError("Inventory must have a persisted ID and valid parent.")
        _data(self.data)

    @property
    def first(self):
        return self.data[0]


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def _http_error(status: int, method: str) -> ApiError:
    if status == 401:
        return AuthenticationError(
            f"{'Write denied: ' if method == 'PUT' else ''}"
            "API token authentication/authorization failed (HTTP 401). "
            "Check token validity and collection permissions, then Reconnect."
        )
    if status == 403:
        if method == "PUT":
            return PermissionDeniedError(
                "Write denied: the API token lacks permission to update this inventory (HTTP 403). "
                "Grant its account Edit elements access to the inventory collection, then rescan."
            )
        return PermissionDeniedError(
            "Read denied: the API token lacks permission to access this resource (HTTP 403). "
            "Check its collection access and detail permissions."
        )
    if 300 <= status < 400:
        return ApiError("Redirect refused; configure the canonical HTTPS installation URL.")
    return ApiError(f"HTTP request failed (status {status}).")


class Client:
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_PAGES = 10000
    MAX_RECORDS = 100000
    MAX_COLLECTIONS = 1000
    PAGE_SIZE = 100

    def __init__(self, base_url: str, token: str, timeout: float = 15):
        try:
            url = parse.urlsplit(base_url)
            valid = (
                url.scheme == "https" and url.hostname and url.port != 0
                and url.username is None and url.password is None
                and not url.query and not url.fragment
                and "?" not in base_url and "#" not in base_url
                and not re.search(r"[\s\\\x00-\x1f\x7f]", base_url)
                and not re.search(r"%2f|%5c|%2e", url.path, re.I)
                and all(part not in (".", "..") for part in url.path.split("/"))
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ApiError("Base URL must be HTTPS without credentials, query or fragment.")
        if not isinstance(token, str) or not token or re.search(r"[\s\x00-\x1f\x7f]", token):
            raise AuthenticationError("A valid API token is required.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ApiError("Timeout must be finite and positive.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.current_user_id = None
        self._token = token
        self._opener = request.build_opener(
            _NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context())
        )

    def _http(self, method, path, params=None, body=None):
        url = f"{self.base_url}/api/v1/{path}"
        if params:
            url += "?" + parse.urlencode(params)
        payload = None
        if body is not None:
            try:
                payload = json.dumps(body, allow_nan=False, ensure_ascii=True).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise ApiError("Request data is not valid JSON.") from None
            if len(payload) > self.MAX_RESPONSE_BYTES:
                raise ApiError("Request exceeds the size limit.")
        req = request.Request(url, data=payload, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise _http_error(status, method)
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                headers = response.headers
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _http_error(status, method) from None
        except (error.URLError, OSError, TimeoutError, ValueError, HTTPException):
            raise ApiError("API connection failed or timed out; check connection and HTTPS configuration.") from None
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise ApiError("API response exceeds the size limit.")
        if status == 204 and not raw:
            return None, headers
        try:
            result = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
            )
        except (UnicodeError, ValueError, RecursionError):
            raise ApiError("API returned invalid JSON.") from None
        if type(result) is dict and (
            ("error" in result and result["error"] not in (None, {}, [], ""))
            or ("errors" in result and result["errors"] not in (None, {}, [], ""))
            or result.get("success") is False
        ):
            raise ApiError("API reported an error.")
        return result, headers

    def _get(self, path, params=None):
        return self._http("GET", path, params)[0]

    def authenticate(self) -> dict:
        self.current_user_id = None
        user = _object(_object(self._get("users/current"), "current user").get("user"), "current user")
        if not _id(user.get("id")) or user.get("type") != "Person":
            raise AuthenticationError("The station token must belong to a Person.")
        self.current_user_id = user["id"]
        return {"id": user["id"], "type": "Person"}

    def resolve(self, code: str) -> Ref:
        # CodeCreator.uuid_to_digit emits exactly 40 zero-padded decimal digits.
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{40}", code):
            raise ApiError("Scan the complete 40-digit chemical QR code, not a short barcode.")
        if not 0 < int(code) < 2**128:
            raise ApiError("Invalid full chemical QR code.")
        log = _object(_object(self._get("code_logs/generic", {"code": code}), "code").get("code_log"), "code")
        return Ref(log.get("source"), log.get("source_id"))

    def describe(self, ref: Ref) -> str:
        record = self._detail(ref)
        for field in ("name", "short_label"):
            name = record.get(field)
            if isinstance(name, str) and name.strip():
                # Never reflect a credential accidentally embedded in a server label.
                return name.replace(self._token, "[redacted]")
        raise ApiError("Element has no readable name or short label.")

    def _detail(self, ref):
        record = _object(_object(self._get(f"{ref.kind}s/{ref.id}"), "element").get(ref.kind), "element")
        if type(record.get("id")) is not int or record["id"] != ref.id:
            raise ApiError("Element detail identity does not match.")
        if record.get("type") != ref.kind:
            raise ApiError("Element detail type does not match.")
        if record.get("is_restricted") is True:
            raise ApiError("Element detail access is restricted.")
        return record

    def _inventory(self, ref, allow_absent=False):
        obj = _object(self._get("chemicals", {ref.parent_key: ref.id}), "chemical inventory")
        # ChemicalAPI returns Chemical.new (all these fields null) for no row.
        if all(key in obj and obj[key] is None for key in (
            "id", "sample_id", "sequence_based_macromolecule_sample_id", "chemical_data"
        )):
            if allow_absent:
                return None
            raise ApiError("Element has no persisted chemical inventory.")
        other = next(f"{kind}_id" for kind in KINDS if kind != ref.kind)
        if type(obj.get(ref.parent_key)) is not int or obj[ref.parent_key] != ref.id or obj.get(other) is not None:
            raise ApiError("Chemical inventory parent identity does not match.")
        return Inventory(ref, obj.get("id"), _data(obj.get("chemical_data")))

    def inventory(self, ref: Ref) -> Inventory:
        return self._inventory(ref)

    def update_inventory(self, latest: Inventory, data: list[dict]) -> Inventory:
        _data(data)
        # Freeze the desired JSON value before sending, including nested objects.
        try:
            desired = json.loads(json.dumps(data, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise ApiError("Inventory data is not valid JSON.") from None
        try:
            self._http("PUT", "chemicals", body={
                latest.ref.parent_key: latest.ref.id, "chemical_data": desired,
            })
        except (AuthenticationError, PermissionDeniedError):
            raise
        except ApiError:
            # A failed/timeout response does not establish whether the PUT committed.
            pass
        try:
            actual = self.inventory(latest.ref)
        except ApiError:
            raise UnverifiedWrite("Write outcome is unknown: read-back failed. Do not replay automatically.") from None
        if actual.id != latest.id or actual.ref != latest.ref or (
            json.dumps(actual.data, sort_keys=True, allow_nan=False)
            != json.dumps(desired, sort_keys=True, allow_nan=False)
        ):
            raise UnverifiedWrite("Write was not verified: inventory identity or complete data differs. Do not replay automatically.")
        return actual

    def _check_collection(self, collection_id, owned):
        collection = _object(
            _object(self._get(f"collections/{collection_id}"), "collection").get("collection"), "collection"
        )
        if type(collection.get("id")) is not int or collection["id"] != collection_id:
            raise ApiError("Collection identity does not match.")
        # SharedCollectionEntity uniquely exposes permission_level; own collections
        # have full owner access. Never infer detail access from permission_level.
        if owned:
            if "permission_level" in collection:
                raise ApiError("Collection ownership changed during audit.")
            return
        if type(collection.get("permission_level")) is not int:
            raise ApiError("Malformed shared collection.")
        shares = _object(
            self._get("collection_shares/for_me", {"collection_id": collection_id}), "collection shares"
        ).get("collection_shares")
        if not isinstance(shares, list) or not shares:
            raise ApiError("Cannot verify full collection detail access.")
        levels = ("sample_detail_level", "sequencebasedmacromoleculesample_detail_level")
        maxima = {key: 0 for key in levels}
        for share in shares:
            if type(share) is not dict or type(share.get("collection_id")) is not int or share["collection_id"] != collection_id:
                raise ApiError("Malformed contributing collection share.")
            for key in levels:
                value = share.get(key)
                if type(value) is not int or not 0 <= value <= 10:
                    raise ApiError("Malformed collection detail level.")
                maxima[key] = max(maxima[key], value)
        if any(value < 10 for value in maxima.values()):
            raise ApiError("Audit requires full sample and SBMM collection detail access.")

    def _list(self, kind, collection_id):
        total = None
        seen = set()
        page = 1
        while page <= self.MAX_PAGES:
            obj, headers = self._http("GET", f"{kind}s", {
                "collection_id": collection_id, "page": page, "per_page": self.PAGE_SIZE,
            })
            obj = _object(obj, "collection list")
            rows = obj.get(f"{kind}s")
            if not isinstance(rows, list):
                raise ApiError("Malformed collection element list.")
            numbers = []
            for key in ("X-Total", "X-Page", "X-Per-Page"):
                value = headers.get(key)
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}", value):
                    raise ApiError("Missing or malformed pagination metadata; audit is incomplete.")
                numbers.append(int(value))
            count, actual_page, per_page = numbers
            if count > self.MAX_RECORDS or actual_page != page or per_page != self.PAGE_SIZE:
                raise ApiError("Pagination changed or exceeded audit bounds.")
            if total is None:
                total = count
            if total != count or ("samples_count" in obj and (
                type(obj["samples_count"]) is not int or obj["samples_count"] != total
            )):
                raise ApiError("Collection changed during pagination; restart the audit.")
            expected = min(self.PAGE_SIZE, max(0, total - (page - 1) * self.PAGE_SIZE))
            if len(rows) != expected:
                raise ApiError("Incomplete collection page.")
            refs = []
            for row in rows:
                row = _object(row, "list element")
                ref = Ref(kind, row.get("id"))
                if row.get("type") != kind or ref in seen:
                    raise ApiError("Duplicate or mismatched collection element.")
                seen.add(ref)
                refs.append(ref)
            yield from refs
            if len(seen) == total:
                return
            page += 1
        raise ApiError("Collection pagination exceeded audit bounds.")

    def inventories(self, collection_ids: list[int]) -> list[Inventory]:
        if not isinstance(collection_ids, list) or not collection_ids or (
            len(collection_ids) > self.MAX_COLLECTIONS or any(not _id(cid) for cid in collection_ids)
        ):
            raise ApiError("Audit requires an explicit, bounded list of positive collection IDs.")
        index = _object(self._get("collections"), "collections")
        ownership = {}
        for key, owned in (("own", True), ("shared_with_me", False)):
            rows = index.get(key)
            if not isinstance(rows, list):
                raise ApiError("Malformed accessible collection index.")
            for row in rows:
                if type(row) is not dict or not _id(row.get("id")) or row["id"] in ownership:
                    raise ApiError("Malformed or duplicate accessible collection.")
                ownership[row["id"]] = owned
        if any(cid not in ownership for cid in collection_ids):
            raise ApiError("An audit collection is not accessible to the station.")
        result = []
        seen = set()
        for cid in dict.fromkeys(collection_ids):
            self._check_collection(cid, ownership[cid])
            for kind in KINDS:
                for ref in self._list(kind, cid):
                    if ref in seen:
                        continue
                    seen.add(ref)
                    if len(seen) > self.MAX_RECORDS:
                        raise ApiError("Audit exceeded the total element limit.")
                    self._detail(ref)
                    inventory = self._inventory(ref, allow_absent=True)
                    if inventory is not None:
                        result.append(inventory)
        return result
