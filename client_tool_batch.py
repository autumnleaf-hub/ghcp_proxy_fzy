"""Explicit client-tool batches: pure bounded decoding and stateless replay."""
import base64
import copy
import hashlib
import json
import re
import client_tool_transport as transport

BATCH_TYPE='client_tool_batch'
MAX_BATCH_CALLS=16
CHILD_PREFIX='cb1.'
_CHILD=re.compile(r'^cb1[.]([0-9a-f]{1,2})[.]([0-9a-f]{1,2})[.]([A-Za-z0-9_-]+)$')
_CALL_TYPES={'function_call','custom_tool_call'}
_RESULT_TYPES={'function_call_output','custom_tool_call_output'}
_CONTENT_TYPES={'input_text','output_text','text','input_image','image_url','image','input_file','file'}

def _code(native):
    if type(native) is not dict or not transport._bounded_json(native): return None
    if native.get('type')!='function_call' or not isinstance(native.get('name'),str) or native['name'] not in transport.DEFAULT_TRANSPORT_NAMES: return None
    if native.keys() & transport._AMBIGUOUS_CALL_KEYS: return None
    ns=native.get('namespace')
    if ns is not None and (type(ns) is not str or not ns or ns!=ns.strip()): return None
    args=transport._decode_object(native.get('arguments'))
    if args is None or args.keys()-transport._TRANSPORT_ARGUMENT_KEYS: return None
    return transport.decode_transport_code(args.get('code'))

def is_batch_candidate(native):
    code=_code(native)
    return type(code) is dict and code.get('type')==BATCH_TYPE

def _entry(item):
    if type(item) is not dict or item.keys()-{'name','namespace','arguments','input'}: return None
    name=item.get('name')
    if type(name) is not str or not name or name!=name.strip() or name in transport.DEFAULT_TRANSPORT_NAMES: return None
    if 'namespace' in item and (type(item['namespace']) is not str or not item['namespace'] or item['namespace']!=item['namespace'].strip()): return None
    if ('arguments' in item)==('input' in item): return None
    result=copy.deepcopy(item)
    if 'arguments' in item:
        args=transport._decode_object(item['arguments'])
        if args is None: return None
        result['arguments']=args
    elif type(item['input']) is not str: return None
    return result if transport._bounded_json(result) else None

def _payload(native):
    code=_code(native)
    if type(code) is not dict or code.get('type')!=BATCH_TYPE: return None,'not_batch'
    if code.keys()!={'type','version','calls'}: return None,'invalid_batch_fields'
    if type(code.get('version')) is not int or code['version']!=1: return None,'unsupported_batch_version'
    calls=code.get('calls')
    if type(calls) is not list or not 1<=len(calls)<=MAX_BATCH_CALLS: return None,'invalid_batch_size'
    parent=native.get('call_id')
    if type(parent) is not str or not parent or parent!=parent.strip() or len(parent)>256: return None,'invalid_batch_parent_identity'
    normalized=[_entry(item) for item in calls]
    if any(item is None for item in normalized): return None,'invalid_batch_entry'
    return normalized,None

def batch_error(native):
    return _payload(native)[1]

def child_call_id(parent,index,count):
    encoded=base64.urlsafe_b64encode(parent.encode('utf-8')).decode('ascii').rstrip('=')
    return f'cb1.{count:x}.{index:x}.{encoded}'

def parse_child_call_id(value):
    if type(value) is not str or len(value)>1500: return None
    m=_CHILD.fullmatch(value)
    if not m: return None
    count,index=int(m[1],16),int(m[2],16)
    if not 1<=count<=MAX_BATCH_CALLS or not 0<=index<count: return None
    try:
        raw=base64.b64decode(m[3]+'='*((-len(m[3]))%4),altchars=b'-_',validate=True)
        parent=raw.decode('utf-8')
    except (ValueError,UnicodeError): return None
    if not parent or len(parent)>256 or parent!=parent.strip(): return None
    if child_call_id(parent,index,count)!=value: return None
    return parent,index,count

def _item_id(call_id):
    return 'fc_'+hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:48]

def _native(parent,calls):
    return {'type':'function_call','id':_item_id(parent),'call_id':parent,'name':'run_officejs','status':'completed','arguments':json.dumps({'summary':'Relay independent client tools','extended_summary':'Execute validated independent client calls and return indexed results','destructive':False,'references':[],'code':json.dumps({'type':BATCH_TYPE,'version':1,'calls':calls},ensure_ascii=False,separators=(',',':'))},ensure_ascii=False,separators=(',',':'))}

