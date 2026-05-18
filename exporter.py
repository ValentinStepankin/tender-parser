"""
exporter.py — запись результатов анализа в Excel.

Структура: каждая закупочная позиция тендера — отдельная строка.
Общие поля тендера (НМЦ, заказчик, сроки и т.д.) дублируются в каждой строке.
Если у тендера нет позиций — пишется одна строка только с общими полями.
"""

from pathlib import Path

import openpyxl


def append_rows(xlsx_path: Path, tender_name: str, data: dict, fields_config: list):
    """
    Дописать строки в Excel для одного тендера.
    Создаёт файл с заголовком если не существует.
    Синхронизирует заголовок с текущим набором колонок при каждом вызове.

    fields_config — список словарей полей из config['fields'] (с метаданными type/item_schema).
    """
    columns = _build_columns(fields_config)

    if xlsx_path.exists():
        wb = openpyxl.load_workbook(str(xlsx_path))
        ws = wb.active
        for i, col_name in enumerate(columns, start=1):
            if ws.cell(row=1, column=i).value != col_name:
                ws.cell(row=1, column=i, value=col_name)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(columns)

    for row in _build_rows(tender_name, data, fields_config):
        ws.append(row)

    wb.save(str(xlsx_path))
    wb.close()


def _build_columns(fields_config: list) -> list:
    """Собрать заголовок Excel: 'файл' + колонки полей (раскрытие списочных полей в колонки подэлементов)."""
    columns = ['файл']
    for f in fields_config:
        if f.get('type') == 'list':
            for item in f.get('item_schema', []):
                columns.append(item['column'])
        else:
            columns.append(f['name'])
    return columns


def _build_rows(tender_name: str, data: dict, fields_config: list) -> list:
    """Развернуть данные тендера в одну или несколько строк Excel."""
    list_field = next((f for f in fields_config if f.get('type') == 'list'), None)
    items = data.get(list_field['name'], []) if list_field else []

    if not items:
        return [_build_row(tender_name, data, fields_config, item=None)]
    return [_build_row(tender_name, data, fields_config, item=item) for item in items]


def _build_row(tender_name: str, data: dict, fields_config: list, item) -> list:
    """Одна строка Excel: имя тендера + значения по колонкам в порядке fields_config."""
    row = [tender_name]
    for f in fields_config:
        if f.get('type') == 'list':
            for sub in f.get('item_schema', []):
                row.append(item.get(sub['key'], '') if item else '')
        else:
            row.append(data.get(f['name'], ''))
    return row
