"""Regression tests for the central DB 1 → 2 migration (projects FK drop).

The guarded ALTER TABLE statements in init_db run *before* the versioned
migrations, so a user_version=1 database always reaches the 1 → 2 step with
post-v2 columns already present. The rebuild must keep all of them (and the
rows in tables that reference projects) while removing the stale FK.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_db


class ProjectsFkMigrationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.td.name, 'aingel.db')
        conn = sqlite3.connect(self.path)
        conn.executescript('''
            CREATE TABLE chats (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                path TEXT NOT NULL,
                budget_monthly REAL DEFAULT 0.0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                aingel_chat_id INTEGER REFERENCES chats(id) ON DELETE SET NULL
            );
            CREATE TABLE roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                system_prompt TEXT DEFAULT '',
                default_model TEXT DEFAULT '',
                context_scope TEXT DEFAULT '',
                is_template INTEGER DEFAULT 0
            );
            INSERT INTO projects (name, slug, path, budget_monthly)
                VALUES ('Legacy', 'legacy', '/tmp/legacy', 42.0);
            INSERT INTO roles (project_id, name, system_prompt)
                VALUES (1, 'Project Role', 'p');
            PRAGMA user_version = 1;
        ''')
        conn.commit()
        conn.close()
        self._patch = patch.object(agent_db, 'DB_PATH', self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def test_v1_database_migrates_without_losing_columns_or_rows(self):
        agent_db.init_db()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0] >= 2, True)
            fks = [r['table'] for r in conn.execute('PRAGMA foreign_key_list(projects)')]
            self.assertNotIn('chats', fks)
            cols = {r['name'] for r in conn.execute('PRAGMA table_info(projects)')}
            # Columns added by the guarded ALTERs before the rebuild survive.
            for col in ('aingel_autopilot', 'scw_session_enabled', 'eu_only'):
                self.assertIn(col, cols)
            row = conn.execute("SELECT * FROM projects WHERE slug='legacy'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['budget_monthly'], 42.0)
            # DROP TABLE projects must not have cascade-deleted project roles.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM roles WHERE project_id=1").fetchone()[0], 1)
            idx = {r['name'] for r in conn.execute('PRAGMA index_list(projects)')}
            self.assertIn('idx_projects_path', idx)
        finally:
            conn.close()

    def test_second_init_is_a_no_op(self):
        agent_db.init_db()
        agent_db.init_db()
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM projects WHERE slug='legacy'").fetchone()[0], 1)
        finally:
            conn.close()


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


class TermsSchemaTests(unittest.TestCase):
    """terms_versions / terms_acceptances are created by init_db() on both a
    fresh DB and the legacy v1 DB, with their uniqueness + FK rules enforced.

    Raw sqlite3 connections here default to foreign_keys=OFF, so every FK
    assertion turns it ON explicitly — otherwise it would pass vacuously.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.td.name, 'aingel.db')
        self._patch = patch.object(agent_db, 'DB_PATH', self.path)

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def _open(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    def _make_fresh(self):
        self._patch.start()
        agent_db.init_db()

    def _make_legacy_v1(self):
        conn = sqlite3.connect(self.path)
        conn.executescript('''
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                path TEXT NOT NULL,
                budget_monthly REAL DEFAULT 0.0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO projects (name, slug, path) VALUES ('Legacy', 'legacy', '/tmp/legacy');
            PRAGMA user_version = 1;
        ''')
        conn.commit()
        conn.close()
        self._patch.start()
        agent_db.init_db()

    def _insert_user(self, conn, sub='s1'):
        conn.execute(
            'INSERT INTO users (oidc_sub, email) VALUES (?, ?)', (sub, sub + '@t'))
        return conn.execute(
            'SELECT id FROM users WHERE oidc_sub=?', (sub,)).fetchone()[0]

    def _insert_version(self, conn, tag='2026-11', lang='en', audience='all'):
        conn.execute(
            'INSERT INTO terms_versions (version_tag, language, audience, '
            'title, content, content_hash) VALUES (?,?,?,?,?,?)',
            (tag, lang, audience, 'T', 'body', 'h-' + tag + lang + audience))
        return conn.execute(
            'SELECT id FROM terms_versions WHERE version_tag=?', (tag,)).fetchone()[0]

    def _assert_schema(self):
        conn = self._open()
        try:
            tables = _tables(conn)
            self.assertIn('terms_versions', tables)
            self.assertIn('terms_acceptances', tables)

            # UNIQUE(version_tag, language, audience)
            self._insert_version(conn, '2026-11', 'en', 'all')
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_version(conn, '2026-11', 'en', 'all')
            # A different language or audience is a different version.
            self._insert_version(conn, '2026-11', 'fr', 'all')
            self._insert_version(conn, '2026-11', 'en', 'member')

            uid = self._insert_user(conn)
            vid = conn.execute(
                "SELECT id FROM terms_versions WHERE language='en' "
                "AND audience='all'").fetchone()[0]
            conn.execute(
                'INSERT INTO terms_acceptances (user_id, version_id) VALUES (?,?)',
                (uid, vid))
            # UNIQUE(user_id, version_id)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    'INSERT INTO terms_acceptances (user_id, version_id) VALUES (?,?)',
                    (uid, vid))
            # Bogus version_id FK rejected (needs foreign_keys=ON — set above).
            uid2 = self._insert_user(conn, 's2')
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    'INSERT INTO terms_acceptances (user_id, version_id) VALUES (?,?)',
                    (uid2, 999999))
            # Deleting an accepted version is rejected: no ON DELETE CASCADE.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM terms_versions WHERE id=?', (vid,))
            # An unaccepted version is deletable.
            conn.execute('DELETE FROM terms_versions WHERE version_tag=? AND language=?',
                         ('2026-11', 'fr'))
            conn.rollback()
        finally:
            conn.close()

    def test_fresh_db_has_terms_schema(self):
        self._make_fresh()
        self._assert_schema()

    def test_legacy_v1_db_gets_terms_schema(self):
        self._make_legacy_v1()
        self._assert_schema()

    def test_init_db_is_idempotent_for_terms(self):
        self._make_fresh()
        agent_db.init_db()
        conn = self._open()
        try:
            self.assertIn('terms_versions', _tables(conn))
            self.assertIn('terms_acceptances', _tables(conn))
            idx = {r['name'] for r in conn.execute(
                'PRAGMA index_list(terms_acceptances)')}
            self.assertIn('idx_terms_acceptances_user', idx)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
