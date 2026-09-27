"""Versioned prompts. Bump PROMPT_VERSION on any change so cached LLM answers are not reused."""

from __future__ import annotations

PROMPT_VERSION = "extract-v2"

SYSTEM = """You extract facts about one company from text taken from its own website.
Rules:
- Use only the text provided. If something is not stated, use null, "unknown" or an empty list. Do not guess.
- summary: at most two sentences on what the company does.
- product_lines: what the company makes or does, as short noun phrases (at most 10, no duplicates).
- end_markets: industries or customer types it serves (at most 10, no duplicates).
- business_model: "manufacturer" if it makes physical products (even if it also designs or services them), "distributor" if it mainly resells others' products, "services" for repair/installation/contract services, "software" for software, "mixed" if several apply equally. Use "unknown" only if the text gives no idea.
- size_signals: only numbers the text actually states. If no employee count is stated, employee_count and employee_count_quote must both be null; otherwise employee_count_quote is the exact phrase with the number.
- ownership: "yes" only if the text says so (e.g. "family-owned", "founded and run by", "a portfolio company of", "a subsidiary of", "NASDAQ:", "NYSE:"). Otherwise "unknown".
- evidence: up to 6 short verbatim quotes (under 200 characters) supporting ownership, size and what the company does. "claim" is the field name the quote supports (e.g. "publicly_traded", "employee_count", "product_lines"); "page" is the page path shown after '###'.
- Never write the name of any individual person anywhere; write [PERSON] instead. Never include email addresses or phone numbers.
Reply with JSON only, matching the schema."""


def user_prompt(company_name: str, domain: str, document: str) -> str:
    return f"Company: {company_name}\nWebsite: {domain}\n\nWebsite text:\n{document}"


FIX_PROMPT = (
    "That reply was not valid JSON matching the schema ({error}). "
    "Reply again with only JSON that matches the schema."
)
