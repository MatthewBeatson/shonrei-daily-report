"""One-off provisioning: create Supabase Auth users for the 6 Shonrei
management users (if they don't already exist) and their reporting.report_users
rows with the correct can_edit / is_admin flags.

Does NOT set a usable password and does NOT trigger any Supabase invite/
magic-link email -- these are real people, not test accounts, so no email
gets sent to them without asking first. Each account is created with
email_confirm=true and a random throwaway password; they get into the app
for the first time via Supabase's "forgot password" flow from the login
page once it exists (or you send them a Supabase invite link yourself later).

Usage: python scripts/create_report_users.py
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

USERS = [
    {'email': 'matthew@shonrei.co.nz', 'full_name': 'Matthew Beatson', 'can_edit': True, 'is_admin': True},
    {'email': 'wesley@shonrei.co.nz', 'full_name': 'Wesley Beatson', 'can_edit': True, 'is_admin': False},
    {'email': 'harvey@shonrei.co.nz', 'full_name': 'Harvey Beatson', 'can_edit': True, 'is_admin': False},
    {'email': 'ben@shonrei.co.nz', 'full_name': 'Ben Beatson', 'can_edit': False, 'is_admin': False},
    {'email': 'jim@shonrei.co.nz', 'full_name': 'Jim Beatson', 'can_edit': False, 'is_admin': False},
    {'email': 'glenn@shonrei.co.nz', 'full_name': 'Glenn Beatson', 'can_edit': False, 'is_admin': False},
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


def upsert_report_user(conn, user_id: str, email: str, full_name: str, can_edit: bool, is_admin: bool):
    # reporting is not in Supabase's exposed-schema list for PostgREST, so
    # this goes straight over the DB connection instead of the REST API.
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into reporting.report_users (id, email, full_name, can_edit, is_admin)
            values (%s, %s, %s, %s, %s)
            on conflict (id) do update set
              email = excluded.email,
              full_name = excluded.full_name,
              can_edit = excluded.can_edit,
              is_admin = excluded.is_admin
            """,
            (user_id, email, full_name, can_edit, is_admin),
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

            upsert_report_user(conn, user_id, u['email'], u['full_name'], u['can_edit'], u['is_admin'])
            print(f"{u['email']}: report_users row set (can_edit={u['can_edit']}, is_admin={u['is_admin']})")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
