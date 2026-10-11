import pytest

from swctl import bitmap


def test_decode_all_28_ports():
    assert bitmap.decode("FF FF FF F0 00 00 00 00") == set(range(1, 29))


def test_decode_port_28_only():
    assert bitmap.decode("00 00 00 10 00 00 00 00") == {28}


def test_encode_round_trip():
    ports = {1, 2, 9, 24, 28}
    assert bitmap.decode(bitmap.encode(ports, 8)) == ports
    assert bitmap.encode({1}, 8) == "80 00 00 00 00 00 00 00"


def test_encode_empty():
    assert bitmap.encode(set(), 8) == "00 00 00 00 00 00 00 00"


def test_encode_rejects_port_beyond_length():
    with pytest.raises(ValueError):
        bitmap.encode({65}, 8)
