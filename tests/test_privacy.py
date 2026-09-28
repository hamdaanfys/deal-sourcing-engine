"""Contact-information scrubbing: phone numbers in digits and in letters, without eating specs."""

import pytest

from dealsource.privacy import REMOVED, contains_contact_info, scrub_contact_info


@pytest.mark.parametrize(
    "phone",
    [
        "478-555-0101",
        "(478) 555-0101",
        "478.555.0101",
        "1-800-356-9377",
        "555-FIXX",
        "555-4FIX",
        "(478) 555-FIXX",
        "478-555-FIXX",
        "478.555.FIXX",
        "800-GOT-JUNK",
        "1-800-FLOWERS",
        "+1-800-FLOWERS",
    ],
)
def test_phone_numbers_in_digits_or_letters_are_scrubbed(phone):
    assert scrub_contact_info(f"Call {phone} today.") == f"Call {REMOVED} today."
    assert contains_contact_info(phone)


@pytest.mark.parametrize(
    "text",
    [
        "ISO 9001:2015 and AS9100D certified",
        "ISO-9001",
        "MIL-STD-810G",
        "5-AXIS CNC",
        "Model 300-XL",
        "SKU A100-BRKT",
        "100 PERCENT AMERICAN MADE",
        "100 THE BEST",
        "250-1000 employees",
        "Part 100-2000",
        "NAICS 332710, since 1962",
    ],
)
def test_specs_codes_and_ranges_are_left_alone(text):
    assert scrub_contact_info(text) == text
    assert not contains_contact_info(text)
