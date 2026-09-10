import os
import re
import glob
import hashlib
import calendar as _cal
import threading
import sqlite3
import smtplib
import requests
from datetime import datetime, timedelta, date as _date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, jsonify, send_from_directory, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='.', static_url_path='')

PLUGGY_BASE   = 'https://api.pluggy.ai'
CLIENT_ID     = os.getenv('PLUGGY_CLIENT_ID', '')
CLIENT_SECRET = os.getenv('PLUGGY_CLIENT_SECRET', '')
ITEM_IDS      = [i.strip() for i in os.getenv('PLUGGY_ITEM_IDS', '').split(',') if i.strip()]
DB_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'family.db')
CONTINGENCIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Contingencia')
SMTP_USER     = os.getenv('SMTP_USER', '')
SMTP_PASS     = os.getenv('SMTP_PASS', '')
RECIPIENTS    = ['andersonpioner@gmail.com', 'vendruscolo.nadia@gmail.com']

# ── Auth ──────────────────────────────────────────────────────────────────────

_lock      = threading.Lock()
_key_store = {'value': None, 'expires': None}


def get_api_key():
    with _lock:
        if _key_store['value'] and _key_store['expires'] and datetime.now() < _key_store['expires']:
            return _key_store['value']
        if not CLIENT_ID or not CLIENT_SECRET:
            raise ValueError('Credenciais não configuradas no .env')
        r = requests.post(f'{PLUGGY_BASE}/auth',
                          json={'clientId': CLIENT_ID, 'clientSecret': CLIENT_SECRET},
                          timeout=15)
        r.raise_for_status()
        key = r.json().get('apiKey') or r.json().get('api_key')
        if not key:
            raise ValueError(f'Resposta inesperada da autenticação: {r.text[:200]}')
        _key_store['value'] = key
        _key_store['expires'] = datetime.now() + timedelta(hours=1, minutes=50)
        return key


def pluggy_get(path, params=None):
    r = requests.get(f'{PLUGGY_BASE}{path}', params=params,
                     headers={'X-API-KEY': get_api_key()}, timeout=30)
    r.raise_for_status()
    return r.json()


