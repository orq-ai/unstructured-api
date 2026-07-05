import json
import re
from typing import Any, Dict, Generic, List, Tuple, TypeVar, Union, get_args, get_origin

T = TypeVar("T")
E = TypeVar("E")

_TRUTHY_STRINGS = frozenset({"true", "1", "yes", "on"})


def _cast_to_type(value: Any, origin_class: type) -> Any:
    """Cast a value to `origin_class` (one of the simple types)."""
    if isinstance(value, str) and origin_class in (int, float):
        try:
            return origin_class(value.strip())
        except ValueError:
            raise ValueError(
                f"Cannot cast {value!r} to {origin_class.__name__}"
            ) from None
    if origin_class is bool and isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return value


def _return_cast_first_element(values: List[E], origin_class: type) -> Union[E, None]:
    """Return the first element of a list cast to `origin_class`, or None if empty."""
    value = next(iter(values), None)
    if value is not None:
        return _cast_to_type(value, origin_class)
    return value


def is_convertible_to_list(s: str) -> Tuple[bool, Union[List, str]]:
    """Determine whether a string is convertible to a list.

    Tries JSON first; if the parsed value is a list, returns (True, list).
    Otherwise falls back to splitting on "," or "+". Returns (False, reason)
    when neither applies.

    Note: the original implementation tested `if delimiter in delimiters`
    (always true), so EVERY non-JSON string — delimiter or not — came back
    as (True, s.split("+")), e.g. "hello" -> (True, ["hello"]). The check is
    now against the input string, and split parts are whitespace-stripped.
    """
    try:
        result = json.loads(s)
        if isinstance(result, list):
            return True, result
        return False, "Input is valid JSON but not a list."
    except json.JSONDecodeError:
        pass

    # Bracketed-but-not-JSON forms the API documents as examples: "[eng]",
    # "['pdf', 'jpg']" (single quotes are invalid JSON).
    stripped = s.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return True, []
        parts = [p.strip().strip("'\"").strip() for p in inner.split(",")]
        return True, [p for p in parts if p]

    for delimiter in (",", "+"):
        if delimiter in s:
            return True, [part.strip() for part in s.split(delimiter)]

    return False, "Input is not valid JSON or a delimited list."


class SmartValueParser(Generic[T]):
    """Handle API parameters passed either as a specific value or as a list of
    strings from which the first element is used, cast to the parametrized type.

    Examples:
        SmartValueParser[int]().value_or_first_element(value)
        SmartValueParser[list[int]]().value_or_first_element(value)
    """

    def value_or_first_element(self, value: Union[T, List[T]]) -> Union[List[T], T, None]:
        """If value is a list, return the first element cast to T; otherwise
        return the value itself cast to T. For T=list[X], cast every element.
        """
        origin_class, container_elems_class = self._get_origin_container_classes()
        if isinstance(value, list) and origin_class is not list:
            return _return_cast_first_element(value, origin_class)
        if isinstance(value, list) and origin_class is list and container_elems_class:
            if len(value) == 1:
                is_list, result = is_convertible_to_list(str(value[0]))
                new_value = result if is_list else value
                return [_cast_to_type(elem, container_elems_class) for elem in new_value]
            return [_cast_to_type(elem, container_elems_class) for elem in value]
        return _cast_to_type(value, origin_class)

    def literal_value_stripped_or_first_element(self, value: str) -> Union[str, None]:
        """Return the literal string with surrounding quotation characters
        stripped.

        Only *enclosing* quote pairs are removed ('"json"' -> json). Interior
        quotes are preserved — the previous implementation deleted every
        quote character anywhere, corrupting values like "O'Brien".
        """
        origin_class, _ = self._get_origin_container_classes()
        stripped = value.strip()
        while (
            len(stripped) >= 2
            and stripped[0] == stripped[-1]
            and stripped[0] in ("'", '"')
        ):
            stripped = stripped[1:-1].strip()
        return _cast_to_type(stripped, origin_class)

    def _get_origin_container_classes(self) -> Tuple[type, Union[type, None]]:
        """Extract the class (and element class, for list types) from the
        type parameter."""
        orig_class = getattr(self, "__orig_class__", None)
        if orig_class is None:
            raise TypeError(
                "SmartValueParser must be parametrized, e.g. SmartValueParser[int]()"
            )
        type_info = orig_class.__args__[0]
        origin_class = get_origin(type_info)
        if origin_class is None:
            return type_info, None
        origin_args = get_args(type_info)
        container_elems_class = origin_args[0] if origin_args else None
        return origin_class, container_elems_class


# ---------------------------------------------------------------------------
# Counting helpers
# ---------------------------------------------------------------------------

def count_characters(s: str) -> int:
    """Number of characters in a string."""
    return len(s)


def count_words(s: str) -> int:
    """Number of whitespace-separated words in a string."""
    return len(s.split())


def count_sentences(s: str) -> int:
    """Approximate number of sentences in a string.

    Counts runs of sentence-ending punctuation followed by whitespace or end
    of string, so "Really?!" is one sentence (not two) and "3.14" is none.
    """
    return len(re.findall(r"[.!?]+(?=\s|$)", s))


def count_paragraphs(s: str) -> int:
    """Number of non-empty paragraphs (blocks separated by blank lines)."""
    return len([p for p in re.split(r"\n\s*\n", s) if p.strip()])


