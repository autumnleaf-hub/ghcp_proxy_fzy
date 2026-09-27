import copy,json,unittest
from unittest import mock
from test_codex_bridge_regressions import OfflineStreamBase,native_call,stream_events,LOCAL_TOOLS,run_immediate
from test_client_tool_recovery import completed,message

class RecoverySSEEdgeTests(OfflineStreamBase):
    def setUp(self):
        super().setUp()
        import proxy,client_tool_recovery,format_translation
        self.proxy=proxy; self.recovery=client_tool_recovery; self.ft=format_translation
        self.source={'tools':LOCAL_TOOLS,'model':'gpt-6-sol'}
        self.bad=completed(native_call('bad',name='absent_tool'))
    def corrected_events(self,candidate,prefix=None,source=None):
        source=source or self.source
        original=completed(*(prefix or []),self.bad['output'][0])
        fixed=self.recovery.accepted_correction(original,candidate,source)
        self.assertIsNotNone(fixed)
        with mock.patch.object(self.proxy,'_retry_excel_tool_conversion',mock.AsyncMock(return_value=fixed)):
            return self.collect(stream_events(original['output']),source_body=source)
    def assert_tools_consistent(self,events,expected):
        final=[v['response'] for kind,v in events if kind=='response.completed']
        self.assertEqual(len(final),1)
        calls=[v for v in final[0]['output'] if v.get('type') in ('function_call','custom_tool_call')]
        self.assertEqual(len(calls),expected)
        for index,item in enumerate(final[0]['output']):
            if item.get('type') not in ('function_call','custom_tool_call'): continue
            done=[v for kind,v in events if kind=='response.output_item.done' and v.get('item',{}).get('call_id')==item['call_id']]
            self.assertEqual(len(done),1); self.assertEqual(done[0]['output_index'],index)
            event='response.function_call_arguments.delta' if item['type']=='function_call' else 'response.custom_tool_call_input.delta'
            key='arguments' if item['type']=='function_call' else 'input'
            value=''.join(v.get('delta','') for kind,v in events if kind==event and v.get('item_id')==item['id'])
            self.assertEqual(value,item[key])
        return final[0]
    def test_corrected_batch_two_calls_has_one_terminal_and_stable_indexes(self):
        events=self.corrected_events(completed(native_call('a'),native_call('b')))
        self.assert_tools_consistent(events,2)
    def test_visible_message_prefix_is_not_duplicated(self):
        events=self.corrected_events(completed(native_call('new')),[message('Before the tool','prefix_message')])
        response=self.assert_tools_consistent(events,1)
        self.assertEqual(response['output'][0]['id'],'prefix_message')
        self.assertEqual(sum(v.get('item',{}).get('id')=='prefix_message' for kind,v in events if kind=='response.output_item.done'),1)
    def test_reasoning_prefix_keeps_index_without_rejected_ciphertext(self):
        prefix={'id':'rs_prefix','type':'reasoning','summary':[],'encrypted_content':'PRIVATE_REJECTED_CIPHER'}
        events=self.corrected_events(completed(native_call('new')),[prefix])
        response=self.assert_tools_consistent(events,1)
        self.assertNotIn('encrypted_content',response['output'][0])
        self.assertEqual(response['output'][0]['id'],'rs_prefix')
    def test_custom_input_recovery_is_byte_exact(self):
        raw='RAW_中文'+chr(10)+'C:'+chr(92)+'fixture'+chr(10)
        item=native_call('custom',raw={'name':'apply_patch','input':raw})
        events=self.corrected_events(completed(item))
        response=self.assert_tools_consistent(events,1)
        self.assertEqual(response['output'][0]['input'],raw)
    def test_duplicate_terminal_and_done_never_reemit_calls(self):
        wire=stream_events([native_call('only')])
        events=self.collect(wire+wire)
        self.assert_tools_consistent(events,1)
    def test_mixed_retry_batch_stays_zero_dispatch_and_zero_cache(self):
        mixed=completed(native_call('valid'),native_call('invalid',name='missing'))
        self.assertIsNone(self.recovery.accepted_correction(self.bad,mixed,self.source))
        with mock.patch.object(self.proxy,'_retry_excel_tool_conversion',mock.AsyncMock(return_value=None)):
            events=self.collect(stream_events(self.bad['output']))
        self.assert_no_dispatched_tools(events)
        self.assertEqual(dict(self.proxy.excel_upstream._native_call_cache),{})
    def test_required_text_response_can_recover_to_a_real_call(self):
        source={**self.source,'tool_choice':{'type':'function','name':'exec_command'}}
        rejected=completed(message('ONLY_TEXT'))
        corrected=self.recovery.accepted_correction(rejected,completed(native_call('forced')),source)
        wire=self.ft.sse_encode('response.completed',{'type':'response.completed','response':rejected})
        with mock.patch.object(self.proxy,'_retry_excel_tool_conversion',mock.AsyncMock(return_value=corrected)):
            events=self.collect(wire,source_body=source)
        self.assert_tools_consistent(events,1)
    def test_final_usage_uses_combined_total_once(self):
        plan=self.proxy.UpstreamRequestPlan('offline-usage','https://offline.invalid',{}, {},None,'gpt-6-sol','gpt-6-sol',
             trace_context={'client_tool_correction':{'total_usage':{'input_tokens':30,'output_tokens':5,'total_tokens':35}}})
        with mock.patch.object(self.proxy,'_protect_plan_prompt_trace_state'),mock.patch.object(self.proxy,'_remember_responses_cache_settle_finish'),mock.patch.object(self.proxy.usage_tracker,'finish_event') as finish,mock.patch.object(self.proxy,'request_tracing_enabled',return_value=False),mock.patch.object(self.proxy,'_debug_prompt_logging_enabled',return_value=False),mock.patch.object(self.proxy,'_should_force_failure_trace',return_value=False):
            self.proxy._finish_usage_and_trace(plan,200,response_payload={'usage':{'total_tokens':35}})
        self.assertEqual(finish.call_args.kwargs['usage']['total_tokens'],35)

if __name__=='__main__': unittest.main()
