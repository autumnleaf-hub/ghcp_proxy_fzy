import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import outbound_proxy


class OutboundProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'outbound-proxy.json'
        self.settings = outbound_proxy.OutboundProxySettings(str(self.path))

    def test_default_does_not_write_and_preserves_environment(self):
        value = self.settings.load()
        self.assertEqual(value, {'enabled': False, 'url': 'http://127.0.0.1:7890'})
        self.assertFalse(self.path.exists())
        self.assertEqual(outbound_proxy.httpx_client_kwargs(value), {})

    def test_enabled_is_explicit_and_no_environment_bypass(self):
        value = self.settings.save({'enabled': True, 'url': 'http://127.0.0.1:7890/'})
        self.assertEqual(self.settings.load(), value)
        self.assertEqual(outbound_proxy.httpx_client_kwargs(value),
                         {'proxy': 'http://127.0.0.1:7890', 'trust_env': False})

    def test_disabled_keeps_saved_address(self):
        self.settings.save({'enabled': False, 'url': 'https://proxy.example:8443'})
        self.assertEqual(self.settings.load()['url'], 'https://proxy.example:8443')

    def test_bad_values_are_rejected_without_writes(self):
        for value in ('', '127.0.0.1:7890', 'ftp://proxy:21', 'http://user:pass@proxy:7890',
                      'http://proxy/path', 'http://proxy?x=1', 'http://proxy#frag',
                      'http://proxy:0', 'http://proxy:65536', 'http://127.0.0.1:8001',
                      'http://localhost:8000', 'http://[::1]:8001', 'http://bad host:7890',
                      'http://proxy' + chr(10) + ':7890'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.settings.save({'enabled': True, 'url': value})
        self.assertFalse(self.path.exists())

    def test_boolean_is_strict(self):
        for enabled in (1, 0, 'true', None, []):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                self.settings.save({'enabled': enabled})

    def test_corruption_does_not_disable_proxy_silently(self):
        self.path.write_text('bad JSON', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.settings.load()

    def test_atomic_failure_keeps_previous_settings(self):
        original = self.settings.save({'enabled': False})
        with mock.patch.object(outbound_proxy.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.settings.save({'enabled': True})
        self.assertEqual(self.settings.load(), original)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_save_can_repair_corrupt_settings_without_bypassing_validation(self):
        self.path.write_text('broken', encoding='utf-8')
        self.settings.save({'enabled': True})
        self.assertTrue(self.settings.load()['enabled'])

    def test_read_failure_does_not_fall_back_to_direct(self):
        with mock.patch('builtins.open', side_effect=PermissionError('no read')):
            with self.assertRaises(ValueError):
                self.settings.load()

    def test_current_port_cannot_proxy_to_itself(self):
        with mock.patch.dict('os.environ', {'GHCP_PORT': '8123'}):
            with self.assertRaises(ValueError):
                self.settings.save({'enabled': True, 'url': 'http://127.0.0.1:8123'})


if __name__ == '__main__':
    unittest.main()
