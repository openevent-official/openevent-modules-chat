"""Validate JSON syntax and locate fields without materializing extension values."""
import json
import re

from .errors import ChatProtocolError


_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_STRING_RUN = re.compile(r'[^"\\\x00-\x1f]*')
_HEX = frozenset('0123456789abcdefABCDEF')


def _invalid():
    raise ChatProtocolError('payload must contain valid UTF-8 JSON')


class _Scanner:
    def __init__(self, text):
        if not isinstance(text, str):
            _invalid()
        try:
            text.encode('utf-8')
        except UnicodeError:
            _invalid()
        self.text = text
        self.size = len(text)

    def space(self, index):
        while index < self.size and self.text[index] in ' \t\r\n':
            index += 1
        return index

    def string(self, index):
        if index >= self.size or self.text[index] != '"':
            _invalid()
        index += 1
        while index < self.size:
            index = _STRING_RUN.match(self.text, index).end()
            if index >= self.size:
                _invalid()
            char = self.text[index]
            if char == '"':
                return index + 1
            if char != '\\' or index + 1 >= self.size:
                _invalid()
            escape = self.text[index + 1]
            if escape in '"\\/bfnrt':
                index += 2
                continue
            if escape != 'u':
                _invalid()
            digits = self.text[index + 2:index + 6]
            if len(digits) != 4 or any(char not in _HEX for char in digits):
                _invalid()
            index += 6
        _invalid()

    def key(self, index, seen=None):
        end = self.string(index)
        key = None
        if seen is not None:
            # Chat field names are decoded so escaped spellings count as equal.
            key = json.loads(self.text[index:end])
            if key in seen:
                raise ChatProtocolError('JSON object contains duplicate fields')
            seen.add(key)
        index = self.space(end)
        if index >= self.size or self.text[index] != ':':
            _invalid()
        return key, self.space(index + 1)

    def value(self, index):
        # Frames track grammar state, never extension keys or JSON value trees.
        stack = []
        need_value = True
        while True:
            if not need_value and not stack:
                return index
            index = self.space(index)
            if need_value:
                if index >= self.size:
                    _invalid()
                char = self.text[index]
                if char == '{':
                    stack.append(['object', 'first'])
                    index += 1
                elif char == '[':
                    stack.append(['array', 'first'])
                    index += 1
                elif char == '"':
                    index = self.string(index)
                elif char in '-0123456789':
                    match = _NUMBER.match(self.text, index)
                    if match is None:
                        _invalid()
                    index = match.end()
                else:
                    for literal in ('true', 'false', 'null'):
                        if self.text.startswith(literal, index):
                            index += len(literal)
                            break
                    else:
                        _invalid()
                need_value = False
            if not stack:
                return index
            frame = stack[-1]
            index = self.space(index)
            if index >= self.size:
                _invalid()
            char = self.text[index]
            closing = '}' if frame[0] == 'object' else ']'
            if frame[1] == 'after':
                if char == closing:
                    stack.pop()
                    index += 1
                elif char == ',':
                    frame[1] = 'next'
                    index += 1
                else:
                    _invalid()
            elif frame[1] == 'first' and char == closing:
                stack.pop()
                index += 1
            else:
                if frame[0] == 'object':
                    _, index = self.key(index)
                frame[1] = 'after'
                need_value = True


def validate_object(text):
    """Validate one JSON object, including deeply nested values, without decoding it."""
    scanner = _Scanner(text)
    start = scanner.space(0)
    if start >= scanner.size or text[start] != '{':
        _invalid()
    if scanner.space(scanner.value(start)) != scanner.size:
        _invalid()


def object_fields(text):
    """Return top-level field spans after validating the complete JSON object."""
    scanner = _Scanner(text)
    index = scanner.space(0)
    if index >= scanner.size or text[index] != '{':
        _invalid()
    index = scanner.space(index + 1)
    result = {}
    seen = set()
    if index < scanner.size and text[index] == '}':
        index += 1
    else:
        while True:
            key, start = scanner.key(index, seen)
            end = scanner.value(start)
            result[key] = (start, end)
            index = scanner.space(end)
            if index >= scanner.size:
                _invalid()
            if text[index] == '}':
                index += 1
                break
            if text[index] != ',':
                _invalid()
            index = scanner.space(index + 1)
    if scanner.space(index) != scanner.size:
        _invalid()
    return result
