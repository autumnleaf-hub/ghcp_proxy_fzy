import copy,json,unittest
from test_codex_bridge_regressions import OfflineBase, native_call, LOCAL_TOOLS
from test_client_tool_recovery import completed,message

class ChoiceAndCompactionTests(OfflineBase):
    def setUp(self):
        super().setUp()
        import excel_upstream as bridge
        import client_tool_recovery as recovery
        self.bridge=bridge; self.recovery=recovery
        self.source={'model':'gpt-6-sol','tools':LOCAL_TOOLS}
    def test_forced_function_restricts_catalog(self):
        source={**self.source,'tool_choice':{'type':'function','name':'exec_command'}}
        self.assertEqual(self.bridge.client_tool_types(source),{'exec_command':'function'})
        self.assertNotIn('view_image',self.bridge._client_tool_protocol_instructions(source))
        self.assertIn('even if the user message asks for text only',self.bridge._client_tool_protocol_instructions(source))
    def test_forced_other_catalog_tool_is_rejected(self):
        source={**self.source,'tool_choice':{'type':'function','name':'exec_command'}}
        response=completed(native_call(name='view_image',arguments={'path':'fixed.png'}))
        self.assertEqual(self.bridge.extract_native_client_tool_calls(response,source),[])
        self.assertEqual(dict(self.bridge._native_call_cache),{})
    def test_forced_namespaced_function(self):
        source={'tools':[{'type':'namespace','name':'browser','tools':[{'type':'function','name':'js','parameters':{}}]}],
                'tool_choice':{'type':'function','namespace':'browser','name':'js'}}
        self.assertEqual(self.bridge.client_tool_types(source),{'browser.js':'function'})
    def test_forced_custom(self):
        source={**self.source,'tool_choice':{'type':'custom','name':'apply_patch'}}
        self.assertEqual(self.bridge.client_tool_types(source),{'apply_patch':'custom'})
    def test_unknown_forced_tool_fails_validation(self):
        for choice in ({'type':'function','name':'not_in_catalog'},'function',{'type':'custom','name':'exec_command'}):
            with self.subTest(choice=choice),self.assertRaises(ValueError):
                self.bridge.validate_client_tool_choice({**self.source,'tool_choice':choice})
    def test_required_without_tools_is_invalid(self):
        with self.assertRaises(ValueError): self.bridge.validate_client_tool_choice({'tool_choice':'required'})
    def test_required_text_only_can_be_corrected(self):
        source={**self.source,'tool_choice':'required'}
        rejected=completed(message('ONLY_TEXT'))
        body=self.recovery.correction_body({'input':[]},source,rejected)
        self.assertIsNotNone(body)
        self.assertIn('requires at least one',body['input'][-1]['content'][0]['text'])
        self.assertIsNone(self.recovery.accepted_correction(rejected,completed(message()),source))
        self.assertIsNotNone(self.recovery.accepted_correction(rejected,completed(native_call()),source))
    def test_parallel_false_rejects_whole_batch(self):
        source={**self.source,'parallel_tool_calls':False}
        response=completed(native_call('a'),native_call('b'))
        self.assertEqual(self.bridge.extract_native_client_tool_calls(response,source),[])
        self.assertFalse(self.recovery.valid_tool_batch(response,source))
        self.assertEqual(dict(self.bridge._native_call_cache),{})
        self.assertEqual(self.bridge.client_tool_selection_issue(response,source),'parallel_client_tools_not_allowed')
    def test_parallel_default_permits_distinct_native_envelopes(self):
        response=completed(native_call('a'),native_call('b'))
        self.assertEqual(len(self.bridge.extract_native_client_tool_calls(response,self.source)),2)
        text=self.bridge._client_tool_protocol_instructions(self.source)
        self.assertIn('client_tool_batch',text)
        self.assertNotIn('Remember: call the outer native run_officejs tool once;',text)
    def test_auto_text_does_not_trigger_recovery(self):
        self.assertIsNone(self.recovery.correction_body({'input':[]},self.source,completed(message())))
    def test_failed_required_response_remains_failed(self):
        source={**self.source,'tool_choice':'required'}
        response={**completed(message()),'status':'failed'}
        self.assertIsNone(self.bridge.client_tool_selection_issue(response,source))
        self.assertIsNone(self.recovery.correction_body({'input':[]},source,response))
    def test_fake_compaction_expands_and_uses_handoff_boundary(self):
        import format_translation as ft
        compact={'type':'compaction','id':'cmp_local','encrypted_content':ft.encode_fake_compaction('COMPAT_X7429')}
        source={'model':'gpt-6-sol','input':[{'role':'user','content':'OLD_DO_NOT_REPLAY'},compact,{'role':'user','content':'What is the code?'}]}
        original=copy.deepcopy(source)
        result=self.bridge.prepare_responses_body(source)
        serialized=json.dumps(result)
        self.assertIn('COMPAT_X7429',serialized)
        self.assertNotIn('OLD_DO_NOT_REPLAY',serialized)
        self.assertNotIn(compact['encrypted_content'],serialized)
        self.assertEqual(source,original)
    def test_opaque_native_compaction_is_not_decoded_or_changed(self):
        item={'type':'compaction','id':'cmp_native','encrypted_content':'opaque-native-ciphertext'}
        result=self.bridge.prepare_responses_body({'model':'gpt-6-sol','input':[item]})
        self.assertIn(item,result['input'])
    def test_latest_local_compaction_wins(self):
        import format_translation as ft
        items=[{'type':'compaction','encrypted_content':ft.encode_fake_compaction(value)} for value in ('OLD_SUMMARY','NEW_SUMMARY')]
        text=json.dumps(self.bridge.prepare_responses_body({'model':'gpt-6-sol','input':items}))
        self.assertIn('NEW_SUMMARY',text); self.assertNotIn('OLD_SUMMARY',text)

if __name__=='__main__': unittest.main()
