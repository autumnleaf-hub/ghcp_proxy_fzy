import copy
import asyncio
from collections import OrderedDict
from fastapi.responses import JSONResponse
import unittest
from unittest import mock

import excel_upstream
import format_translation
import proxy


class ExcelImageCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def materialize(self, body):
        async def upload(url, headers):
            return 'file-' + url.rsplit(',', 1)[-1]
        with mock.patch.object(proxy, '_excel_file_id_for_image', side_effect=upload) as upload_mock:
            result = await proxy._materialize_excel_inline_images(body, {})
        return result, upload_mock

    async def test_images_without_text_keep_every_image_in_order(self):
        for count in (1, 2, 16, 129):
            with self.subTest(count=count):
                images = [{'type': 'input_image', 'image_url': f'data:image/png;base64,{i}', 'detail': 'high'} for i in range(count)]
                body = {'model': 'gpt-6-astra', 'input': [{'role': 'user', 'content': images}]}
                before = copy.deepcopy(body)
                result, upload = await self.materialize(body)
                wire = excel_upstream.prepare_responses_body(result)
                content = wire['input'][-1]['content']
                self.assertEqual([x['file_id'] for x in content], [f'file-{i}' for i in range(count)])
                self.assertEqual(upload.call_count, count)
                self.assertEqual(body, before)

    async def test_tool_images_remain_inline_when_user_adds_images(self):
        tool_image = {'type': 'input_image', 'image_url': 'data:image/png;base64,tool', 'detail': 'high'}
        for count in (0, 1, 3):
            with self.subTest(user_image_count=count):
                body = {'input': [
                    {'type': 'function_call_output', 'call_id': 'call-image', 'output': [tool_image]},
                    {'role': 'user', 'content': [{'type': 'input_image', 'image_url': f'data:image/png;base64,user{i}'} for i in range(count)]},
                ]}
                original = copy.deepcopy(body)
                result, upload = await self.materialize(body)
                self.assertEqual(result['input'][0]['output'], [tool_image])
                self.assertEqual(upload.call_count, count)
                self.assertEqual(body, original)
                self.assertTrue(proxy._request_headers_module.has_vision_input(result['input']))

    async def test_text_and_images_keep_original_order(self):
        parts = [{'type': 'input_text', 'text': 'Compare'}, {'type': 'input_image', 'image_url': 'data:image/png;base64,a'}, {'type': 'input_text', 'text': 'with'}, {'type': 'input_image', 'image_url': 'data:image/png;base64,b'}]
        result, _ = await self.materialize({'input': [{'role': 'user', 'content': parts}]})
        self.assertEqual([x['type'] for x in result['input'][0]['content']], [x['type'] for x in parts])
        self.assertEqual(result['input'][0]['content'][2]['text'], 'with')

    async def test_wrapped_image_url_is_uploaded_and_detail_preserved(self):
        result, upload = await self.materialize({'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': {'url': 'data:image/png;base64,a', 'detail': 'high'}}]}]})
        self.assertEqual(result['input'][0]['content'][0], {'type': 'input_image', 'file_id': 'file-a', 'detail': 'high'})
        self.assertEqual(upload.call_count, 1)

    async def test_image_base64_form_is_uploaded(self):
        result, upload = await self.materialize({'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_base64': 'a', 'media_type': 'image/png'}]}]})
        self.assertEqual(result['input'][0]['content'][0], {'type': 'input_image', 'file_id': 'file-a'})
        self.assertEqual(upload.call_count, 1)

    async def test_file_id_and_remote_url_do_not_upload(self):
        parts = [{'type': 'input_image', 'file_id': 'file-existing', 'detail': 'auto'}, {'type': 'input_image', 'image_url': 'https://example.test/a.png'}]
        result, upload = await self.materialize({'input': [{'role': 'user', 'content': parts}]})
        self.assertEqual(result['input'][0]['content'], parts)
        upload.assert_not_called()

    async def test_malformed_image_is_not_forwarded_as_opaque_422(self):
        for image in ({'type': 'input_image'}, {'type': 'input_image', 'image_url': {'url': 42}}, {'type': 'input_image', 'file_id': 'f', 'image_url': 'https://example.test/a.png'}):
            with self.subTest(image=image), self.assertRaises(proxy.ExcelInlineImageUploadError):
                await self.materialize({'input': [{'role': 'user', 'content': [image]}]})

    def test_chat_image_url_accepts_string_and_preserves_detail(self):
        for value in ('data:image/png;base64,a', {'url': 'data:image/png;base64,a', 'detail': 'high'}):
            result = format_translation._chat_content_item_to_response_content({'type': 'image_url', 'image_url': value, 'detail': 'high'})
            self.assertEqual(result, {'type': 'input_image', 'image_url': 'data:image/png;base64,a', 'detail': 'high'})

    def test_image_cache_is_scoped_to_actual_bps_account_header(self):
        key = proxy._excel_image_cache_key('image', {'x-openai-account-id': 'one'})
        other = proxy._excel_image_cache_key('image', {'x-openai-account-id': 'two'})
        self.assertNotEqual(key, other)


    async def test_empty_text_does_not_drop_images(self):
        for text in ('', '  '):
            parts = [{'type': 'input_text', 'text': text}, {'type': 'input_image', 'image_url': 'data:image/png;base64,a'}]
            result, upload = await self.materialize({'input': [{'role': 'user', 'content': parts}]})
            self.assertEqual(result['input'][0]['content'][0], parts[0])
            self.assertEqual(result['input'][0]['content'][1]['file_id'], 'file-a')
            self.assertEqual(upload.call_count, 1)

    async def test_tool_schema_and_metadata_are_not_rewritten(self):
        image = {'type': 'input_image', 'image_url': 'data:image/png;base64,private'}
        body = {'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,a'}], 'metadata': image}], 'tools': [image]}
        result, upload = await self.materialize(body)
        self.assertEqual(result['tools'], [image])
        self.assertEqual(result['input'][0]['metadata'], image)
        self.assertEqual(upload.call_count, 1)

    async def test_duplicate_images_reuse_cache_and_accounts_do_not(self):
        upload = mock.AsyncMock(side_effect=['file-one', 'file-two'])
        with mock.patch.object(proxy, '_excel_image_file_ids', OrderedDict()), mock.patch.object(proxy, '_excel_image_file_ids_lock', asyncio.Lock()), mock.patch.object(proxy, '_save_excel_image_file_ids'), mock.patch.object(proxy, '_upload_excel_inline_image', upload):
            first = await proxy._excel_file_id_for_image('data:image/png;base64,a', {'x-openai-account-id': 'one'})
            again = await proxy._excel_file_id_for_image('data:image/png;base64,a', {'X-OpenAI-Account-ID': 'one'})
            other = await proxy._excel_file_id_for_image('data:image/png;base64,a', {'x-openai-account-id': 'two'})
        self.assertEqual((first, again, other), ('file-one', 'file-one', 'file-two'))
        self.assertEqual(upload.await_count, 2)

    async def test_base64_error_identifies_image_position(self):
        with mock.patch.object(proxy, '_excel_file_id_for_image', side_effect=proxy.ExcelImageInputError('invalid base64')):
            with self.assertRaisesRegex(proxy.ExcelImageInputError, r'input\[0\]\.content\[1\]'):
                await proxy._materialize_excel_inline_images({'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': ''}, {'type': 'input_image', 'image_url': 'data:image/png;base64,invalid'}]}]}, {})

    async def test_handler_reports_client_error_without_contacting_upstream(self):
        with (
            mock.patch.object(proxy.excel_session_capture, 'refresh_macos_excel_session'),
            mock.patch.object(proxy.excel_session_capture, 'refresh_windows_excel_session'),
            mock.patch.object(proxy.openai_oauth.login_service, 'ensure_session'),
            mock.patch.object(proxy.bps_credentials.credential_pool, 'migrate_legacy'),
            mock.patch.object(proxy.bps_credentials.credential_pool, 'acquire',
                              return_value=proxy.bps_credentials.CredentialSelection('test-account', {})),
            mock.patch.object(proxy.bps_credentials.credential_pool, 'record_result'),
            mock.patch.object(proxy, '_prepare_upstream_request') as forward,
        ):
            result = await proxy._handle_excel_responses(mock.Mock(headers={}), {'model': 'gpt-6-astra', 'input': [{'role': 'user', 'content': [{'type': 'input_image'}]}]})
        self.assertEqual(result.status_code, 400)
        self.assertIn(b'input[0].content[0]', result.body)
        forward.assert_not_called()

    async def test_handler_preserves_tool_image_and_all_user_images(self):
        tool_image = {'type': 'input_image', 'image_url': 'data:image/png;base64,tool'}
        body = {'model': 'gpt-6-astra', 'input': [
            {'type': 'function_call_output', 'call_id': 'call-image', 'output': [tool_image]},
            {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,a'}, {'type': 'input_image', 'image_url': 'data:image/png;base64,b'}]},
        ]}
        upload = mock.AsyncMock(side_effect=['file-a', 'file-b'])
        with (
            mock.patch.object(proxy.excel_session_capture, 'refresh_macos_excel_session'),
            mock.patch.object(proxy.excel_session_capture, 'refresh_windows_excel_session'),
            mock.patch.object(proxy.openai_oauth.login_service, 'ensure_session'),
            mock.patch.object(proxy.bps_credentials.credential_pool, 'migrate_legacy'),
            mock.patch.object(proxy.bps_credentials.credential_pool, 'acquire',
                              return_value=proxy.bps_credentials.CredentialSelection('test-account', {})),
            mock.patch.object(proxy.bps_credentials.credential_pool, 'record_result'),
            mock.patch.object(proxy, '_excel_file_id_for_image', upload),
            mock.patch.object(proxy, '_prepare_upstream_request', return_value=(mock.Mock(), None)) as forward,
            mock.patch.object(proxy, '_post_excel_non_streaming_request', mock.AsyncMock(return_value=JSONResponse({'ok': True}))),
        ):
            result = await proxy._handle_excel_responses(mock.Mock(headers={}), body)
        self.assertEqual(result.status_code, 200)
        wire = forward.call_args.kwargs['body']['input']
        self.assertEqual(wire[-2]['output'], [tool_image])
        self.assertEqual([p['file_id'] for p in wire[-1]['content']], ['file-a', 'file-b'])
        self.assertEqual(upload.await_count, 2)
        self.assertEqual(forward.call_args.kwargs['header_builder'](None, None)['Copilot-Vision-Request'], 'true')

    def test_data_url_with_folded_base64_is_accepted(self):
        mime, data = proxy._decode_excel_inline_image('data:image/png;base64, Y Q== ')
        self.assertEqual((mime, data), ('image/png', b'a'))

    def test_cache_falls_back_to_credentials_without_account_id(self):
        self.assertNotEqual(proxy._excel_image_cache_key('image', {'Authorization': 'one'}), proxy._excel_image_cache_key('image', {'Authorization': 'two'}))


if __name__ == '__main__':
    unittest.main()
