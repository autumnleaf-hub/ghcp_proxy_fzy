"""Bounded local attachment staging; never execute or index uploaded content."""
import base64,json,os,re,sys,threading,time,uuid,hmac
from pathlib import Path,PurePosixPath,PureWindowsPath
from app_paths import user_state_dir

class FileStoreError(ValueError):
    def __init__(self,message,status_code=400):
        super().__init__(message);self.status_code=status_code

class AttachmentStore:
    ID_PATTERN=re.compile(r'^file-ghcp-[0-9a-f]{32}$')
    def __init__(self,root=None,*,persistent=None,max_file_bytes=20*1024*1024,max_total_bytes=128*1024*1024,max_files=64,ttl_seconds=86400,clock=time.time,encrypt=None,decrypt=None):
        self.root=Path(root or Path(user_state_dir())/'attachments').resolve()
        self.persistent=(sys.platform=='win32') if persistent is None else persistent
        self.max_file_bytes=max_file_bytes;self.max_total_bytes=max_total_bytes;self.max_files=max_files;self.ttl_seconds=ttl_seconds;self.clock=clock
        self.encrypt=encrypt;self.decrypt=decrypt;self._lock=threading.RLock();self._memory={}
    def _protect(self,data):
        if self.encrypt:return self.encrypt(data)
        from excel_upstream import _protect_windows_data
        return _protect_windows_data(data)
    def _unprotect(self,data):
        if self.decrypt:return self.decrypt(data)
        from excel_upstream import _unprotect_windows_data
        return _unprotect_windows_data(data)
    def _path(self,file_id):
        if not isinstance(file_id,str) or not self.ID_PATTERN.fullmatch(file_id):raise FileStoreError('Unknown local file ID',404)
        p=self.root/(file_id+'.dpapi')
        if p.is_symlink() or p.resolve().parent!=self.root:raise FileStoreError('Invalid file storage entry',400)
        return p
    def _prune(self):
        cutoff=self.clock()-self.ttl_seconds
        if self.persistent and self.root.exists():
            for p in self.root.glob('file-ghcp-*.dpapi'):
                if not self.ID_PATTERN.fullmatch(p.stem):continue
                p=self._path(p.stem)
                if p.stat().st_mtime<cutoff:p.unlink()
        for key in list(self._memory):
            if self._memory[key]['created_at']<cutoff:del self._memory[key]
    @staticmethod
    def metadata(record):
        return {k:record[k] for k in ('id','object','bytes','created_at','filename','purpose','status','expires_at')}
    def put(self,filename,data,purpose='user_data',owner='internal'):
        if not isinstance(filename,str) or not filename or any(ord(c)<32 for c in filename):raise FileStoreError('Invalid filename')
        filename=PureWindowsPath(PurePosixPath(filename).name).name
        if not filename or filename in ('.','..') or len(filename)>255:raise FileStoreError('Invalid filename')
        if not isinstance(data,bytes):raise FileStoreError('File data must be bytes')
        if len(data)>self.max_file_bytes:raise FileStoreError('File exceeds the 20 MiB upload limit',413)
        if not isinstance(purpose,str) or not purpose or len(purpose)>64 or any(ord(c)<32 for c in purpose):raise FileStoreError('Invalid file purpose')
        with self._lock:
            self._prune();now=int(self.clock());fid='file-ghcp-'+uuid.uuid4().hex
            record={'id':fid,'object':'file','bytes':len(data),'created_at':now,'filename':filename,'purpose':purpose,'status':'processed','expires_at':now+self.ttl_seconds,'owner_scope':owner,'data':data}
            if not self.persistent:
                if len(self._memory)>=self.max_files or sum(r['bytes'] for r in self._memory.values())+len(data)>self.max_total_bytes:raise FileStoreError('Local upload storage is full',413)
                self._memory[fid]=record;return self.metadata(record)
            self.root.mkdir(parents=True,exist_ok=True)
            entries=[self._path(p.stem) for p in self.root.glob('file-ghcp-*.dpapi') if self.ID_PATTERN.fullmatch(p.stem)]
            payload={**self.metadata(record),'owner_scope':owner,'data_base64':base64.b64encode(data).decode('ascii')}
            try:protected=self._protect(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
            except Exception as exc:raise FileStoreError('Encrypted file storage is unavailable',500) from exc
            if len(entries)>=self.max_files or sum(p.stat().st_size for p in entries)+len(protected)>self.max_total_bytes:raise FileStoreError('Local upload storage is full',413)
            path=self._path(fid);temp=self.root/('upload-'+uuid.uuid4().hex+'.tmp')
            try:
                with temp.open('xb') as f:f.write(protected);f.flush();os.fsync(f.fileno())
                os.replace(temp,path)
            finally:
                if temp.exists():temp.unlink()
            return self.metadata(record)
    def get(self,file_id,owner='internal'):
        with self._lock:
            path=self._path(file_id);self._prune()
            if not self.persistent:
                record=self._memory.get(file_id)
                if record is None:raise FileStoreError('File not found or expired; upload it again',404)
                if not hmac.compare_digest(record.get('owner_scope',''),owner):raise FileStoreError('File not found or expired; upload it again',404)
                return dict(record)
            if not path.is_file():raise FileStoreError('File not found or expired; upload it again',404)
            if path.stat().st_size>self.max_file_bytes*2+65536:raise FileStoreError('Invalid encrypted upload size',400)
            try:
                payload=json.loads(self._unprotect(path.read_bytes()).decode('utf-8'))
                if not hmac.compare_digest(payload.get('owner_scope',''),owner):raise FileStoreError('File not found or expired; upload it again',404)
                encoded=payload.pop('data_base64')
                if not isinstance(encoded,str) or len(encoded)>((self.max_file_bytes+2)//3)*4:raise ValueError('size')
                data=base64.b64decode(encoded,validate=True)
                if payload.get('id')!=file_id or payload.get('bytes')!=len(data) or len(data)>self.max_file_bytes:raise ValueError('identity')
                if payload['expires_at']<=self.clock():raise FileStoreError('File not found or expired; upload it again',404)
                return {**payload,'data':data}
            except FileStoreError:raise
            except Exception as exc:raise FileStoreError('Cannot read encrypted file storage entry',500) from exc
    def list(self,owner='internal'):
        with self._lock:
            self._prune()
            ids=([p.stem for p in self.root.glob('file-ghcp-*.dpapi') if self.ID_PATTERN.fullmatch(p.stem)] if self.root.exists() else []) if self.persistent else list(self._memory)
            records=[]
            for fid in ids:
                try:records.append(self.metadata(self.get(fid,owner)))
                except FileStoreError as exc:
                    if exc.status_code!=404:raise

            return sorted(records,key=lambda r:(r['created_at'],r['id']),reverse=True)
    def delete(self,file_id,owner='internal'):
        with self._lock:
            self.get(file_id,owner)
            if self.persistent:self._path(file_id).unlink()
            else:del self._memory[file_id]
            return {'id':file_id,'object':'file','deleted':True}
