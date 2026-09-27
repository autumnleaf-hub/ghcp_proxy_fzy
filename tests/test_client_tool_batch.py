import copy,json,unittest
from unittest import mock
import client_tool_batch as batch
import excel_upstream as bridge
import client_tool_recovery as recovery
from tests.test_codex_bridge_regressions import OfflineBase,OfflineStreamBase,LOCAL_TOOLS,native_call,stream_events,run_immediate

def parent(entries=None,call_id='call_batch_test',version=1):
    if entries is None: entries=[{'name':'exec_command','arguments':{'cmd':'echo first'}},{'name':'exec_command','arguments':{'cmd':'echo second'}}]
    return {'type':'function_call','id':'fc_original_batch','call_id':call_id,'name':'run_officejs','status':'completed','arguments':json.dumps({'code':json.dumps({'type':'client_tool_batch','version':version,'calls':entries})})}
def response(item):return {'id':'resp_batch','status':'completed','output':[item]}
def source(**extra):return {'model':'gpt-6-sol','tools':LOCAL_TOOLS,'parallel_tool_calls':True,**extra}
def history(native=None):
    native=native or parent();calls=bridge.extract_native_client_tool_calls(response(native),source(),remember=False)
    outs=[{'type':'custom_tool_call_output' if c['type']=='custom_tool_call' else 'function_call_output','call_id':c['call_id'],'output':str(i+7)} for i,c in enumerate(calls)]
    return calls,outs

