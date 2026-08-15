from dataclasses import dataclass
from datetime import datetime
from logging import getLogger
from typing import IO, NamedTuple
from collections.abc import Iterator
from zipfile import ZipFile

from xml.parsers.expat import ParserCreate

# -----------------
# Constants
#------------------

CONTENT_XML_FILE_NAME = 'content.xml'

OFFICE_NS = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE_NS = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'

VALUE_TYPE_ATTRIBUTE = f'{OFFICE_NS}|value-type'
VALUE_ATTRIBUTE = f'{OFFICE_NS}|value'
STRING_VALUE_ATTRIBUTE = f'{OFFICE_NS}|string-value'

TABLE_NAME_ATTRIBUTE = f'{TABLE_NS}|name'
TABLE_NUMBER_COLUMNS_REPEATED_ATTRIBUTE = f'{TABLE_NS}|number-columns-repeated'
TABLE_NUMBER_ROWS_REPEATED_ATTRIBUTE = f'{TABLE_NS}|number-rows-repeated'

TABLE_TABLE_TAG = f'{TABLE_NS}|table'
TABLE_ROW_TAG = f'{TABLE_NS}|table-row'
TABLE_CELL_TAG = f'{TABLE_NS}|table-cell'

READ_CHUNK_SIZE = 2**16

logger = getLogger(__name__)

#------------------
# Options
#------------------

@dataclass(slots=True)
class ODSParserOptions:
    table: str | int = 0
    convert_values: bool = False
    take_n_rows: int | None = None
    skip_n_rows: int | None = None
    skip_empty_rows_at_start: bool = False
    verify_zip: bool = True

#------------------
# Parser
#------------------


CellValue = str | float | datetime | None


class RowInfo(NamedTuple):
    values: tuple[CellValue, ...]
    repeat: int
    has_value: bool


