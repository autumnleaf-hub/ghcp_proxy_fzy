import unittest
from tests.test_codex_bridge_regressions import OfflineBase
import excel_upstream as b

class NativeParallelControlTests(OfflineBase):
    def wire(self,**extra):
        return b.prepare_responses_body({'model':'gpt-6-sol','input':[],**extra})
    def test_normal_clients_remain_unchanged(self):
        for value in (True,False):
            self.assertNotIn('parallel_tool_calls',self.wire(parallel_tool_calls=value))
    def test_probe_forwards_both_booleans(self):
        for value in (True,False):
            wire=self.wire(parallel_tool_calls=value,metadata={'ghcp_native_parallel_probe':True})
            self.assertIs(wire['parallel_tool_calls'],value)
    def test_local_marker_never_reaches_upstream_metadata(self):
        wire=self.wire(parallel_tool_calls=True,metadata={'ghcp_native_parallel_probe':True,'fixture':'native'})
        self.assertNotIn('ghcp_native_parallel_probe',wire['metadata'])
        self.assertEqual(wire['metadata']['fixture'],'native')
    def test_nonboolean_values_are_not_forwarded(self):
        for value in ('true',1,None,[]):
            self.assertNotIn('parallel_tool_calls',self.wire(parallel_tool_calls=value,metadata={'ghcp_native_parallel_probe':True}))
    def test_missing_control_not_invented(self):
        self.assertNotIn('parallel_tool_calls',self.wire(metadata={'ghcp_native_parallel_probe':True}))
    def test_marker_requires_exact_true(self):
        for marker in ('true',1,False,None):
            self.assertNotIn('parallel_tool_calls',self.wire(parallel_tool_calls=True,metadata={'ghcp_native_parallel_probe':marker}))
    def test_non_object_metadata_is_safe(self):
        self.assertNotIn('parallel_tool_calls',self.wire(parallel_tool_calls=True,metadata=[]))
    def test_verified_client_batch_capability_is_advertised(self):
        self.assertTrue(all(v['parallel_tool_calls'] is True for v in b.LOCAL_MODEL_CAPABILITIES.values()))

if __name__=='__main__': unittest.main()
