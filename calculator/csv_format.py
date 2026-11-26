"""Streaming structural validation only. Every cell remains an inert string."""
import csv
import io

FIELD_CHARS = 65_536
RECORD_CHARS = 1_048_576
MAX_COLUMNS = 256


class InvalidCSV(ValueError):
    """Messages contain fixed descriptions/line numbers, never uploaded cell contents."""


class Lines:
    def __init__(self, stream):
        self.stream = stream
        self.record_chars = 0
        self.line = 0
        self.state = 'start'

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(RECORD_CHARS + 1)
        if not line:
            raise StopIteration
        self.line += 1
        self.record_chars += len(line)
        if self.record_chars > RECORD_CHARS:
            raise InvalidCSV(f'CSV record near line {self.line} exceeds 1,048,576 characters.')
        for char in line:
            if (ord(char) < 32 and char not in '\t\r\n') or 127 <= ord(char) < 160:
                raise InvalidCSV(f'Unsupported control character near line {self.line}.')
            if self.state == 'quoted':
                if char == '"':
                    self.state = 'after_quote'
            elif self.state == 'after_quote' and char == '"':
                self.state = 'quoted'
            elif char in ',\r\n':
                self.state = 'start'
            elif self.state == 'start':
                self.state = 'quoted' if char == '"' else 'plain'
            elif self.state == 'after_quote' or char == '"':
                raise InvalidCSV(f'Invalid CSV quoting near line {self.line}.')
        return line


def validate(stream):
    """Read a binary stream in bounded records; never infer types or evaluate formulas."""
    csv.field_size_limit(FIELD_CHARS)
    source = Lines(io.TextIOWrapper(stream, encoding='utf-8-sig', errors='strict', newline=''))
    reader = csv.reader(source, delimiter=',', quotechar='"', doublequote=True, strict=True)
    width, rows = None, 0
    try:
        while True:
            source.record_chars = 0
            try:
                row = next(reader)
            except StopIteration:
                break
            if width is None:
                if not row or len(row) > MAX_COLUMNS:
                    raise InvalidCSV('CSV needs a header with 1–256 columns.')
                names = [name.strip() for name in row]
                if any(not name or len(name) > 128 for name in names) or len(set(names)) != len(names):
                    raise InvalidCSV('Headers must be nonempty, unique, and at most 128 characters each.')
                width = len(row)
            else:
                if len(row) != width:
                    raise InvalidCSV(f'Column count does not match the header near line {reader.line_num}.')
                rows += 1
    except UnicodeError:
        raise InvalidCSV('CSV must use UTF-8 encoding.') from None
    except csv.Error:
        raise InvalidCSV(f'Invalid CSV quoting or field longer than 65,536 characters near line {source.line}.') from None
    finally:
        source.stream.detach()  # The caller owns the binary stream.
    if width is None or not rows:
        raise InvalidCSV('CSV needs a header and at least one data row.')
    return {'rows': rows, 'columns': width}
