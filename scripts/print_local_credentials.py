"""Run this ON THIS MACHINE (where daily_refresh.py has been running) to
read the Xero/Cin7 credentials out of Windows Credential Manager, so you
can copy them into Render's Environment tab yourself.

This deliberately isn't something Claude runs for you -- these are live
API secrets, and the safer path is you copying them directly from your own
terminal into Render, never through a chat transcript.

Usage: python scripts/print_local_credentials.py
"""
import keyring

SERVICE = 'ShonreiDailyReport'  # matches daily_refresh.py's SERVICE constant

KEYS = {
    'xero_client_id': 'XERO_CLIENT_ID',
    'xero_client_secret': 'XERO_CLIENT_SECRET',
    'xero_refresh_token': 'XERO_INITIAL_REFRESH_TOKEN',
    'cin7_account_id': 'CIN7_ACCOUNT_ID',
    'cin7_api_key': 'CIN7_API_KEY',
}

print('Paste these into the shonrei-report-refresh service\'s Environment tab in Render:\n')
for cred_key, env_name in KEYS.items():
    value = keyring.get_password(SERVICE, cred_key)
    status = value if value else '(not found in Credential Manager)'
    print(f'{env_name}={status}')
