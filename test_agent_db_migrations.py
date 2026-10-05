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


if __name__ == '__main__':
    unittest.main()
