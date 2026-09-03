"""Run the Monthly Dispatch Plan generator locally, against real Cin7 +
Supabase data, without needing the refresh-service deployed anywhere.

Loads Supabase pooler creds from .env (same as scripts/run_migration.py)
and Cin7 creds from Windows Credential Manager (same as
scripts/dump_sample_sale.py / scripts/test_dispatch_plan_data.py) --
nothing sensitive is printed or passed through chat.

This performs REAL side effects: it hits the live Cin7 API, uploads a
real .xlsx to the 'dispatch-plans' Supabase Storage bucket, and updates
reporting.dispatch_plan_current / dispatch_plan_log.

Usage: python scripts/run_dispatch_plan_locally.py
"""
import os
import sys
from pathlib import Path

import keyring
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / '.env')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'refresh-service'))

SERVICE = 'ShonreiDailyReport'


def main():
    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)
    os.environ['CIN7_ACCOUNT_ID'] = account_id
    os.environ['CIN7_API_KEY'] = api_key

    required = ['SUPABASE_DB_HOST', 'SUPABASE_DB_USER', 'SUPABASE_DB_PASSWORD', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f'.env is missing: {", ".join(missing)}')
        sys.exit(1)

    from dispatch_plan_run import run_dispatch_plan

    print('Running the full Monthly Dispatch Plan pipeline against LIVE data...')
    result = run_dispatch_plan(triggered_by=None)
    print()
    print(result)
    sys.exit(0 if result.get('status') == 'ok' else 1)


if __name__ == '__main__':
    main()