def expand_batch(native):
    calls,error=_payload(native)
    if error: return None
    result=[]
    for index,call in enumerate(calls):
        cid=child_call_id(native['call_id'],index,len(calls))
        result.append({'type':'function_call','id':_item_id(cid),'call_id':cid,'name':'run_officejs','status':'completed','arguments':json.dumps({'code':json.dumps(call,ensure_ascii=False,separators=(',',':'))},ensure_ascii=False,separators=(',',':'))})
    return result

def _history_call(item):
    name=item.get('name')
    call={'name':name}
    if 'namespace' in item: call['namespace']=item['namespace']
    if item.get('type')=='custom_tool_call': call['input']=item.get('input')
    else: call['arguments']=item.get('arguments')
    normalized=_entry(call)
    if normalized is None: raise ValueError('invalid_batch_history_call')
    return normalized

def _same_values(left,right):
    key='input' if 'input' in left else 'arguments'
    return key in right and left[key]==right[key]

def _result_parts(parent,children,results):
    parts=[{'type':'input_text','text':json.dumps({'type':'client_tool_batch_results','version':1,'parent_call_id':parent,'count':len(children)},separators=(',',':'))}]
    for index in range(len(children)):
        child=children[index][1]; result=results[index][1]
        label={'batch_index':index,'call_id':result['call_id'],'name':child['name']}
        if 'namespace' in child: label['namespace']=child['namespace']
        for key in ('is_error','error','status'):
            if key in result: label[key]=copy.deepcopy(result[key])
        parts.append({'type':'input_text','text':json.dumps(label,ensure_ascii=False,separators=(',',':'))})
        value=result['output']
        typed=(isinstance(value,list) and value and all(isinstance(v,dict) and isinstance(v.get('type'),str) and v['type'] in _CONTENT_TYPES for v in value))
        if typed: parts.extend(copy.deepcopy(value))
        else: parts.append({'type':'input_text','text':json.dumps({'output':value},ensure_ascii=False,separators=(',',':'))})
    return parts

def collapse_history(items,lookup_native=None):
    """Collapse a complete batch deterministically; never invent missing results."""
    if not isinstance(items,list): return items
    groups={}; member_positions={}
    for position,item in enumerate(items):
        if not isinstance(item,dict) or not isinstance(item.get('type'),str): continue
        kind=item['type']
        if kind not in _CALL_TYPES|_RESULT_TYPES: continue
        identity=parse_child_call_id(item.get('call_id'))
        if identity is None:
            if isinstance(item.get('call_id'),str) and item['call_id'].startswith(CHILD_PREFIX):
                raise ValueError('invalid_batch_child_identity')
            continue
        parent,index,count=identity
        group=groups.setdefault(parent,{'count':count,'calls':{},'results':{}})
        if group['count']!=count: raise ValueError('inconsistent_batch_count')
        target='calls' if kind in _CALL_TYPES else 'results'
        if index in group[target]: raise ValueError('duplicate_batch_child_or_result')
        if target=='calls': _history_call(item)
        elif 'output' not in item: raise ValueError('batch_result_missing_output')
        group[target][index]=(position,item)
        member_positions[position]=(parent,target)
    if not groups: return items
    replacements={}
    for parent,group in groups.items():
        count=group['count'];calls=group['calls'];results=group['results']
        expected=set(range(count))
        if set(calls)!=expected: raise ValueError('batch_missing_child_calls')
        if set(results)!=expected: raise ValueError('batch_missing_child_results')
        if max(v[0] for v in calls.values())>=min(v[0] for v in results.values()):
            raise ValueError('batch_result_precedes_call')
        envelopes=[_history_call(calls[index][1]) for index in range(count)]
        cached=lookup_native(parent) if lookup_native is not None else None
        if cached is not None:
            old,error=_payload(cached)
            if error or cached.get('call_id')!=parent or len(old)!=count or any(not _same_values(old[i],envelopes[i]) for i in range(count)):
                raise ValueError('batch_replay_cache_conflict')
            native=copy.deepcopy(cached)
        else: native=_native(parent,envelopes)
        first_call=min(v[0] for v in calls.values())
        last_result=max(v[0] for v in results.values())
        replacements[first_call]=native
        replacements[last_result]={'type':'function_call_output','call_id':parent,'output':_result_parts(parent,calls,results)}
    return [copy.deepcopy(replacements.get(position,item)) for position,item in enumerate(items) if position not in member_positions or position in replacements]
