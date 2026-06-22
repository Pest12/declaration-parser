from fill_dates import extract_google
import argparse


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Мониторинг деклараций')
    parser.add_argument('--mode', choices=['daily', 'full'], default='daily',
                        help='Режим запуска: daily (проверка статусов), full (полная сверка дат)')
    args = parser.parse_args()
    extract_google(mode=args.mode)
