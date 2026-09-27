"""Offline managed built-in routing tests; all configuration lives in temp dirs.

Run: ./.venv/Scripts/python.exe -B -m unittest -v test_managed_builtin_routes
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_model_alias_routing as existing

routing = existing.routing
ASTRA = existing.ASTRA
SOL = existing.SOL


class ManagedBuiltinRoutingTests(unittest.TestCase):
    setUp = existing.ModelAliasRoutingTests.setUp
    rule = existing.ModelAliasRoutingTests.rule
    persist = existing.ModelAliasRoutingTests.persist
    assert_bad_request = existing.ModelAliasRoutingTests.assert_bad_request

    def test_missing_config_seeds_catalog_rows_in_memory_only(self):
        payload = self.service.config_payload()
        rows = payload['builtin_mappings']
        self.assertEqual(len(rows), 6)
        self.assertEqual([row['target_model'] for row in rows], list(existing.excel_upstream.MODEL_IDS))
        for row in rows:
            canonical = row['target_model']
            base = canonical.removesuffix('-excel')
            self.assertEqual(row['source_model'], f'{base},{base}-basispoints')
            self.assertTrue(row['enabled'])
            self.assertEqual(row['source_provider'], 'codex')
            self.assertEqual(row['target_provider'], 'codex')
            for alias in (base, base + '-basispoints'):
                self.assertEqual(self.service.resolve_target_model(alias), canonical)
                self.assertEqual(self.service.resolve_bps_model(alias), canonical)
            self.assertIsNone(self.service.resolve_target_model(canonical))
            self.assertEqual(self.service.resolve_bps_model(canonical), canonical)
        self.assertFalse(payload['enabled'])
        self.assertFalse(self.path.exists())

    def test_defaults_derive_from_model_ids_not_a_second_catalog(self):
        with mock.patch.object(existing.excel_upstream, 'MODEL_IDS', ('gpt-future-excel',)):
            service = routing.ModelRoutingConfigService(self.service._config)
            rows = service.load_settings()['builtin_mappings']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['source_model'], 'gpt-future,gpt-future-basispoints')
            self.assertEqual(service.resolve_bps_model('gpt-future'), 'gpt-future-excel')

    def test_old_config_migration_preserves_disabled_custom_and_legacy(self):
        self.persist({'enabled': False, 'mappings': [self.rule('old-custom')],
                      'approval_enabled': True,
                      'approval_mappings': [self.rule('approval', 'gpt-5.4')],
                      'claude_code_defaults': {'opus_model': 'gpt-5.4'}})
        original = self.path.read_bytes()
        settings = self.service.load_settings()
        self.assertEqual(len(settings['builtin_mappings']), 6)
        self.assertEqual(settings['warnings'], [])
        self.assertEqual(self.path.read_bytes(), original)
        saved = self.service.save_settings({'builtin_mappings': [self.rule('managed', SOL)]})
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['mappings'], settings['mappings'])
        for key in ('approval_enabled', 'approval_mappings', 'claude_code_defaults'):
            self.assertEqual(saved[key], settings[key])
        self.assertIsNone(self.service.resolve_bps_model('old-custom'))
        self.assertEqual(self.service.resolve_bps_model('managed'), SOL)
        self.service.save_settings({'enabled': True})
        self.assertEqual(self.service.resolve_bps_model('old-custom'), ASTRA)

    def test_old_client_save_materializes_defaults(self):
        saved = self.service.save_settings({'enabled': True, 'mappings': [self.rule()]})
        stored = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(stored['builtin_mappings'], saved['builtin_mappings'])
        self.assertEqual(len(stored['builtin_mappings']), 6)

    def test_explicit_empty_survives_reload_and_omitted_field_saves(self):
        self.service.save_settings({'builtin_mappings': []})
        for update in ({'enabled': True, 'mappings': [self.rule()]}, {}, {'enabled': False}):
            self.assertEqual(self.service.save_settings(update)['builtin_mappings'], [])
        fresh = routing.ModelRoutingConfigService(self.service._config)
        self.assertEqual(fresh.load_settings()['builtin_mappings'], [])
        for canonical in existing.excel_upstream.MODEL_IDS:
            base = canonical.removesuffix('-excel')
            for alias in (base, base + '-basispoints'):
                self.assertIsNone(fresh.resolve_bps_model(alias))
                self.assertIsNone(fresh.resolve_target_model(alias))
            self.assertEqual(fresh.resolve_bps_model(canonical), canonical)
        self.assertEqual(json.loads(self.path.read_text())['builtin_mappings'], [])

    def test_disabling_custom_preserves_rules_and_builtin_resolution(self):
        self.service.save_settings({'enabled': True, 'mappings': [self.rule('gpt-6-astra', SOL)]})
        self.assertEqual(self.service.resolve_bps_model('gpt-6-astra'), SOL)
        previous = self.service.load_settings()
        saved = self.service.save_settings({'enabled': False})
        self.assertEqual(saved['mappings'], previous['mappings'])
        self.assertEqual(saved['builtin_mappings'], previous['builtin_mappings'])
        self.assertEqual(self.service.resolve_bps_model('gpt-6-astra'), ASTRA)
        self.assertEqual(self.service.resolve_target_model('gpt-6-astra'), ASTRA)

    def test_builtin_rename_edit_delete_and_disabled_row(self):
        rows = self.service.load_settings()['builtin_mappings']
        rows[0].update(source_model=' Renamed,Alternate ', target_model=SOL)
        rows[1]['enabled'] = False
        removed = rows.pop(2)
        saved = self.service.save_settings({'builtin_mappings': rows})
        self.assertEqual(saved['builtin_mappings'][0]['source_model'], 'renamed,alternate')
        self.assertEqual(self.service.resolve_bps_model(' RENAMED '), SOL)
        self.assertEqual(self.service.resolve_bps_model('alternate'), SOL)
        for source in ('gpt-6-astra,gpt-6-astra-basispoints', rows[1]['source_model'], removed['source_model']):
            for alias in source.split(','):
                self.assertIsNone(self.service.resolve_bps_model(alias))
        self.assertEqual(self.service.resolve_bps_model(ASTRA), ASTRA)
        self.assertEqual(self.service.resolve_bps_model(SOL), SOL)
        loaded = self.service.load_settings()['builtin_mappings']
        self.assertEqual(loaded, saved['builtin_mappings'])
        loaded[1]['enabled'] = True
        self.service.save_settings({'builtin_mappings': loaded})
        self.assertEqual(self.service.resolve_bps_model('gpt-6-sol-basispoints'), SOL)

    def test_custom_priority_and_literal_alias_boundaries(self):
        self.service.save_settings({'enabled': True,
            'mappings': [self.rule('gpt-6-astra', SOL), self.rule(ASTRA, SOL)]})
        self.assertEqual(self.service.resolve_bps_model('gpt-6-astra'), SOL)
        self.assertEqual(self.service.resolve_bps_model('gpt-6-astra-basispoints'), ASTRA)
        self.assertEqual(self.service.resolve_bps_model(ASTRA), SOL)
        self.service.save_settings({'builtin_mappings': [self.rule('gpt-6-astra', ASTRA, enabled=False)]})
        self.assertEqual(self.service.resolve_bps_model('gpt-6-astra'), SOL)

    def test_resolution_is_one_step_even_for_canonical_cycles(self):
        self.service.save_settings({'enabled': True, 'mappings': [self.rule(ASTRA, SOL)],
                                   'builtin_mappings': [self.rule('start', ASTRA), self.rule(SOL, ASTRA)]})
        self.assertEqual(self.service.resolve_bps_model('start'), ASTRA)
        self.assertEqual(self.service.resolve_bps_model(ASTRA), SOL)
        self.assertEqual(self.service.resolve_bps_model(SOL), ASTRA)

    def test_no_implicit_prefix_or_alias_expansion(self):
        for requested in (None, False, 5, [], {}, '', 'a,b', 'x' * 257,
                          'openai/' + ASTRA, 'openai/gpt-6-astra', 'gpt_6_astra',
                          'gpt-6-astra-excel-basispoints', 'gpt-6-astra-basispoints-excel'):
            with self.subTest(requested=requested):
                self.assertIsNone(self.service.resolve_bps_model(requested))
        self.assertEqual(self.service.resolve_bps_model(' GPT-6-ASTRA-EXCEL '), ASTRA)
        self.service.save_settings({'builtin_mappings': [self.rule('openai/' + ASTRA)]})
        self.assertEqual(self.service.resolve_bps_model('openai/' + ASTRA), ASTRA)

    def test_duplicates_checked_within_builtin_list_even_disabled(self):
        for rows in ([self.rule('one, ONE')],
                     [self.rule('one,two'), self.rule('TWO', SOL)],
                     [self.rule('one', enabled=False), self.rule('one')]):
            self.assert_bad_request({'builtin_mappings': rows})
        saved = self.service.save_settings({'enabled': True, 'mappings': [self.rule('one', SOL)],
                                           'builtin_mappings': [self.rule('one')]})
        self.assertEqual(len(saved['builtin_mappings']), 1)
        self.assertEqual(self.service.resolve_bps_model('one'), SOL)

    def test_invalid_builtin_collections_rows_flags_and_targets_are_atomic(self):
        self.service.save_settings({'builtin_mappings': [self.rule('kept')]})
        for raw in (None, {}, '', False, 1, [None], [{}], [self.rule('')],
                    [self.rule(target='unknown')]):
            self.assert_bad_request({'builtin_mappings': raw})
        for enabled in (None, 'false', 'true', 0, 1, [], {}):
            self.assert_bad_request({'builtin_mappings': [self.rule(enabled=enabled)]})
        self.assertEqual(self.service.resolve_bps_model('kept'), ASTRA)

    def test_invalid_stored_builtin_rows_warn_without_resurrection(self):
        self.persist({'builtin_mappings': [self.rule('good', enabled=False),
                      self.rule('good', SOL), self.rule('secret-token', '<script>secret</script>'),
                      self.rule('bad-flag', enabled='false'), self.rule('valid')]})
        original = self.path.read_bytes()
        settings = self.service.load_settings()
        self.assertEqual(len(settings['warnings']), 3)
        self.assertEqual([r['source_model'] for r in settings['builtin_mappings']], ['good', 'valid'])
        self.assertNotIn('secret', json.dumps(settings))
        self.assertIsNone(self.service.resolve_bps_model('good'))
        self.assertIsNone(self.service.resolve_bps_model('gpt-6-astra'))
        self.assertEqual(self.service.resolve_bps_model('valid'), ASTRA)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_stored_list_warns_and_does_not_seed(self):
        for raw in (None, {}, 'bad', False):
            self.persist({'builtin_mappings': raw})
            settings = self.service.load_settings()
            self.assertEqual(settings['builtin_mappings'], [])
            self.assertEqual(len(settings['warnings']), 1)
            self.assertIsNone(self.service.resolve_bps_model('gpt-6-astra'))

    def test_corrupt_config_fails_closed_for_aliases_but_keeps_canonicals(self):
        for content in ('{', '[]'):
            self.path.write_text(content, encoding='utf-8')
            self.assertEqual(self.service.load_settings()['builtin_mappings'], [])
            self.assertTrue(self.service.load_settings()['warnings'])
            self.assertIsNone(self.service.resolve_bps_model('gpt-6-astra'))
            self.assertEqual(self.service.resolve_bps_model(ASTRA), ASTRA)
            self.assertEqual(self.path.read_text(), content)

    def test_builtin_write_is_complete_atomic_and_failure_preserves_previous(self):
        self.service.save_settings({'builtin_mappings': [self.rule('before')]})
        before = self.path.read_bytes()
        real_replace = os.replace
        def inspect(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(self.path.read_bytes(), before)
            payload = json.loads(Path(source).read_text())
            self.assertEqual(payload['builtin_mappings'], [])
            self.assertNotIn('warnings', payload)
            self.assertNotIn('available_models', payload)
            return real_replace(source, destination)
        with mock.patch.object(routing.os, 'replace', side_effect=inspect), mock.patch.object(routing.os, 'fsync', wraps=os.fsync) as fsync:
            self.service.save_settings({'builtin_mappings': []})
        fsync.assert_called_once()
        after = self.path.read_bytes()
        with mock.patch.object(routing.os, 'replace', side_effect=OSError('simulated')):
            with self.assertRaises(OSError):
                self.service.save_settings({'builtin_mappings': [self.rule('after')]})
        self.assertEqual(self.path.read_bytes(), after)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == '__main__':
    unittest.main()
