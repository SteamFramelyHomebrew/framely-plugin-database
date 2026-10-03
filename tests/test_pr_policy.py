import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('pr_policy', pathlib.Path(__file__).parents[1] / 'scripts/pr_policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class Policy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.org')
        (self.root / 'README.md').write_text('Development only')
        (self.root / '.gitmodules').write_text('')
        self.base = self.commit()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'Fixture')
        return self.git('rev-parse', 'HEAD')

    def pin(self):
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.base + ',plugins/example.plugin')
        (self.root / '.gitmodules').write_text('[submodule "example.plugin"]\n path = plugins/example.plugin\n url = https://example.org/plugin.git\n')
        self.git('add', '.gitmodules')
        self.git('commit', '-qm', 'Plugin pin')
        return self.git('rev-parse', 'HEAD')

    def test_gitlink_is_allowed_and_candidate_contains_pinned_commit(self):
        head = self.pin()
        self.assertTrue(policy.eligible(self.root, self.base, head))
        candidate = pathlib.Path(self.temp.name) / 'candidate'
        self.assertTrue(policy.prepare(self.root, candidate, self.base, head))
        pin = subprocess.check_output(['git', '-C', str(candidate), 'rev-parse', ':plugins/example.plugin'], text=True).strip()
        self.assertEqual(pin, self.base)

    def test_regular_payload_and_symlink_cannot_impersonate_gitlinks(self):
        (self.root / 'plugins').mkdir()
        path = self.root / 'plugins/example.plugin'
        path.write_text('executable code')
        self.assertFalse(policy.eligible(self.root, self.base, self.commit()))
        path.unlink()
        path.symlink_to('../README.md')
        self.assertFalse(policy.eligible(self.root, self.base, self.commit()))

    def test_modules_symlink_and_executable_are_rejected(self):
        path = self.root / '.gitmodules'
        path.chmod(0o755)
        self.assertFalse(policy.eligible(self.root, self.base, self.commit()))
        path.unlink()
        path.symlink_to('README.md')
        self.assertFalse(policy.eligible(self.root, self.base, self.commit()))

    def test_scripts_workflows_catalog_and_docs_require_manual_handling(self):
        for name in ('scripts/database.py', '.github/workflows/validate.yml', 'testing/catalog.json', 'README.md'):
            with self.subTest(path=name):
                self.git('reset', '--hard', self.base)
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('modified')
                self.assertFalse(policy.eligible(self.root, self.base, self.commit()))

    def test_gitlink_removal_is_allowed_and_empty_diff_is_not(self):
        head = self.pin()
        self.git('rm', '--cached', 'plugins/example.plugin')
        (self.root / '.gitmodules').write_text('')
        removed = self.commit()
        self.assertTrue(policy.eligible(self.root, head, removed))
        self.assertFalse(policy.eligible(self.root, removed, removed))


if __name__ == '__main__':
    unittest.main()
