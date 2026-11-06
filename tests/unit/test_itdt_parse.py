"""
File:   test_itdt_parse.py
Brief:  Parser unit tests on real captured ITDT output fixtures.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

import pytest

from tapevaultui.infrastructure.itdt_parse import (
    ItDtParseError,
    is_open_failure,
    parse_devinfo,
    parse_getparms,
    parse_inquiry_json,
    parse_qrypos,
    parse_vpd,
)
from tests.conftest import load_fixture


def test_parse_vpd_fixture() -> None:
    """Real vpd output parses into IBM ULTRIUM-TD3 93G0 identity."""
    info = parse_vpd(load_fixture("fixture_vpd.txt"))
    assert info.manufacturer == "IBM"
    assert info.product_id == "ULTRIUM-TD3"
    assert info.revision == "93G0"


def test_parse_vpd_missing_fields() -> None:
    """Unrecognized vpd output raises ItDtParseError."""
    with pytest.raises(ItDtParseError):
        parse_vpd("some unexpected text\n")


def test_parse_qrypos_fixture() -> None:
    """Real qrypos output parses into logical/physical positions at BOP."""
    position = parse_qrypos(load_fixture("fixture_qrypos.txt"))
    assert position.logical.partition == 0
    assert position.logical.block_id == 0
    assert position.logical.at_beginning_of_partition is True
    assert position.logical.at_leading_end_of_partition is False
    assert position.physical.partition == 0
    assert position.physical.block_id == 0


def test_parse_qrypos_nonzero_block() -> None:
    """A synthetic qrypos with a nonzero block id parses both id forms."""
    text = (
        "Querying tape position....\n"
        "\n"
        "Logical Tape position:\n"
        "\n"
        "    Partition Number ........... 0\n"
        "    Beginning of Partition ..... No\n"
        "    Leading End of Partition ... No\n"
        "    Current Block ID ........... 1234 0x000004D2\n"
        "\n"
        "Physical Tape position:\n"
        "\n"
        "    Partition Number ........... 0\n"
        "    Beginning of Partition ..... No\n"
        "    Leading End of Partition ... No\n"
        "    Current Block ID ........... 1234 0x000004D2\n"
        "\n"
        "Exit with code: 0\n"
    )
    position = parse_qrypos(text)
    assert position.logical.block_id == 1234
    assert position.logical.at_beginning_of_partition is False


def test_parse_getparms_fixture() -> None:
    """Real getparms output parses compression/block size/buffering."""
    parms = parse_getparms(load_fixture("fixture_getparms.txt"))
    assert parms.compression is True
    assert parms.block_size == 65536
    assert parms.buffered_mode is True


def test_parse_devinfo_fixture() -> None:
    """Real devinfo output parses device type and block size."""
    info = parse_devinfo(load_fixture("fixture_devinfo.txt"))
    assert info.devtype == "SCSI Tape"
    assert info.tapetype == "Streaming Tape Drive"
    assert info.block_size == 65536


def test_parse_inquiry_json_fixture() -> None:
    """Real inqj (JSON) output parses drive identity."""
    info = parse_inquiry_json(load_fixture("fixture_inqj.txt"))
    assert info.vendor == "IBM"
    assert info.product == "ULTRIUM-TD3"
    assert info.revision == "93G0"


def test_parse_inquiry_json_garbage() -> None:
    """No JSON in output raises ItDtParseError."""
    with pytest.raises(ItDtParseError):
        parse_inquiry_json("Open failed: something\n")


def test_is_open_failure() -> None:
    """Open-failure detection works on real failure output."""
    assert is_open_failure("Open failed: The system cannot find the file specified")
    assert not is_open_failure(load_fixture("fixture_vpd.txt"))


def test_runtimeinfo_unsupported_fixture_is_not_open_failure() -> None:
    """The unsupported runtimeinfo fixture is not an open failure."""
    assert not is_open_failure(load_fixture("fixture_runtimeinfo.txt"))
