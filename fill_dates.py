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
import json
import tempfile
import logging


if os.path.exists('.env'):
    load_dotenv()

logging.getLogger().setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

file_handler = logging.FileHandler('declaration_monitor.log')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

SERVICE_ACCOUNT_JSON = os.getenv('SERVICE_ACCOUNT_JSON')
if SERVICE_ACCOUNT_JSON:
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as tmp:
        json.dump(json.loads(SERVICE_ACCOUNT_JSON), tmp)
        SERVICE_ACCOUNT_FILE = tmp.name
else:
    SERVICE_ACCOUNT_FILE = 'service_account.json'

CHECK_EXISTING = False
CHECK_STATUS_ALWAYS = True

SHEET_CONFIGS = [
    {'name': 'куриные ДС ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14, 'col_scan': 9, 'col_pdf': 10, 'col_number': 1},
    {'name': 'рыбные дс ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14, 'col_scan': 9, 'col_pdf': 10, 'col_number': 1},
    {'name': 'прочие ЛВ', 'col_url': 11, 'col_start': 6, 'col_end': 7, 'col_status': 14, 'col_scan': 9, 'col_pdf': 10, 'col_number': 1},
]

REQUEST_DELAY = 0.5


def get_google_sheet():
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME)

def get_hyperlink_from_cell(spreadsheet_id, sheet_name, row, col):
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
                        "rowIndex": row - 1,
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
        logger.error('Fail to access hyperlink %s', e)


def download_file_from_hyperlink(url):

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
                file_meta = service.files().get(fileId=file_id, fields='mimeType').execute()
                mime_type = file_meta.get('mimeType', '')
                request = service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                file_bytes = fh.getvalue()
                if 'pdf' in mime_type:
                    return file_bytes, 'pdf'
                elif 'jpeg' in mime_type or 'jpg' in mime_type:
                    return file_bytes, 'jpg'
                elif 'png' in mime_type:
                    return file_bytes, 'png'
                else:
                    return file_bytes, guess_file_type(file_bytes)
            except Exception as e:
                logger.error('Loading error from Google Disc %s', e)
                return None, None

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
            logger.error('шибка HTTP %s', resp.status_code)
    except Exception as e:
        logger.error('Access error %s', e)
    return None, None

def guess_file_type(file_bytes):
    if file_bytes.startswith(b'%PDF'):
        return 'pdf'
    elif file_bytes.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    elif file_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    else:
        return None

def extract_qr_from_file(file_bytes, file_type):
    if file_type is None:
        file_type = guess_file_type(file_bytes)
    if file_type == 'pdf':
        return extract_qr_from_pdf(file_bytes)
    elif file_type in ('jpg', 'png'):
        return extract_qr_from_image(file_bytes)
    else:
        logger.error('Unknown file')
        return None

def extract_qr_from_pdf(file_bytes):
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        logger.error('Fail to open to pdf file %s', e)
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
            logger.error("Can't decode image")
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        decoded_objects = decode(gray)
        for obj in decoded_objects:
            data = obj.data.decode('utf-8')
            match = re.search(r'https://pub\.fsa\.gov\.ru/rds/declaration/view/\d+', data)
            if match:
                return match.group(0)
    except Exception as e:
        logger.error('Image fail %s', e)
    return None


def col_letter(col_number):
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
            # Стратегия загрузки: ждём загрузки DOM, а не всех сетевых запросов
            page.goto(f'https://pub.fsa.gov.ru/rds/declaration/view/{decl_id}/common',
                      wait_until='domcontentloaded', timeout=60000)
            # Ждём ключевой элемент с тайм-аутом 30 секунд
            try:
                page.wait_for_selector('span.card-view-toolbar__title__name', timeout=30000)
            except Exception:
                # Если элемент не появился, но страница загрузилась — пробуем получить текст сразу
                pass
            title_text = page.inner_text('span.card-view-toolbar__title__name')

            match_start = re.search(r'от\s*(\d{2}\.\d{2}\.\d{4})', title_text)
            match_end = re.search(r'до\s*(\d{2}\.\d{2}\.\d{4})', title_text)
            start_date = match_start.group(1) if match_start else None
            end_date = match_end.group(1) if match_end else None

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
            logger.error("Can't get access to data %s", e)
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
            file_bytes, file_type = download_file_from_hyperlink(file_url)
            if file_bytes:
                qr_url = extract_qr_from_file(file_bytes, file_type)
                if qr_url:
                    return qr_url
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
    except Exception as e:
        logger.error('Email send error %s', e)


def extract_google(mode='daily'):
    global CHECK_EXISTING, CHECK_STATUS_ALWAYS
    if mode == 'full':
        CHECK_EXISTING = True
        CHECK_STATUS_ALWAYS = True
    else:
        CHECK_EXISTING = False
        CHECK_STATUS_ALWAYS = True
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
            continue

        logger.info('Checking list %s', sheet_name)
        all_records = sheet.get_all_values()

        for idx, row in enumerate(all_records[1:], start=2):
            col_number = cfg.get('col_number', 1)
            number = row[col_number - 1].strip() if len(row) >= col_number else ''
            existing_start = row[col_start - 1].strip() if len(row) >= col_start else ''
            existing_end = row[col_end - 1].strip() if len(row) >= col_end else ''
            existing_status = row[col_status - 1].strip() if col_status and len(row) >= col_status else ''

            url_cell = get_hyperlink_from_cell(spreadsheet_id, sheet_name, idx, col_url)
            if not url_cell:
                url_cell = row[col_url - 1].strip()
            decl_id = extract_id_from_url(url_cell) if url_cell else None

            if not decl_id:
                new_url = try_get_declaration_url_from_files(spreadsheet_id, sheet_name, idx, col_scan, col_pdf)
                if new_url:
                    set_hyperlink_cell(sheet, idx, col_url, new_url)
                    logger.info('Write new link %s', new_url)
                    decl_id = extract_id_from_url(new_url)
                    url_cell = new_url

            if not decl_id:
                if url_cell:
                    logger.error('Fail to get ID %s', idx)
                continue

            need_dates = (not existing_start or not existing_end) or CHECK_EXISTING
            need_status = col_status and CHECK_STATUS_ALWAYS
            if need_status and existing_status and existing_status != 'Действует':
                need_status = False
            if not need_dates and not need_status:
                continue

            logger.info('Processing ID %s', decl_id)
            max_retries = 3
            for attempt in range(max_retries):
                start_date, end_date, status = fetch_declaration_info(decl_id)
                if start_date or end_date or status:
                    break
                logger.warning(f'Attempt {attempt + 1} is failed, repeat after 5 sec...')
                time.sleep(5)
            else:
                logger.error(f'Failed after {max_retries} попыток')
                continue

            updated = False
            if need_dates:
                if start_date and start_date != existing_start:
                    sheet.update_cell(idx, col_start, start_date)
                    logger.info('Update register date %s', start_date)
                    updated = True
                if end_date and end_date != existing_end:
                    sheet.update_cell(idx, col_end, end_date)
                    logger.info('Update ending date %s', end_date)
                    updated = True
                if not (start_date or end_date):
                    logger.error('Failed to access dates')

            if status and col_status:
                if need_status:
                    if existing_status == 'Действует' and status != 'Действует':
                        logger.warning('Status had changed...', status)
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
                        sheet.update_cell(idx, col_status, status)
                        logger.warning('Update status...', status)
                        updated = True
                else:
                    if not existing_status:
                        sheet.update_cell(idx, col_status, status)
                        logger.info('Status has writing', status)
                        updated = True

            if not updated:
                logger.info('Data already relevant')

            time.sleep(REQUEST_DELAY)
