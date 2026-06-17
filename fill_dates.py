import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
import requests
import time
import re
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


GOOGLE_SHEET_NAME = 'Копия Реестр деклараций'
SERVICE_ACCOUNT_FILE = 'service_account.json'

CHECK_EXISTING = False
CHECK_STATUS_ALWAYS = True

SHEET_CONFIGS = [
    {'name': 'куриные ДС ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14},
    {'name': 'рыбные дс ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14},
    {'name': 'прочие ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14},
]

REQUEST_DELAY = 0.5


# ================= АВТОРИЗАЦИЯ GOOGLE =================
def get_google_sheet():
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME)

# ================= РАБОТА С ГИПЕРССЫЛКАМИ =================
def get_hyperlink_from_cell(spreadsheet_id, sheet_name, row, col):
    """
    Извлекает URL гиперссылки из ячейки (row, col) листа sheet_name.
    Возвращает URL или None.
    """
    # Сначала попробуем прочитать значение как формулу (на случай =HYPERLINK)
    try:
        sheet = get_google_sheet().worksheet(sheet_name)
        cell_value = sheet.cell(row, col, value_render_option='FORMULA').value
        if cell_value.startswith('=HYPERLINK('):
            match = re.search(r'=HYPERLINK\("(.+?)"', cell_value)
            if match:
                return match.group(1)
            match = re.search(r"=HYPERLINK\('(.+?)'", cell_value)
            if match:
                return match.group(1)
    except Exception:
        pass

    # Запрос к Sheets API v4 для получения гиперссылки (обычная вставка)
    scope = ['https://www.googleapis.com/auth/spreadsheets']
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    service = build('sheets', 'v4', credentials=creds)

    range_name = f"'{sheet_name}'!{col_letter(col)}{row}"
    try:
        result = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=range_name,
            fields='sheets/data/rowData/values/hyperlink'
        ).execute()
        link = result['sheets'][0]['data'][0]['rowData'][0]['values'][0].get('hyperlink')
        return link
    except (IndexError, KeyError, Exception):
        return None

def col_letter(col_number):
    """Переводит номер столбца (1 -> A) в буквенное обозначение."""
    result = ''
    while col_number > 0:
        col_number, remainder = divmod(col_number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def fetch_declaration_info(decl_id):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(f'https://pub.fsa.gov.ru/rds/declaration/view/{decl_id}/common',
                      wait_until='networkidle', timeout=30000)
            page.wait_for_selector('span.card-view-toolbar__title__name', timeout=15000)
            title_text = page.inner_text('span.card-view-toolbar__title__name')

            # Даты
            match_start = re.search(r'от\s*(\d{2}\.\d{2}\.\d{4})', title_text)
            match_end = re.search(r'до\s*(\d{2}\.\d{2}\.\d{4})', title_text)
            start_date = match_start.group(1) if match_start else None
            end_date = match_end.group(1) if match_end else None

            # Статус
            status = None
            status_label = page.query_selector('text=Статус:')
            if status_label:
                parent = status_label.evaluate('node => node.parentElement')
                if parent:
                    status_div = page.evaluate_handle('(parent) => parent.querySelector("div.text")', parent)
                    if status_div:
                        status = status_div.as_element().inner_text().strip()
            if not status:
                status_elem = page.query_selector('div.text')
                if status_elem:
                    status = status_elem.inner_text().strip()
        except Exception as e:
            print(f'  Ошибка получения данных: {e}')
            start_date = end_date = status = None
        finally:
            browser.close()
    return start_date, end_date, status

# def parse_dates_from_response(data):
#     reg_date = data.get('declRegDate')
#     end_date = data.get('declEndDate')
#     return format_date(reg_date), format_date(end_date)
#
# def format_date(date_str):
#     if not date_str:
#         return ''
#     if re.match(r'\d{2}\.\d{2}\.\d{4}', str(date_str)):
#         return date_str
#     try:
#         dt = datetime.strptime(date_str, '%Y-%m-%d')
#         return dt.strftime('%d.%m.%Y')
#     except ValueError:
#         pass
#     return str(date_str)

def extract_id_from_url(url_str):
    if not url_str:
        return None
    match = re.search(r'/view/(\d+)', url_str)
    if match:
        return match.group(1)
    if url_str.isdigit():
        return url_str
    return None

# ================= ОСНОВНАЯ ЛОГИКА =================
def extract_google():
    spreadsheet = get_google_sheet()
    spreadsheet_id = spreadsheet.id

    for cfg in SHEET_CONFIGS:
        sheet_name = cfg['name']
        col_url = cfg['col_url']
        col_start = cfg['col_start']
        col_end = cfg['col_end']
        col_status = cfg.get('col_status', 0)

        try:
            sheet = spreadsheet.worksheet(sheet_name)
        except Exception:
            print(f'Лист "{sheet_name}" не найден, пропускаю.')
            continue

        print(f'\n--- Обрабатываю лист: {sheet_name} ---')
        all_records = sheet.get_all_values()

        for idx, row in enumerate(all_records[1:], start=2):
            existing_start = row[col_start - 1].strip() if len(row) >= col_start else ''
            existing_end = row[col_end - 1].strip() if len(row) >= col_end else ''
            existing_status = row[col_status - 1].strip() if col_status and len(row) >= col_status else ''

            # Получаем URL и ID
            url_cell = get_hyperlink_from_cell(spreadsheet_id, sheet_name, idx, col_url)
            if not url_cell:
                url_cell = row[col_url - 1].strip()
            decl_id = extract_id_from_url(url_cell)
            if not decl_id:
                if url_cell:
                    print(f'Строка {idx}: не найден ID в {url_cell}')
                continue

            # Нужны ли даты?
            need_dates = (not existing_start or not existing_end) or CHECK_EXISTING
            # Нужен ли статус? (ежедневная проверка только для действующих/пустых)
            need_status = col_status and CHECK_STATUS_ALWAYS
            if need_status:
                # Если статус уже есть и он не "Действует", то не проверяем
                if existing_status and existing_status != 'Действует':
                    need_status = False

            # Если ни даты, ни статус не нужны, пропускаем
            if not need_dates and not need_status:
                continue

            print(f'Обрабатываю ID {decl_id} (строка {idx})…')
            start_date, end_date, status = fetch_declaration_info(decl_id)

            updated = False
            # Обновляем даты, если нужно
            if need_dates:
                if start_date and start_date != existing_start:
                    sheet.update_cell(idx, col_start, start_date)
                    print(f'  Обновлена дата регистрации: {start_date}')
                    updated = True
                if end_date and end_date != existing_end:
                    sheet.update_cell(idx, col_end, end_date)
                    print(f'  Обновлена дата окончания: {end_date}')
                    updated = True
                if not (start_date or end_date):
                    print('  Даты не получены.')

            # Обновляем статус (с учётом условий)
            if status and col_status:
                if need_status:
                    # Режим ежедневной проверки: обновляем только если изменился
                    if status != existing_status:
                        print(f'  Статус изменился: было "{existing_status}", стало "{status}"')
                        # Здесь будет вызов отправки уведомления
                        sheet.update_cell(idx, col_status, status)
                        updated = True
                else:
                    # Статус нужен только для первичного заполнения (если пустой)
                    if not existing_status:
                        sheet.update_cell(idx, col_status, status)
                        print(f'  Статус записан: {status}')
                        updated = True

            if not updated:
                print('  Данные уже актуальны.')

            time.sleep(REQUEST_DELAY)

    print('\nГотово.')