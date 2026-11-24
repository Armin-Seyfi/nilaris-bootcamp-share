import hashlib
import json
import socket

import pytest

from nilaris.rfq_request import inspect_request


def synthetic_source():
    return {
        "filename": "fixture-internal.pdf", "sha256": "e" * 64,
        "attribute": "Pressure-maximum", "unit": "kPa",
        "comparison_attribute": "Pressure-normal", "section": "Pump ratings",
        "passage": "1\tPressure-maximum\t9876\tkPa",
        "qualifier_comparison": "2\tPressure-normal\t4321\tkPa",
        "unrelated_passage": "Shipment identifier DEMO-111",
        "page_text": "Pump ratings\n1\tPressure-maximum\t9876\tkPa\n"
                     "2\tPressure-normal\t4321\tkPa\nShipment identifier DEMO-111",
    }


def test_inspection_captures_complete_minimized_http_exchange_without_network(monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("inspection attempted network access")
    monkeypatch.setattr(socket, "getaddrinfo", forbid_network)
    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)

    source = synthetic_source()
    inspected = inspect_request(source)
    request = inspected["request"]
    headers = {name.lower(): value for name, value in request["headers"]}
    body = request["body_utf8"].encode("utf-8")
    assert int(headers["content-length"]) == len(body) == request["body_bytes"]
    assert request["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer synthetic-offline"
    assert (request["method"], request["url"]) == ("POST", "https://api.openai.com/v1/responses")
    wire = json.loads(body)
    assert wire["store"] is False
    assert "qualifier" in wire["instructions"]
    assert '"name" for the proposed Attribute name' in wire["instructions"]
    assert '"selected_label" for the exact supplied label you chose' in wire["instructions"]
    assert json.loads(wire["input"]) == inspected["comparison"]["retained"] == {
        "attributes": [{"label": "Pressure-maximum", "unit": "kPa"},
                       {"label": "Pressure-normal", "unit": "kPa"}],
    }
    for token in ("fixture-internal.pdf", "DEMO-111", "9876", "4321", source["sha256"]):
        assert token not in json.dumps(request, ensure_ascii=False)
    assert set(inspected) == {"request", "comparison", "transport"}
    assert inspected["comparison"]["omitted_passage"] == "Shipment identifier DEMO-111"
    assert inspected["transport"]["captured_requests"] == 1
    assert inspected["transport"]["external_model_requests"] == 0


def test_captured_request_normalizes_both_split_cubic_metre_units():
    source = synthetic_source()
    for field in ("unit", "passage", "qualifier_comparison", "page_text"):
        source[field] = source[field].replace("kPa", "m\n3/h")

    inspected = inspect_request(source)
    body = json.loads(inspected["request"]["body_utf8"])
    fields = json.loads(body["input"])
    assert [item["unit"] for item in fields["attributes"]] == ["m³/h", "m³/h"]
    assert inspected["comparison"]["source_rows"] == [source["passage"], source["qualifier_comparison"]]


def test_discovery_request_is_identical_when_source_roles_are_swapped():
    source = synthetic_source()
    reversed_source = dict(source, attribute=source["comparison_attribute"],
                           comparison_attribute=source["attribute"],
                           passage=source["qualifier_comparison"],
                           qualifier_comparison=source["passage"])
    assert inspect_request(source)["request"] == inspect_request(reversed_source)["request"]


def test_unsubstantiated_unit_or_qualifier_refuses_capture():
    for field, value in (("unit", "bar"), ("attribute", "Pressure"),
                         ("comparison_attribute", "Pressure-maximum")):
        source = dict(synthetic_source(), **{field: value})
        with pytest.raises(ValueError, match="source row"):
            inspect_request(source)
