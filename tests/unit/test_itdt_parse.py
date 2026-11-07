"""
File:   test_itdt_parse.py
Brief:  Parser unit tests on real captured ITDT output fixtures.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

import pytest

from tapevaultui.infrastructure.itdt_parse import (
    ItDtParseError,
    is_medium_not_present,
    is_medium_removal_prevented,
    is_open_failure,
    parse_device_statistics,
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


def test_parse_device_statistics_fixture() -> None:
    """devicestatistics two-column table parses into a normalized map."""
    stats = parse_device_statistics(load_fixture("fixture_devstat.txt"))
    assert stats["lifetime_media_loads"] == "103"
    assert stats["lifetime_cleaning_op"] == "0"
    assert stats["lifetime_poh"] == "1850"
    assert stats["lt_meters_tape_processed"] == "298950"
    assert stats["mmh_since_last_clean"] == "16"
    assert stats["lifetime_power_cycles"] == "12"
    assert stats["hard_write_errors"] == "0"
    assert stats["hard_read_errors"] == "0"
    assert stats["ready_duty_cycle"] == "99"
    assert stats["drive_sn"] == "1013000284"
    assert stats["drive_mfg_sn"] == "1013000284"
    # Footer lines must not leak into the map.
    assert not any("exit" in key for key in stats)


def test_parse_device_statistics_missing_table() -> None:
    """Output without a statistics table raises ItDtParseError."""
    with pytest.raises(ItDtParseError, match="Device Statistics"):
        parse_device_statistics("Operation not supported, errno 1\n")


def test_is_medium_not_present_sense_3a() -> None:
    """Empty-drive qrypos failure with 3A/00 sense is medium-not-present."""
    assert is_medium_not_present(load_fixture("fixture_qrypos_no_medium.txt"))


def test_is_medium_not_present_initializing_sense() -> None:
    """The 04/02 (NOT READY, initializing) variant is also classified."""
    assert is_medium_not_present(
        load_fixture("fixture_qrypos_no_medium_initializing.txt")
    )


def test_is_medium_not_present_text_form() -> None:
    """An explicit textual report is recognized without a sense dump."""
    assert is_medium_not_present("Operation FAILED: MEDIUM NOT PRESENT\n")


def test_is_medium_not_present_healthy_output() -> None:
    """Healthy qrypos/vpd output is not classified as empty drive."""
    assert not is_medium_not_present(load_fixture("fixture_qrypos.txt"))
    assert not is_medium_not_present(load_fixture("fixture_vpd.txt"))


def test_is_medium_not_present_open_failure() -> None:
    """A plain open failure (no sense data) is not medium-not-present."""
    assert not is_medium_not_present(
        "Open failed: The system cannot find the file specified\n"
    )


def test_is_medium_removal_prevented_sense_5302() -> None:
    """An unload rejected with 53/02 sense is a locked cartridge."""
    assert is_medium_removal_prevented(load_fixture("fixture_unload_prevented.txt"))


def test_is_medium_removal_prevented_text_form() -> None:
    """An explicit textual report is recognized without a sense dump."""
    assert is_medium_removal_prevented("Operation FAILED: MEDIUM REMOVAL PREVENTED\n")


def test_is_medium_removal_prevented_negative_cases() -> None:
    """Other failures (no medium, healthy output) are not 'prevented'."""
    no_medium = load_fixture("fixture_qrypos_no_medium.txt")
    assert not is_medium_removal_prevented(no_medium)
    assert not is_medium_removal_prevented(load_fixture("fixture_qrypos.txt"))
    assert not is_medium_removal_prevented(load_fixture("fixture_vpd.txt"))