# ---------------------------------------------------------------------------
# PII extraction / redaction
#
# These exist to strip PII before text is chunked, stored, and embedded, so
# a silent miss is a privacy incident. Each cleaner removes text via a single
# regex substitution over the matched span; the old extract-then-
# str.replace() approach failed whenever the extracted form differed from
# the literal text (normalized card numbers, lower-cased emails) and could
# clobber unrelated occurrences of the same substring elsewhere.
# ---------------------------------------------------------------------------

# Local pattern instead of unstructured's extract_email_address, which
# lower-cases the text first: replacing its output back into the original
# left every mixed-case email ("John@Example.com") unredacted.
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_PHONE_PATTERN = re.compile(
    # (?<!\d\.) / (?!\.\d) block matching inside decimals like 3.14159265
    # without blocking a phone number followed by sentence punctuation.
    r"(?<!\w)(?<!\d\.)(?:\+\d{1,3}[-.\s]?)?(?:\(\d{1,4}\)|\d{1,4})"
    r"(?:[-.\s]?\d{1,4}){1,3}(?!\w)(?!\.\d)"
)

# ISO and day-first/US date shapes the phone pattern would otherwise eat.
_DATE_LIKE = re.compile(
    r"^(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4})$"
)

# A single-dot decimal like 3.14159265 (real dotted phones have 2+ dots).
_DECIMAL_LIKE = re.compile(r"^\d+\.\d+$")

# Candidate card numbers: 13-19 digits, optionally grouped by spaces/dashes.
_CARD_PATTERN = re.compile(r"(?<!\d)\d(?:[ -]?\d){11,18}(?!\d)")


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum. Every real card number passes it, so requiring it
    cannot miss a genuine card, but it spares 16-digit order IDs and other
    look-alikes from being redacted."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_phone_like(match_text: str) -> bool:
    stripped = match_text.strip()
    if _DATE_LIKE.match(stripped) or _DECIMAL_LIKE.match(stripped):
        return False
    digit_count = sum(ch.isdigit() for ch in match_text)
    # E.164 numbers are 7-15 digits; anything shorter is almost always a
    # quantity, year, or fragment.
    return 7 <= digit_count <= 15


def extract_email_addresses(text: str) -> List[str]:
    """Extract email addresses, preserving their original casing."""
    return _EMAIL_PATTERN.findall(text)


def extract_phone_numbers(text: str) -> List[str]:
    """Extract phone-number-like strings (7-15 digits, incl. international
    formats like +34672013593), skipping date-shaped matches."""
    return [m for m in _PHONE_PATTERN.findall(text) if _is_phone_like(m)]


def extract_credit_card_numbers(text: str) -> List[str]:
    """Extract Luhn-valid card numbers (13-19 digits, e.g. Visa/MC 16,
    Amex 15), normalized to bare digits."""
    results = []
    for match in _CARD_PATTERN.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if _luhn_valid(digits):
            results.append(digits)
    return results


def clean_emails(text: str, replacement: str = "") -> str:
    """Remove (or replace) email addresses in the text."""
    return _EMAIL_PATTERN.sub(replacement, text)


def clean_phone_numbers(text: str, replacement: str = "") -> str:
    """Remove (or replace) phone numbers in the text, leaving dates and
    short numeric fragments intact."""
    return _PHONE_PATTERN.sub(
        lambda m: replacement if _is_phone_like(m.group()) else m.group(), text
    )


def clean_credit_card_numbers(text: str, replacement: str = "") -> str:
    """Remove (or replace) Luhn-valid card numbers in the text.

    Substitutes the matched span directly. The previous implementation
    normalized the extracted number (stripping spaces/dashes) and then
    str.replace()d the *normalized* form — which no longer appeared in the
    text — so every formatted card number ("4111 1111 1111 1111") survived
    "redaction" untouched.
    """
    return _CARD_PATTERN.sub(
        lambda m: replacement
        if _luhn_valid(re.sub(r"[ -]", "", m.group()))
        else m.group(),
        text,
    )


# ---------------------------------------------------------------------------
# NATS
# ---------------------------------------------------------------------------

# NATS subject tokens must not contain '.', '*', '>', or whitespace: '.'
# changes the routing depth and '*'/'>' are wildcards. These fields originate
# from bus messages, so an entityId like "x.>" would otherwise let a message
# publish itself into arbitrary subjects.
_NATS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _nats_token(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _NATS_TOKEN_RE.match(value):
        raise ValueError(f"Invalid NATS subject token for {field!r}: {value!r}")
    return value


def compute_dispatch_nats_subject(verified_event: Dict[str, str]) -> str:
    """Compute the dispatch subject for an event, validating every token so
    event-supplied fields cannot inject '.' separators or NATS wildcards."""
    try:
        message_type = verified_event["messageType"]
        entity_type = verified_event["entityType"].replace("_", "-")
        entity_id = verified_event["entityId"]
        event_type = verified_event["type"].split(".")[-1]
    except KeyError as e:
        raise ValueError(f"Event is missing required field {e.args[0]!r}") from None

    return ".".join(
        (
            _nats_token(message_type, "messageType"),
            _nats_token(entity_type, "entityType"),
            _nats_token(entity_id, "entityId"),
            _nats_token(event_type, "type"),
        )
    )
