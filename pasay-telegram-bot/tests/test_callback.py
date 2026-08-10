"""callback_data encode/decode: roundtrip, 64B limit, unknown version, nonce."""
import pytest

from pasay_bot.keyboards import (
    MAX_CALLBACK_BYTES,
    decode,
    encode,
    new_nonce,
    now_ts,
)

TS = 1700000000


def test_roundtrip_full():
    data = encode("cnf", "inc", "42", nonce="abc12345", ts=TS)
    assert decode(data) == {
        "action": "cnf",
        "entity": "inc",
        "ref": "42",
        "nonce": "abc12345",
        "ts": TS,
    }


def test_roundtrip_partial_trims_trailing_fields():
    data = encode("nav", "properties")
    assert data == "v1:nav:properties"
    parsed = decode(data)
    assert parsed["action"] == "nav"
    assert parsed["entity"] == "properties"
    assert parsed["ref"] == "" and parsed["nonce"] == "" and parsed["ts"] is None


def test_pagination_encoding():
    assert encode("pg", "ovd", "2") == "v1:pg:ovd:2"


def test_unknown_version_rejected():
    assert decode("v2:cnf:inc:42") is None


def test_garbage_rejected():
    for bad in ("", "garbage", "v1", "v1:", "v1:cnf:inc:abc",
                "v1:cnf:inc:42:xyz:123", "v1:cnf:inc:42:abc12345:12x"):
        assert decode(bad) is None, bad


def test_non_ascii_rejected():
    assert decode("v1:导航:properties") is None
    assert decode("v1:cnf:inc:42:中文:123") is None


def test_64_byte_limit_enforced():
    with pytest.raises(ValueError):
        encode("cnf", "inc", "9" * 60, nonce="abcdef01", ts=TS)


def test_normal_data_within_limit():
    data = encode("cnf", "inc", "123456", nonce="abcdef01", ts=TS)
    assert len(data.encode("ascii")) <= MAX_CALLBACK_BYTES


def test_nonce_roundtrip_and_uniqueness():
    n1, n2 = new_nonce(), new_nonce()
    assert len(n1) == 8 and len(n2) == 8
    assert decode(encode("cnf", "ren", nonce=n1, ts=TS))["nonce"] == n1
    assert encode("cnf", "ren", nonce=n1, ts=TS) != encode("cnf", "ren", nonce=n2, ts=TS)


def test_ts_is_optional_and_roundtrips():
    assert decode(encode("cnf", "ren", nonce="abcdef01"))["ts"] is None
    assert decode(encode("cnf", "ren", nonce="abcdef01", ts=now_ts()))["ts"] is not None
