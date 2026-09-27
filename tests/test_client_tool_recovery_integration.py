import asyncio, json, unittest
from contextlib import ExitStack
from unittest import mock
import httpx
from tests.test_codex_bridge_regressions import OfflineStreamBase, native_call, LOCAL_TOOLS, stream_events, run_immediate
from tests.test_client_tool_recovery import completed, message

class RecoveryIntegrationTests(OfflineStreamBase):
    def setUp(self):
        super().setUp()
        import proxy
        self.proxy=proxy
        self.source={'tools':LOCAL_TOOLS,'model':'gpt-6-sol'}
        self.plan=proxy.UpstreamRequestPlan('offline-recovery','https://offline.invalid/responses',{},
            {'model':'gpt-6-sol','input':[],'stream':True},None,'gpt-6-sol','gpt-6-sol',trace_context={})
        self.bad=completed(native_call('bad',name='unknown_tool'))
        self.good=completed(native_call('good'))
        self.request=httpx.Request('POST','https://offline.invalid/responses')
        self.client=mock.Mock()
        self.client.build_request.return_value=self.request
        self.send=mock.AsyncMock()
        stack=ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(proxy,'_get_excel_upstream_client',return_value=self.client))
        stack.enter_context(mock.patch.object(proxy,'throttled_client_send',self.send))
        async def immediate(coro,timeout): return await coro
        stack.enter_context(mock.patch.object(proxy.asyncio,'wait_for',side_effect=immediate))
    def respond(self,payload,sse=False):
        if sse:
            return httpx.Response(200,request=self.request,content=stream_events(payload['output']),headers={'content-type':'text/event-stream'})
        return httpx.Response(200,request=self.request,json=payload)
    def retry(self):
        return run_immediate(self.proxy._retry_excel_tool_conversion(self.plan,self.source,self.bad))
    def test_success_uses_only_one_extra_request_and_no_early_cache(self):
        self.send.return_value=self.respond(self.good)
        result=self.retry()
        self.assertIsNotNone(result)
        self.assertIsNone(self.retry())
        self.assertEqual(self.send.await_count,1)
        self.assertEqual(dict(self.proxy.excel_upstream._native_call_cache),{})
        self.assertEqual(self.plan.trace_context['client_tool_correction']['outcome'],'recovered')
    def test_second_rejection_does_not_recurse_or_dispatch(self):
        self.send.return_value=self.respond(self.bad)
        self.assertIsNone(self.retry()); self.assertIsNone(self.retry())
        self.assertEqual(self.send.await_count,1)
        self.assertEqual(dict(self.proxy.excel_upstream._native_call_cache),{})
    def test_second_malformed_candidate_has_separate_redacted_diagnostic(self):
        item=dict(self.good['output'][0])
        broken='{"name":"exec_command","arguments":{"cmd":"PRIVATE_SENTINEL"bad"}}'
        item['arguments']=json.dumps({'code':broken})
        self.send.return_value=self.respond(completed(item))
        self.assertIsNone(self.retry())
        record=self.plan.trace_context['client_tool_correction']
        self.assertEqual(record['outcome'],'correction_rejected')
        self.assertEqual(record['correction_diagnostic']['reason'],'invalid_tool_batch')
        detail=record['correction_transport_details'][0]
        self.assertEqual(detail['phase'],'invalid_transport_code_json')
        self.assertEqual(detail['json_error'],'missing_delimiter')
        self.assertGreater(detail['offset'],0)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(record))
        self.assertEqual(self.send.await_count,1)

    def test_parallel_probe_reports_original_native_calls(self):
        source={**self.source,'parallel_tool_calls':True,'metadata':{'ghcp_native_parallel_probe':True}}
        self.plan.body['parallel_tool_calls']=True
        self.send.return_value=self.respond(completed(native_call('alpha'),native_call('beta')))
        with mock.patch.object(self.proxy,'_finish_usage_and_trace'):
            result=run_immediate(self.proxy._post_excel_non_streaming_request(self.plan,client_body=source))
        self.assertEqual(result.headers['x-ghcp-native-tool-call-count'],'2')
        self.assertEqual(result.headers['x-ghcp-original-native-tool-call-count'],'2')
        self.assertEqual(result.headers['x-ghcp-parallel-control'],'true')
        self.assertEqual(len(json.loads(result.body)['output']),2)

    def test_normal_response_omits_parallel_probe_headers(self):
        self.send.return_value=self.respond(self.good)
        with mock.patch.object(self.proxy,'_finish_usage_and_trace'):
            result=run_immediate(self.proxy._post_excel_non_streaming_request(self.plan,client_body=self.source))
        self.assertNotIn('x-ghcp-native-tool-call-count',result.headers)

    def test_http_error_keeps_safe_fallback(self):
        self.send.return_value=httpx.Response(429,request=self.request,json={'error':'rate limited'})
        self.assertIsNone(self.retry())
        self.assertEqual(self.plan.trace_context['client_tool_correction']['outcome'],'upstream_http_error')
    def test_transport_exception_keeps_safe_fallback(self):
        self.send.side_effect=httpx.RemoteProtocolError('PRIVATE_EXCEPTION_DATA')
        self.assertIsNone(self.retry())
        self.assertNotIn('PRIVATE_EXCEPTION_DATA',json.dumps(self.plan.trace_context))
    def test_timeout_is_bounded_and_not_retried_again(self):
        async def timeout(coro,timeout):
            coro.close(); raise TimeoutError('PRIVATE_TIMEOUT')
        with mock.patch.object(self.proxy.asyncio,'wait_for',side_effect=timeout):
            self.assertIsNone(self.retry())
        self.assertIsNone(self.retry())
        self.assertNotIn('PRIVATE_TIMEOUT',json.dumps(self.plan.trace_context))
    def test_correction_deadlines_allow_generation_and_bound_idle_io(self):
        self.send.return_value=self.respond(self.good)
        seen=[]
        async def capture(coro,timeout):
            seen.append(timeout)
            return await coro
        with mock.patch.object(self.proxy.asyncio,'wait_for',side_effect=capture):
            self.assertIsNotNone(self.retry())
        self.assertEqual(seen,[120.0])
        timeout=self.client.build_request.call_args.kwargs['timeout']
        self.assertEqual(timeout.connect,10.0)
        self.assertEqual(timeout.read,120.0)
        self.assertEqual(timeout.write,30.0)
        self.assertEqual(timeout.pool,30.0)
        self.assertEqual(self.plan.trace_context['client_tool_correction']['timeout_seconds'],120)
    def test_cancellation_propagates_and_stops_retry(self):
        async def cancel(coro,timeout):
            coro.close(); raise asyncio.CancelledError()
        with mock.patch.object(self.proxy.asyncio,'wait_for',side_effect=cancel):
            with self.assertRaises(asyncio.CancelledError): self.retry()
        self.assertEqual(self.plan.trace_context['client_tool_correction']['outcome'],'cancelled')
    def test_sse_retry_response_parses_without_recursion(self):
        self.send.return_value=self.respond(self.good,sse=True)
        self.assertIsNotNone(self.retry())
        self.assertEqual(self.send.await_count,1)
    def test_stream_unknown_tool_corrected_has_one_completion(self):
        self.send.return_value=self.respond(self.good)
        events=self.collect(stream_events(self.bad['output']),trace_plan=self.plan,source_body=self.source)
        terminals=[data['response'] for kind,data in events if kind=='response.completed']
        self.assertEqual(len(terminals),1)
        self.assertNotIn('tool_conversion_rejected',json.dumps(events))
        calls=[v for v in terminals[0]['output'] if v.get('type')=='function_call']
        self.assertEqual([v['name'] for v in calls],['exec_command'])
        self.assertNotIn('bad',self.proxy.excel_upstream._native_call_cache)
        self.assertIn('good',self.proxy.excel_upstream._native_call_cache)
    def test_stream_malformed_transport_is_corrected(self):
        self.bad['output'][0]['arguments']=json.dumps({'code':'wrong script here'})
        self.send.return_value=self.respond(self.good)
        events=self.collect(stream_events(self.bad['output']),trace_plan=self.plan,source_body=self.source)
        self.assertEqual(sum(kind=='response.completed' for kind,_ in events),1)
        self.assertNotIn('tool_conversion_rejected',json.dumps(events))
    def test_stream_failed_correction_is_zero_dispatch_normal_completion(self):
        self.send.return_value=self.respond(self.bad)
        events=self.collect(stream_events(self.bad['output']),trace_plan=self.plan,source_body=self.source)
        self.assert_no_dispatched_tools(events)
        self.assertEqual(sum(kind=='response.completed' for kind,_ in events),1)
        self.assertFalse(any(kind=='response.failed' for kind,_ in events))
        self.assertEqual(self.send.await_count,1)
    def test_stream_text_explanation_is_emitted_once(self):
        self.send.return_value=self.respond(completed(message('Browser unavailable in current catalog.')))
        events=self.collect(stream_events(self.bad['output']),trace_plan=self.plan,source_body=self.source)
        self.assertEqual(sum(kind=='response.completed' for kind,_ in events),1)
        text=''.join(data.get('delta','') for kind,data in events if kind=='response.output_text.delta')
        self.assertEqual(text,'Browser unavailable in current catalog.')
        self.assert_no_dispatched_tools(events)
    def test_nonstream_path_is_corrected(self):
        self.send.side_effect=[self.respond(self.bad),self.respond(self.good)]
        with mock.patch.object(self.proxy,'_finish_usage_and_trace'):
            result=run_immediate(self.proxy._post_excel_non_streaming_request(self.plan,client_body=self.source))
        output=json.loads(result.body)['output']
        self.assertEqual([v['name'] for v in output if v.get('type')=='function_call'],['exec_command'])
        self.assertEqual(self.send.await_count,2)
    def test_original_failed_response_never_retries(self):
        self.bad['status']='failed'
        self.assertIsNone(self.retry()); self.send.assert_not_awaited()
    def test_retry_token_usage_counts_both_attempts(self):
        self.bad['usage']={'input_tokens':10,'output_tokens':2,'total_tokens':12}
        self.good['usage']={'input_tokens':20,'output_tokens':3,'total_tokens':23}
        self.send.return_value=self.respond(self.good)
        result=self.retry()
        self.assertEqual(result['usage']['total_tokens'],35)
        self.assertEqual(self.plan.trace_context['client_tool_correction']['total_usage']['input_tokens'],30)

if __name__=='__main__': unittest.main()
