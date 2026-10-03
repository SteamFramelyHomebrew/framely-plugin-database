import importlib.util
import pathlib
import json
import tempfile
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'scripts'))
import ownership as policy


class Ownership(unittest.TestCase):
    def setUp(self):
        self.actor = {'id': 1, 'login': 'alice'}
        self.registry = policy.empty_registry()
        self.requests = []
        self.repositories = {
            'alice/one': (101, 1), 'alice/two': (102, 1),
            'bob/one': (201, 2), 'org/one': (301, 3),
        }
        self.permissions = {}
        self.mock = patch.object(policy, 'api', side_effect=self.api)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def api(self, path):
        self.requests.append(path)
        if '/collaborators/' in path:
            name, user = path[6:].split('/collaborators/')
            user = user[:-len('/permission')]
            value = self.permissions.get((name, user))
            if isinstance(value, Exception):
                raise value
            return value or {'permission': 'read', 'user': {'id': 1}}
        name = path[6:]
        repo_id, owner_id = self.repositories[name]
        return {'id': repo_id, 'owner': {'id': owner_id}, 'full_name': name, 'private': False}

    def entry(self, identifier='example.one', name='alice/one', commit='a' * 40):
        return {'id': identifier, 'repository': 'https://github.com/' + name + '.git', 'commit': commit}

    def seed(self, *entries):
        for entry in entries:
            policy.reserve(self.registry, entry)

    def change(self, old, new):
        return policy.check_changes(old, new, self.registry, self.actor)

    def test_first_registration_requires_owner_and_namespaced_id(self):
        entry = self.entry()
        self.change({}, {'plugins/example.one': entry})
        self.assertEqual(self.registry['namespaces'], {'example': 1})
        for identifier in ('example', 'example..one', 'Example.one', '.one'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                policy.reserve(policy.empty_registry(), self.entry(identifier))
        with self.assertRaises(ValueError):
            self.change({}, {'plugins/bob.one': self.entry('bob.one', 'bob/one')})

    def test_shared_namespace_requires_same_owner_even_with_write_access(self):
        self.seed(self.entry())
        self.change({}, {'plugins/example.two': self.entry('example.two', 'alice/two')})
        self.permissions[('bob/one', 'alice')] = {'permission': 'write', 'user': {'id': 1}}
        with self.assertRaises(ValueError):
            self.change({}, {'plugins/example.other': self.entry('example.other', 'bob/one')})

    def test_owner_can_reserve_five_prefixes_but_not_a_sixth(self):
        for index in range(5):
            entry = self.entry(f'prefix{index}.one')
            self.change({}, {f'plugins/{entry["id"]}': entry})
        self.assertEqual(len(self.registry['namespaces']), 5)
        with self.assertRaisesRegex(ValueError, 'at most 5'):
            self.change({}, {'plugins/sixth.one': self.entry('sixth.one')})
        self.assertNotIn('sixth', self.registry['namespaces'])
        self.assertNotIn('sixth.one', self.registry['plugins'])
        # More plugins and updates under existing prefixes do not consume quota.
        extra = self.entry('prefix0.two', 'alice/two')
        self.change({}, {'plugins/prefix0.two': extra})
        self.change({'plugins/prefix0.two': extra}, {'plugins/prefix0.two': {**extra, 'commit': 'b' * 40}})
        self.assertEqual(len(self.registry['namespaces']), 5)

    def test_prefix_quota_counts_history_and_is_independent_per_owner(self):
        for index in range(5):self.seed(self.entry(f'prefix{index}.one'))
        old = self.entry('prefix0.one')
        self.change({'plugins/prefix0.one': old}, {})
        with self.assertRaisesRegex(ValueError, 'at most 5'):
            self.change({'plugins/prefix1.one': self.entry('prefix1.one')}, {'plugins/sixth.one': self.entry('sixth.one')})
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / 'ownership.json'
            path.write_text(json.dumps(self.registry))
            restored = policy.read_registry(path)
            with self.assertRaisesRegex(ValueError, 'at most 5'):
                policy.reserve(restored, self.entry('sixth.one'))
        self.actor = {'id': 2, 'login': 'bob'}
        for index in range(5):
            entry = self.entry(f'bob{index}.one', 'bob/one')
            self.change({}, {f'plugins/{entry["id"]}': entry})
        self.assertEqual(len(self.registry['namespaces']), 10)
        with self.assertRaisesRegex(ValueError, 'at most 5'):
            self.change({}, {'plugins/bob6.one': self.entry('bob6.one', 'bob/one')})

    def test_multi_plugin_pr_cannot_reserve_six_prefixes_at_once(self):
        new = {f'plugins/prefix{index}.one': self.entry(f'prefix{index}.one') for index in range(6)}
        with self.assertRaisesRegex(ValueError, 'at most 5'):
            self.change({}, new)

    def test_updates_require_owner_or_verified_write_permission(self):
        old = self.entry('org.one', 'org/one');self.seed(old)
        new = {**old, 'commit': 'b' * 40}
        with self.assertRaises(ValueError):self.change({'plugins/org.one': old}, {'plugins/org.one': new})
        for permission in ('write', 'maintain', 'admin'):
            self.permissions[('org/one', 'alice')] = {'permission': permission, 'user': {'id': 1}}
            self.change({'plugins/org.one': old}, {'plugins/org.one': new})
        self.permissions[('org/one', 'alice')] = {'permission': 'admin', 'user': {'id': 9}}
        with self.assertRaises(ValueError):self.change({'plugins/org.one': old}, {'plugins/org.one': new})

    def test_unverifiable_collaborator_access_fails_closed(self):
        old = self.entry('org.one', 'org/one');self.seed(old)
        self.permissions[('org/one', 'alice')] = urllib.error.HTTPError('https://api.github.com/', 403, 'Forbidden', {}, None)
        with self.assertRaisesRegex(ValueError, 'automatic merge denied'):
            self.change({'plugins/org.one': old}, {'plugins/org.one': {**old, 'commit': 'b' * 40}})

    def test_id_rename_requires_authorization_and_unused_id(self):
        old = self.entry();self.seed(old)
        self.change({'plugins/example.one': old}, {'plugins/example.new': self.entry('example.new')})
        self.assertIn('example.one', self.registry['plugins'])
        with self.assertRaises(ValueError):
            self.change({'plugins/example.new': self.entry('example.new')}, {'plugins/example.one': old})
        self.actor = {'id': 2, 'login': 'bob'}
        with self.assertRaises(ValueError):
            self.change({'plugins/example.one': old}, {'plugins/example.other': self.entry('example.other')})

    def test_transfer_repository_replacement_and_deleted_id_takeover_are_denied(self):
        old = self.entry();self.seed(old)
        with self.assertRaises(ValueError):
            self.change({'plugins/example.one': old}, {'plugins/example.one': self.entry(name='alice/two')})
        self.change({'plugins/example.one': old}, {})
        with self.assertRaises(ValueError):policy.reserve(self.registry, self.entry(name='bob/one'))
        self.repositories['alice/one'] = (101, 2)
        with self.assertRaises(ValueError):policy.reserve(self.registry, old)
        self.repositories['alice/one'] = (999, 1)
        with self.assertRaises(ValueError):policy.reserve(self.registry, old)

    def test_every_repository_in_multi_plugin_pr_must_be_authorized(self):
        one = self.entry();two = self.entry('org.one', 'org/one');self.seed(one, two)
        old = {'plugins/example.one': one, 'plugins/org.one': two}
        new = {path: {**entry, 'commit': 'b' * 40} for path, entry in old.items()}
        with self.assertRaises(ValueError):self.change(old, new)
        self.permissions[('org/one', 'alice')] = {'permission': 'write', 'user': {'id': 1}}
        self.change(old, new)
        self.assertEqual(len(policy.affected_repositories(old, new, self.registry)), 2)

    def test_persisted_reservations_survive_removal_and_cover_other_channel(self):
        self.seed(self.entry())
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / 'ownership.json'
            path.write_text(json.dumps(self.registry))
            restored = policy.read_registry(path)
            with patch.object(policy, 'entries', return_value={}):policy.seed(restored, ['stable', 'testing'])
            self.assertEqual(restored, self.registry)
            with self.assertRaises(ValueError):policy.reserve(restored, self.entry('example.other', 'bob/one'))
            with self.assertRaises(ValueError):policy.reserve(restored, self.entry(name='alice/two'))

    def test_submodule_path_must_match_manifest_id(self):
        with patch.object(policy.db, 'source_pins', return_value=[('plugins/wrong.id', 'url', 'sha')]), patch.object(policy.db, 'registrations', return_value=[self.entry()]):
            with self.assertRaises(ValueError):policy.entries('.')

    def test_unchanged_plugin_does_not_require_author_permission(self):
        entry = self.entry('org.one', 'org/one');self.seed(entry)
        self.change({'plugins/org.one': entry}, {'plugins/org.one': entry})
        self.assertFalse(any('/collaborators/' in path for path in self.requests))


if __name__ == '__main__':
    unittest.main()
