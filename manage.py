#!/usr/bin/env python
import os
import sys

def main():
    # încarcă .env înainte de a porni Django
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv())
    except Exception:
        # dacă python-dotenv nu e instalat, nu bloca pornirea
        pass

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kiosk_site.settings')

    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)

if __name__ == '__main__':
    main()
