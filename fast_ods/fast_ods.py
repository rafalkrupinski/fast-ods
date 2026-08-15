from dataclasses import dataclass, replace
from datetime import datetime
from io import IOBase
from logging import getLogger
from typing import Iterator, NamedTuple
from zipfile import ZipFile

from xml.parsers.expat import ParserCreate

# -----------------
# Constants
#------------------

CONTENT_XML_FILE_NAME = 'content.xml'

OFFICE_NS = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
TABLE_NS = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'

VALUE_TYPE_ATTRIBUTE = f'{OFFICE_NS}}}value-type'
VALUE_ATTRIBUTE = f'{OFFICE_NS}}}value'
STRING_VALUE_ATTRIBUTE = f'{OFFICE_NS}}}string-value'

TABLE_NAME_ATTRIBUTE = f'{TABLE_NS}}}name'
TABLE_NUMBER_COLUMNS_REPEATED_ATTRIBUTE = f'{TABLE_NS}}}number-columns-repeated'
TABLE_NUMBER_ROWS_REPEATED_ATTRIBUTE = f'{TABLE_NS}}}number-rows-repeated'

TABLE_TABLE_TAG = f'{TABLE_NS}}}table'
TABLE_ROW_TAG = f'{TABLE_NS}}}table-row'
TABLE_CELL_TAG = f'{TABLE_NS}}}table-cell'

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


class ODSParser():
    def __init__(self, default_options: ODSParserOptions | None = None):
        self.default_options = default_options or ODSParserOptions()

    def _parse_table_internal(self, ods_contents: IOBase, options: ODSParserOptions) -> Iterator[tuple]:
        if ods_contents is None:
            raise ValueError("'ods_contents' was null")
        
        if options is None:
            raise ValueError("'options' was null")
        
        table = options.table

        if not isinstance(table, (int, str)):
            raise ValueError("'table' must be an int or str")

        # Cached option values
        convert_values = options.convert_values
        take_n_rows = options.take_n_rows
        skip_n_rows = options.skip_n_rows
        skip_empty_rows_at_start = options.skip_empty_rows_at_start

        # Tracking variables for finding the right table
        seen_target_table = False
        number_of_tables_checked = 0

        # Attributes of the row/cell currently being parsed
        row_attrs: dict[str, str] = {}
        cell_attrs: dict[str, str] = {}
        in_cell = False
        cell_chars: list[str] = []

        # Value accumulator for the current row
        current_row: list[CellValue] = []
        current_row_has_value = False

        # Row counting for the take/skip N rows functionality
        row_count = 0
        rows_taken = 0

        # rows completed since the last drain
        collected_rows: list[RowInfo] = []

        def start_element(name: str, attrs: dict[str, str]):
            nonlocal seen_target_table, number_of_tables_checked, row_attrs, cell_attrs, in_cell, cell_chars

            if not seen_target_table and name == TABLE_TABLE_TAG:
                table_name = attrs.get(TABLE_NAME_ATTRIBUTE)

                if (isinstance(table, str) and table == table_name) or (isinstance(table, int) and table == number_of_tables_checked):
                    seen_target_table = True
                else:
                    number_of_tables_checked += 1

                return

            if not seen_target_table:
                return

            if name == TABLE_ROW_TAG:
                row_attrs = attrs
            elif name == TABLE_CELL_TAG:
                in_cell = True
                cell_chars = []
                cell_attrs = attrs

        def char_data(data: str):
            if in_cell:
                cell_chars.append(data)

        def end_element(name: str):
            nonlocal in_cell, cell_chars, current_row, current_row_has_value

            if not seen_target_table:
                return

            # Handle </table:table-cell>
            if name == TABLE_CELL_TAG:
                cell_value = cell_attrs.get(STRING_VALUE_ATTRIBUTE)

                if cell_value is None:
                    cell_value = cell_attrs.get(VALUE_ATTRIBUTE)

                if cell_value is None and cell_chars:
                    cell_value = "".join(cell_chars)

                # Convert the cell value to the type specified in the cell 'value-type' attribute
                if cell_value is not None and convert_values:
                    value_type_attribute = cell_attrs.get(VALUE_TYPE_ATTRIBUTE)

                    if value_type_attribute in ("float", "currency", "percentage"):
                        cell_value = float(cell_value)
                    elif value_type_attribute == "date":
                        cell_value = datetime.fromisoformat(cell_value)
                    elif cell_value is not None:
                        cell_value = str(cell_value)

                if cell_value is not None:
                    current_row_has_value = True

                # Append cell values to the current row
                column_repeat_amount = int(cell_attrs.get(TABLE_NUMBER_COLUMNS_REPEATED_ATTRIBUTE, 1))

                if column_repeat_amount == 1:
                    current_row.append(cell_value)
                else:
                    current_row.extend([cell_value] * column_repeat_amount)

                in_cell = False
                cell_chars = []
                return

            # Handle </table:table-row>
            if name == TABLE_ROW_TAG:
                row_repeat_amount = int(row_attrs.get(TABLE_NUMBER_ROWS_REPEATED_ATTRIBUTE, 1))

                collected_rows.append(RowInfo(tuple(current_row), row_repeat_amount, current_row_has_value))

                current_row = []
                current_row_has_value = False

        def drain():
            nonlocal row_count, rows_taken, skip_empty_rows_at_start

            for row in collected_rows:
                for _ in range(row.repeat):
                    # Increment row count by 1
                    row_count += 1

                    # Skip the requested amount of rows
                    if skip_n_rows and row_count <= skip_n_rows:
                        continue

                    # Skip the row if it's empty and the "skip_empty_rows_at_start" option is True
                    if (not row.has_value) and skip_empty_rows_at_start:
                        continue

                    # Clear the "skip_empty_rows_at_start" option when the first row with data is found
                    skip_empty_rows_at_start = False

                    yield row.values

                    rows_taken += 1

                    # Stop iteration if the targeted number of rows have already been returned
                    if take_n_rows and rows_taken >= take_n_rows:
                        return

            collected_rows.clear()

        parser = ParserCreate(namespace_separator='}')
        parser.buffer_text = True
        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        parser.CharacterDataHandler = char_data

        while True:
            chunk = ods_contents.read(READ_CHUNK_SIZE)
            is_final = not chunk

            parser.Parse(chunk, is_final)

            yield from drain()

            if is_final or (take_n_rows and rows_taken >= take_n_rows):
                break

    def _merge_options(self, overrides: dict) -> ODSParserOptions:
        return replace(self.default_options, **overrides) if not overrides is None else self.default_options

    def parse(self, path: str, **options) -> Iterator[tuple]:
        merged_options = self._merge_options(options)

        if not path.endswith('.ods'):
            logger.warning('File does not have the .ods extension')

        with ZipFile(path, mode='r') as zip_stream:
            if merged_options.verify_zip:
                bad_file = zip_stream.testzip()

                if bad_file == CONTENT_XML_FILE_NAME:
                    logger.warning('ODS file may be corrupted')

            with zip_stream.open(CONTENT_XML_FILE_NAME) as content_stream:
                yield from self._parse_table_internal(content_stream, merged_options)
