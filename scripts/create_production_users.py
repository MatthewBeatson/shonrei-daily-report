"""One-off provisioning: create Supabase Auth users (if they don't
already exist) and their production.production_users rows.

Deliberately separate from scripts/create_report_users.py /
reporting.report_users -- production access is expected to widen to
warehouse/floor-admin staff who must NOT be able to see the daily
report, so this list is independent on purpose. Someone can be in both
USERS lists (this script's and create_report_users.py's) if they
genuinely need both accounts -- that's fine, the two tables just don't
imply each other.

Does NOT set a usable password and does NOT trigger any Supabase invite/
magic-link email -- these are real people, not test accounts, so no email
gets sent to them without asking first. Each account is created with
email_confirm=true and a random throwaway password; they get in for the
first time via Supabase's "forgot password" flow (production/admin
doesn't have its own "forgot password" UI yet -- use the Supabase
dashboard to send a reset, or add that flow the same way the daily
report's frontend/app.js already has it, if this becomes frequent).

Usage: python scripts/create_production_users.py
"""
from __future__ import annotations
import os, secrets
import truststore
truststore.inject_into_ssl()
import requests
import psycopg2
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / '.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SERVICE_ROLE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']

# Edit this list to add/remove production users -- can_edit=False gets a
# read-only view (targets/batches/stocktake/locations), can_edit=True can
# also sync demand, plan batches, push stock adjustments, and add/assign
# locations. Nobody in here automatically gets reporting.report_users
# access, and vice versa -- add someone to scripts/create_report_users.py
# too, separately, if they genuinely need both.
USERS = [
    {'email': 'matthew@shonrei.co.nz', 'full_name': 'Matthew Beatson', 'can_edit': True},
]

ADMIN_HEADERS = {
    'apikey': SERVICE_ROLE_KEY,
    'Authorization': f'Bearer {SERVICE_ROLE_KEY}',
    'Content-Type': 'application/json',
}


def find_existing_auth_user(email: str):
    r = requests.get(
        f'{SUPABASE_URL}/auth/v1/admin/users',
        headers=ADMIN_HEADERS,
        params={'page': 1, 'per_page': 200},
        timeout=30,
    )
    r.raise_for_status()
    for u in r.json().get('users', []):
        if u.get('email', '').lower() == email.lower():
            return u
    return None


def create_auth_user(email: str, full_name: str):
    r = requests.post(
        f'{SUPABASE_URL}/auth/v1/admin/users',
        headers=ADMIN_HEADERS,
        json={
            'email': email,
            'password': secrets.token_urlsafe(24),  # throwaway; reset via "forgot password"
            'email_confirm': True,
            'user_metadata': {'full_name': full_name},
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def upsert_production_user(conn, user_id: str, email: str, full_name: str, can_edit: bool):
    # production is not in Supabase's exposed-schema list for PostgREST
    # (same reasoning as reporting), so this goes straight over the DB
    # connection instead of the REST API.
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into production.production_users (id, email, full_name, can_edit)
            values (%s, %s, %s, %s)
            on conflict (id) do update set
              email = excluded.email,
              full_name = excluded.full_name,
              can_edit = excluded.can_edit
            """,
            (user_id, email, full_name, can_edit),
        )
    conn.commit()


def main():
    conn = psycopg2.connect(
        host=os.environ['SUPABASE_DB_HOST'],
        port=os.environ['SUPABASE_DB_PORT'],
        dbname=os.environ['SUPABASE_DB_NAME'],
        user=os.environ['SUPABASE_DB_USER'],
        password=os.environ['SUPABASE_DB_PASSWORD'],
        sslmode='require',
    )
    try:
        for u in USERS:
            existing = find_existing_auth_user(u['email'])
            if existing:
                user_id = existing['id']
                print(f"{u['email']}: auth user already exists ({user_id})")
            else:
                created = create_auth_user(u['email'], u['full_name'])
                user_id = created['id']
                print(f"{u['email']}: created auth user ({user_id})")

            upsert_production_user(conn, user_id, u['email'], u['full_name'], u['can_edit'])
            print(f"{u['email']}: production_users row set (can_edit={u['can_edit']})")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
