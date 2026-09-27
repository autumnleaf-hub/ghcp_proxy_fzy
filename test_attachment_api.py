import asyncio,base64,sys,tempfile,unittest
from pathlib import Path
import httpx
from fastapi import FastAPI
from attachment_store import AttachmentStore,FileStoreError
from attachment_api import create_attachment_router

class AttachmentStoreTests(unittest.TestCase):
    def test_memory_store_preserves_bytes_and_hides_other_owners(self):
        s=AttachmentStore(persistent=False);m=s.put('folder/文件.txt',b'private','user_data','owner-a')
        self.assertEqual(m['filename'],'文件.txt');self.assertNotIn('data',m);self.assertNotIn('owner_scope',m)
        self.assertEqual(s.get(m['id'],'owner-a')['data'],b'private')
        with self.assertRaises(FileStoreError) as e:s.get(m['id'],'owner-b')
        self.assertEqual(e.exception.status_code,404);self.assertEqual(s.list('owner-b'),[])
    def test_quota_and_size_limits(self):
        s=AttachmentStore(persistent=False,max_file_bytes=4,max_total_bytes=6,max_files=2)
        s.put('one.txt',b'abcd')
        with self.assertRaises(FileStoreError):s.put('large.txt',b'abcde')
        with self.assertRaises(FileStoreError):s.put('two.txt',b'abc')
    def test_expiry_removes_memory_upload(self):
        now=[100];s=AttachmentStore(persistent=False,ttl_seconds=10,clock=lambda:now[0]);m=s.put('a.txt',b'a');now[0]=111
        with self.assertRaises(FileStoreError):s.get(m['id'])
        self.assertEqual(s.list(),[])
    def test_delete_only_authorized_upload(self):
        s=AttachmentStore(persistent=False);m=s.put('a.txt',b'a',owner='a')
        with self.assertRaises(FileStoreError):s.delete(m['id'],'b')
        self.assertEqual(s.get(m['id'],'a')['data'],b'a');self.assertTrue(s.delete(m['id'],'a')['deleted'])
        with self.assertRaises(FileStoreError):s.get(m['id'],'a')
    def test_invalid_ids_and_header_filename_rejected(self):
        s=AttachmentStore(persistent=False)
        for value in ('../secret','file-ghcp-../x',None):
            with self.assertRaises(FileStoreError):s.get(value)
        with self.assertRaises(FileStoreError):s.put('bad'+chr(10)+'.txt',b'x')
    def test_persistent_roundtrip_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as d:
            kwargs={} if sys.platform=='win32' else {'encrypt':lambda b:b'protected:'+base64.b64encode(b),'decrypt':lambda b:base64.b64decode(b.removeprefix(b'protected:'))}
            s=AttachmentStore(d,persistent=True,**kwargs);data=b'PRIVATE_ATTACHMENT_FIXTURE_028193';m=s.put('a.txt',data,owner='a')
            files=list(Path(d).glob('*.dpapi'));self.assertEqual(len(files),1);self.assertNotIn(data,files[0].read_bytes())
            reopened=AttachmentStore(d,persistent=True,**kwargs);self.assertEqual(reopened.get(m['id'],'a')['data'],data)
            with self.assertRaises(FileStoreError):reopened.get(m['id'],'b')
            self.assertTrue(reopened.delete(m['id'],'a')['deleted'])

class AttachmentAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=AttachmentStore(persistent=False,max_file_bytes=4096)
        app=FastAPI();app.include_router(create_attachment_router(self.store,8001))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',12345)),base_url='http://127.0.0.1:8001',headers={'Authorization':'Bearer fixture-owner-a'})
    async def asyncTearDown(self):await self.client.aclose()
    async def upload(self):return await self.client.post('/v1/files',data={'purpose':'user_data'},files={'file':('文件.txt',b'actual bytes','text/plain')})
    async def test_create_retrieve_content_delete(self):
        r=await self.upload();self.assertEqual(r.status_code,200);m=r.json();fid=m['id']
        self.assertEqual(m['bytes'],12);self.assertEqual(m['filename'],'文件.txt')
        self.assertEqual((await self.client.get('/v1/files/'+fid)).status_code,200)
        content=await self.client.get('/v1/files/'+fid+'/content');self.assertEqual(content.content,b'actual bytes');self.assertEqual(content.headers['cache-control'],'no-store')
        self.assertTrue((await self.client.delete('/v1/files/'+fid)).json()['deleted'])
        self.assertEqual((await self.client.get('/v1/files/'+fid)).status_code,404)
    async def test_other_key_cannot_list_read_or_delete(self):
        fid=(await self.upload()).json()['id'];headers={'Authorization':'Bearer fixture-owner-b'}
        self.assertEqual((await self.client.get('/v1/files',headers=headers)).json()['data'],[])
        for method,path in (('GET',''),('GET','/content'),('DELETE','')):
            self.assertEqual((await self.client.request(method,'/v1/files/'+fid+path,headers=headers)).status_code,404)
        self.assertEqual((await self.client.get('/v1/files/'+fid)).status_code,200)
    async def test_bearer_key_is_required(self):
        r=await self.client.get('/v1/files',headers={'Authorization':''});self.assertEqual(r.status_code,401)
    async def test_cross_origin_and_host_are_rejected(self):
        for headers in ({'Origin':'https://untrusted.example'},{'Host':'untrusted.example:8001'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual((await self.client.get('/v1/files',headers=headers)).status_code,403)
    async def test_remote_client_is_rejected(self):
        app=FastAPI();app.include_router(create_attachment_router(self.store,8001))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('192.0.2.1',1)),base_url='http://127.0.0.1:8001',headers={'Authorization':'Bearer a'}) as c:
            self.assertEqual((await c.get('/v1/files')).status_code,403)
    async def test_missing_file_and_wrong_content_type(self):
        self.assertEqual((await self.client.post('/v1/files',json={'file':'not multipart'})).status_code,415)
        self.assertEqual((await self.client.post('/v1/files',files={'wrong':('x.txt',b'x')})).status_code,400)
    async def test_file_size_limit(self):
        r=await self.client.post('/v1/files',files={'file':('too-large.txt',b'x'*4097)});self.assertEqual(r.status_code,413)
    async def test_listing_and_pagination(self):
        await self.upload();await self.upload();r=(await self.client.get('/v1/files?limit=1')).json()
        self.assertEqual(len(r['data']),1);self.assertTrue(r['has_more'])
        second=(await self.client.get('/v1/files',params={'limit':1,'after':r['last_id']})).json();self.assertEqual(len(second['data']),1);self.assertFalse(second['has_more'])
        self.assertEqual((await self.client.get('/v1/files?limit=0')).status_code,400)

if __name__=='__main__':unittest.main()
