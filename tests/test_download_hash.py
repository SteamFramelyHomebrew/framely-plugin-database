import hashlib
import importlib.util
import json
import os
import pathlib
import unittest
import urllib.error
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('database', pathlib.Path(__file__).parents[1] / 'scripts/database.py')
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)
URL = 'https://github.com/author/plugin/releases/download/v1.0.0/test.plugin-1.0.0.framely'
DATA = b'package fixture'
DIGEST = hashlib.sha256(DATA).hexdigest()


class DownloadHash(unittest.TestCase):
    def entry(self, **extra):
        return {'id': 'test.plugin', 'packageUrl': URL, 'automaticDownload': True, 'expectedSha256': None, **extra}

    def release(self, **extra):
        return {'tag_name': 'v1.0.0', 'draft': False, 'assets': [{
            'name': 'test.plugin-1.0.0.framely', 'browser_download_url': URL,
            'state': 'uploaded', 'digest': 'sha256:' + DIGEST}], **extra}

    def test_automatic_download_reuses_release_digest_after_downloading(self):
        release = json.dumps(self.release()).encode()
        with patch.object(db, 'download', side_effect=[release, DATA]) as download, patch.object(db, 'verify_package', return_value=({}, {})):
            self.assertEqual(db.verified_download(self.entry())[2], DIGEST)
        self.assertEqual(download.call_args_list[0].args[0], 'https://api.github.com/repos/author/plugin/releases/tags/v1.0.0')
        self.assertEqual(download.call_args_list[1].args[0], URL)

    def test_custom_url_uses_configured_hash_and_still_downloads(self):
        entry = self.entry(automaticDownload=False, packageUrl='https://example.org/plugin.framely', expectedSha256=DIGEST)
        with patch.object(db, 'download', return_value=DATA) as download, patch.object(db, 'release_sha256') as release, patch.object(db, 'verify_package', return_value=({}, {})):
            self.assertEqual(db.verified_download(entry)[2], DIGEST)
        release.assert_not_called()
        download.assert_called_once_with(entry['packageUrl'])

    def test_mismatched_hash_and_unreachable_file_prevent_package_validation(self):
        for automatic in (False, True):
            for failure in (None, urllib.error.HTTPError(URL, 404, 'Not found', {}, None)):
                with self.subTest(automatic=automatic, failure=failure), patch.object(db, 'release_sha256', return_value=DIGEST), patch.object(db, 'download', return_value=b'corrupted', side_effect=failure), patch.object(db, 'verify_package') as verify:
                    with self.assertRaises((ValueError, urllib.error.HTTPError)):
                        db.verified_download(self.entry(automaticDownload=automatic, expectedSha256=DIGEST))
                    verify.assert_not_called()

    def test_automatic_url_cannot_override_release_hash(self):
        with patch.object(db, 'release_sha256', return_value=DIGEST), patch.object(db, 'download') as download:
            with self.assertRaises(ValueError):
                db.verified_download(self.entry(expectedSha256='0' * 64))
            download.assert_not_called()

    def test_missing_release_hash_asset_or_published_tag_is_rejected(self):
        releases = [self.release(draft=True), self.release(tag_name='v2.0.0'), self.release(assets=[])]
        for digest in (None, 'md5:' + 'a' * 32, 'sha256:bad'):
            value = self.release();value['assets'][0]['digest'] = digest;releases.append(value)
        for value in releases:
            with self.subTest(value=value), patch.object(db, 'download', return_value=json.dumps(value).encode()), self.assertRaises(ValueError):
                db.release_sha256(self.entry())

    def test_api_token_is_not_sent_to_download_hosts_or_redirects(self):
        for url in ('https://api.github.com/repos/author/plugin/releases/tags/v1.0.0', 'https://example.org/plugin.framely'):
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'test-token'}), patch.object(db.urllib.request, 'build_opener') as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.geturl.return_value = url
                response.headers = {}
                response.read.return_value = DATA
                db.download(url)
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-token' if 'api.github.com' in url else None)
                redirected = db.HTTPSRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://example.org/redirect')
                self.assertIsNone(redirected.get_header('Authorization'))

    def test_custom_url_without_hash_is_rejected(self):
        with self.assertRaises(ValueError):
            db.verified_download(self.entry(automaticDownload=False, packageUrl='https://example.org/plugin.framely'))


if __name__ == '__main__':
    unittest.main()
