"""Local-only Files API for staged attachments, not remote provider file IDs."""
import asyncio,mimetypes,hashlib
from urllib.parse import quote
from fastapi import APIRouter,Request
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse,Response
from attachment_store import FileStoreError

def file_owner_scope(request):
    scheme,_,token=request.headers.get('authorization','').partition(' ')
    if scheme.lower()!='bearer' or not token.strip() or len(token)>4096:
        raise FileStoreError('A Bearer client key is required for local file access',401)
    return hashlib.sha256(token.strip().encode('utf-8')).hexdigest()


def create_attachment_router(store,port):
    router=APIRouter()
    hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
    origins={scheme+'://'+host for scheme in ('http','https') for host in hosts}
    def guard(request):
        if request.client is None or request.client.host not in ('127.0.0.1','::1'):raise FileStoreError('File endpoints are loopback-only',403)
        if request.headers.get('host','').lower() not in hosts:raise FileStoreError('Unexpected file API host',403)
        origin=request.headers.get('origin')
        if origin and origin not in origins:raise FileStoreError('Cross-origin file access is not allowed',403)
        if request.headers.get('sec-fetch-site')=='cross-site':raise FileStoreError('Cross-site file access is not allowed',403)
        return file_owner_scope(request)
    def error(exc):
        return JSONResponse({'error':{'message':str(exc),'type':'invalid_request_error' if exc.status_code<500 else 'server_error','param':None,'code':'local_file_error'}},status_code=exc.status_code,headers={'Cache-Control':'no-store'})
    async def upload_form(request):
        if not request.headers.get('content-type','').lower().startswith('multipart/form-data'):raise FileStoreError('Use multipart/form-data with a file field',415)
        max_body=store.max_file_bytes+256*1024
        length=request.headers.get('content-length')
        if length is not None:
            try:declared=int(length)
            except ValueError:raise FileStoreError('Invalid content length')
            if declared<0:raise FileStoreError('Invalid content length')
            if declared>max_body:raise FileStoreError('Upload body exceeds the allowed limit',413)
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>max_body:raise FileStoreError('Upload body exceeds the allowed limit',413)
        sent=False
        async def receive():
            nonlocal sent
            if sent:return {'type':'http.disconnect'}
            sent=True;return {'type':'http.request','body':bytes(data),'more_body':False}
        clone=Request(request.scope,receive=receive)
        try:
            async with clone.form(max_files=1,max_fields=4) as form:
                file=form.get('file')
                if not isinstance(file,UploadFile):raise FileStoreError('A file field is required')
                purpose=form.get('purpose','user_data')
                if not isinstance(purpose,str):raise FileStoreError('Invalid purpose')
                content=await file.read(store.max_file_bytes+1)
                if len(content)>store.max_file_bytes:raise FileStoreError('File exceeds the upload limit',413)
                return file.filename or 'attachment',content,purpose
        except FileStoreError:raise
        except Exception as exc:raise FileStoreError('Invalid multipart upload') from exc
    @router.post('/v1/files')
    async def create_file(request:Request):
        try:
            owner=guard(request);filename,data,purpose=await upload_form(request)
            result=await asyncio.to_thread(store.put,filename,data,purpose,owner)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except FileStoreError as exc:return error(exc)
    @router.get('/v1/files')
    async def list_files(request:Request,limit:int=20,after:str|None=None,purpose:str|None=None):
        try:
            owner=guard(request)
            if not 1<=limit<=100:raise FileStoreError('limit must be between 1 and 100')
            items=await asyncio.to_thread(store.list,owner)
            if purpose is not None:items=[x for x in items if x['purpose']==purpose]
            if after is not None:
                pos=next((i for i,x in enumerate(items) if x['id']==after),None)
                if pos is None:raise FileStoreError('Unknown pagination cursor')
                items=items[pos+1:]
            page=items[:limit]
            return JSONResponse({'object':'list','data':page,'first_id':page[0]['id'] if page else None,'last_id':page[-1]['id'] if page else None,'has_more':len(items)>limit},headers={'Cache-Control':'no-store'})
        except FileStoreError as exc:return error(exc)
    @router.get('/v1/files/{file_id}/content')
    async def file_content(file_id:str,request:Request):
        try:
            owner=guard(request);record=await asyncio.to_thread(store.get,file_id,owner)
            mime=mimetypes.guess_type(record['filename'])[0] or 'application/octet-stream'
            disposition="attachment; filename*=UTF-8''"+quote(record['filename'],safe='')
            return Response(record['data'],media_type=mime,headers={'Content-Disposition':disposition,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox; default-src 'none'"})
        except FileStoreError as exc:return error(exc)
    @router.get('/v1/files/{file_id}')
    async def file_metadata(file_id:str,request:Request):
        try:
            owner=guard(request);record=await asyncio.to_thread(store.get,file_id,owner)
            return JSONResponse(store.metadata(record),headers={'Cache-Control':'no-store'})
        except FileStoreError as exc:return error(exc)
    @router.delete('/v1/files/{file_id}')
    async def delete_file(file_id:str,request:Request):
        try:
            owner=guard(request);result=await asyncio.to_thread(store.delete,file_id,owner)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except FileStoreError as exc:return error(exc)
    return router
