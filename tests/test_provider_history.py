import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'provider_history.py'
SPEC = importlib.util.spec_from_file_location('provider_history', SCRIPT)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class ProviderHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        (self.home / 'config.toml').write_text('model_provider = "shared"\n', encoding='utf-8')
        self.db = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, '
                               'rollout_path TEXT, archived INTEGER, title TEXT)')
        self.originals = {}

    def add_thread(self, name, provider='legacy-provider', compressed=False, metadata_provider=None):
        suffix = '.jsonl.zst' if compressed else '.jsonl'
        path = self.home / (name + suffix)
        meta = {'type': 'session_meta', 'payload': {'id': name, 'model_provider': provider}}
        if metadata_provider is not None:
            meta['payload']['model_provider'] = metadata_provider
        # Preserve deliberate whitespace, Unicode and CRLF in the synthetic body.
        plain = (json.dumps(meta, ensure_ascii=False).encode('utf-8') + b'\r\n'
                 + '{"type":"event_msg","payload":{"text":"上下文 ✓"}}\r\n'.encode('utf-8'))
        data = tool.pack(path, plain)
        path.write_bytes(data)
        self.originals[name] = data
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?)',
                               (name, provider, str(path), int(name.endswith('2')), 'Sample title'))
        return path

    def run_tool(self, *args):
        return tool.main(['--home', str(self.home), *args])

    def provider(self, name):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            return connection.execute('SELECT model_provider FROM threads WHERE id=?', (name,)).fetchone()[0]

    def backup(self):
        return next((self.home / 'backups').glob('*/manifest.json'))

    def test_preview_has_no_file_or_database_changes(self):
        path = self.add_thread('thread1')
        db_before = self.db.read_bytes()
        result = self.run_tool('--all')
        self.assertEqual(result['changes'], 1)
        self.assertEqual(path.read_bytes(), self.originals['thread1'])
        self.assertEqual(self.db.read_bytes(), db_before)
        self.assertFalse((self.home / 'backups').exists())

    def test_migrate_preserves_body_offsets_archived_and_other_columns(self):
        paths = [self.add_thread('thread1'), self.add_thread('thread2')]
        result = self.run_tool('--all', '--apply', '--offline')
        self.assertEqual(result['migrated'], 2)
        for path in paths:
            original = self.originals[path.stem]
            modified = path.read_bytes()
            self.assertEqual(len(original), len(modified))
            self.assertEqual(original.partition(b'\n')[2], modified.partition(b'\n')[2])
            self.assertEqual(len(original.partition(b'\n')[0]), len(modified.partition(b'\n')[0]))
            self.assertEqual(self.provider(path.stem), 'shared')
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute('SELECT archived,title FROM threads ORDER BY id').fetchall(),
                             [(0, 'Sample title'), (1, 'Sample title')])
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.run_tool('--all', '--apply', '--offline')['changes'], 0)
        self.assertEqual(len(list((self.home / 'backups').iterdir())), 1)

    def test_single_thread_selection(self):
        self.add_thread('thread1')
        self.add_thread('thread2')
        self.run_tool('--thread', 'thread1', '--apply', '--offline')
        self.assertEqual(self.provider('thread1'), 'shared')
        self.assertEqual(self.provider('thread2'), 'legacy-provider')

    def test_metadata_mismatch_is_repaired_even_if_database_matches(self):
        path = self.add_thread('thread1', provider='shared', metadata_provider='legacy-provider')
        self.run_tool('--all', '--apply', '--offline')
        self.assertEqual(json.loads(path.read_bytes().splitlines()[0])['payload']['model_provider'], 'shared')

    def test_compressed_round_trip(self):
        try:
            tool.codec()
        except tool.MigrationError:
            self.skipTest('Zstandard implementation unavailable')
        path = self.add_thread('thread1', compressed=True)
        before = tool.unpack(path, path.read_bytes())
        self.run_tool('--all', '--apply', '--offline')
        after = tool.unpack(path, path.read_bytes())
        self.assertEqual(len(before), len(after))
        self.assertEqual(before.partition(b'\n')[2], after.partition(b'\n')[2])
        self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        self.assertEqual(path.read_bytes(), self.originals['thread1'])

    def test_restore_exact_bytes_is_repeatable_and_keeps_new_threads(self):
        path = self.add_thread('thread1')
        self.run_tool('--all', '--apply', '--offline')
        self.add_thread('thread2', provider='other')
        for _ in range(2):
            self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        self.assertEqual(path.read_bytes(), self.originals['thread1'])
        self.assertEqual(self.provider('thread1'), 'legacy-provider')
        self.assertEqual(self.provider('thread2'), 'other')

    def test_restore_refuses_newer_history_before_changing_anything(self):
        path = self.add_thread('thread1')
        self.run_tool('--all', '--apply', '--offline')
        newer = path.read_bytes() + b'{"type":"new_event"}\n'
        path.write_bytes(newer)
        with self.assertRaises(SystemExit):
            self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        self.assertEqual(path.read_bytes(), newer)
        self.assertEqual(self.provider('thread1'), 'shared')

    def test_recover_crash_between_file_replacement_and_database_commit(self):
        path = self.add_thread('thread1')
        self.run_tool('--all', '--apply', '--offline')
        manifest_path = self.backup()
        manifest = json.loads(manifest_path.read_text())
        manifest['status'] = 'prepared'
        manifest_path.write_text(json.dumps(manifest))
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('UPDATE threads SET model_provider="legacy-provider"')
        self.run_tool('--restore', str(manifest_path), '--apply', '--offline')
        self.assertEqual(path.read_bytes(), self.originals['thread1'])
        self.assertEqual(self.provider('thread1'), 'legacy-provider')

    def test_failed_second_replacement_rolls_back_first_file_and_database(self):
        first = self.add_thread('thread1')
        second = self.add_thread('thread2')
        real_write = tool.atomic_write

        def fail_second(path, data):
            if path == second:
                raise OSError('Synthetic failure')
            real_write(path, data)

        with patch.object(tool, 'atomic_write', side_effect=fail_second):
            with self.assertRaises(SystemExit):
                self.run_tool('--all', '--apply', '--offline')
        self.assertEqual(first.read_bytes(), self.originals['thread1'])
        self.assertEqual(second.read_bytes(), self.originals['thread2'])
        self.assertEqual(self.provider('thread1'), 'legacy-provider')
        self.assertEqual(json.loads(self.backup().read_text())['status'], 'rolled_back')

    def test_corrupt_backup_refused(self):
        self.add_thread('thread1')
        self.run_tool('--all', '--apply', '--offline')
        (self.backup().parent / '000000.before').write_bytes(b'altered')
        with self.assertRaises(SystemExit):
            self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        self.assertEqual(self.provider('thread1'), 'shared')

    def test_missing_identity_and_oversized_target_refused_without_backup(self):
        path = self.add_thread('thread1')
        with self.assertRaises(SystemExit):
            self.run_tool('--all', '--target', 'x' * 1000, '--apply', '--offline')
        self.assertFalse((self.home / 'backups').exists())
        path.write_bytes(path.read_bytes().replace(b'thread1', b'wrongid'))
        with self.assertRaises(SystemExit):
            self.run_tool('--all')
        with self.assertRaises(SystemExit):
            self.run_tool('--thread', 'unknown')

    def test_offline_confirmation_and_existing_lock(self):
        self.add_thread('thread1')
        with self.assertRaises(SystemExit):
            self.run_tool('--all', '--apply')
        (self.home / '.provider-history.lock').write_text('123')
        with self.assertRaises(SystemExit):
            self.run_tool('--all', '--apply', '--offline')

    def test_profile_requires_explicit_target(self):
        self.add_thread('thread1')
        (self.home / 'config.toml').write_text('profile="work"\n')
        with self.assertRaises(SystemExit):
            self.run_tool('--all')
        self.assertEqual(self.run_tool('--all', '--target', 'shared')['changes'], 1)

    def test_multiple_databases_require_explicit_selection(self):
        self.add_thread('thread1')
        (self.home / 'state_6.sqlite').write_bytes(self.db.read_bytes())
        with self.assertRaises(SystemExit):
            self.run_tool('--all')
        self.assertEqual(self.run_tool('--all', '--db', str(self.db))['changes'], 1)

    def test_config_change_before_write_is_detected(self):
        path = self.add_thread('thread1')
        real_save = tool.save_manifest

        def change_config(folder, manifest):
            real_save(folder, manifest)
            if manifest['status'] == 'prepared':
                (self.home / 'config.toml').write_text('model_provider="new-provider"\n')

        with patch.object(tool, 'save_manifest', side_effect=change_config):
            with self.assertRaises(SystemExit):
                self.run_tool('--all', '--apply', '--offline')
        self.assertEqual(path.read_bytes(), self.originals['thread1'])
        self.assertEqual(self.provider('thread1'), 'legacy-provider')

    def test_null_database_provider_can_be_migrated_and_restored(self):
        self.add_thread('thread1')
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('UPDATE threads SET model_provider=NULL')
        self.run_tool('--all', '--apply', '--offline')
        self.assertEqual(self.provider('thread1'), 'shared')
        self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        self.assertIsNone(self.provider('thread1'))

    def test_interrupted_restore_can_be_retried(self):
        first = self.add_thread('thread1')
        second = self.add_thread('thread2')
        self.run_tool('--all', '--apply', '--offline')
        real_write = tool.atomic_write

        def interrupt_second(path, data):
            if path == second:
                raise OSError('Synthetic interrupted restore')
            real_write(path, data)

        with patch.object(tool, 'atomic_write', side_effect=interrupt_second):
            with self.assertRaises(SystemExit):
                self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        self.assertEqual(first.read_bytes(), self.originals['thread1'])
        self.assertEqual(self.provider('thread1'), 'shared')
        self.run_tool('--restore', str(self.backup()), '--apply', '--offline')
        for path in (first, second):
            self.assertEqual(path.read_bytes(), self.originals[path.stem])
            self.assertEqual(self.provider(path.stem), 'legacy-provider')

    def test_database_snapshot_captures_wal_rows(self):
        self.add_thread('thread1')
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('UPDATE threads SET title="Committed WAL title"')
            writer.commit()
            self.run_tool('--all', '--apply', '--offline')
            saved = self.backup().parent / 'database.sqlite'
            with closing(sqlite3.connect(saved)) as backup:
                self.assertEqual(backup.execute('SELECT title FROM threads').fetchone()[0],
                                 'Committed WAL title')

    def test_script_entry_point_outputs_preview_json(self):
        import subprocess
        import sys
        self.add_thread('thread1')
        result = subprocess.run([sys.executable, str(SCRIPT), '--home', str(self.home), '--all'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['changes'], 1)


if __name__ == '__main__':
    unittest.main()
