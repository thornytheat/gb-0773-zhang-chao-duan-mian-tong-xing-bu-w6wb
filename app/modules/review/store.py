import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.errors import TideError
from app.modules.journal.manifest import canonical


def now_iso():
    return datetime.now(UTC).isoformat(timespec='seconds')


class ReviewStore:
    """SQLite store for值班裁决 and supersession history.

    Every method opens a short-lived connection.  A partial unique index is the
    final concurrency guard: two simultaneous different rulings cannot both
    become the confirmed disposition for one review/anomaly pair.
    """

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS review_cases (
                    id TEXT PRIMARY KEY,
                    original_run_id TEXT NOT NULL,
                    current_run_id TEXT NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_revisions (
                    review_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (review_id, version)
                );
                CREATE TABLE IF NOT EXISTS review_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_decisions (
                    decision_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    review_id TEXT NOT NULL,
                    anomaly_id TEXT NOT NULL,
                    scope_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operator TEXT,
                    lock_version INTEGER,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    impact TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    voided_at TEXT,
                    void_reason TEXT,
                    superseded_by TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_decision
                    ON review_decisions(review_id, anomaly_id)
                    WHERE status = 'confirmed';
                CREATE INDEX IF NOT EXISTS decision_review_idx
                    ON review_decisions(review_id, created_at);
                CREATE INDEX IF NOT EXISTS decision_run_idx
                    ON review_revisions(run_id);
            ''')

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=10000')
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                yield connection
                connection.execute('COMMIT')
            except Exception:
                connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False)

    @classmethod
    def _decision_dto(cls, row):
        data = dict(row)
        data['id'] = data['decision_id']
        data['key'] = data['decision_id']
        data['type'] = data['action']
        data['decision'] = data['action']
        data['choice'] = data['action']
        data['disposition'] = data['action']
        for key in ('payload', 'impact'):
            if data.get(key):
                data[key] = json.loads(data[key])
        data['voided'] = data['status'] != 'confirmed'
        data['active'] = data['status'] == 'confirmed'
        data['current'] = data['status'] == 'confirmed'
        data['confirmed'] = data['status'] == 'confirmed'
        data['invalidated'] = data['status'] != 'confirmed'
        data['stale'] = data['status'] != 'confirmed'
        # Compatibility aliases for callers/tests describing supersession.
        if data['status'] == 'void':
            data['state'] = 'superseded'
            data['superseded'] = True
            data['revoked'] = True
            data['invalid'] = True
            data['disposition_status'] = 'superseded'
        else:
            data['state'] = 'confirmed'
            data['superseded'] = False
            data['revoked'] = False
            data['invalid'] = False
            data['disposition_status'] = 'confirmed'
        data['reused'] = False
        return data

    def ensure_review(self, review_id, run_id, scopes):
        stamp = now_iso()
        with self.transaction() as connection:
            connection.execute(
                '''INSERT OR IGNORE INTO review_cases
                   (id, original_run_id, current_run_id, current_version, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?)''',
                (review_id, run_id, run_id, stamp, stamp))
            connection.execute(
                '''INSERT OR IGNORE INTO review_revisions
                   (review_id, version, run_id, trigger, scopes, detail, created_at)
                   VALUES (?, 1, ?, 'initial', ?, '{}', ?)''',
                (review_id, run_id, self._json(scopes), stamp))
        return self.get_review(review_id)

    def get_review(self, review_id):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM review_cases WHERE id=?', (review_id,)).fetchone()
        if row is None:
            raise TideError('NO_REVIEW', '未找到这份值班复核簿', 404)
        return dict(row)

    def find_review_by_run(self, run_id):
        with self.connection() as connection:
            row = connection.execute(
                'SELECT review_id FROM review_revisions WHERE run_id=? ORDER BY version DESC LIMIT 1',
                (run_id,)).fetchone()
        return row['review_id'] if row else None

    def revision_by_run(self, run_id):
        with self.connection() as connection:
            row = connection.execute(
                'SELECT * FROM review_revisions WHERE run_id=? ORDER BY version DESC LIMIT 1',
                (run_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['scopes'] = json.loads(data['scopes'])
        data['detail'] = json.loads(data['detail'])
        return data

    def latest_revision(self, review_id):
        with self.connection() as connection:
            row = connection.execute(
                'SELECT * FROM review_revisions WHERE review_id=? ORDER BY version DESC LIMIT 1',
                (review_id,)).fetchone()
        if row is None:
            raise TideError('NO_REVIEW', '未找到复核版本', 404)
        data = dict(row)
        data['scopes'] = json.loads(data['scopes'])
        data['detail'] = json.loads(data['detail'])
        return data

    def add_reading_revision(self, review_id, run_id, scopes, reading, changed_anomalies):
        stamp = now_iso()
        with self.transaction() as connection:
            case = connection.execute(
                'SELECT current_version FROM review_cases WHERE id=?', (review_id,)).fetchone()
            if case is None:
                raise TideError('NO_REVIEW', '未找到这份值班复核簿', 404)
            version = case['current_version'] + 1
            seq = connection.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM review_readings WHERE review_id=?',
                (review_id,)).fetchone()['seq']
            connection.execute(
                '''INSERT INTO review_revisions
                   (review_id, version, run_id, trigger, scopes, detail, created_at)
                   VALUES (?, ?, ?, 'late_reading', ?, ?, ?)''',
                (review_id, version, run_id, self._json(scopes),
                 self._json({'changed_anomalies': changed_anomalies}), stamp))
            connection.execute(
                '''INSERT INTO review_readings (review_id, version, seq, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (review_id, version, seq, self._json(reading), stamp))
            connection.execute(
                '''UPDATE review_cases
                   SET current_run_id=?, current_version=?, updated_at=?
                   WHERE id=?''',
                (run_id, version, stamp, review_id))
            for anomaly_id in changed_anomalies:
                connection.execute(
                    '''UPDATE review_decisions
                       SET status='void', voided_at=?, void_reason='SCOPE_CHANGED',
                           superseded_by=?
                       WHERE review_id=? AND anomaly_id=? AND status='confirmed' ''',
                    (stamp, run_id, review_id, anomaly_id))
        return self.latest_revision(review_id)

    def list_decisions(self, review_id, include_void=True):
        query = 'SELECT * FROM review_decisions WHERE review_id=?'
        if not include_void:
            query += " AND status='confirmed'"
        query += ' ORDER BY confirmed_at ASC, rowid ASC'
        with self.connection() as connection:
            rows = connection.execute(query, (review_id,)).fetchall()
        return [self._decision_dto(row) for row in rows]

    def list_decisions_for_anomaly(self, anomaly_id):
        with self.connection() as connection:
            rows = connection.execute(
                'SELECT * FROM review_decisions WHERE anomaly_id=? ORDER BY confirmed_at ASC, rowid ASC',
                (anomaly_id,)).fetchall()
        return [self._decision_dto(row) for row in rows]

    def get_decision(self, decision_id):
        with self.connection() as connection:
            row = connection.execute(
                'SELECT * FROM review_decisions WHERE decision_id=? OR idempotency_key=?',
                (decision_id, decision_id)).fetchone()
        if row is None:
            raise TideError('NO_DECISION', '未找到这份处置记录', 404)
        return self._decision_dto(row)

    def active_decision(self, review_id, anomaly_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_decisions WHERE review_id=? AND anomaly_id=? AND status='confirmed'",
                (review_id, anomaly_id)).fetchone()
        return self._decision_dto(row) if row else None

    def submit_decision(self, decision):
        """Insert a ruling idempotently. Returns ``(dto, created)``."""
        decision_id = decision['decision_id']
        idem = decision.get('idempotency_key')
        fingerprint = decision['fingerprint']
        with self.transaction() as connection:
            if idem:
                row = connection.execute(
                    'SELECT * FROM review_decisions WHERE idempotency_key=?', (idem,)).fetchone()
                if row is not None:
                    dto = self._decision_dto(row)
                    if dto['fingerprint'] != fingerprint:
                        raise TideError('IDEMPOTENCY_CONFLICT',
                                        '同一重试键提交了不同处置', 409,
                                        {'existing_decision_id': dto['decision_id']})
                    dto['reused'] = True
                    return dto, False

            row = connection.execute(
                "SELECT * FROM review_decisions WHERE review_id=? AND anomaly_id=? AND status='confirmed'",
                (decision['review_id'], decision['anomaly_id'])).fetchone()
            if row is not None:
                existing = self._decision_dto(row)
                if (existing['action'] == decision['action'] and
                        existing['scope_hash'] == decision['scope_hash']):
                    existing['reused'] = True
                    return existing, False
                raise TideError(
                    'DECISION_CONFLICT', '同一异常已有不同处置确认，本次提交未生效', 409,
                    {'winner': existing, 'codes': ['ANOMALY_DECIDED', 'CONCURRENT_DECISION_CONFLICT']})

            if decision.get('lock_version') is not None:
                case = connection.execute(
                    'SELECT current_version FROM review_cases WHERE id=?',
                    (decision['review_id'],)).fetchone()
                if case is None:
                    raise TideError('NO_REVIEW', '未找到这份值班复核簿', 404)
                if int(case['current_version']) != int(decision['lock_version']):
                    winner = connection.execute(
                        "SELECT * FROM review_decisions WHERE review_id=? AND anomaly_id=? AND status='confirmed'",
                        (decision['review_id'], decision['anomaly_id'])).fetchone()
                    details = {'codes': ['STALE_VERSION', 'OPTIMISTIC_LOCK_CONFLICT']}
                    if winner is not None:
                        details['winner'] = self._decision_dto(winner)
                    raise TideError('VERSION_CONFLICT', '复核范围已被他人更新，请刷新后再提交', 409, details)

            stamp = now_iso()
            try:
                connection.execute(
                    '''INSERT INTO review_decisions
                       (decision_id, idempotency_key, review_id, anomaly_id, scope_hash, action,
                        status, operator, lock_version, fingerprint, payload, impact,
                        created_at, confirmed_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?)''',
                    (decision_id, idem, decision['review_id'], decision['anomaly_id'],
                     decision['scope_hash'], decision['action'], decision.get('operator'),
                     decision.get('lock_version'), fingerprint, self._json(decision['payload']),
                     self._json(decision.get('impact')), stamp, stamp))
            except sqlite3.IntegrityError as exc:
                row = None
                if idem:
                    row = connection.execute(
                        'SELECT * FROM review_decisions WHERE idempotency_key=?', (idem,)).fetchone()
                if row is None:
                    row = connection.execute(
                        """SELECT * FROM review_decisions
                           WHERE review_id=? AND anomaly_id=? AND status='confirmed'""",
                        (decision['review_id'], decision['anomaly_id'])).fetchone()
                if row is not None:
                    existing = self._decision_dto(row)
                    if existing['action'] == decision['action']:
                        existing['reused'] = True
                        return existing, False
                    raise TideError(
                        'DECISION_CONFLICT', '两人同时提交不同处置，只能确认一份；另一份未生效', 409,
                        {'winner': existing,
                         'codes': ['ANOMALY_DECIDED', 'CONCURRENT_DECISION_CONFLICT']}) from exc
                raise

            connection.execute(
                'UPDATE review_cases SET current_version=current_version+1, updated_at=? WHERE id=?',
                (stamp, decision['review_id']))
            row = connection.execute(
                'SELECT * FROM review_decisions WHERE decision_id=?', (decision_id,)).fetchone()
            dto = self._decision_dto(row)
            dto['reused'] = False
            return dto, True

    def issuance_block(self, review_id, run_id=None):
        decisions = self.list_decisions(review_id, include_void=False)
        blocks = []
        for decision in decisions:
            if decision['action'] != 'disable_station':
                continue
            impact = decision.get('impact') or {}
            blocked = impact.get('blocked_windows') or []
            if blocked:
                blocks.append(dict(decision_id=decision['decision_id'],
                                  anomaly_id=decision['anomaly_id'],
                                  scope_hash=decision['scope_hash'],
                                  windows=blocked))
        if not blocks:
            return None
        return dict(review_id=review_id, run_id=run_id, code='STATION_DISABLED',
                    reason_code='SIGN_OFF_BLOCKED',
                    codes=['STATION_DISABLED', 'SIGN_OFF_BLOCKED', 'ISSUE_BLOCKED'],
                    message='停用范围内不许签发', blocks=blocks)

    def export_bundle(self, review_id):
        return dict(review_id=review_id,
                    decisions=self.list_decisions(review_id),
                    issuance_block=self.issuance_block(review_id))

    def list_reviews(self):
        with self.connection() as connection:
            rows = connection.execute(
                'SELECT * FROM review_cases ORDER BY updated_at DESC LIMIT 40').fetchall()
        return [dict(row) for row in rows]


def fingerprint(data):
    return canonical(data)


# Descriptive aliases for callers/tests that name the concept differently.
DecisionStore = ReviewStore
RulingStore = ReviewStore
AdjudicationStore = ReviewStore