def paginate(path, params=None):
    params = dict(params or {})
    params.setdefault('pageSize', 500)
    data    = pluggy_get(path, params)
    results = list(data.get('results', []))
    for page in range(2, data.get('totalPages', 1) + 1):
        params['page'] = page
        results.extend(pluggy_get(path, params).get('results', []))
    return results


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS items (
                id              TEXT PRIMARY KEY,
                connector_name  TEXT,
                status          TEXT,
                last_updated_at TEXT,
                created_at      TEXT,
                products        TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id               TEXT PRIMARY KEY,
                item_id          TEXT,
                name             TEXT,
                type             TEXT,
                subtype          TEXT,
                balance          REAL,
                currency_code    TEXT,
                institution      TEXT,
                credit_limit     REAL,
                available_credit REAL,
                hidden           INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id            TEXT PRIMARY KEY,
                account_id    TEXT,
                description   TEXT,
                amount        REAL,
                date          TEXT,
                type          TEXT,
                category      TEXT,
                balance       REAL,
                currency_code TEXT,
                account_name  TEXT,
                account_type  TEXT,
                institution   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_txn_date       ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_txn_account    ON transactions(account_id);
            CREATE TABLE IF NOT EXISTS investments (
                id                      TEXT PRIMARY KEY,
                item_id                 TEXT,
                type                    TEXT,
                subtype                 TEXT,
                name                    TEXT,
                number                  TEXT,
                balance                 REAL,
                amount                  REAL,
                taxes                   REAL,
                taxes2                  REAL,
                date                    TEXT,
                value                   REAL,
                quantity                REAL,
                last_month_rate         REAL,
                last_twelve_months_rate REAL,
                annual_rate             REAL,
                code                    TEXT,
                currency_code           TEXT,
                isin                    TEXT,
                institution             TEXT,
                owner                   TEXT,
                status                  TEXT,
                due_date                TEXT,
                issue_date              TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inv_item ON investments(item_id);
            CREATE TABLE IF NOT EXISTS tags (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name  TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#8899aa'
            );
            CREATE TABLE IF NOT EXISTS transaction_tags (
                transaction_id TEXT    NOT NULL,
                tag_id         INTEGER NOT NULL,
                PRIMARY KEY (transaction_id, tag_id)
            );
            CREATE INDEX IF NOT EXISTS idx_txntag ON transaction_tags(transaction_id);
            CREATE TABLE IF NOT EXISTS conjuntos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                color       TEXT NOT NULL DEFAULT '#8899aa',
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS transaction_conjuntos (
                transaction_id TEXT    NOT NULL,
                conjunto_id    INTEGER NOT NULL,
                PRIMARY KEY (transaction_id, conjunto_id)
            );
            CREATE INDEX IF NOT EXISTS idx_txnconjunto ON transaction_conjuntos(transaction_id);
            CREATE INDEX IF NOT EXISTS idx_txnconjunto_conjunto ON transaction_conjuntos(conjunto_id);
            CREATE TABLE IF NOT EXISTS portfolio_assets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                pluggy_id     TEXT UNIQUE,
                name          TEXT NOT NULL DEFAULT '',
                subtype       TEXT DEFAULT '',
                renda_tipo    TEXT DEFAULT 'FIXA',
                indexador     TEXT DEFAULT '',
                vencimento    TEXT DEFAULT '',
                balance       REAL DEFAULT 0,
                amount        REAL DEFAULT 0,
                annual_rate   REAL,
                quantity      REAL,
                value         REAL,
                code          TEXT DEFAULT '',
                isin          TEXT DEFAULT '',
                currency_code TEXT DEFAULT 'BRL',
                institution   TEXT DEFAULT '',
                owner         TEXT DEFAULT '',
                status        TEXT DEFAULT 'ACTIVE',
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS portfolio_asset_cashflows (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id      INTEGER NOT NULL,
                type          TEXT NOT NULL,
                start_date    TEXT NOT NULL,
                end_date      TEXT NOT NULL,
                periodicity   TEXT NOT NULL,
                FOREIGN KEY(asset_id) REFERENCES portfolio_assets(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pac_asset ON portfolio_asset_cashflows(asset_id);
            CREATE TABLE IF NOT EXISTS sync_log (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at            TEXT NOT NULL,
                transactions_added   INTEGER DEFAULT 0,
                accounts_updated     INTEGER DEFAULT 0,
                date_from            TEXT,
                date_to              TEXT
            );
            CREATE TABLE IF NOT EXISTS contingencia_txns (
                id           TEXT PRIMARY KEY,
                account_id   TEXT,
                account_name TEXT,
                account_type TEXT,
                owner        TEXT DEFAULT '',
                date         TEXT,          -- 'YYYY-MM-DD' (sem hora)
                description  TEXT,
                amount       REAL,          -- valor efetivo em BRL (NULL enquanto pendente de conversão)
                type         TEXT,          -- 'DEBIT' | 'CREDIT'
                raw_amount   REAL,          -- valor original do arquivo
                raw_currency TEXT DEFAULT 'BRL',
                installment  TEXT DEFAULT '',
                kind_label   TEXT DEFAULT '',
                source_file  TEXT,
                imported_at  TEXT,
                validada     INTEGER DEFAULT 0,
                validada_at  TEXT DEFAULT '',
                ignored      INTEGER DEFAULT 0,
                is_fixed     INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_cont_date    ON contingencia_txns(date);
            CREATE INDEX IF NOT EXISTS idx_cont_account ON contingencia_txns(account_id);
        ''')



init_db()

# Migrações para bancos já criados
with get_db() as conn:
    for sql in [
        'ALTER TABLE accounts ADD COLUMN hidden  INTEGER DEFAULT 0',
        'ALTER TABLE accounts ADD COLUMN owner   TEXT    DEFAULT ""',
        'ALTER TABLE transactions ADD COLUMN ignored  INTEGER DEFAULT 0',
        'ALTER TABLE transactions ADD COLUMN is_fixed INTEGER DEFAULT 0',
        'ALTER TABLE investments ADD COLUMN due_date   TEXT DEFAULT ""',
        'ALTER TABLE investments ADD COLUMN issue_date TEXT DEFAULT ""',
        'ALTER TABLE portfolio_assets ADD COLUMN periodicidade TEXT DEFAULT ""',
        'ALTER TABLE portfolio_assets ADD COLUMN dy REAL',
        'ALTER TABLE portfolio_assets ADD COLUMN data_pagamento TEXT DEFAULT ""',
        'ALTER TABLE portfolio_assets ADD COLUMN liquidez INTEGER DEFAULT 0',
        'ALTER TABLE contingencia_txns ADD COLUMN ignored  INTEGER DEFAULT 0',
        'ALTER TABLE contingencia_txns ADD COLUMN is_fixed INTEGER DEFAULT 0',
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass


_DEFAULT_TAGS = [
    ('Luz',               '#ffd54f'),
    ('Gás',               '#ffb74d'),
    ('Escola da Isabela', '#64b5f6'),
    ('Marlene',           '#f48fb1'),
    ('Remédio',           '#ef9a9a'),
    ('Médicos e Saúde',   '#80cbc4'),
    ('Internet',          '#4dd0e1'),
    ('Celular Anderson',  '#90caf9'),
    ('Celular Nadia',     '#ce93d8'),
]
with get_db() as conn:
    for name, color in _DEFAULT_TAGS:
        try:
            conn.execute('INSERT OR IGNORE INTO tags (name, color) VALUES (?, ?)', (name, color))
        except Exception:
            pass


def _upsert_investment(conn, inv, institution, owner):
    conn.execute(
        '''INSERT OR REPLACE INTO investments
           (id, item_id, type, subtype, name, number, balance, amount,
            taxes, taxes2, date, value, quantity,
            last_month_rate, last_twelve_months_rate, annual_rate,
            code, currency_code, isin, institution, owner, status,
            due_date, issue_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (inv['id'], inv.get('itemId', ''),
         inv.get('type', ''), inv.get('subtype', ''),
         inv.get('name', ''), inv.get('number', ''),
         inv.get('balance'), inv.get('amount'),
         inv.get('taxes'), inv.get('taxes2'),
         inv.get('date', ''), inv.get('value'), inv.get('quantity'),
         inv.get('lastMonthRate'), inv.get('lastTwelveMonthsRate'), inv.get('annualRate'),
         inv.get('code', ''), inv.get('currencyCode', 'BRL'), inv.get('isin', ''),
         institution, owner, inv.get('status', ''),
         inv.get('dueDate', ''), inv.get('issueDate', ''))
    )


def _upsert_item(conn, item):
    conn.execute(
        'INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)',
        (item['id'],
         item.get('connector', {}).get('name', ''),
         item.get('status', ''),
         item.get('lastUpdatedAt', ''),
         item.get('createdAt', ''),
         ','.join(item.get('products', [])))
    )


def _upsert_account(conn, acc, institution):
    credit      = acc.get('creditData') or {}
    acc_id      = acc['id']
    pluggy_owner = (acc.get('owner') or '').strip()
    conn.execute(
        '''INSERT OR REPLACE INTO accounts
           (id, item_id, name, type, subtype, balance, currency_code,
            institution, credit_limit, available_credit, hidden, owner)
           VALUES (?,?,?,?,?,?,?,?,?,?,
               COALESCE((SELECT hidden FROM accounts WHERE id=?), 0),
               COALESCE(NULLIF(?,  ''), (SELECT owner FROM accounts WHERE id=?), ''))''',
        (acc_id, acc.get('itemId', ''), acc.get('name', ''),
         acc.get('type', ''), acc.get('subtype', ''),
         acc.get('balance', 0), acc.get('currencyCode', 'BRL'),
         institution,
         credit.get('creditLimit'), credit.get('availableCreditLimit'),
         acc_id,
         pluggy_owner, acc_id)
    )


def _insert_transaction(conn, txn, account_name, account_type, institution):
    day = (txn.get('date', '') or '')[:10]
    # Dedup por account_id, não account_name: contas diferentes (ex: após uma
    # reconexão que cria um novo item/account_id na Pluggy) podem compartilhar
    # o mesmo nome de exibição ("BTG Banking"), e comparar por nome fazia
    # transações legítimas de uma conta nova serem descartadas como
    # "duplicatas" de transações já salvas sob o account_id antigo.
    dup = conn.execute(
        '''SELECT 1 FROM transactions
           WHERE SUBSTR(date,1,10)=? AND ROUND(ABS(amount),2)=ROUND(ABS(?),2)
             AND description=? AND account_id=? AND type=?''',
        (day, txn.get('amount', 0), txn.get('description', ''), txn.get('accountId', ''), txn.get('type', ''))
    ).fetchone()
    if dup:
        return False
    conn.execute(
        '''INSERT OR IGNORE INTO transactions
           (id, account_id, description, amount, date, type, category,
            balance, currency_code, account_name, account_type, institution, ignored)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)''',
        (txn['id'], txn.get('accountId', ''),
         txn.get('description', ''), txn.get('amount', 0),
         txn.get('date', ''), txn.get('type', ''),
         txn.get('category', ''), txn.get('balance', 0),
         txn.get('currencyCode', 'BRL'),
         account_name, account_type, institution)
    )
    return True


# ── Sync logic ────────────────────────────────────────────────────────────────

def _resolve_date_from(requested_from):
    if requested_from:
        return requested_from
    with get_db() as conn:
        row = conn.execute('SELECT MAX(date) as d FROM transactions').fetchone()
    if row and row['d']:
        last = datetime.fromisoformat(row['d'][:10])
        return (last - timedelta(days=3)).strftime('%Y-%m-%d')
    return (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')


def run_investments_sync():
    with get_db() as conn:
        acc_rows = conn.execute(
            "SELECT item_id, owner FROM accounts WHERE owner != '' GROUP BY item_id"
        ).fetchall()
    owner_by_item = {r['item_id']: r['owner'] for r in acc_rows}

    total = 0
    with get_db() as conn:
        for item_id in ITEM_IDS:
            try:
                item        = pluggy_get(f'/items/{item_id}')
                institution = item.get('connector', {}).get('name', item_id)
                owner       = owner_by_item.get(item_id, '')
                invs        = paginate('/investments', {'itemId': item_id})
                for inv in invs:
                    _upsert_investment(conn, inv, institution, owner)
                    total += 1
            except Exception:
                continue
    # Re-link: for assets linked to non-ACTIVE investments, try to find an
    # ACTIVE replacement (same name + owner, not already taken). If none
    # exists, keep the stale link — COALESCE in the query falls back to
    # the stored balance, so nothing breaks.
    with get_db() as conn:
        stale = conn.execute('''
            SELECT pa.id, pa.name, pa.owner
            FROM portfolio_assets pa
            JOIN investments inv ON pa.pluggy_id = inv.id
            WHERE inv.status != 'ACTIVE'
        ''').fetchall()
        for asset in stale:
            replacement = conn.execute(
                '''SELECT id FROM investments
                   WHERE LOWER(TRIM(name))  = LOWER(TRIM(?))
                     AND LOWER(TRIM(owner)) = LOWER(TRIM(?))
                     AND status = 'ACTIVE'
                     AND id NOT IN (
                         SELECT pluggy_id FROM portfolio_assets WHERE pluggy_id IS NOT NULL
                     )
                   LIMIT 1''',
                (asset['name'], asset['owner'])
            ).fetchone()
            if replacement:
                conn.execute(
                    'UPDATE portfolio_assets SET pluggy_id=?, updated_at=? WHERE id=?',
                    (replacement['id'], datetime.now().isoformat(), asset['id'])
                )

    # Auto-link: portfolio assets with no pluggy_id at all (added manually)
    # that now have a matching ACTIVE investment.
    with get_db() as conn:
        unlinked = conn.execute(
            'SELECT id, name, institution, owner FROM portfolio_assets WHERE pluggy_id IS NULL'
        ).fetchall()
        for asset in unlinked:
            inv = conn.execute(
                '''SELECT id FROM investments
                   WHERE LOWER(TRIM(name))        = LOWER(TRIM(?))
                     AND LOWER(TRIM(institution)) = LOWER(TRIM(?))
                     AND LOWER(TRIM(owner))       = LOWER(TRIM(?))
                     AND status = 'ACTIVE'
                     AND id NOT IN (
                         SELECT pluggy_id FROM portfolio_assets WHERE pluggy_id IS NOT NULL
                     )
                   LIMIT 1''',
                (asset['name'], asset['institution'], asset['owner'])
            ).fetchone()
            if inv:
                conn.execute(
                    'UPDATE portfolio_assets SET pluggy_id=?, updated_at=? WHERE id=?',
                    (inv['id'], datetime.now().isoformat(), asset['id'])
                )
    return total


def run_sync(date_from, date_to):
    txn_added = acc_updated = 0
    with get_db() as conn:
        for item_id in ITEM_IDS:
            item        = pluggy_get(f'/items/{item_id}')
            institution = item.get('connector', {}).get('name', item_id)
            _upsert_item(conn, item)

            accounts = paginate('/accounts', {'itemId': item_id})
            for acc in accounts:
                _upsert_account(conn, acc, institution)
                acc_updated += 1

                txns = paginate('/transactions', {
                    'accountId': acc['id'],
                    'from': date_from,
                    'to':   date_to,
                })
                for txn in txns:
                    if _insert_transaction(conn, txn, acc.get('name', ''),
                                           acc.get('type', ''), institution):
                        txn_added += 1

        conn.execute(
            'INSERT INTO sync_log (synced_at, transactions_added, accounts_updated, date_from, date_to) '
            'VALUES (?,?,?,?,?)',
            (datetime.now().isoformat(), txn_added, acc_updated, date_from, date_to)
        )
    run_investments_sync()
    return txn_added, acc_updated


# ── API routes ────────────────────────────────────────────────────────────────

@app.route('/api/items')
def api_items():
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT * FROM items').fetchall()
        return jsonify({'results': [dict(r) for r in rows], 'total': len(rows)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/accounts')
def api_accounts():
    try:
        item_id = request.args.get('itemId')
        base = '''SELECT a.*,
                    (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS txn_count,
                    (SELECT MAX(t.date) FROM transactions t WHERE t.account_id = a.id) AS last_txn
                  FROM accounts a'''
        with get_db() as conn:
            if item_id:
                rows = conn.execute(base + ' WHERE a.item_id=?', (item_id,)).fetchall()
            else:
                rows = conn.execute(base).fetchall()
        return jsonify({'results': [dict(r) for r in rows], 'total': len(rows)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/accounts/<account_id>/owner', methods=['POST'])
def set_account_owner(account_id):
    try:
        owner = ((request.get_json(silent=True) or {}).get('owner') or '').strip()
        with get_db() as conn:
            conn.execute('UPDATE accounts SET owner=? WHERE id=?', (owner, account_id))
        return jsonify({'id': account_id, 'owner': owner})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _txn_table(txn_id):
    # transações de contingência têm id com prefixo 'cont_' e vivem em outra tabela
    return 'contingencia_txns' if str(txn_id).startswith('cont_') else 'transactions'


@app.route('/api/transactions/<txn_id>/ignored', methods=['POST'])
def toggle_transaction_ignored(txn_id):
    try:
        tbl = _txn_table(txn_id)
        with get_db() as conn:
            conn.execute(
                f'UPDATE {tbl} SET ignored = CASE WHEN ignored=1 THEN 0 ELSE 1 END WHERE id=?',
                (txn_id,)
            )
            row = conn.execute(f'SELECT ignored FROM {tbl} WHERE id=?', (txn_id,)).fetchone()
        return jsonify({'id': txn_id, 'ignored': bool(row['ignored'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/transactions/<txn_id>/fixed', methods=['POST'])
def toggle_transaction_fixed(txn_id):
    try:
        tbl = _txn_table(txn_id)
        with get_db() as conn:
            conn.execute(
                f'UPDATE {tbl} SET is_fixed = CASE WHEN is_fixed=1 THEN 0 ELSE 1 END WHERE id=?',
                (txn_id,)
            )
            row = conn.execute(f'SELECT is_fixed FROM {tbl} WHERE id=?', (txn_id,)).fetchone()
        return jsonify({'id': txn_id, 'is_fixed': bool(row['is_fixed'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags', methods=['GET'])
def api_tags_list():
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT * FROM tags ORDER BY name').fetchall()
        return jsonify({'results': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags', methods=['POST'])
def api_tags_create():
    try:
        body  = request.get_json(silent=True) or {}
        name  = (body.get('name') or '').strip()
        color = body.get('color', '#8899aa')
        if not name:
            return jsonify({'error': 'Nome obrigatório'}), 400
        with get_db() as conn:
            conn.execute('INSERT INTO tags (name, color) VALUES (?, ?)', (name, color))
            row = conn.execute('SELECT * FROM tags WHERE name=?', (name,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:tag_id>', methods=['PUT'])
def api_tags_update(tag_id):
    try:
        body  = request.get_json(silent=True) or {}
        name  = (body.get('name') or '').strip()
        color = body.get('color', '#8899aa')
        if not name:
            return jsonify({'error': 'Nome obrigatório'}), 400
        with get_db() as conn:
            conn.execute('UPDATE tags SET name=?, color=? WHERE id=?', (name, color, tag_id))
            row = conn.execute('SELECT * FROM tags WHERE id=?', (tag_id,)).fetchone()
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
def api_tags_delete(tag_id):
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM transaction_tags WHERE tag_id=?', (tag_id,))
            conn.execute('DELETE FROM tags WHERE id=?', (tag_id,))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/transactions/<txn_id>/tags', methods=['POST'])
def api_transaction_set_tags(txn_id):
    try:
        body    = request.get_json(silent=True) or {}
        tag_ids = [int(x) for x in (body.get('tag_ids') or [])]
        with get_db() as conn:
            conn.execute('DELETE FROM transaction_tags WHERE transaction_id=?', (txn_id,))
            for tid in tag_ids:
                conn.execute('INSERT OR IGNORE INTO transaction_tags (transaction_id, tag_id) VALUES (?, ?)', (txn_id, tid))
        tag_ids_str = ','.join(str(x) for x in tag_ids)
        return jsonify({'id': txn_id, 'tag_ids': tag_ids_str})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Conjuntos ─────────────────────────────────────────────────────────────────
# Um "conjunto" reúne transações de um mesmo evento/tema pontual (ex: uma
# consulta médica + exames + transporte de um problema de saúde específico).
# Diferente de tag: não é uma categoria recorrente usada mês a mês, é um
# agrupamento fechado, geralmente de curta duração, que só existe uma vez.

@app.route('/api/conjuntos', methods=['GET'])
def api_conjuntos_list():
    try:
        with get_db() as conn:
            rows = conn.execute('''
                SELECT c.*,
                       COUNT(x.id)                                                AS txn_count,
                       COALESCE(SUM(CASE WHEN x.type='CREDIT' THEN -ABS(x.amount) ELSE ABS(x.amount) END), 0) AS total,
                       MIN(x.date) AS first_date,
                       MAX(x.date) AS last_date
                FROM conjuntos c
                LEFT JOIN transaction_conjuntos tc ON tc.conjunto_id = c.id
                LEFT JOIN (
                    SELECT id, amount, date, type FROM transactions
                    UNION ALL
                    SELECT id, amount, date, type FROM contingencia_txns WHERE validada=1
                ) x ON x.id = tc.transaction_id
                GROUP BY c.id
                ORDER BY c.created_at DESC
            ''').fetchall()
        return jsonify({'results': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conjuntos', methods=['POST'])
def api_conjuntos_create():
    try:
        body        = request.get_json(silent=True) or {}
        name        = (body.get('name') or '').strip()
        description = (body.get('description') or '').strip()
        color       = body.get('color', '#8899aa')
        if not name:
            return jsonify({'error': 'Nome obrigatório'}), 400
        with get_db() as conn:
            cur = conn.execute(
                'INSERT INTO conjuntos (name, description, color, created_at) VALUES (?, ?, ?, ?)',
                (name, description, color, datetime.now().isoformat())
            )
            row = conn.execute('SELECT * FROM conjuntos WHERE id=?', (cur.lastrowid,)).fetchone()
        result = dict(row)
        result.update({'txn_count': 0, 'total': 0, 'first_date': None, 'last_date': None})
        return jsonify(result), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conjuntos/<int:conjunto_id>', methods=['PUT'])
def api_conjuntos_update(conjunto_id):
    try:
        body        = request.get_json(silent=True) or {}
        name        = (body.get('name') or '').strip()
        description = (body.get('description') or '').strip()
        color       = body.get('color', '#8899aa')
        if not name:
            return jsonify({'error': 'Nome obrigatório'}), 400
        with get_db() as conn:
            conn.execute(
                'UPDATE conjuntos SET name=?, description=?, color=? WHERE id=?',
                (name, description, color, conjunto_id)
            )
            row = conn.execute('SELECT * FROM conjuntos WHERE id=?', (conjunto_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Conjunto não encontrado'}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conjuntos/<int:conjunto_id>', methods=['DELETE'])
def api_conjuntos_delete(conjunto_id):
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM transaction_conjuntos WHERE conjunto_id=?', (conjunto_id,))
            conn.execute('DELETE FROM conjuntos WHERE id=?', (conjunto_id,))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conjuntos/<int:conjunto_id>/transactions', methods=['GET'])
def api_conjuntos_transactions(conjunto_id):
    try:
        with get_db() as conn:
            rows = conn.execute('''
                SELECT t.*, COALESCE(a.owner, '') as owner_name,
                       COALESCE((SELECT GROUP_CONCAT(tt.tag_id)
                                 FROM transaction_tags tt
                                 WHERE tt.transaction_id = t.id), '') as tag_ids,
                       COALESCE((SELECT GROUP_CONCAT(tc2.conjunto_id)
                                 FROM transaction_conjuntos tc2
                                 WHERE tc2.transaction_id = t.id), '') as conjunto_ids
                FROM transaction_conjuntos tc
                JOIN transactions t ON t.id = tc.transaction_id
                LEFT JOIN accounts a ON t.account_id = a.id
                WHERE tc.conjunto_id = ?
                ORDER BY t.date DESC
            ''', (conjunto_id,)).fetchall()
            crows = conn.execute('''
                SELECT ct.*,
                       COALESCE((SELECT GROUP_CONCAT(tt.tag_id)
                                 FROM transaction_tags tt
                                 WHERE tt.transaction_id = ct.id), '') as tag_ids,
                       COALESCE((SELECT GROUP_CONCAT(tc2.conjunto_id)
                                 FROM transaction_conjuntos tc2
                                 WHERE tc2.transaction_id = ct.id), '') as conjunto_ids
                FROM transaction_conjuntos tc
                JOIN contingencia_txns ct ON ct.id = tc.transaction_id
                WHERE tc.conjunto_id = ?
            ''', (conjunto_id,)).fetchall()

        results = [dict(r) for r in rows]
        for r in crows:
            results.append({
                'id': r['id'], 'account_id': r['account_id'], 'description': r['description'],
                'amount': r['amount'], 'date': r['date'], 'type': r['type'], 'category': '',
                'balance': None, 'currency_code': 'BRL',
                'account_name': r['account_name'], 'account_type': r['account_type'],
                'institution': 'Contingência', 'ignored': r['ignored'], 'is_fixed': r['is_fixed'],
                'owner_name': r['owner'] or '', 'tag_ids': r['tag_ids'], 'conjunto_ids': r['conjunto_ids'],
                'contingencia': 1,
            })
        results.sort(key=lambda x: (x.get('date') or ''), reverse=True)
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/transactions/<txn_id>/conjuntos', methods=['POST'])
def api_transaction_set_conjuntos(txn_id):
    try:
        body         = request.get_json(silent=True) or {}
        conjunto_ids = [int(x) for x in (body.get('conjunto_ids') or [])]
        with get_db() as conn:
            conn.execute('DELETE FROM transaction_conjuntos WHERE transaction_id=?', (txn_id,))
            for cid in conjunto_ids:
                conn.execute('INSERT OR IGNORE INTO transaction_conjuntos (transaction_id, conjunto_id) VALUES (?, ?)', (txn_id, cid))
        conjunto_ids_str = ','.join(str(x) for x in conjunto_ids)
        return jsonify({'id': txn_id, 'conjunto_ids': conjunto_ids_str})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/accounts/<account_id>/hidden', methods=['POST'])
def toggle_account_hidden(account_id):
    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE accounts SET hidden = CASE WHEN hidden=1 THEN 0 ELSE 1 END WHERE id=?',
                (account_id,)
            )
            row = conn.execute('SELECT hidden FROM accounts WHERE id=?', (account_id,)).fetchone()
        return jsonify({'id': account_id, 'hidden': bool(row['hidden'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/transactions')
def api_transactions():
    try:
        account_id = request.args.get('accountId')
        date_from  = request.args.get('from')
        date_to    = request.args.get('to')
        search     = request.args.get('q')

        q = '''SELECT t.*, COALESCE(a.owner, '') as owner_name,
                  COALESCE((SELECT GROUP_CONCAT(tt.tag_id)
                            FROM transaction_tags tt
                            WHERE tt.transaction_id = t.id), '') as tag_ids,
                  COALESCE((SELECT GROUP_CONCAT(tc.conjunto_id)
                            FROM transaction_conjuntos tc
                            WHERE tc.transaction_id = t.id), '') as conjunto_ids
               FROM transactions t
               LEFT JOIN accounts a ON t.account_id = a.id
               WHERE 1=1'''
        p = []
        if account_id:
            q += ' AND t.account_id=?';  p.append(account_id)
        if date_from:
            q += ' AND t.date>=?';     p.append(date_from)
        if date_to:
            q += ' AND t.date<=?';     p.append(date_to + 'T23:59:59')
        if search:
            q += ' AND t.description LIKE ?'; p.append(f'%{search}%')
        q += ' ORDER BY t.date DESC'

        # transações de contingência já validadas entram no mesmo fluxo
        cq = 'SELECT * FROM contingencia_txns WHERE validada=1'
        cp = []
        if account_id:
            cq += ' AND account_id=?';  cp.append(account_id)
        if date_from:
            cq += ' AND date>=?';       cp.append(date_from[:10])
        if date_to:
            cq += ' AND date<=?';       cp.append(date_to[:10])
        if search:
            cq += ' AND description LIKE ?'; cp.append(f'%{search}%')

        with get_db() as conn:
            rows  = conn.execute(q, p).fetchall()
            crows = conn.execute(cq, cp).fetchall()

            results = [dict(r) for r in rows]
            for r in crows:
                ctags = conn.execute(
                    'SELECT GROUP_CONCAT(tag_id) FROM transaction_tags WHERE transaction_id=?', (r['id'],)
                ).fetchone()[0] or ''
                cconj = conn.execute(
                    'SELECT GROUP_CONCAT(conjunto_id) FROM transaction_conjuntos WHERE transaction_id=?', (r['id'],)
                ).fetchone()[0] or ''
                results.append({
                    'id': r['id'], 'account_id': r['account_id'], 'description': r['description'],
                    'amount': r['amount'], 'date': r['date'], 'type': r['type'], 'category': '',
                    'balance': None, 'currency_code': 'BRL',
                    'account_name': r['account_name'], 'account_type': r['account_type'],
                    'institution': 'Contingência', 'ignored': r['ignored'], 'is_fixed': r['is_fixed'],
                    'owner_name': r['owner'] or '', 'tag_ids': ctags, 'conjunto_ids': cconj,
                    'contingencia': 1, 'installment': r['installment'] or '',
                })
        results.sort(key=lambda x: (x.get('date') or ''), reverse=True)
        return jsonify({'results': results, 'total': len(results)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync', methods=['POST'])
def api_sync():
    try:
        body      = request.get_json(silent=True) or {}
        date_to   = body.get('to',   datetime.now().strftime('%Y-%m-%d'))
        date_from = _resolve_date_from(body.get('from'))

        txn_added, acc_updated = run_sync(date_from, date_to)
        return jsonify({
            'ok': True,
            'transactions_added': txn_added,
            'accounts_updated':   acc_updated,
            'date_from': date_from,
            'date_to':   date_to,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync/status')
def api_sync_status():
    try:
        with get_db() as conn:
            last   = conn.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT 1').fetchone()
            counts = conn.execute('''
                SELECT
                    (SELECT COUNT(*) FROM items)        AS items,
                    (SELECT COUNT(*) FROM accounts)     AS accounts,
                    (SELECT COUNT(*) FROM transactions) AS transactions,
                    (SELECT MIN(date) FROM transactions) AS oldest_txn,
                    (SELECT MAX(date) FROM transactions) AS newest_txn
            ''').fetchone()
        return jsonify({
            'last_sync': dict(last)   if last   else None,
            'counts':    dict(counts) if counts else None,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500




def _fmt_brl(v):
    try:
        n = abs(float(v))
        s = f'{n:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
        return f'R$ {s}'
    except Exception:
        return 'R$ 0,00'


def _fmt_k(v):
    try:
        n = abs(float(v))
        if n >= 1000:
            k = n / 1000
            return f'{k:.1f}k'.replace('.0k', 'k').replace('.', ',')
        return f'{n:.0f}'
    except Exception:
        return '0'


def _build_cal_html(iso_day, month_txns):
    year    = int(iso_day[:4])
    month_n = int(iso_day[5:7])
    MONTHS  = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
               'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    # aggregate per calendar day
    day_data = {}
    for t in month_txns:
        d   = (t.get('date') or '')[:10]
        amt = abs(float(t.get('amount') or 0))
        if d not in day_data:
            day_data[d] = {'income': 0, 'disc': 0, 'fixed': 0}
        if t.get('type') == 'CREDIT':
            day_data[d]['income'] += amt
        elif t.get('is_fixed'):
            day_data[d]['fixed']  += amt
        else:
            day_data[d]['disc']   += amt

    days_in_month = _cal.monthrange(year, month_n)[1]
    # Python weekday: Mon=0 Sun=6 → convert to Sun=0 (JS-style)
    first_js_dow  = (_date(year, month_n, 1).weekday() + 1) % 7

    DOW_LABELS = ['Dom','Seg','Ter','Qua','Qui','Sex','Sáb']
    hdr = ''.join(
        f'<th style="padding:3px 2px;font-size:9px;color:#556677;text-align:center;font-weight:600">{d}</th>'
        for d in DOW_LABELS
    )

    cells = ['<td style="background:#0d1117;border:1px solid #161b22"></td>'] * first_js_dow
    for day in range(1, days_in_month + 1):
        key  = f'{year}-{month_n:02d}-{day:02d}'
        data = day_data.get(key, {})
        sel  = key == iso_day
        bg   = '#1a2535' if sel else '#111418'
        bdr  = '1px solid #00d4aa' if sel else '1px solid #1a1f2e'
        nclr = '#00d4aa' if sel else '#c8d8e8'

        inc_h   = f'<div style="font-size:8px;color:#26a69a;font-family:monospace;line-height:1.3">+{_fmt_k(data["income"])}</div>'  if data.get('income', 0)  > 0 else ''
        disc_h  = f'<div style="font-size:8px;color:#ef5350;font-family:monospace;line-height:1.3">−{_fmt_k(data["disc"])}</div>'    if data.get('disc',   0)  > 0 else ''
        fixed_h = f'<div style="font-size:8px;color:#9c6fe4;font-family:monospace;line-height:1.3">⬡{_fmt_k(data["fixed"])}</div>'   if data.get('fixed',  0)  > 0 else ''

        cells.append(
            f'<td style="padding:3px 4px;background:{bg};border:{bdr};vertical-align:top;min-width:52px">'
            f'<div style="font-size:10px;color:{nclr};font-weight:700;font-family:monospace">{day}</div>'
            f'{inc_h}{disc_h}{fixed_h}'
            f'</td>'
        )

    # fill to complete last row
    remainder = len(cells) % 7
    if remainder:
        cells += ['<td style="background:#0d1117;border:1px solid #161b22"></td>'] * (7 - remainder)

    rows = ''
    for i in range(0, len(cells), 7):
        rows += '<tr>' + ''.join(cells[i:i+7]) + '</tr>'

    return (
        f'<div style="margin-top:32px;padding-top:24px;border-top:1px solid #1f2833">'
        f'<div style="font-size:11px;color:#8899aa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px;font-weight:600">{MONTHS[month_n-1]} {year}</div>'
        f'<table width="100%" cellpadding="0" cellspacing="2" style="border-collapse:separate;border-spacing:2px;table-layout:fixed">'
        f'<thead><tr>{hdr}</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table>'
        f'<div style="margin-top:8px;font-size:9px;color:#556677;font-family:monospace">'
        f'<span style="color:#26a69a;margin-right:12px">+ entradas</span>'
        f'<span style="color:#ef5350;margin-right:12px">− discricionário</span>'
        f'<span style="color:#9c6fe4">⬡ vinculado</span>'
        f'</div>'
        f'</div>'
    )


def _build_report_html(date_label, txns, month_txns=None, iso_day=None):
    income    = sum(abs(float(t.get('amount', 0))) for t in txns if t.get('type') == 'CREDIT')
    disc_exp  = sum(abs(float(t.get('amount', 0))) for t in txns if t.get('type') != 'CREDIT' and not t.get('is_fixed'))
    fixed_exp = sum(abs(float(t.get('amount', 0))) for t in txns if t.get('type') != 'CREDIT' and t.get('is_fixed'))
    expense   = disc_exp + fixed_exp
    balance   = income - expense

    owners = {}
    for t in txns:
        raw   = (t.get('owner_name') or t.get('owner') or '').strip()
        first = raw.split()[0].capitalize() if raw else 'Outros'
        owners.setdefault(first, []).append(t)

    def txn_table(lst, label=None, label_color='#8899aa'):
        if not lst:
            return ''
        label_html = (
            f'<div style="font-size:10px;color:{label_color};text-transform:uppercase;'
            f'letter-spacing:1px;font-weight:600;padding:8px 12px 4px;background:#0d1117">{label}</div>'
        ) if label else ''
        rows = ''
        for t in sorted(lst, key=lambda x: abs(float(x.get('amount', 0))), reverse=True):
            amt   = abs(float(t.get('amount', 0)))
            color = '#26a69a' if t.get('type') == 'CREDIT' else ('#9c6fe4' if t.get('is_fixed') else '#ef5350')
            sign  = '+' if t.get('type') == 'CREDIT' else '−'
            rows += (
                f'<tr style="border-bottom:1px solid #1f2833">'
                f'<td style="padding:7px 12px;font-size:13px;color:#e8f0f8">{t.get("description","—")}</td>'
                f'<td style="padding:7px 12px;font-size:11px;color:#8899aa;font-family:monospace">{t.get("category","—")}</td>'
                f'<td style="padding:7px 12px;font-size:11px;color:#8899aa">{t.get("account_name","—")}</td>'
                f'<td style="padding:7px 12px;font-size:13px;color:{color};font-family:monospace;text-align:right;font-weight:600">{sign}{_fmt_brl(amt)}</td>'
                f'</tr>'
            )
        thead = (
            f'<thead><tr style="background:#1e242c">'
            f'<th style="padding:6px 12px;font-size:10px;color:#8899aa;text-align:left;text-transform:uppercase;letter-spacing:1px">Descrição</th>'
            f'<th style="padding:6px 12px;font-size:10px;color:#8899aa;text-align:left;text-transform:uppercase;letter-spacing:1px">Categoria</th>'
            f'<th style="padding:6px 12px;font-size:10px;color:#8899aa;text-align:left;text-transform:uppercase;letter-spacing:1px">Conta</th>'
            f'<th style="padding:6px 12px;font-size:10px;color:#8899aa;text-align:right;text-transform:uppercase;letter-spacing:1px">Valor</th>'
            f'</tr></thead>'
        )
        return (
            f'{label_html}'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #1f2833;border-radius:4px;margin-bottom:12px">'
            f'{thead}<tbody>{rows}</tbody>'
            f'</table>'
        )

    owner_sections = ''
    for name in sorted(owners):
        clr   = '#4a90d9' if name == 'Anderson' else ('#e879a0' if name == 'Nadia' else '#00d4aa')
        txns_o = owners[name]
        disc   = [t for t in txns_o if t.get('type') == 'CREDIT' or not t.get('is_fixed')]
        fixed  = [t for t in txns_o if t.get('type') != 'CREDIT' and t.get('is_fixed')]
        owner_sections += f'<h3 style="color:{clr};margin:24px 0 8px;font-size:14px;letter-spacing:1px;text-transform:uppercase">{name}</h3>'
        if disc and fixed:
            owner_sections += txn_table(disc,  label='Discricionário', label_color='#ef5350')
            owner_sections += txn_table(fixed, label='Vinculado',       label_color='#9c6fe4')
        else:
            owner_sections += txn_table(txns_o)

    bal_color = '#26a69a' if balance >= 0 else '#ef5350'
    bal_sign  = '+' if balance >= 0 else '−'

    cal_html = _build_cal_html(iso_day, month_txns) if iso_day and month_txns else ''

    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="background:#0a0c0f;color:#e8f0f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:0">
<div style="max-width:700px;margin:0 auto;padding:24px 20px">
  <div style="border-bottom:2px solid #00d4aa;padding-bottom:16px;margin-bottom:24px">
    <div style="font-family:monospace;font-size:11px;color:#00d4aa;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px">Pluggy Família</div>
    <h1 style="font-size:22px;color:#e8f0f8;margin:0 0 4px">Relatório do Dia</h1>
    <div style="font-size:14px;color:#8899aa;text-transform:capitalize">{date_label}</div>
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border-spacing:8px 0;margin-bottom:28px">
    <tr>
      <td style="background:#111418;border:1px solid #1f2833;border-radius:4px;padding:12px;width:25%">
        <div style="font-size:9px;color:#8899aa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px">Entradas</div>
        <div style="font-size:16px;font-family:monospace;font-weight:700;color:#26a69a">+{_fmt_brl(income)}</div>
      </td>
      <td style="background:#111418;border:1px solid #1f2833;border-radius:4px;padding:12px;width:25%">
        <div style="font-size:9px;color:#8899aa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px">Discricionário</div>
        <div style="font-size:16px;font-family:monospace;font-weight:700;color:#ef5350">−{_fmt_brl(disc_exp)}</div>
      </td>
      <td style="background:#111418;border:1px solid #1f2833;border-radius:4px;padding:12px;width:25%">
        <div style="font-size:9px;color:#8899aa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px">Vinculado</div>
        <div style="font-size:16px;font-family:monospace;font-weight:700;color:#9c6fe4">−{_fmt_brl(fixed_exp)}</div>
      </td>
      <td style="background:#111418;border:1px solid #1f2833;border-radius:4px;padding:12px;width:25%">
        <div style="font-size:9px;color:#8899aa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px">Saldo do Dia</div>
        <div style="font-size:16px;font-family:monospace;font-weight:700;color:{bal_color}">{bal_sign}{_fmt_brl(abs(balance))}</div>
      </td>
    </tr>
  </table>
  {owner_sections}
  {cal_html}
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #1f2833;font-size:10px;color:#8899aa;font-family:monospace;text-align:center">
    Pluggy Família · Relatório gerado automaticamente
  </div>
</div>
</body></html>'''


@app.route('/api/connect-token', methods=['POST'])
def api_connect_token():
    try:
        body    = request.get_json(silent=True) or {}
        item_id = body.get('itemId')
        payload = {'itemId': item_id} if item_id else {}
        r = requests.post(f'{PLUGGY_BASE}/connect_token', json=payload,
                          headers={'X-API-KEY': get_api_key()}, timeout=20)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investments')
def api_investments():
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT * FROM investments').fetchall()
        return jsonify({'results': [dict(r) for r in rows], 'total': len(rows)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investments/sync', methods=['POST'])
def api_investments_sync():
    try:
        total = run_investments_sync()
        return jsonify({'ok': True, 'investments_updated': total})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _fetch_month_txns(iso_day):
    if not iso_day or len(iso_day) < 7:
        return []
    ym   = iso_day[:7]
    yr   = int(iso_day[:4])
    mo   = int(iso_day[5:7])
    last = _cal.monthrange(yr, mo)[1]
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT t.*, COALESCE(a.owner,'') AS owner_name
               FROM transactions t
               LEFT JOIN accounts a ON t.account_id = a.id
               WHERE t.date >= ? AND t.date <= ? AND (t.ignored IS NULL OR t.ignored = 0)
               ORDER BY t.date''',
            (f'{ym}-01', f'{ym}-{last:02d}T23:59:59'),
        ).fetchall()
    return [dict(r) for r in rows]


@app.route('/api/report-preview', methods=['POST'])
def api_report_preview():
    try:
        body       = request.get_json(silent=True) or {}
        date_label = body.get('date', '')
        txns       = body.get('transactions', [])
        iso_day    = body.get('isoDay', '')
        month_txns = _fetch_month_txns(iso_day)
        return jsonify({'html': _build_report_html(date_label, txns, month_txns=month_txns, iso_day=iso_day)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/send-report', methods=['POST'])
def api_send_report():
    try:
        if not SMTP_USER or not SMTP_PASS or SMTP_PASS == 'sua_app_password_aqui':
            return jsonify({'error': 'Configure SMTP_USER e SMTP_PASS válidos no arquivo .env'}), 400
        body       = request.get_json(silent=True) or {}
        date_label = body.get('date', '')
        txns       = body.get('transactions', [])
        iso_day    = body.get('isoDay', '')
        to_list    = [e for e in (body.get('recipients') or RECIPIENTS) if e in RECIPIENTS]
        if not to_list:
            return jsonify({'error': 'Nenhum destinatário válido selecionado'}), 400
        month_txns     = _fetch_month_txns(iso_day)
        html           = _build_report_html(date_label, txns, month_txns=month_txns, iso_day=iso_day)
        msg            = MIMEMultipart('alternative')
        msg['Subject'] = f'Relatório Financeiro — {date_label}'
        msg['From']    = SMTP_USER
        msg['To']      = ', '.join(to_list)
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=20) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(SMTP_USER, to_list, msg.as_string())
        return jsonify({'ok': True, 'recipients': to_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio', methods=['GET'])
def api_portfolio_list():
    try:
        with get_db() as conn:
            rows = conn.execute('''
                SELECT
                    pa.id, pa.pluggy_id, pa.name, pa.subtype,
                    pa.renda_tipo, pa.indexador, pa.vencimento,
                    pa.code, pa.isin, pa.currency_code,
                    pa.institution, pa.owner, pa.status,
                    pa.periodicidade, pa.dy, pa.data_pagamento, pa.liquidez,
                    pa.created_at, pa.updated_at,
                    COALESCE(CASE WHEN inv.status='ACTIVE' THEN inv.balance     END, pa.balance)     AS balance,
                    COALESCE(CASE WHEN inv.status='ACTIVE' THEN inv.amount      END, pa.amount)      AS amount,
                    COALESCE(CASE WHEN inv.status='ACTIVE' THEN inv.annual_rate END, pa.annual_rate) AS annual_rate,
                    COALESCE(CASE WHEN inv.status='ACTIVE' THEN inv.quantity    END, pa.quantity)    AS quantity,
                    COALESCE(CASE WHEN inv.status='ACTIVE' THEN inv.value       END, pa.value)       AS value,
                    CASE WHEN inv.status='ACTIVE' THEN inv.taxes                END AS taxes,
                    CASE WHEN inv.status='ACTIVE' THEN inv.last_month_rate      END AS last_month_rate,
                    CASE WHEN inv.status='ACTIVE' THEN inv.last_twelve_months_rate END AS last_twelve_months_rate,
                    CASE WHEN inv.status='ACTIVE' THEN inv.date                 END AS pluggy_last_date
                FROM portfolio_assets pa
                LEFT JOIN investments inv ON pa.pluggy_id = inv.id
                ORDER BY pa.renda_tipo, pa.name
            ''').fetchall()
        return jsonify({'results': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio', methods=['POST'])
def api_portfolio_create():
    try:
        body = request.get_json(silent=True) or {}
        name = (body.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Nome é obrigatório'}), 400
        now = datetime.now().isoformat()
        with get_db() as conn:
            # Verifica duplicata manual (nome + instituição)
            if not body.get('pluggy_id'):
                dup = conn.execute(
                    'SELECT id FROM portfolio_assets WHERE pluggy_id IS NULL AND LOWER(name)=LOWER(?) AND LOWER(institution)=LOWER(?)',
                    (name, body.get('institution', ''))
                ).fetchone()
                if dup:
                    return jsonify({'error': 'Já existe um ativo com este nome e instituição na carteira'}), 409
            cur = conn.execute(
                '''INSERT INTO portfolio_assets
                   (pluggy_id, name, subtype, renda_tipo, indexador, vencimento,
                    balance, amount, annual_rate, quantity, value,
                    code, isin, currency_code, institution, owner, status,
                    periodicidade, dy, data_pagamento, liquidez, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (body.get('pluggy_id'), name,
                 body.get('subtype', ''), body.get('renda_tipo', 'FIXA'),
                 body.get('indexador', ''), body.get('vencimento', ''),
                 body.get('balance', 0), body.get('amount', 0),
                 body.get('annual_rate'), body.get('quantity'), body.get('value'),
                 body.get('code', ''), body.get('isin', ''),
                 body.get('currency_code', 'BRL'),
                 body.get('institution', ''), body.get('owner', ''),
                 body.get('status', 'ACTIVE'),
                 body.get('periodicidade', ''), body.get('dy'),
                 body.get('data_pagamento', ''), 1 if body.get('liquidez') else 0,
                 now, now)
            )
            row = conn.execute('SELECT * FROM portfolio_assets WHERE id=?', (cur.lastrowid,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/<int:asset_id>', methods=['PUT'])
def api_portfolio_update(asset_id):
    try:
        body = request.get_json(silent=True) or {}
        name = (body.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Nome é obrigatório'}), 400
        now = datetime.now().isoformat()
        with get_db() as conn:
            conn.execute(
                '''UPDATE portfolio_assets SET
                   name=?, subtype=?, renda_tipo=?, indexador=?, vencimento=?,
                   balance=?, amount=?, annual_rate=?, quantity=?, value=?,
                   code=?, isin=?, currency_code=?, institution=?, owner=?, status=?,
                   periodicidade=?, dy=?, data_pagamento=?, liquidez=?, updated_at=?
                   WHERE id=?''',
                (name, body.get('subtype', ''), body.get('renda_tipo', 'FIXA'),
                 body.get('indexador', ''), body.get('vencimento', ''),
                 body.get('balance', 0), body.get('amount', 0),
                 body.get('annual_rate'), body.get('quantity'), body.get('value'),
                 body.get('code', ''), body.get('isin', ''),
                 body.get('currency_code', 'BRL'),
                 body.get('institution', ''), body.get('owner', ''),
                 body.get('status', 'ACTIVE'),
                 body.get('periodicidade', ''), body.get('dy'),
                 body.get('data_pagamento', ''), 1 if body.get('liquidez') else 0,
                 now, asset_id)
            )
            row = conn.execute('SELECT * FROM portfolio_assets WHERE id=?', (asset_id,)).fetchone()
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/<int:asset_id>', methods=['DELETE'])
def api_portfolio_delete(asset_id):
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM portfolio_asset_cashflows WHERE asset_id=?', (asset_id,))
            conn.execute('DELETE FROM portfolio_assets WHERE id=?', (asset_id,))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def generate_cashflow_dates(start_date_str, end_date_str, periodicity):
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except Exception:
        return []

    if start_date > end_date:
        return []

    if periodicity == 'Único':
        return [start_date.strftime('%Y-%m-%d')]

    dates = []
    curr = start_date

    step_months = {
        'Mensal': 1,
        'Bimestral': 2,
        'Trimestral': 3,
        'Semestral': 6,
        'Anual': 12
    }.get(periodicity, 1)

    while curr <= end_date:
        dates.append(curr.strftime('%Y-%m-%d'))
        
        # Calculate next month index
        month_idx = curr.month - 1 + step_months
        year_offset = month_idx // 12
        new_month = (month_idx % 12) + 1
        new_year = curr.year + year_offset
        
        # Check max days in that month and anchor to start_date.day
        max_day = _cal.monthrange(new_year, new_month)[1]
        new_day = min(start_date.day, max_day)
        curr = _date(new_year, new_month, new_day)

    return dates


@app.route('/api/portfolio/<int:asset_id>/cashflows', methods=['GET'])
def api_portfolio_cashflows_get(asset_id):
    try:
        with get_db() as conn:
            asset = conn.execute('SELECT name FROM portfolio_assets WHERE id=?', (asset_id,)).fetchone()
            if not asset:
                return jsonify({'error': 'Ativo não encontrado'}), 404
            
            rows = conn.execute('SELECT * FROM portfolio_asset_cashflows WHERE asset_id=? ORDER BY start_date', (asset_id,)).fetchall()
            
        series = [dict(r) for r in rows]
        
        juros_dates = set()
        amortizacao_dates = set()
        
        for s in series:
            dates = generate_cashflow_dates(s['start_date'], s['end_date'], s['periodicity'])
            if s['type'] == 'JUROS':
                juros_dates.update(dates)
            else:
                amortizacao_dates.update(dates)
                
        return jsonify({
            'series': series,
            'juros_dates': sorted(list(juros_dates)),
            'amortizacao_dates': sorted(list(amortizacao_dates))
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/<int:asset_id>/cashflows', methods=['POST'])
def api_portfolio_cashflows_save(asset_id):
    try:
        body = request.get_json(silent=True) or {}
        series = body.get('series', [])
        
        with get_db() as conn:
            asset = conn.execute('SELECT name FROM portfolio_assets WHERE id=?', (asset_id,)).fetchone()
            if not asset:
                return jsonify({'error': 'Ativo não encontrado'}), 404
            
            conn.execute('DELETE FROM portfolio_asset_cashflows WHERE asset_id=?', (asset_id,))
            for s in series:
                conn.execute(
                    '''INSERT INTO portfolio_asset_cashflows (asset_id, type, start_date, end_date, periodicity)
                       VALUES (?,?,?,?,?)''',
                    (asset_id, s['type'], s['start_date'], s['end_date'], s['periodicity'])
                )
            
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/portfolio/<int:asset_id>/liquidez', methods=['POST'])
def api_portfolio_toggle_liquidez(asset_id):
    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE portfolio_assets SET liquidez = CASE WHEN liquidez=1 THEN 0 ELSE 1 END WHERE id=?',
                (asset_id,)
            )
            row = conn.execute('SELECT liquidez FROM portfolio_assets WHERE id=?', (asset_id,)).fetchone()
        return jsonify({'id': asset_id, 'liquidez': bool(row['liquidez'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/pending-import', methods=['GET'])
def api_portfolio_pending():
    try:
        with get_db() as conn:
            imported_ids = {r[0] for r in conn.execute(
                'SELECT pluggy_id FROM portfolio_assets WHERE pluggy_id IS NOT NULL'
            ).fetchall()}
            rows = conn.execute(
                'SELECT * FROM investments WHERE status=? ORDER BY type, name',
                ('ACTIVE',)
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d['already_imported'] = d['id'] in imported_ids
            results.append(d)
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Contingência: importação manual de transações de cartão ───────────────────

_MONTHS_PT = {
    'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8, 'setembro': 9,
    'outubro': 10, 'novembro': 11, 'dezembro': 12,
}
_SKIP_WORDS = {
    'segunda-feira', 'terça-feira', 'terca-feira', 'quarta-feira',
    'quinta-feira', 'sexta-feira', 'sábado', 'sabado', 'domingo',
    'ontem', 'hoje', 'anteontem', 'undefined', '',
}
_DATE_RE    = re.compile(r'^(\d{1,2})\s+de\s+([a-zà-ú]+)$', re.IGNORECASE)
_AMOUNT_RE  = re.compile(r'^([+-])\s*([A-Za-z]{1,3}\$)\s*([\d.]+,\d{2})$')
_INSTALL_RE = re.compile(r'\((\d+/\d+)\)')


def _parse_brl_number(s):
    return float(s.replace('.', '').replace(',', '.'))


def _resolve_cont_year(month_num, anchor_year, anchor_month):
    # Extratos vão para trás no tempo a partir do mês âncora; só vira o ano
    # quando o mês da transação está muito à frente do âncora (ex.: Dez numa
    # pasta de Janeiro).
    if month_num - anchor_month > 6:
        return anchor_year - 1
    return anchor_year


def _parse_contingencia_file(path, anchor_year, anchor_month):
    with open(path, encoding='utf-8') as f:
        lines = [ln.strip() for ln in f if ln.strip() != '']

    out = []
    current_date = None
    occ = {}
    i = 0
    while i < len(lines):
        ln  = lines[i]
        low = ln.lower()

        m = _DATE_RE.match(ln)
        if m:
            mon = _MONTHS_PT.get(m.group(2).lower())
            if mon:
                yr = _resolve_cont_year(mon, anchor_year, anchor_month)
                current_date = f'{yr:04d}-{mon:02d}-{int(m.group(1)):02d}'
            i += 1
            continue

        if low in _SKIP_WORDS:
            i += 1
            continue

        # `ln` = descrição (merchant); as 2 linhas seguintes = rótulo e valor
        if i + 2 < len(lines) and current_date:
            desc, kind, amt_l = ln, lines[i + 1], lines[i + 2]
            am = _AMOUNT_RE.match(amt_l)
            if am:
                sign, cur, num = am.groups()
                raw_currency = 'BRL' if cur in ('R$', 'r$') else ('USD' if cur.upper() == 'US$' else cur.replace('$', '').upper())
                raw_amount   = _parse_brl_number(num)
                txn_type     = 'CREDIT' if sign == '+' else 'DEBIT'
                inst_m       = _INSTALL_RE.search(desc) or _INSTALL_RE.search(kind)
                key = (current_date, desc, f'{raw_amount:.2f}', txn_type)
                occ[key] = occ.get(key, 0) + 1
                out.append({
                    'date': current_date,
                    'description': desc,
                    'type': txn_type,
                    'raw_amount': raw_amount,
                    'raw_currency': raw_currency,
                    'amount': raw_amount if raw_currency == 'BRL' else None,
                    'kind_label': kind,
                    'installment': inst_m.group(1) if inst_m else '',
                    'occ': occ[key],
                })
                i += 3
                continue

        i += 1

    return out


def _contingencia_id(t):
    # sem o nome do arquivo: a mesma transação em arquivos diferentes gera o
    # mesmo id, então reimportar de outro extrato não duplica.
    base = f"{t['date']}|{t['description']}|{t['raw_amount']:.2f}|{t['type']}|{t['occ']}"
    return 'cont_' + hashlib.sha1(base.encode('utf-8')).hexdigest()[:24]


def _cont_dup_flags(conn, row):
    """Retorna (dup_pluggy, dup_cont): se bate com uma transação da Pluggy e/ou
    com outra transação de contingência (mesma conta, dia, valor e tipo)."""
    if row['amount'] is None:
        return 0, 0
    pluggy = conn.execute(
        '''SELECT 1 FROM transactions
           WHERE account_id = ? AND SUBSTR(date,1,10) = ?
             AND ROUND(ABS(amount),2) = ROUND(ABS(?),2) AND type = ?
           LIMIT 1''',
        (row['account_id'], row['date'][:10], row['amount'], row['type'])
    ).fetchone()
    cont = conn.execute(
        '''SELECT 1 FROM contingencia_txns
           WHERE id <> ? AND account_id = ? AND date = ?
             AND ROUND(ABS(amount),2) = ROUND(ABS(?),2) AND type = ?
           LIMIT 1''',
        (row['id'], row['account_id'], row['date'][:10], row['amount'], row['type'])
    ).fetchone()
    return (1 if pluggy else 0), (1 if cont else 0)


@app.route('/api/contingencia/files')
def api_contingencia_files():
    try:
        out = []
        if os.path.isdir(CONTINGENCIA_DIR):
            for p in sorted(glob.glob(os.path.join(CONTINGENCIA_DIR, '*', '*.txt'))):
                rel = os.path.relpath(p, CONTINGENCIA_DIR).replace('\\', '/')
                out.append({'path': rel, 'size': os.path.getsize(p)})
        return jsonify({'results': out})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/contingencia/import', methods=['POST'])
def api_contingencia_import():
    try:
        body       = request.get_json(silent=True) or {}
        rel        = (body.get('path') or '').replace('\\', '/').lstrip('/')
        account_id = body.get('account_id') or ''
        if not rel or '..' in rel:
            return jsonify({'error': 'arquivo inválido'}), 400
        full = os.path.join(CONTINGENCIA_DIR, *rel.split('/'))
        if not os.path.isfile(full):
            return jsonify({'error': 'arquivo não encontrado'}), 404

        folder = rel.split('/')[0]
        mm = re.match(r'^(\d{4})(\d{2})$', folder)
        if not mm:
            return jsonify({'error': f'pasta "{folder}" não está no formato AAAAMM'}), 400
        anchor_year, anchor_month = int(mm.group(1)), int(mm.group(2))

        with get_db() as conn:
            acc = conn.execute('SELECT id, name, type, owner FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not acc:
                return jsonify({'error': 'conta não encontrada'}), 400

            parsed = _parse_contingencia_file(full, anchor_year, anchor_month)
            imported = skipped = pending_intl = 0
            now = datetime.now().isoformat()
            for t in parsed:
                cid = _contingencia_id(t)
                if conn.execute('SELECT 1 FROM contingencia_txns WHERE id=?', (cid,)).fetchone():
                    skipped += 1
                    continue
                if t['amount'] is None:
                    pending_intl += 1
                conn.execute(
                    '''INSERT INTO contingencia_txns
                       (id, account_id, account_name, account_type, owner, date, description,
                        amount, type, raw_amount, raw_currency, installment, kind_label,
                        source_file, imported_at, validada, validada_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'')''',
                    (cid, acc['id'], acc['name'], acc['type'], acc['owner'] or '',
                     t['date'], t['description'], t['amount'], t['type'],
                     t['raw_amount'], t['raw_currency'], t['installment'], t['kind_label'],
                     rel, now)
                )
                imported += 1

        return jsonify({
            'ok': True,
            'parsed_total': len(parsed),
            'imported': imported,
            'skipped_existing': skipped,
            'international_pending': pending_intl,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/contingencia')
def api_contingencia_list():
    try:
        date_from = request.args.get('from')
        date_to   = request.args.get('to')
        q, p = 'SELECT * FROM contingencia_txns WHERE 1=1', []
        if date_from:
            q += ' AND date >= ?'; p.append(date_from[:10])
        if date_to:
            q += ' AND date <= ?'; p.append(date_to[:10])
        q += ' ORDER BY date DESC, description'
        with get_db() as conn:
            rows = conn.execute(q, p).fetchall()
            out  = []
            for r in rows:
                d = dict(r)
                d['dup_suspeita'], d['dup_cont'] = _cont_dup_flags(conn, r)
                d['tag_ids'] = conn.execute(
                    'SELECT GROUP_CONCAT(tag_id) FROM transaction_tags WHERE transaction_id=?', (r['id'],)
                ).fetchone()[0] or ''
                d['conjunto_ids'] = conn.execute(
                    'SELECT GROUP_CONCAT(conjunto_id) FROM transaction_conjuntos WHERE transaction_id=?', (r['id'],)
                ).fetchone()[0] or ''
                out.append(d)
        return jsonify({'results': out})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/contingencia/<cid>', methods=['POST'])
def api_contingencia_update(cid):
    try:
        body = request.get_json(silent=True) or {}
        with get_db() as conn:
            row = conn.execute('SELECT * FROM contingencia_txns WHERE id=?', (cid,)).fetchone()
            if not row:
                return jsonify({'error': 'não encontrada'}), 404

            fields, vals = [], []
            for k in ('description', 'date', 'type'):
                if k in body and body[k] not in (None, ''):
                    fields.append(f'{k}=?'); vals.append(body[k])
            if 'amount' in body:
                amt = body['amount']
                fields.append('amount=?'); vals.append(None if amt in (None, '') else float(amt))
            if fields:
                conn.execute(f'UPDATE contingencia_txns SET {", ".join(fields)} WHERE id=?', (*vals, cid))
                row = conn.execute('SELECT * FROM contingencia_txns WHERE id=?', (cid,)).fetchone()

            if 'validada' in body:
                want = 1 if body['validada'] else 0
                if want and row['amount'] is None:
                    return jsonify({'error': 'Informe o valor em reais antes de validar esta transação internacional.'}), 400
                conn.execute(
                    'UPDATE contingencia_txns SET validada=?, validada_at=? WHERE id=?',
                    (want, datetime.now().isoformat() if want else '', cid)
                )

            row = conn.execute('SELECT * FROM contingencia_txns WHERE id=?', (cid,)).fetchone()
            d = dict(row)
            d['dup_suspeita'], d['dup_cont'] = _cont_dup_flags(conn, row)
            d['tag_ids'] = conn.execute(
                'SELECT GROUP_CONCAT(tag_id) FROM transaction_tags WHERE transaction_id=?', (cid,)
            ).fetchone()[0] or ''
            d['conjunto_ids'] = conn.execute(
                'SELECT GROUP_CONCAT(conjunto_id) FROM transaction_conjuntos WHERE transaction_id=?', (cid,)
            ).fetchone()[0] or ''
        return jsonify(d)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _cont_purge_ids(conn, ids):
    """Remove as linhas de contingência e seus vínculos de tag/conjunto."""
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    conn.execute(f'DELETE FROM transaction_tags       WHERE transaction_id IN ({ph})', ids)
    conn.execute(f'DELETE FROM transaction_conjuntos  WHERE transaction_id IN ({ph})', ids)
    return conn.execute(f'DELETE FROM contingencia_txns WHERE id IN ({ph})', ids).rowcount


@app.route('/api/contingencia/<cid>', methods=['DELETE'])
def api_contingencia_delete(cid):
    try:
        with get_db() as conn:
            _cont_purge_ids(conn, [cid])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/contingencia/delete-bulk', methods=['POST'])
def api_contingencia_delete_bulk():
    try:
        body = request.get_json(silent=True) or {}
        with get_db() as conn:
            if body.get('all'):
                ids = [r[0] for r in conn.execute('SELECT id FROM contingencia_txns')]
            elif body.get('source_file'):
                ids = [r[0] for r in conn.execute(
                    'SELECT id FROM contingencia_txns WHERE source_file=?', (body['source_file'],))]
            elif body.get('ids'):
                ids = list(body['ids'])
            else:
                return jsonify({'error': 'nada para excluir'}), 400
            n = _cont_purge_ids(conn, ids)
        return jsonify({'ok': True, 'deleted': n})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


if __name__ == '__main__':
    print('=' * 52)
    print('  Pluggy Família — http://localhost:5050')
    print('=' * 52)
    app.run(host='0.0.0.0', port=5050, debug=False)