class BatchProtocolTests(OfflineBase):
    def test_expands_two_children_with_reversible_stable_ids(self):
        native=parent();children=batch.expand_batch(native)
        self.assertEqual(children,batch.expand_batch(native));self.assertEqual(len(children),2)
        for i,c in enumerate(children):self.assertEqual(batch.parse_child_call_id(c['call_id']),('call_batch_test',i,2))
        self.assertNotEqual(children[0]['call_id'],children[1]['call_id'])
    def test_other_parent_cannot_collide(self):
        a=batch.expand_batch(parent())[0]['call_id'];b=batch.expand_batch(parent(call_id='other'))[0]['call_id']
        self.assertNotEqual(a,b)
    def test_bad_versions_sizes_and_fields_rejected(self):
        for version in (True,2,'1',None):self.assertIsNone(batch.expand_batch(parent(version=version)))
        for calls in ([],[{'name':'exec_command','arguments':{}}]*17):self.assertIsNone(batch.expand_batch(parent(calls)))
        for entry in ({'name':'exec_command','arguments':{},'call_id':'spoof'},{'name':'run_officejs','arguments':{}},{'name':'x','arguments':{},'input':'both'}):
            self.assertIsNone(batch.expand_batch(parent([entry])))
    def test_namespace_and_custom_input_are_exact(self):
        raw='line one'+chr(10)+'中文'+chr(92)
        native=parent([{'name':'x','namespace':'scope','arguments':{}},{'name':'apply_patch','input':raw}])
        children=batch.expand_batch(native)
        import client_tool_transport as t
        self.assertEqual(t.decode_transport_envelope(children[0])['namespace'],'scope')
        self.assertEqual(t.decode_transport_envelope(children[1])['input'],raw)
    def test_malformed_reserved_ids_rejected(self):
        for value in ('cb1.0.0.YQ','cb1.2.2.YQ','cb1.2.0.!!!!','cb1.02.0.YQ'):
            self.assertIsNone(batch.parse_child_call_id(value))
    def test_shape_checks_do_not_raise_for_unhashable_names(self):
        native=parent();native['name']={}
        self.assertFalse(batch.is_batch_candidate(native));self.assertIsNone(batch.expand_batch(native))
    def test_atomic_validation_caches_only_parent(self):
        native=parent();calls=bridge.extract_native_client_tool_calls(response(native),source())
        self.assertEqual(len(calls),2);self.assertEqual(list(bridge._native_call_cache),['call_batch_test'])
    def test_unknown_child_rejects_whole_batch_without_caching(self):
        native=parent([{'name':'exec_command','arguments':{'cmd':'fixed'}},{'name':'not_available','arguments':{}}])
        self.assertEqual(bridge.extract_native_client_tool_calls(response(native),source()),[])
        self.assertEqual(dict(bridge._native_call_cache),{})
    def test_wrong_schema_rejects_valid_sibling(self):
        native=parent([{'name':'exec_command','arguments':{'cmd':'fixed'}},{'name':'exec_command','arguments':{'cmd':False}}])
        self.assertEqual(bridge.extract_native_client_tool_calls(response(native),source()),[])
        self.assertEqual(dict(bridge._native_call_cache),{})
    def test_parallel_false_and_forced_choice_remain_enforced(self):
        self.assertEqual(bridge.extract_native_client_tool_calls(response(parent()),source(parallel_tool_calls=False)),[])
        native=parent([{'name':'exec_command','arguments':{'cmd':'fixed'}},{'name':'view_image','arguments':{'path':'fixed.png'}}])
        self.assertEqual(bridge.extract_native_client_tool_calls(response(native),source(tool_choice={'type':'function','name':'exec_command'})),[])
    def test_duplicate_parents_are_rejected_atomically(self):
        p=parent();r={'output':[p,copy.deepcopy(p)]}
        self.assertEqual(bridge.extract_native_client_tool_calls(r,source()),[])
    def test_batch_before_later_visible_message_is_safely_rejected(self):
        r={'output':[parent(),{'type':'message','id':'m','content':[],'role':'assistant'}]}
        self.assertEqual(bridge.extract_native_client_tool_calls(r,source()),[])
    def test_recovery_dry_validation_never_caches(self):
        self.assertTrue(recovery.valid_tool_batch(response(parent()),source()))
        self.assertEqual(dict(bridge._native_call_cache),{})
    def test_reverse_result_arrival_restores_one_parent(self):
        native=parent();calls,outs=history(native);raw=calls+list(reversed(outs));before=copy.deepcopy(raw)
        merged=batch.collapse_history(raw,lambda p:native)
        self.assertEqual(raw,before);self.assertEqual(len(merged),2);self.assertEqual(merged[0],native)
        self.assertEqual(merged[1]['call_id'],native['call_id'])
        self.assertEqual(json.loads(merged[1]['output'][1]['text'])['batch_index'],0)
        self.assertEqual(json.loads(merged[1]['output'][2]['text'])['output'],'7')
    def test_restart_without_cache_rebuilds_batch_not_nested_transport(self):
        calls,outs=history();wire=bridge.translate_input_items(calls+outs)
        self.assertEqual(len(wire),2);self.assertTrue(batch.is_batch_candidate(wire[0]))
        self.assertEqual(wire[0]['call_id'],'call_batch_test');self.assertEqual(wire[1]['call_id'],'call_batch_test')
    def test_missing_duplicate_and_wrong_results_are_not_invented(self):
        calls,outs=history()
        for items in (calls,calls+outs[:1],calls+outs+[outs[0]],calls[:1]+outs):
            with self.assertRaises(ValueError):batch.collapse_history(items)
    def test_error_and_image_parts_survive_aggregation(self):
        calls,outs=history();image={'type':'input_image','image_url':'data:image/png;base64,fixture'}
        outs[0]['output']=[image];outs[1].update(output='Denied by client',is_error=True,status='cancelled')
        merged=batch.collapse_history(calls+outs)[1]['output']
        self.assertIn(image,merged);self.assertIn('cancelled',json.dumps(merged));self.assertIn('Denied by client',json.dumps(merged))
    def test_two_batches_do_not_mix_results(self):
        a,ao=history(parent(call_id='a'));b,bo=history(parent(call_id='b'))
        merged=batch.collapse_history(a+b+[ao[1],bo[0],ao[0],bo[1]])
        self.assertEqual([v['call_id'] for v in merged],['a','b','a','b'])
    def test_normal_history_is_unchanged(self):
        items=[{'role':'user','content':'ordinary'}];self.assertEqual(batch.collapse_history(items),items)

class BatchStreamTests(OfflineStreamBase):
    def test_stream_batch_has_contiguous_children_and_one_terminal(self):
        events=self.collect(stream_events([parent()]),source_body=source())
        terminal=[v for k,v in events if k=='response.completed'];self.assertEqual(len(terminal),1)
        self.assertEqual(len(terminal[0]['response']['output']),2)
        done=[v for k,v in events if k=='response.output_item.done' and v.get('item',{}).get('type')=='function_call']
        self.assertEqual([v['output_index'] for v in done],[0,1])
    def test_batch_and_following_native_call_do_not_reuse_indexes(self):
        events=self.collect(stream_events([parent(),native_call('single_after')]),source_body=source())
        done=[v for k,v in events if k=='response.output_item.done' and v.get('item',{}).get('type')=='function_call']
        self.assertEqual([v['output_index'] for v in done],[0,1,2])
        self.assertEqual(sum(k=='response.completed' for k,v in events),1)
    def test_incomplete_batch_request_returns_400_before_authentication(self):
        import proxy
        calls,outs=history();b={**source(),'input':calls+outs[:1]}
        with mock.patch.object(proxy.excel_session_capture,'refresh_windows_excel_session') as capture:
            result=run_immediate(proxy._handle_excel_responses(None,b))
        self.assertEqual(result.status_code,400);capture.assert_not_called()

if __name__=='__main__':unittest.main()
