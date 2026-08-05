"""Apply a .sql migration file to the shared Supabase project via the
session pooler connection (never the direct db.<ref>.supabase.co host --
it's IPv6-only and fails from this dev environment).

Usage:
    python scripts/run_migration.py migrations/001_reporting_schema.sql
"""
from __future__ import annotations
import sys
from pathlib import Path
import psycopg2
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / '.env')


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/run_migration.py <path-to-sql-file>')
        sys.exit(1)
    sql_path = Path(sys.argv[1])
    sql = sql_path.read_text(encoding='utf-8')

    conn = psycopg2.connect(
        host=os.environ['SUPABASE_DB_HOST'],
        port=os.environ['SUPABASE_DB_PORT'],
        dbname=os.environ['SUPABASE_DB_NAME'],
        user=os.environ['SUPABASE_DB_USER'],
        password=os.environ['SUPABASE_DB_PASSWORD'],
        sslmode='require',
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f'Applied {sql_path} successfully.')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