class ODSParser:
    __slots__ = (
        'options',
        'skip_empty_rows_at_start',
        'seen_target_table',
        'number_of_tables_checked',
        'row_attrs',
        'cell_attrs',
        'in_cell',
        'cell_chars',
        'current_row',
        'current_row_has_value',
        'row_count',
        'rows_taken',
        'collected_rows',
    )

    def __init__(self, options: ODSParserOptions):
        if options is None:
            raise ValueError("'options' was null")

        if not isinstance(options.table, (int, str)):
            raise ValueError("'table' must be an int or str")

        self.options = options
        self.skip_empty_rows_at_start = options.skip_empty_rows_at_start

        # Tracking variables for finding the right table
        self.seen_target_table = False
        self.number_of_tables_checked = 0

        # Attributes of the row/cell currently being parsed
        self.row_attrs: dict[str, str] = {}
        self.cell_attrs: dict[str, str] = {}
        self.in_cell = False
        self.cell_chars: list[str] = []

        # Value accumulator for the current row
        self.current_row: list[CellValue] = []
        self.current_row_has_value = False

        # Row counting for the take/skip N rows functionality
        self.row_count = 0
        self.rows_taken = 0

        # rows completed since the last drain
        self.collected_rows: list[RowInfo] = []

    def start_element(self, name: str, attrs: dict[str, str]):
        if not self.seen_target_table and name == TABLE_TABLE_TAG:
            table_name = attrs.get(TABLE_NAME_ATTRIBUTE)

            if (isinstance(self.options.table, str) and self.options.table == table_name) or (isinstance(self.options.table, int) and self.options.table == self.number_of_tables_checked):
                self.seen_target_table = True
            else:
                self.number_of_tables_checked += 1

            return

        if not self.seen_target_table:
            return

        if name == TABLE_ROW_TAG:
            self.row_attrs = attrs
        elif name == TABLE_CELL_TAG:
            self.in_cell = True
            self.cell_chars = []
            self.cell_attrs = attrs

    def char_data(self, data: str):
        if self.in_cell:
            self.cell_chars.append(data)

    def end_element(self, name: str):
        if not self.seen_target_table:
            return

        # Handle </table:table-cell>
        if name == TABLE_CELL_TAG:
            raw_value = self.cell_attrs.get(STRING_VALUE_ATTRIBUTE)

            if raw_value is None:
                raw_value = self.cell_attrs.get(VALUE_ATTRIBUTE)

            if raw_value is None and self.cell_chars:
                raw_value = "".join(self.cell_chars)

            cell_value: str | float | datetime | None = raw_value

            # Convert the cell value to the type specified in the cell 'value-type' attribute
            if raw_value is not None and self.options.convert_values:
                value_type_attribute = self.cell_attrs.get(VALUE_TYPE_ATTRIBUTE)

                if value_type_attribute in ("float", "currency", "percentage"):
                    cell_value = float(raw_value)
                elif value_type_attribute == "date":
                    cell_value = datetime.fromisoformat(raw_value)
                else:
                    cell_value = str(raw_value)

            if cell_value is not None:
                self.current_row_has_value = True

            # Append cell values to the current row
            column_repeat_amount = int(self.cell_attrs.get(TABLE_NUMBER_COLUMNS_REPEATED_ATTRIBUTE, 1))

            if column_repeat_amount == 1:
                self.current_row.append(cell_value)
            else:
                self.current_row.extend([cell_value] * column_repeat_amount)

            self.in_cell = False
            self.cell_chars = []
            return

        # Handle </table:table-row>
        if name == TABLE_ROW_TAG:
            row_repeat_amount = int(self.row_attrs.get(TABLE_NUMBER_ROWS_REPEATED_ATTRIBUTE, 1))

            self.collected_rows.append(RowInfo(tuple(self.current_row), row_repeat_amount, self.current_row_has_value))

            self.current_row = []
            self.current_row_has_value = False

    def _drain(self) -> Iterator[tuple]:
        for row in self.collected_rows:
            for _ in range(row.repeat):
                # Increment row count by 1
                self.row_count += 1

                # Skip the requested amount of rows
                if self.options.skip_n_rows and self.row_count <= self.options.skip_n_rows:
                    continue

                # Skip the row if it's empty and the "skip_empty_rows_at_start" option is True
                if (not row.has_value) and self.skip_empty_rows_at_start:
                    continue

                # Clear the "skip_empty_rows_at_start" option when the first row with data is found
                self.skip_empty_rows_at_start = False

                yield row.values

                self.rows_taken += 1

                # Stop iteration if the targeted number of rows have already been returned
                if self.options.take_n_rows and self.rows_taken >= self.options.take_n_rows:
                    return

        self.collected_rows.clear()

    def parse_table(self, ods_contents: IO[bytes]) -> Iterator[tuple]:
        if ods_contents is None:
            raise ValueError("'ods_contents' was null")

        parser = ParserCreate(namespace_separator='|')
        parser.buffer_text = True
        parser.StartElementHandler = self.start_element
        parser.EndElementHandler = self.end_element
        parser.CharacterDataHandler = self.char_data

        while True:
            chunk = ods_contents.read(READ_CHUNK_SIZE)
            is_final = not chunk

            parser.Parse(chunk, is_final)

            yield from self._drain()

            if is_final or (self.options.take_n_rows and self.rows_taken >= self.options.take_n_rows):
                break


def parse(path: str, **kwargs) -> Iterator[tuple]:
    options = ODSParserOptions(**kwargs)

    if not path.endswith('.ods'):
        logger.warning('File does not have the .ods extension')

    with ZipFile(path, mode='r') as zip_stream:
        if options.verify_zip:
            bad_file = zip_stream.testzip()

            if bad_file == CONTENT_XML_FILE_NAME:
                logger.warning('ODS file may be corrupted')

        with zip_stream.open(CONTENT_XML_FILE_NAME) as content_stream:
            yield from parse_content(content_stream, options)


def parse_content(content_stream: IO[bytes], opts: ODSParserOptions) -> Iterator[tuple]:
    return ODSParser(opts).parse_table(content_stream)
