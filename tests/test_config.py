"""Portable directory locking and process-interruption recovery."""

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openevent.chat_app import config


def hold_store(directory, connection):
    store = config.ConfigStore(directory)
    try:
        connection.send("ready")
        connection.recv()
    finally:
        store.close()
        connection.close()


class DirectoryTests(unittest.TestCase):
    def test_nested_directory_and_committed_session_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "new/nested/channels"
            store = config.ConfigStore(directory)
            try:
                pending = store.begin(config.new_ulid(), config.new_ulid(), 123)
                pending = store.record_channel(pending, 456)
                session = store.commit(pending)
                self.assertEqual(list(store.pending.iterdir()), [])
            finally:
                store.close()
            reopened = config.ConfigStore(directory)
            try:
                self.assertEqual(reopened.sessions, {session.session_id: session})
                self.assertEqual(reopened.requests[session.create_request_id], session.session_id)
            finally:
                reopened.close()

    def test_second_store_is_rejected_and_thread_close_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "channels"
            store = config.ConfigStore(directory)
            try:
                with self.assertRaisesRegex(config.ConfigurationError, "already in use"):
                    config.ConfigStore(directory)
                with ThreadPoolExecutor(1) as executor:
                    executor.submit(store.close).result(timeout=5)
                reopened = config.ConfigStore(directory)
                reopened.close()
            finally:
                store.close()

    def test_process_exit_releases_lock_without_stale_lock_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "channels"
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=hold_store, args=(directory, child))
            process.start()
            child.close()
            try:
                self.assertTrue(parent.poll(10), "child did not acquire the configuration lock")
                self.assertEqual(parent.recv(), "ready")
                with self.assertRaisesRegex(config.ConfigurationError, "already in use"):
                    config.ConfigStore(directory)
                process.kill()
                process.join(10)
                self.assertFalse(process.is_alive())
                reopened = config.ConfigStore(directory)
                reopened.close()
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(10)
                process.close()
                parent.close()

    def test_failed_commit_keeps_complete_pending_for_validated_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "channels"
            store = config.ConfigStore(directory)
            try:
                pending = store.begin(config.new_ulid(), config.new_ulid(), 123)
                pending = store.record_channel(pending, 456)
                with patch.object(config.os, "replace", side_effect=OSError("replace failed")):
                    with self.assertRaises(OSError):
                        store.commit(pending)
                self.assertEqual(store.sessions, {})
                self.assertTrue((store.pending / (pending["session_id"] + ".json")).is_file())
            finally:
                store.close()
            reopened = config.ConfigStore(directory)
            try:
                sid = pending["session_id"]
                self.assertEqual(reopened.sessions, {})
                self.assertEqual(reopened.requests, {})
                self.assertEqual(reopened.recoverable[sid].as_json(), pending)
                self.assertTrue((reopened.pending / (sid + ".json")).is_file())
                self.assertFalse((directory / (sid + ".json")).exists())
            finally:
                reopened.close()

    def test_commit_atomically_moves_the_original_complete_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "channels"
            store = config.ConfigStore(directory)
            try:
                pending = store.record_channel(store.begin(config.new_ulid(), config.new_ulid(), 123), 456)
                source = store.pending / (pending["session_id"] + ".json")
                target = directory / source.name
                with patch.object(config.os, "replace", wraps=config.os.replace) as move:
                    session = store.commit(pending)
                move.assert_called_once_with(source, target)
                self.assertEqual(json.loads(target.read_text()), session.as_json())
                self.assertFalse(source.exists())
                self.assertEqual({path.name for path in directory.iterdir() if path.is_file()},
                                 {".lock", target.name})
            finally:
                store.close()

    def test_partial_pending_refuses_startup_without_altering_other_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "channels"
            store = config.ConfigStore(directory)
            full = store.record_channel(store.begin(config.new_ulid(), config.new_ulid(), 123), 456)
            partial = store.begin(config.new_ulid(), config.new_ulid(), 789)
            store.close()
            before = {path.name: path.read_bytes() for path in (directory / ".pending").iterdir()}
            for _ in range(2):
                with self.assertRaisesRegex(config.ConfigurationError, "creation result is uncertain"):
                    config.ConfigStore(directory)
            self.assertEqual({path.name: path.read_bytes() for path in (directory / ".pending").iterdir()}, before)
            self.assertFalse((directory / (full["session_id"] + ".json")).exists())
            self.assertIn(partial["session_id"] + ".json", before)

    def test_duplicates_across_formal_and_pending_or_between_pending_are_rejected(self):
        for collision in ("session_id", "create_request_id", "channel_id"):
            for first_is_committed in (False, True):
                with self.subTest(collision=collision, committed=first_is_committed), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary) / "channels"
                    store = config.ConfigStore(directory)
                    first = store.record_channel(store.begin(config.new_ulid(), config.new_ulid(), 1), 100)
                    if first_is_committed:
                        store.commit(first)
                    second = dict(first, session_id=config.new_ulid(), create_request_id=config.new_ulid(), channel_id="101")
                    second[collision] = first[collision]
                    if not first_is_committed and collision == "session_id":
                        # A second file with the same identity is necessarily also a filename mismatch.
                        filename = config.new_ulid() + ".json"
                    else:
                        filename = second["session_id"] + ".json"
                    (store.pending / filename).write_text(json.dumps(second))
                    store.close()
                    before = {str(path.relative_to(directory)): path.read_bytes()
                              for path in directory.rglob("*.json")}
                    with self.assertRaises(config.ConfigurationError):
                        config.ConfigStore(directory)
                    self.assertEqual({str(path.relative_to(directory)): path.read_bytes()
                                      for path in directory.rglob("*.json")}, before)

    def test_old_config_temp_and_malformed_pending_are_not_repaired(self):
        for filename, body in (("old.config.tmp", "{}"), ("partial.json", '{"format_version":')):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "channels"
                store = config.ConfigStore(directory)
                path = store.pending / filename
                path.write_text(body)
                store.close()
                with self.assertRaises(config.ConfigurationError):
                    config.ConfigStore(directory)
                self.assertEqual(path.read_text(), body)

    def test_existing_directory_can_restart_through_an_ancestor_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(actual, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable for this user")
            directory = alias / "nested/channels"
            for _ in range(2):
                store = config.ConfigStore(directory)
                try:
                    self.assertTrue((directory / ".pending").is_dir())
                finally:
                    store.close()
            (root / "linked-channels").symlink_to(directory, target_is_directory=True)
            with self.assertRaises(config.ConfigurationError):
                config.ConfigStore(root / "linked-channels")
