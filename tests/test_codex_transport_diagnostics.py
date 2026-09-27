import json
import unittest
import client_tool_transport as t

def native(code):
    return {'type':'function_call','name':'run_officejs','arguments':json.dumps({'code':code})}

class TransportDiagnosticTests(unittest.TestCase):
    def test_literal_whitespace_keeps_exact_function_values(self):
        value='alpha'+chr(10)+'beta'+chr(13)+chr(10)+chr(9)+'end'
        encoded=json.dumps({'name':'exec_command','arguments':{'cmd':value}})
        for ch in (chr(9),chr(10),chr(13)):
            encoded=encoded.replace(json.dumps(ch)[1:-1],ch)
        result=t.decode_transport_envelope(native(encoded))
        self.assertEqual(result['arguments'],{'cmd':value})

    def test_literal_custom_input_roundtrips(self):
        value='first'+chr(10)+'second'+chr(9)+'中文'
        encoded=json.dumps({'name':'apply_patch','input':value},ensure_ascii=False)
        for ch in (chr(9),chr(10)):
            encoded=encoded.replace(json.dumps(ch)[1:-1],ch)
        self.assertEqual(t.decode_transport_envelope(native(encoded))['input'],value)

    def test_actual_controls_after_odd_even_slash_runs_preserve_values(self):
        for control in (chr(9),chr(10),chr(13)):
            for count in range(9):
                code='{"name":"exec_command","arguments":{"cmd":"prefix '+chr(92)*count+control+'tail"}}'
                result=t.decode_transport_envelope(native(code))
                self.assertEqual(result['arguments']['cmd'],'prefix '+chr(92)*(count//2)+control+'tail')

    def test_valid_literal_backslash_n_is_not_unescaped_again(self):
        values=[chr(92)+'n',chr(92)*2+'n',chr(92)+chr(10),chr(92)+chr(13)+chr(10)]
        for value in values:
            code=json.dumps({'name':'exec_command','arguments':{'cmd':value}})
            self.assertEqual(t.decode_transport_envelope(native(code))['arguments']['cmd'],value)

    def test_other_controls_still_rejected(self):
        for n in (0,1,8,11,12,31):
            bad='{"name":"exec_command","arguments":{"cmd":"a'+chr(n)+'b"}}'
            self.assertIsNone(t.decode_transport_envelope(native(bad)))

    def test_inner_arguments_string_not_repaired(self):
        args='{"cmd":"a'+chr(10)+'b"}'
        code=json.dumps({'name':'exec_command','arguments':args})
        self.assertIsNone(t.decode_transport_envelope(native(code)))

    def test_bad_quote_diagnostic_hides_values(self):
        bad='{"name":"exec_command","arguments":{"cmd":"PRIVATE_SENTINEL"bad"}}'
        detail=t.diagnose_transport_envelope_details(native(bad))
        self.assertEqual(detail['phase'],'invalid_transport_code_json')
        self.assertEqual(detail['json_error'],'missing_delimiter')
        self.assertGreater(detail['offset'],0)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(detail))
        self.assertNotIn('exec_command',json.dumps(detail))

    def test_script_and_concatenated_calls_never_repaired(self):
        call=json.dumps({'name':'exec_command','arguments':{'cmd':'fixed'}})
        for bad in ('const payload='+call+';',call+call,'return '+call):
            self.assertIsNone(t.decode_transport_envelope(native(bad)))

    def test_valid_envelope_has_no_failure_details(self):
        call=json.dumps({'name':'exec_command','arguments':{'cmd':'fixed'}})
        self.assertEqual(t.diagnose_transport_envelope_details(native(call)),{})

if __name__=='__main__':
    unittest.main()
