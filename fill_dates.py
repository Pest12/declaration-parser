import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
import requests
from playwright.sync_api import sync_playwright
import time
import re
import io
import os
import fitz
import cv2
import numpy as np
from pyzbar.pyzbar import decode
from googleapiclient.http import MediaIoBaseDownload
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv


load_dotenv()


SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
SERVICE_ACCOUNT_FILE = 'service_account.json'

CHECK_EXISTING = False
CHECK_STATUS_ALWAYS = True

SHEET_CONFIGS = [
    {'name': 'куриные ДС ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14, 'col_scan': 9, 'col_pdf': 10, 'col_number': 1},
    {'name': 'рыбные дс ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14, 'col_scan': 9, 'col_pdf': 10, 'col_number': 1},
    {'name': 'прочие ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14, 'col_scan': 9, 'col_pdf': 10, 'col_number': 1},
]

REQUEST_DELAY = 0.5


# ================= АВТОРИЗАЦИЯ GOOGLE =================
def get_google_sheet():
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
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

def set_hyperlink_cell(sheet, row, col, url):
    """
    Записывает в ячейку (row, col) текст 'ссылка' и прикрепляет гиперссылку url.
    Использует Sheets API v4 напрямую.
    """
    spreadsheet_id = sheet.spreadsheet.id
    sheet_id = sheet.id

    scope = ['https://www.googleapis.com/auth/spreadsheets']
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    service = build('sheets', 'v4', credentials=creds)

    body = {
        "requests": [
            {
                "updateCells": {
                    "rows": [
                        {
                            "values": [
                                {
                                    "userEnteredValue": {"stringValue": "ссылка"},
                                    "textFormatRuns": [
                                        {
                                            "startIndex": 0,
                                            "format": {
                                                "link": {"uri": url}
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                    "fields": "userEnteredValue,textFormatRuns",
                    "start": {
                        "sheetId": sheet_id,
                        "rowIndex": row - 1,  # API использует индексацию с 0
                        "columnIndex": col - 1
                    }
                }
            }
        ]
    }

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body
        ).execute()
    except Exception as e:
        print(f"  Ошибка при установке гиперссылки: {e}")


def download_file_from_hyperlink(url):
    """
    Скачивает файл по прямой ссылке или из Google Диска.
    Возвращает кортеж (file_bytes, file_type), где file_type: 'pdf', 'jpg', 'png' или None.
    """
    # Если это Google Диск
    if 'drive.google.com' in url:
        file_id = None
        match = re.search(r'/d/([^/]+)', url)
        if match:
            file_id = match.group(1)
        else:
            match = re.search(r'id=([^&]+)', url)
            if match:
                file_id = match.group(1)
        if file_id:
            try:
                scope = ['https://www.googleapis.com/auth/drive.readonly']
                creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
                service = build('drive', 'v3', credentials=creds)
                # Получаем метаданные для определения типа
                file_meta = service.files().get(fileId=file_id, fields='mimeType').execute()
                mime_type = file_meta.get('mimeType', '')
                request = service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                file_bytes = fh.getvalue()
                # Определяем тип по MIME
                if 'pdf' in mime_type:
                    return file_bytes, 'pdf'
                elif 'jpeg' in mime_type or 'jpg' in mime_type:
                    return file_bytes, 'jpg'
                elif 'png' in mime_type:
                    return file_bytes, 'png'
                else:
                    # Попробуем по сигнатуре
                    return file_bytes, guess_file_type(file_bytes)
            except Exception as e:
                print(f'  Ошибка скачивания с Google Диска: {e}')
                return None, None

    # Прямая ссылка
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            content_type = resp.headers.get('Content-Type', '').lower()
            file_bytes = resp.content
            if 'pdf' in content_type:
                return file_bytes, 'pdf'
            elif 'jpeg' in content_type or 'jpg' in content_type:
                return file_bytes, 'jpg'
            elif 'png' in content_type:
                return file_bytes, 'png'
            else:
                return file_bytes, guess_file_type(file_bytes)
        else:
            print(f'  Ошибка HTTP {resp.status_code}')
    except Exception as e:
        print(f'  Ошибка запроса: {e}')
    return None, None

def guess_file_type(file_bytes):
    """Определяет тип файла по сигнатуре."""
    if file_bytes.startswith(b'%PDF'):
        return 'pdf'
    elif file_bytes.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    elif file_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    else:
        return None

def extract_qr_from_file(file_bytes, file_type):
    """
    Извлекает QR-код из байтов файла.
    file_type: 'pdf', 'jpg', 'png' (или None – тогда пробуем автоопределение).
    Возвращает URL или None.
    """
    if file_type is None:
        file_type = guess_file_type(file_bytes)
    if file_type == 'pdf':
        return extract_qr_from_pdf(file_bytes)
    elif file_type in ('jpg', 'png'):
        return extract_qr_from_image(file_bytes)
    else:
        print(f'  Неизвестный тип файла.')
        return None

def extract_qr_from_pdf(file_bytes):
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        print(f'  Не удалось открыть PDF: {e}')
        return None
    for page_num in range(min(len(doc), 3)):
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=200)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if img.ndim == 3 and img.shape[2] == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        elif img.ndim == 3 and img.shape[2] == 4:
            gray = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
        else:
            gray = img
        decoded_objects = decode(gray)
        for obj in decoded_objects:
            data = obj.data.decode('utf-8')
            match = re.search(r'https://pub\.fsa\.gov\.ru/rds/declaration/view/\d+', data)
            if match:
                return match.group(0)
    return None

def extract_qr_from_image(file_bytes):
    try:
        nparr = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            print('  Не удалось декодировать изображение.')
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        decoded_objects = decode(gray)
        for obj in decoded_objects:
            data = obj.data.decode('utf-8')
            match = re.search(r'https://pub\.fsa\.gov\.ru/rds/declaration/view/\d+', data)
            if match:
                return match.group(0)
    except Exception as e:
        print(f'  Ошибка обработки изображения: {e}')
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


def extract_id_from_url(url_str):
    if not url_str:
        return None
    match = re.search(r'/view/(\d+)', url_str)
    if match:
        return match.group(1)
    if url_str.isdigit():
        return url_str
    return None

def try_get_declaration_url_from_files(spreadsheet_id, sheet_name, row, col_scan, col_pdf):
    for col in (col_scan, col_pdf):
        if not col:
            continue
        file_url = get_hyperlink_from_cell(spreadsheet_id, sheet_name, row, col)
        if not file_url:
            try:
                sheet = get_google_sheet().worksheet(sheet_name)
                file_url = sheet.cell(row, col).value
            except:
                pass
        if file_url:
            print(f'  Скачиваю файл из столбца {col_letter(col)}...')
            file_bytes, file_type = download_file_from_hyperlink(file_url)
            if file_bytes:
                print(f'  Файл получен (тип: {file_type}), ищу QR-код...')
                qr_url = extract_qr_from_file(file_bytes, file_type)
                if qr_url:
                    return qr_url
                else:
                    print('  QR-код не найден.')
            else:
                print('  Не удалось скачать файл.')
    return None


def send_email(subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECIPIENT_EMAIL
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        print('  Уведомление отправлено.')
    except Exception as e:
        print(f'  Ошибка отправки email: {e}')


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
        col_scan = cfg.get('col_scan', 0)
        col_pdf = cfg.get('col_pdf', 0)

        try:
            sheet = spreadsheet.worksheet(sheet_name)
        except Exception:
            print(f'Лист "{sheet_name}" не найден, пропускаю.')
            continue

        print(f'\n--- Обрабатываю лист: {sheet_name} ---')
        all_records = sheet.get_all_values()

        for idx, row in enumerate(all_records[1:], start=2):
            col_number = cfg.get('col_number', 1)  # по умолчанию столбец A
            number = row[col_number - 1].strip() if len(row) >= col_number else ''
            existing_start = row[col_start - 1].strip() if len(row) >= col_start else ''
            existing_end = row[col_end - 1].strip() if len(row) >= col_end else ''
            existing_status = row[col_status - 1].strip() if col_status and len(row) >= col_status else ''

            # Получаем URL реестра (основной)
            url_cell = get_hyperlink_from_cell(spreadsheet_id, sheet_name, idx, col_url)
            if not url_cell:
                url_cell = row[col_url - 1].strip()
            decl_id = extract_id_from_url(url_cell) if url_cell else None

            # Если нет ссылки на реестр, пробуем извлечь из файлов
            if not decl_id:
                print(f'Строка {idx}: нет ссылки на реестр, ищем в файлах...')
                new_url = try_get_declaration_url_from_files(spreadsheet_id, sheet_name, idx, col_scan, col_pdf)
                if new_url:
                    # Записываем найденную ссылку в столбец col_url
                    set_hyperlink_cell(sheet, idx, col_url, new_url)
                    print(f'  Записали новую ссылку: {new_url}')
                    decl_id = extract_id_from_url(new_url)
                    url_cell = new_url  # для дальнейшего использования

            if not decl_id:
                if url_cell:
                    print(f'Строка {idx}: не удалось извлечь ID')
                continue

            # Дальше всё как прежде: определяем необходимость обновления дат/статуса
            need_dates = (not existing_start or not existing_end) or CHECK_EXISTING
            need_status = col_status and CHECK_STATUS_ALWAYS
            if need_status and existing_status and existing_status != 'Действует':
                need_status = False
            if not need_dates and not need_status:
                continue

            print(f'Обрабатываю ID {decl_id} (строка {idx})…')
            start_date, end_date, status = fetch_declaration_info(decl_id)

            updated = False
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

            if status and col_status:
                if need_status:
                    # Ежедневная проверка для статуса "Действует"
                    if existing_status == 'Действует' and status != 'Действует':
                        # Статус изменился с "Действует" на другой – отправляем уведомление
                        print(f'  Статус изменился: было "{existing_status}", стало "{status}"')
                        subject = f'Изменение статуса декларации {number}'
                        body = (f'Статус декларации изменился.\n\n'
                                f'Номер декларации: {number}\n'
                                f'Лист: {sheet_name}\n'
                                f'Предыдущий статус: {existing_status}\n'
                                f'Новый статус: {status}\n'
                                f'Ссылка: {url_cell}')
                        send_email(subject, body)
                        sheet.update_cell(idx, col_status, status)
                        updated = True
                    elif status != existing_status:
                        # Статус изменился, но не с "Действует" – просто обновляем ячейку без письма
                        sheet.update_cell(idx, col_status, status)
                        print(f'  Статус обновлён: было "{existing_status}", стало "{status}"')
                        updated = True
                else:
                    # Первичное заполнение (пустая ячейка) – без уведомления
                    if not existing_status:
                        sheet.update_cell(idx, col_status, status)
                        print(f'  Статус записан: {status}')
                        updated = True

            if not updated:
                print('  Данные уже актуальны.')

            time.sleep(REQUEST_DELAY)

    print('\nГотово.')