"""Stdlib tests mirroring the repository's Ruby API/entity response shapes."""

import io
import json
import ssl
import unittest
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import patch
from urllib import error, parse, request

from tools.inventory_scanner.api import (
    ApiError, AuthenticationError, Client, Inventory, PermissionDeniedError, Ref, UnverifiedWrite,
)
from tools.inventory_scanner.workflow import Config, Pending, Service, Target


TOKEN = "chemtoken_station_secret"
SAMPLE = Ref("sample", 7)
SBMM = Ref("sequence_based_macromolecule_sample", 8)
CODE = "0123456789012345678901234567890123456789"


def chemical(ref=SAMPLE, data=None, identity=91):
    return {
        "id": identity, "sample_id": None, "sequence_based_macromolecule_sample_id": None,
        ref.parent_key: ref.id, "chemical_data": data if data is not None else [{"location": "A"}],
    }


def absent():
    return {
        "id": None, "sample_id": None, "sequence_based_macromolecule_sample_id": None,
        "chemical_data": None, "cas": None,
    }


def detail(ref):
    return {ref.kind: {"id": ref.id, "type": ref.kind, "name": "Human name", "short_label": "AB-1"}}


def page_headers(total, page=1, size=100):
    return {"X-Total": str(total), "X-Page": str(page), "X-Per-Page": str(size)}


