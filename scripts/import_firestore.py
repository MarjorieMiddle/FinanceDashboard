#!/usr/bin/env python3
"""Merge a local Firestore export into the dashboard's income tracker.

By default this is a read-only preview. Add --apply to create a SQLite backup
and insert records in one transaction. Firebase is never contacted or changed.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / 'data' / 'firestore-export.json'
DEFAULT_DATABASE = ROOT / 'data' / 'finance.sqlite3'
ALLOWED_CATEGORIES = {'WTDC', 'Gift', 'Trading Payout', 'Other', 'Spent', 'GasBonus'}
DIRECT_CATEGORIES = {'WTDC', 'Gift', 'Trading Payout', 'Other', 'GasBonus'}
LEGACY_TO_CATEGORY = {'Tutoring': 'Other', 'CodeNinjas': 'Other', 'Interest Payment': 'Other'}


def normalized_date(value):
    if not isinstance(value, str):
        raise ValueError('date is missing or is not text')
    value = value.strip()
    for pattern in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(value, pattern).strftime('%Y-%m-%d')
        except ValueError:
            pass
    raise ValueError(f'unsupported date {value!r}')


def number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -1e9 < float(value) < 1e9:
        raise ValueError(f'{field} is not a valid amount')
    return float(value)


def string(value, field, required=False):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(f'{field} is not text')
    value = value.strip()
    if (required and not value) or len(value) > 200:
        raise ValueError(f'{field} is invalid')
    return value


def source_documents(exported, collection):
    documents = exported.get('collections', {}).get(collection)
    if not isinstance(documents, list):
        raise ValueError(f'Firestore export is missing the {collection!r} collection')
    return documents


def entry_from_document(document, collection):
    data = document.get('data')
    document_id = document.get('id')
    if not isinstance(data, dict) or not isinstance(document_id, str) or not document_id:
        raise ValueError('document is malformed')
    job = string(data.get('job'), 'job', required=True)
    description = string(data.get('description'), 'description')
    if collection == 'spentHistory':
        if job != 'Spent':
            raise ValueError(f'spentHistory job {job!r} is not supported')
        category = 'Spent'
    elif job in DIRECT_CATEGORIES:
        category = job
    elif job in LEGACY_TO_CATEGORY:
        category = LEGACY_TO_CATEGORY[job]
        description = f'{job}{": " + description if description else ""}'
    else:
        # Preserve an unrecognized legacy job label rather than discard it.
        category = 'Other'
        description = f'{job}{": " + description if description else ""}'
    if category == 'Spent' and not description:
        raise ValueError('a spending description is required')
    return {
        'id': f'firestore-{collection}-{document_id}',
        'category': category,
        'description': description,
        'amount': number(data.get('amount'), 'amount'),
        'date': normalized_date(data.get('date')),
    }


def subscription_from_document(document):
    data = document.get('data')
    document_id = document.get('id')
    if not isinstance(data, dict) or not isinstance(document_id, str) or not document_id:
        raise ValueError('subscription document is malformed')
    cadence = string(data.get('cadence'), 'cadence', required=True)
    if cadence not in {'Monthly', 'Yearly'}:
        raise ValueError(f'unsupported subscription cadence {cadence!r}')
    return {
        'id': f'firestore-Subscriptions-{document_id}',
        'cadence': cadence,
        'description': string(data.get('description'), 'description', required=True),
        'amount': number(data.get('amount'), 'amount'),
        'due_date': normalized_date(data.get('dueDate')),
    }


def initialize(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS income_tracker_entries (id TEXT PRIMARY KEY, category TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS income_tracker_subscriptions (id TEXT PRIMARY KEY, cadence TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL, due_date TEXT NOT NULL)')


def backup_database(database):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = database.with_name(f'finance-before-firestore-import-{stamp}.sqlite3')
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def main():
    parser = argparse.ArgumentParser(description='Preview or import a Firestore export into the finance dashboard.')
    parser.add_argument('--input', type=Path, default=DEFAULT_INPUT)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--apply', action='store_true', help='Create a backup and insert records. Without this flag, only preview changes.')
    args = parser.parse_args()
    exported = json.loads(args.input.read_text())
    if exported.get('format') != 'finance-dashboard-firestore-export/v1':
        raise ValueError('This is not an export produced by export-firestore.mjs')
    entries = [entry_from_document(d, 'incomeData') for d in source_documents(exported, 'incomeData')]
    entries += [entry_from_document(d, 'spentHistory') for d in source_documents(exported, 'spentHistory')]
    subscriptions = [subscription_from_document(d) for d in source_documents(exported, 'Subscriptions')]
    if len({entry['id'] for entry in entries}) != len(entries):
        raise ValueError('duplicate Firestore entry IDs found')
    if len({subscription['id'] for subscription in subscriptions}) != len(subscriptions):
        raise ValueError('duplicate Firestore subscription IDs found')

    with sqlite3.connect(args.database) as connection:
        initialize(connection)
        existing_entries = {row[0] for row in connection.execute('SELECT id FROM income_tracker_entries')}
        existing_subscriptions = {row[0] for row in connection.execute('SELECT id FROM income_tracker_subscriptions')}
    new_entries = [entry for entry in entries if entry['id'] not in existing_entries]
    new_subscriptions = [row for row in subscriptions if row['id'] not in existing_subscriptions]
    legacy = sum(entry['description'].split(':', 1)[0] in LEGACY_TO_CATEGORY for entry in entries if entry['category'] == 'Other')
    print(f'Firestore export: {len(entries)} income/spending records and {len(subscriptions)} subscriptions')
    print(f'Dashboard database: {len(new_entries)} new records and {len(new_subscriptions)} new subscriptions to add')
    print(f'Legacy income labels preserved in description and mapped to Other: {legacy}')
    if not args.apply:
        print('Preview only. Re-run with --apply to create a backup and insert these new records.')
        return
    backup = backup_database(args.database)
    with sqlite3.connect(args.database) as connection:
        initialize(connection)
        connection.execute('BEGIN IMMEDIATE')
        connection.executemany('INSERT OR IGNORE INTO income_tracker_entries(id,category,description,amount,date) VALUES (:id,:category,:description,:amount,:date)', new_entries)
        connection.executemany('INSERT OR IGNORE INTO income_tracker_subscriptions(id,cadence,description,amount,due_date) VALUES (:id,:cadence,:description,:amount,:due_date)', new_subscriptions)
    print(f'Imported successfully. SQLite backup: {backup}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        print(f'Import stopped: {error}', file=sys.stderr)
        raise SystemExit(1)
