import json
import re
from typing import TypeVar, Union, List, Generic, get_origin, get_args, Any, Tuple
from unstructured.cleaners.extract import extract_email_address

T = TypeVar("T")
E = TypeVar("E")


def _cast_to_type(value: Any, origin_class: type) -> Any:
    """Cast a value to a type E

    Args:
        value (Any): value to cast to a type T
        origin_class (type): type to cast the value to. Should be one of simple types

    Returns:
        T: value cast to a type T
    """
    if isinstance(value, str) and (origin_class == int or origin_class == float):
        return origin_class(value)  # noqa
    if origin_class == bool and isinstance(value, str):
        return value.lower() == "true"
    return value


def _return_cast_first_element(values: list[E], origin_class: type) -> E | None:
    """Return the first element of a list cast to a type T, or None if the list is empty

    Args:
        values (list[str]): list of strings
        origin_class (type): type to cast the first element to. Should be one of simple types

    Returns:
        T | None: first element cast to a type T, or None if the list is empty
    """
    value = next(iter(values), None)
    if value is not None:
        return _cast_to_type(value, origin_class)  # noqa
    return value


def is_convertible_to_list(s: str) -> Tuple[bool, Union[List, str]]:
    """
    Determines if a given string is convertible to a list.

    This function first tries to parse the string as JSON. If the parsed JSON is a list, it returns
    True along with the list. If parsing as JSON fails, it then checks if the string can be split
    into a list using predefined delimiters ("," or "+"). If so, it returns True and the resulting list.
    If neither condition is met, it returns False and a message indicating the string cannot
    be converted to a list.
    """

    try:
        result = json.loads(s)
        if isinstance(result, list):
            return True, result  # Return the list if conversion is successful
        else:
            return False, "Input is valid JSON but not a list."  # Valid JSON but not a list
    except json.JSONDecodeError:
        pass  # proceed to check using delimiters if JSON parsing fails

    delimiters = ["+", ","]
    for delimiter in delimiters:
        if delimiter in delimiters:
            return True, s.split(delimiter)

    return False, "Input is not valid JSON."  # Invalid JSON


class SmartValueParser(Generic[T]):
    """Class handle api parameters that are passed in form of a specific value or as a list of strings from which
    the first element is used, cast to a proper type
    Should be parametrized with a type to which the value should be casted.

    Examples:
        SmartValueParser[int]().value_or_first_element(value)
        SmartValueParser[list[int]]().value_or_first_element(value)
    """

    def value_or_first_element(self, value: Union[T, list[T]]) -> list[T] | T | None:
        """If value is a list, return the first element cast to a type T, otherwise return the value itself

        Args:
            value (Union[T, List[str]]): value to cast to a type T or return as is
        """
        origin_class, container_elems_class = self._get_origin_container_classes()
        if isinstance(value, list) and not isinstance(value, origin_class):
            extracted_value: T | None = _return_cast_first_element(value, origin_class)
            return extracted_value
        elif isinstance(value, list) and origin_class == list and container_elems_class:
            if len(value) == 1:
                is_list, result = is_convertible_to_list(str(value[0]))
                new_value = result if is_list else value
                return [_cast_to_type(elem, container_elems_class) for elem in new_value]
            return [_cast_to_type(elem, container_elems_class) for elem in value]
        return _cast_to_type(value, origin_class)  # noqa

    def literal_value_stripped_or_first_element(self, value: str) -> str | None:
        """Returns the value itself for literal strings and strips quotation characters.

        Args:
            value (Union[T, List[str]]): value to cast to a type T or return as is
        """
        origin_class, container_elems_class = self._get_origin_container_classes()
        value = value.replace("'", "")
        value = value.replace('"', "")
        return _cast_to_type(value, origin_class)

    def _get_origin_container_classes(self) -> tuple[type, type | None]:
        """Extracts class (and container class if it's a list) from a type hint

        Returns:
            tuple[type, type | None]: class and container class of the type hint
        """
        type_info = self.__orig_class__.__args__[0]  # type: ignore
        origin_class = get_origin(type_info)
        if origin_class is None:
            # it's a basic type like int or bool - return it and no container class
            return type_info, None
        origin_args = get_args(type_info)
        container_elems_class = origin_args[0] if origin_args else None
        return origin_class, container_elems_class


def count_characters(s: str) -> int:
    """Count the number of characters in a string

    Args:
        s (str): string to count characters in

    Returns:
        int: number of characters in the string
    """
    return len(s)


def count_words(s: str) -> int:
    """Count the number of words in a string

    Args:
        s (str): string to count words in

    Returns:
        int: number of words in the string
    """
    return len(s.split())


def count_sentences(s: str) -> int:
    """Count the number of sentences in a string

    Args:
        s (str): string to count sentences in

    Returns:
        int: number of sentences in the string
    """
    return s.count(".") + s.count("?") + s.count("!")


def count_paragraphs(s: str) -> int:
    """Count the number of paragraphs in a string

    Args:
        s (str): string to count paragraphs in

    Returns:
        int: number of paragraphs in the string
    """
    return len(s.split("\n\n"))


def extract_phone_numbers(text: str):
    # Updated pattern to include international formats like +34672013593
    pattern = r"\b(?:\+\d{1,3}[-.\s]?)?(?:\(\d{1,4}\)|\d{1,4})[-.\s]?\d{1,4}[-.\s]?\d{1,9}\b"

    # Find all matches in the text
    phone_numbers = re.findall(pattern, text)

    return phone_numbers


def extract_credit_card_numbers(text: str):
    # Regex pattern for common credit card formats
    pattern = r"\b(?:\d{4}[-\s]?){3}\d{4}\b|\b\d{16}\b"

    # Find all matches in the text
    credit_cards = re.findall(pattern, text)

    # Clean up the results (remove spaces and dashes)
    cleaned_cards = [re.sub(r"[-\s]", "", card) for card in credit_cards]

    return cleaned_cards


def clean_emails(text: str) -> str:

    # Extract email addresses from the text
    emails = extract_email_address(text)

    # Remove the email addresses from the text
    cleaned_text = text

    for email in emails:
        cleaned_text = cleaned_text.replace(email, "")

    return cleaned_text


def clean_phone_numbers(text: str) -> str:
    # Extract phone numbers from the text
    phone_numbers = extract_phone_numbers(text)

    # Remove the phone numbers from the text
    cleaned_text = text

    for phone in phone_numbers:
        cleaned_text = cleaned_text.replace(phone, "")

    return cleaned_text


def clean_credit_card_numbers(text: str) -> str:
    # Extract credit card numbers from the text
    credit_cards = extract_credit_card_numbers(text)

    # Remove the credit card numbers from the text
    cleaned_text = text

    for card in credit_cards:
        cleaned_text = cleaned_text.replace(card, "")

    return cleaned_text