class Response:
    def __init__(self, obj=None, headers=None, status=200, raw=None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.raw = raw if raw is not None else json.dumps(obj).encode()
        self.read_size = None

    def read(self, size):
        self.read_size = size
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("https://eln.example/lab", TOKEN)
        self.calls = []
        self.replies = []
        self.client._opener.open = self.open

    def open(self, req, timeout):
        self.calls.append(req)
        self.assertEqual(timeout, 15)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def queue(self, obj=None, **kwargs):
        self.replies.append(Response(obj, **kwargs))

    def assert_safe(self, exc):
        self.assertNotIn(TOKEN, str(exc))
        self.assertNotIn("private body", str(exc))

    def auth(self, user_type="Person"):
        self.queue({"user": {"id": 3, "type": user_type}})

    def collection(self, cid=1, shared=False, levels=(10, 10)):
        obj = {"id": cid, "label": "Cabinet"}
        if shared:
            obj["permission_level"] = 1
        self.queue({"collection": obj})
        if shared:
            self.queue({"collection_shares": [{
                "collection_id": cid, "sample_detail_level": levels[0],
                "sequencebasedmacromoleculesample_detail_level": levels[1],
            }]})

    def index(self, own=(1,), shared=()):
        self.queue({
            "own": [{"id": cid} for cid in own],
            "shared_with_me": [{"id": cid} for cid in shared],
        })

    def listing(self, kind, ids=(), total=None, page=1, **kwargs):
        obj = {f"{kind}s": [{"id": identity, "type": kind} for identity in ids]}
        count = len(ids) if total is None else total
        if kind == "sample":
            obj["samples_count"] = count
        self.queue(obj, headers=page_headers(count, page), **kwargs)

    def test_public_values(self):
        self.assertEqual(SAMPLE.parent_key, "sample_id")
        self.assertEqual(SBMM.parent_key, "sequence_based_macromolecule_sample_id")
        self.assertEqual(SBMM.key, "sequence_based_macromolecule_sample:8")
        inventory = Inventory(SAMPLE, 9, [{"a": 1}, {"b": 2}])
        self.assertEqual(inventory.first, {"a": 1})
        with self.assertRaises(AttributeError):
            SAMPLE.id = 1
        for value in (True, False, 0, -1, "7", None):
            with self.subTest(value=value), self.assertRaises(ApiError):
                Ref("sample", value)
        with self.assertRaises(ApiError):
            Ref("reaction", 1)

    def test_base_url_and_timeout_validation(self):
        for base in ("http://eln.example", "https://u:p@eln.example", "https://@eln.example", "https://eln.example?q=1",
                     "https://eln.example#", "https://eln.example?", "https://eln.example/#abc",
                     "https://eln.example/../lab", "https://eln.example/%2fattack", "https://eln.example\\evil",
                     "https://eln.example:bad", "https://eln.example/\n", None):
            with self.subTest(base=base), self.assertRaises(ApiError) as exc:
                Client(base, TOKEN)
            self.assert_safe(exc.exception)
        for timeout in (True, 0, -1, float("inf"), float("nan"), "15"):
            with self.subTest(timeout=timeout), self.assertRaises(ApiError):
                Client("https://eln.example", TOKEN, timeout)
        for token in ("", "x\r\nHeader: evil"):
            with self.assertRaises(AuthenticationError):
                Client("https://eln.example", token)

    def test_https_context_and_redirect_handler(self):
        opener = request.build_opener
        with patch("tools.inventory_scanner.api.request.build_opener", wraps=opener) as build:
            Client("https://eln.example", TOKEN)
        handlers = build.call_args.args
        https = next(item for item in handlers if isinstance(item, request.HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        redirect = next(item for item in handlers if isinstance(item, request.HTTPRedirectHandler))
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_authenticate_never_requests_groups_or_borrower_names(self):
        self.auth()
        self.assertEqual(self.client.authenticate(), {"id": 3, "type": "Person"})
        self.assertEqual(self.client.current_user_id, 3)
        self.assertEqual([req.full_url for req in self.calls], [
            "https://eln.example/lab/api/v1/users/current",
        ])
        self.assertEqual(self.calls[0].get_header("Authorization"), f"Bearer {TOKEN}")

    def test_static_destination_assignment_uses_only_inventory_endpoints(self):
        original = [{"current_building": "Old", "borrowed_by": "Previous", "amount": {"value": 10}},
                    {"preserve": True}]
        for kind in ("person", "location"):
            with self.subTest(kind=kind):
                target = Target(kind, "Printed group", "B1", borrower="Printed borrower")
                desired = [{**original[0], **target.assignment}, original[1]]
                self.queue(chemical(data=original))
                self.queue(True)
                self.queue(chemical(data=desired))
                pending = Pending(Inventory(SAMPLE, 91, original), "Bottle", CODE)
                before = len(self.calls)
                saved, _ = Service(self.client, Config("https://eln.example")).assign(pending, target)
                calls = self.calls[before:]
                self.assertEqual([req.method for req in calls], ["GET", "PUT", "GET"])
                self.assertTrue(all(parse.urlsplit(req.full_url).path.endswith("/chemicals") for req in calls))
                self.assertEqual(json.loads(calls[1].data)["chemical_data"], desired)
                self.assertEqual(saved.data, desired)

    def test_person_required(self):
        for user in ({"id": True, "type": "Person"}, {"id": 3, "type": "Group"}, {"id": 3}):
            self.queue({"user": user})
            with self.assertRaises(AuthenticationError):
                self.client.authenticate()

    def test_resolve_both_sources_and_encoding(self):
        for ref in (SAMPLE, SBMM):
            self.queue({"code_log": {"source": ref.kind, "source_id": ref.id, "value": CODE}})
            self.assertEqual(self.client.resolve(CODE), ref)
            self.assertEqual(parse.parse_qs(parse.urlsplit(self.calls[-1].full_url).query), {"code": [CODE]})

    def test_full_code_required_no_network_for_short_or_invalid(self):
        for code in ("123456", "12345678", CODE[:-1], CODE + "0", " " + CODE, CODE + "\n",
                     "https://eln.example/" + CODE, "１" * 40, "0" * 40, "9" * 40, None):
            with self.subTest(code=code), self.assertRaises(ApiError):
                self.client.resolve(code)
        self.assertFalse(self.calls)
        self.queue({"code_log": {"source": "reaction", "source_id": 7}})
        with self.assertRaises(ApiError):
            self.client.resolve(CODE)

    def test_describe_actual_detail_roots(self):
        for ref in (SAMPLE, SBMM):
            self.queue(detail(ref))
            self.assertEqual(self.client.describe(ref), "Human name")
            self.assertTrue(self.calls[-1].full_url.endswith(f"/{ref.kind}s/{ref.id}"))
        self.queue({"sample": {"id": 7, "type": "sample", "name": "", "short_label": "AB-1"}})
        self.assertEqual(self.client.describe(SAMPLE), "AB-1")
        for obj in ({"sample": {"id": 9, "type": "sample", "name": "Wrong"}},
                    {"sample": {"id": True, "type": "sample"}},
                    {"sample": {"id": 7, "type": "sample", "is_restricted": True}},
                    {"sample": {"id": 7, "type": "sample"}}, {}):
            self.queue(obj)
            with self.assertRaises(ApiError):
                self.client.describe(SAMPLE)

    def test_inventory_raw_model_shapes_both_types(self):
        for ref in (SAMPLE, SBMM):
            self.queue(chemical(ref))
            result = self.client.inventory(ref)
            self.assertEqual(result, Inventory(ref, 91, [{"location": "A"}]))
            self.assertEqual(parse.parse_qs(parse.urlsplit(self.calls[-1].full_url).query), {
                ref.parent_key: [str(ref.id)]
            })

    def test_inventory_malformed_not_absent(self):
        cases = [absent(), {}, [], {"chemical": chemical()}]
        for field, value in (("id", True), ("id", None), ("sample_id", True),
                             ("sample_id", 8), ("sequence_based_macromolecule_sample_id", 9),
                             ("chemical_data", []), ("chemical_data", None),
                             ("chemical_data", ["private body"]), ("chemical_data", {})):
            obj = chemical()
            obj[field] = value
            cases.append(obj)
        for obj in cases:
            self.queue(obj)
            with self.subTest(obj=obj), self.assertRaises(ApiError) as exc:
                self.client.inventory(SAMPLE)
            self.assert_safe(exc.exception)

    def test_strict_json_error_bodies_and_size(self):
        for raw in (b'{"error":"private body"}', b'{"errors":["private body"]}',
                    b'{"success":false}', b'{"id":1,"id":2}', b'{"x":NaN}', b'{"x":1e999}',
                    b'not json', b'{"x":Infinity}', b'\xff', b'{"id":'):
            self.queue(raw=raw)
            with self.subTest(raw=raw), self.assertRaises(ApiError) as exc:
                self.client.inventory(SAMPLE)
            self.assert_safe(exc.exception)
        self.client.MAX_RESPONSE_BYTES = 20
        response = Response(raw=b"x" * 21)
        self.replies.append(response)
        with self.assertRaises(ApiError):
            self.client.inventory(SAMPLE)
        self.assertEqual(response.read_size, 21)

    def test_http_and_network_errors_safe_and_no_redirect(self):
        for code in (301, 302, 303, 307, 308, 400, 401, 403, 404, 500):
            self.replies.append(error.HTTPError(
                "https://eln.example/" + TOKEN, code, TOKEN + " private body", {},
                io.BytesIO((TOKEN + " private body").encode()),
            ))
            with self.subTest(code=code), self.assertRaises(ApiError) as exc:
                self.client.inventory(SAMPLE)
            self.assert_safe(exc.exception)
            if code == 401:
                self.assertIsInstance(exc.exception, AuthenticationError)
            if code == 403:
                self.assertIsInstance(exc.exception, PermissionDeniedError)
        for problem in (error.URLError(TOKEN), TimeoutError(TOKEN), OSError(TOKEN), IncompleteRead(b"private body")):
            self.replies.append(problem)
            with self.assertRaises(ApiError) as exc:
                self.client.inventory(SAMPLE)
            self.assert_safe(exc.exception)
        self.assertEqual(len(self.calls), 14)

    def test_update_whole_array_verified_after_success_and_uncertain_put(self):
        desired = [{"location": "B", "nested": {"unchanged": [1, 2]}}, {"vendor": "preserve"}]
        for reply in (Response(True), Response(raw=b"", status=204), TimeoutError(TOKEN),
                      error.HTTPError("", 500, TOKEN, {}, io.BytesIO(b"private body")),
                      Response({"error": "private body"}), Response(raw=b"invalid")):
            with self.subTest(reply=reply):
                self.replies.append(reply)
                self.queue(chemical(SBMM, desired))
                before = len(self.calls)
                actual = self.client.update_inventory(Inventory(SBMM, 91, [{"old": True}]), desired)
                self.assertEqual(actual.data, desired)
                calls = self.calls[before:]
                self.assertEqual([req.method for req in calls], ["PUT", "GET"])
                self.assertEqual(json.loads(calls[0].data), {
                    "sequence_based_macromolecule_sample_id": 8, "chemical_data": desired,
                })

    def test_update_not_verified_on_partial_or_wrong_identity(self):
        desired = [{"location": "B"}, {"other": True}]
        for obj in (chemical(data=desired[:1]), chemical(data=desired, identity=92),
                    chemical(SBMM, desired), absent(), {"error": "private body"},
                    chemical(data=[{"location": "B"}, {"other": 1}])):
            self.queue(True)
            self.queue(obj)
            with self.subTest(obj=obj), self.assertRaises(UnverifiedWrite) as exc:
                self.client.update_inventory(Inventory(SAMPLE, 91, [{}]), desired)
            self.assert_safe(exc.exception)
        self.assertEqual([req.method for req in self.calls].count("PUT"), 6)

    def test_update_readback_failure_never_replayed(self):
        self.replies.extend([TimeoutError(TOKEN), error.URLError(TOKEN)])
        with self.assertRaises(UnverifiedWrite) as exc:
            self.client.update_inventory(Inventory(SAMPLE, 91, [{}]), [{}])
        self.assert_safe(exc.exception)
        self.assertEqual([req.method for req in self.calls], ["PUT", "GET"])

    def test_forbidden_writes_report_token_permissions_without_readback_or_replay(self):
        for reply in (
            error.HTTPError("", 403, TOKEN, {}, io.BytesIO(b"private body")),
            Response(status=403, raw=b"private body"),
        ):
            with self.subTest(reply=reply):
                self.replies.append(reply)
                before = len(self.calls)
                with self.assertRaisesRegex(PermissionDeniedError, "Write denied.*API token.*permission") as exc:
                    self.client.update_inventory(Inventory(SAMPLE, 91, [{}]), [{"location": "B"}])
                self.assert_safe(exc.exception)
                self.assertEqual([req.method for req in self.calls[before:]], ["PUT"])
                self.assertFalse(self.replies)

    def test_unauthorized_write_reports_authentication_and_permission_checks(self):
        self.replies.append(error.HTTPError("", 401, TOKEN, {}, io.BytesIO(b"private body")))
        with self.assertRaisesRegex(AuthenticationError, "Write denied.*collection permissions") as exc:
            self.client.update_inventory(Inventory(SAMPLE, 91, [{}]), [{"location": "B"}])
        self.assert_safe(exc.exception)
        self.assertEqual([req.method for req in self.calls], ["PUT"])

    def test_forbidden_readback_after_put_is_still_an_uncertain_write(self):
        self.queue(True)
        self.replies.append(error.HTTPError("", 403, TOKEN, {}, io.BytesIO(b"private body")))
        with self.assertRaises(UnverifiedWrite):
            self.client.update_inventory(Inventory(SAMPLE, 91, [{}]), [{"location": "B"}])
        self.assertEqual([req.method for req in self.calls], ["PUT", "GET"])

    def test_update_invalid_json_no_write(self):
        for data in ([], [1], [{"bad": float("nan")}], [{"bad": object()}]):
            with self.assertRaises(ApiError):
                self.client.update_inventory(Inventory(SAMPLE, 91, [{}]), data)
        self.assertFalse(self.calls)

    def test_both_lists_paginated_dedup_across_collections_and_absent(self):
        self.client.PAGE_SIZE = 2
        self.index(own=(1, 2))
        self.collection(1)
        self.queue({"samples": [{"id": 7, "type": "sample"}, {"id": 9, "type": "sample"}],
                    "samples_count": 3}, headers=page_headers(3, 1, 2))
        self.queue(detail(SAMPLE))
        self.queue(chemical())
        self.queue(detail(Ref("sample", 9)))
        self.queue(absent())
        self.queue({"samples": [{"id": 10, "type": "sample"}], "samples_count": 3},
                   headers=page_headers(3, 2, 2))
        self.queue(detail(Ref("sample", 10)))
        self.queue(chemical(Ref("sample", 10), identity=92))
        self.queue({"sequence_based_macromolecule_samples": [
            {"id": 8, "type": SBMM.kind}, {"id": 11, "type": SBMM.kind},
        ]}, headers=page_headers(3, 1, 2))
        self.queue(detail(SBMM))
        self.queue(chemical(SBMM, identity=93))
        self.queue(detail(Ref(SBMM.kind, 11)))
        self.queue(absent())
        self.queue({"sequence_based_macromolecule_samples": [{"id": 12, "type": SBMM.kind}]},
                   headers=page_headers(3, 2, 2))
        self.queue(detail(Ref(SBMM.kind, 12)))
        self.queue(chemical(Ref(SBMM.kind, 12), identity=94))
        self.collection(2)
        self.queue({"samples": [{"id": 7, "type": "sample"}], "samples_count": 1},
                   headers=page_headers(1, 1, 2))
        self.queue({"sequence_based_macromolecule_samples": [{"id": 8, "type": SBMM.kind}]},
                   headers=page_headers(1, 1, 2))
        result = self.client.inventories([1, 2, 1])
        self.assertEqual([item.id for item in result], [91, 92, 93, 94])
        self.assertFalse(self.replies)
        list_calls = [req for req in self.calls if parse.urlsplit(req.full_url).path.endswith(("samples",))]
        self.assertEqual(len(list_calls), 6)
        self.assertTrue(all("collection_id=" in req.full_url for req in list_calls))

    def test_shared_collection_full_detail_required(self):
        self.index(own=(), shared=(1,))
        self.collection(shared=True)
        self.listing("sample")
        self.listing(SBMM.kind)
        self.assertEqual(self.client.inventories([1]), [])
        self.assertIn("/collection_shares/for_me?", self.calls[2].full_url)
        for levels in ((9, 10), (10, 0), (True, 10)):
            self.index(own=(), shared=(1,))
            self.collection(shared=True, levels=levels)
            with self.subTest(levels=levels), self.assertRaises(ApiError):
                self.client.inventories([1])

    def test_shared_detail_max_across_contributing_shares(self):
        self.index(own=(), shared=(1,))
        self.queue({"collection": {"id": 1, "permission_level": 1}})
        self.queue({"collection_shares": [
            {"collection_id": 1, "sample_detail_level": 10, "sequencebasedmacromoleculesample_detail_level": 0},
            {"collection_id": 1, "sample_detail_level": 0, "sequencebasedmacromoleculesample_detail_level": 10},
        ]})
        self.listing("sample")
        self.listing(SBMM.kind)
        self.assertEqual(self.client.inventories([1]), [])

    def test_audit_scope_access_and_bad_inventory_fail_closed(self):
        for ids in ([], None, [True], [0], ["1"]):
            with self.subTest(ids=ids), self.assertRaises(ApiError):
                self.client.inventories(ids)
        self.assertFalse(self.calls)
        self.index(own=())
        with self.assertRaises(ApiError):
            self.client.inventories([1])
        self.index()
        self.queue({"collection": {"id": 2}})
        with self.assertRaises(ApiError):
            self.client.inventories([1])
        self.index()
        self.collection()
        self.listing("sample", [7])
        self.queue(detail(SAMPLE))
        self.queue({"id": None})
        with self.assertRaises(ApiError):
            self.client.inventories([1])

    def test_bad_pagination_detected_for_both_types(self):
        for kind in ("sample", SBMM.kind):
            cases = [
                ([], {}, 0),
                ([], page_headers(1), 1),
                ([{"id": 7, "type": kind}], page_headers(1, 2), 1),
                ([{"id": 7, "type": kind}], page_headers(1, size=50), 1),
                ([{"id": True, "type": kind}], page_headers(1), 1),
                ([{"id": 7, "type": "reaction"}], page_headers(1), 1),
                ([{"id": 7, "type": kind}] * 2, page_headers(2), 2),
                ([], page_headers(100001), 100001),
            ]
            for rows, headers, count in cases:
                obj = {kind + "s": rows}
                if kind == "sample":
                    obj["samples_count"] = count
                self.queue(obj, headers=headers)
                with self.subTest(kind=kind, headers=headers, rows=rows), self.assertRaises(ApiError):
                    list(self.client._list(kind, 1))

    def test_repeated_reset_page_and_changing_totals_fail(self):
        self.client.PAGE_SIZE = 1
        for kind in ("sample", SBMM.kind):
            for total, actual_page, second_id in ((2, 1, 7), (3, 2, 8), (2, 2, 7)):
                self.queue({kind + "s": [{"id": 7, "type": kind}]}, headers=page_headers(2, 1, 1))
                self.queue({kind + "s": [{"id": second_id, "type": kind}]},
                           headers=page_headers(total, actual_page, 1))
                with self.assertRaises(ApiError):
                    list(self.client._list(kind, 1))

    def test_audit_page_bound(self):
        self.client.PAGE_SIZE = 1
        self.client.MAX_PAGES = 1
        self.queue({"samples": [{"id": 7, "type": "sample"}], "samples_count": 2},
                   headers=page_headers(2, 1, 1))
        with self.assertRaises(ApiError):
            list(self.client._list("sample", 1))


if __name__ == "__main__":
    unittest.main()
