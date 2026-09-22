"""Portable directory locking and process-interruption recovery."""

from concurrent.futures import ThreadPoolExecutor
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

    def test_failed_commit_keeps_evidence_and_blocks_restart(self):
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
            with self.assertRaisesRegex(config.ConfigurationError, "Channel created but configuration not committed"):
                config.ConfigStore(directory)
            # Failed startup must also release its lock.
            with self.assertRaisesRegex(config.ConfigurationError, "Channel created but configuration not committed"):
                config.ConfigStore(directory)

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
