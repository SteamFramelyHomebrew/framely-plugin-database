#!/usr/bin/env python3
"""Validate pinned source registrations and generate a Framely catalog from author releases."""
import argparse, configparser, hashlib, json, os, pathlib, re, stat, struct, subprocess, tempfile, urllib.error, urllib.parse, urllib.request, zipfile
MAX_PACKAGE=64*1024*1024
MAX_EXPANDED=128*1024*1024
ID=re.compile(r'[a-z0-9][a-z0-9._-]{0,79}\Z')

def require(condition,message):
    if not condition: raise ValueError(message)
def object_json(data):
    def unique(pairs):
        out={}
        for key,value in pairs:
            require(key not in out,'Duplicate JSON key: '+key);out[key]=value
        return out
    value=json.loads(data,object_pairs_hook=unique)
    require(isinstance(value,dict),'Expected JSON object');return value

def identifier(value):
    require(isinstance(value,str) and ID.fullmatch(value) and '..' not in value,'Invalid plugin ID');return value

def safe_path(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9/._+-]+',value) and all(p not in ('','.','..') for p in value.split('/')),'Unsafe payload path');return value

def https(value):
    require(isinstance(value,str) and len(value)<=2048 and not any(c.isspace() for c in value),'Invalid URL');url=urllib.parse.urlsplit(value)
    require(url.scheme=='https' and url.hostname and not url.username and not url.password and not url.fragment,'Use an HTTPS URL');return value

def text(value,limit,label,nonempty=False):
    require(isinstance(value,str) and len(value.encode())<=limit and (not nonempty or value.strip()),'Invalid '+label)

def fields(value,allowed,required):
    require(isinstance(value,dict) and set(value)<=set(allowed) and set(required)<=set(value),'Missing or unknown object fields')

MANIFEST_FIELDS=('schemaVersion','apiVersion','id','name','version','description','author','icon','details','category','tags','screenshots','changelog','backend','lifecycle','ui','permissions','downloadUrl','publish','files')

def validate_lifecycle(manifest,hashed=None):
    backend=manifest.get('backend')
    if backend is not None:
        require(backend.get('restart','on-failure') in ('never','on-failure'),'Invalid restart policy')
        limit=backend.get('restartLimit',3);require(type(limit) is int and 1<=limit<=10,'Invalid restart limit')
    lifecycle=manifest.get('lifecycle')
    if lifecycle is None:return
    fields(lifecycle,('runAs','onInstall','onUpdate','onStart','onStop','onUninstall','onCrashCleanup','timeoutSeconds'),())
    user=lifecycle.get('runAs');require(user is None or user in ('framely','steamos','root'),'Invalid hook identity')
    require(backend is None or user is None or user==backend.get('runAs','framely'),'Hook/backend identity differs')
    timeout=lifecycle.get('timeoutSeconds',10);require(type(timeout) is int and 1<=timeout<=15,'Invalid hook timeout')
    for phase in ('onStart','onStop'):
        require(type(lifecycle.get(phase,False)) is bool,'Invalid backend hook flag')
        require(not lifecycle.get(phase,False) or backend is not None,'Backend hook requires backend')
    for phase in ('onInstall','onUpdate','onUninstall','onCrashCleanup'):
        hook=lifecycle.get(phase)
        if hook is None:continue
        fields(hook,('entry','args'),('entry',));safe_path(hook['entry'])
        if hashed is not None:hashed(hook['entry'])
        args=hook.get('args',[]);require(isinstance(args,list) and len(args)<=64,'Invalid hook arguments')
        for arg in args:text(arg,4096,'hook argument');require('\0' not in arg,'NUL hook argument')

def source_pins(root):
    root=pathlib.Path(root);modules=configparser.ConfigParser(interpolation=None)
    if (root/'.gitmodules').exists():modules.read(root/'.gitmodules')
    registered={}
    for section in modules.sections():
        path=modules[section]['path'];url=modules[section]['url'];https(url)
        require(path.startswith('plugins/') and path.count('/')==1,'Submodule must be plugins/<name>');identifier(path[8:])
        require(path not in registered,'Duplicate submodule path');registered[path]=url
    index=subprocess.check_output(['git','-C',str(root),'ls-files','--stage','-z']).decode().split('\0');pins={}
    for item in filter(None,index):
        meta,path=item.split('\t',1);mode,commit,stage=meta.split()
        require(stage=='0','Resolve Git conflicts before validating')
        if mode=='160000':pins[path]=commit
    require(set(registered)==set(pins),'Every plugin gitlink needs one submodule URL')
    require(len(pins)<=1000,'Catalog exceeds 1000 plugins')
    return [(path,registered[path],pins[path]) for path in sorted(pins)]

def fetch_sources(root):
    root=pathlib.Path(root).resolve()
    for path,repository,commit in source_pins(root):
        require(not (root/path).is_symlink(),'Submodule cannot be a symlink')
        subprocess.run(['git','-C',str(root),'submodule','sync','--',path],check=True)
        subprocess.run(['git','-C',str(root),'-c','protocol.file.allow=never','-c','protocol.ssh.allow=never','-c','protocol.http.allow=never','-c','protocol.https.allow=always','submodule','update','--init','--checkout','--',path],check=True)
    # Read Git objects at the pin, not files that checkout filters could modify.
    return registrations(root)

def registrations(root):
    root=pathlib.Path(root).resolve();entries=[];seen=set()
    for path,repository,commit in source_pins(root):
        source=root/path
        require(source.is_dir() and not source.is_symlink(),'Initialize submodules using database.py fetch')
        top=subprocess.check_output(['git','-C',str(source),'rev-parse','--show-toplevel'],text=True).strip()
        require(pathlib.Path(top).resolve()==source,'Plugin must be a separate Git submodule')
        spec=commit+':manifest.json'
        size=int(subprocess.check_output(['git','-C',str(source),'cat-file','-s',spec]))
        require(size<=256*1024,'Manifest too large')
        manifest=object_json(subprocess.check_output(['git','-C',str(source),'show',spec]))
        fields(manifest,MANIFEST_FIELDS,('schemaVersion','apiVersion','id','name','version','author','files','downloadUrl'))
        if manifest.get('backend') is not None:fields(manifest['backend'],('entry','args','runAs','autostart','restart','restartLimit'),('entry',))
        validate_lifecycle(manifest)
        ui=manifest.get('ui',{});fields(ui,('quickPage','windows'),())
        for window in ui.get('windows',{}).values():fields(window,('entry','title','dockIcon'),('entry','title'))
        if manifest.get('publish') is not None:fields(manifest['publish'],('icon','screenshots'),())
        identifier(manifest['id']);require(manifest['id'] not in seen,'Duplicate plugin ID');seen.add(manifest['id'])
        require(manifest['schemaVersion']==1 and manifest['apiVersion']==1,'Unsupported Framely API')
        text(manifest['version'],64,'version',True);require(re.fullmatch(r'[A-Za-z0-9.+-]+',manifest['version']),'Invalid version')
        url=https(manifest['downloadUrl']);parsed=urllib.parse.urlsplit(url)
        if parsed.hostname=='github.com':
            parts=parsed.path.split('/')
            require(len(parts)==7 and parts[3:5]==['releases','download'] and parts[5]!='latest','Use a fixed GitHub Release download URL')
        entries.append({'id':manifest['id'],'version':manifest['version'],'repository':repository,'commit':commit,'packageUrl':url,'manifest':manifest})
    return entries

class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections=5
    max_repeats=5
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        https(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def download(url):
    https(url);request=urllib.request.Request(url,headers={'User-Agent':'Framely-Plugin-Database/1'})
    with urllib.request.build_opener(HTTPSRedirect).open(request,timeout=30) as response:
        https(response.geturl())
        length=response.headers.get('Content-Length');require(length is None or 0<int(length)<=MAX_PACKAGE,'Package exceeds size limit')
        data=response.read(MAX_PACKAGE+1);require(0<len(data)<=MAX_PACKAGE,'Package exceeds size limit')
        require(length is None or len(data)==int(length),'Truncated package download');return data

def metadata(manifest):
    # Normalize defaults emitted by the Rust packer before comparing declarations.
    value={key:manifest.get(key,default) for key,default in [('schemaVersion',1),('apiVersion',1),('id',''),('name',''),('version',''),('description',''),('author',''),('icon',None),('details',''),('category','其他'),('tags',[]),('screenshots',[]),('changelog',''),('downloadUrl',None)]}
    backend=manifest.get('backend')
    value['backend']=None if backend is None else {key:backend.get(key,default) for key,default in [('entry',''),('args',[]),('runAs','framely'),('autostart',False),('restart','on-failure'),('restartLimit',3)]}
    lifecycle=manifest.get('lifecycle')
    value['lifecycle']=None if lifecycle is None else {key:lifecycle.get(key,default) for key,default in [('runAs',None),('onStart',False),('onStop',False),('timeoutSeconds',10)]}
    if lifecycle is not None:
        for phase in ('onInstall','onUpdate','onUninstall','onCrashCleanup'):
            hook=lifecycle.get(phase);value['lifecycle'][phase]=None if hook is None else {'entry':hook['entry'],'args':hook.get('args',[])}
    ui=manifest.get('ui',{});value['ui']={'quickPage':ui.get('quickPage'),'windows':{key:{field:window.get(field,default) for field,default in [('entry',''),('title',''),('dockIcon',False)]} for key,window in ui.get('windows',{}).items()}}
    publish=manifest.get('publish') or {};value['publish']={'icon':publish.get('icon'),'screenshots':publish.get('screenshots',[])}
    value['permissions']=sorted(manifest.get('permissions',[]))
    return value

def verify_package(data,entry):
    require(len(data)<=MAX_PACKAGE,'Package exceeds 64 MiB')
    import io
    files={};expanded=0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.infolist())<=2049,'Too many ZIP members')
        for member in archive.infolist():
            safe_path(member.filename);require(not member.is_dir() and stat.S_IFMT(member.external_attr>>16) not in (stat.S_IFLNK,stat.S_IFCHR,stat.S_IFBLK,stat.S_IFIFO,stat.S_IFSOCK),'Invalid ZIP member type');require(member.filename not in files,'Duplicate ZIP member');expanded+=member.file_size;require(expanded<=MAX_EXPANDED,'Expanded package exceeds 128 MiB');files[member.filename]=archive.read(member)
    raw=files.pop('manifest.json');require(len(raw)<=256*1024,'Manifest too large')
    manifest=object_json(raw)
    fields(manifest,MANIFEST_FIELDS,('schemaVersion','apiVersion','id','name','version','author','files','downloadUrl'))
    require(manifest['schemaVersion']==1 and manifest['apiVersion']==1,'Unsupported Framely API');require(manifest['id']==entry['id'] and manifest['version']==entry['version'],'ID/version differs from registration')
    https(manifest['downloadUrl']);require(metadata(manifest)==metadata(entry['manifest']),'Package declarations differ from pinned source manifest')
    for key,limit in [('name',120),('author',120),('description',4096),('details',32768),('category',80),('changelog',16384)]:text(manifest.get(key,'其他' if key=='category' else ''),limit,key,key in ('name','author'))
    tags=manifest.get('tags',[]);require(isinstance(tags,list) and len(tags)<=12,'Too many tags')
    for tag in tags:text(tag,80,'tag')
    hashes=manifest['files'];require(isinstance(hashes,dict) and 0<len(hashes)<=2048 and set(hashes)==set(files),'Missing or unlisted payload')
    for path,digest in hashes.items():safe_path(path);require(hashlib.sha256(files[path]).hexdigest()==digest,'Payload hash mismatch: '+path)
    def hashed(path):safe_path(path);require(path in files,'Entry missing from payload')
    backend=manifest.get('backend')
    if backend is not None:
        fields(backend,('entry','args','runAs','autostart','restart','restartLimit'),('entry',));hashed(backend['entry']);require(backend.get('runAs','framely') in ('framely','steamos','root'),'Invalid backend identity');require(isinstance(backend.get('autostart',False),bool),'Invalid autostart');args=backend.get('args',[]);require(isinstance(args,list) and len(args)<=64,'Invalid backend arguments')
        for arg in args:text(arg,4096,'backend argument');require('\0' not in arg,'NUL backend argument')
    validate_lifecycle(manifest,hashed)
    ui=manifest.get('ui',{});fields(ui,('quickPage','windows'),());
    if ui.get('quickPage'):hashed(ui['quickPage'])
    windows=ui.get('windows',{});require(isinstance(windows,dict) and len(windows)<=8,'Invalid windows')
    for key,window in windows.items():identifier(key);fields(window,('entry','title','dockIcon'),('entry','title'));hashed(window['entry']);text(window['title'],120,'window title',True);require(isinstance(window.get('dockIcon',False),bool),'Invalid dockIcon')
    permissions=manifest.get('permissions',[]);require(isinstance(permissions,list) and all(p in ('network','windows','notifications') for p in permissions),'Invalid permissions')
    if manifest.get('icon'):
        icon=manifest['icon'];hashed(icon);image=files[icon];require(icon.endswith('.png') and 24<=len(image)<=1024*1024 and image.startswith(b'\x89PNG\r\n\x1a\n') and image[12:16]==b'IHDR','Invalid PNG icon');require(all(0<v<=1024 for v in struct.unpack('>II',image[16:24])),'Invalid icon dimensions')
    screenshots=manifest.get('screenshots',[]);require(isinstance(screenshots,list) and len(screenshots)<=8,'Too many screenshots')
    for screenshot in screenshots:hashed(screenshot);require(screenshot.endswith(('.png','.jpg','.jpeg')),'Invalid screenshot extension')
    publish=manifest.get('publish')
    if publish is not None:
        fields(publish,('icon','screenshots'),())
        if publish.get('icon'):https(publish['icon'])
        images=publish.get('screenshots',[]);require(isinstance(images,list) and len(images)<=8,'Too many store screenshots')
        for image in images:https(image)
    return manifest,files

def previous_catalog(url,path):
    https(url)
    try:
        with urllib.request.build_opener(HTTPSRedirect).open(url,timeout=30) as response:
            data=response.read(2*1024*1024+1);require(len(data)<=2*1024*1024,'Previous catalog too large');object_json(data)
    except urllib.error.HTTPError as error:
        if error.code!=404:raise
        data=b'{"schemaVersion":1,"plugins":[]}'
    pathlib.Path(path).write_bytes(data)

def build(root,output,name='Framely Plugins',previous=None,report=None):
    root=pathlib.Path(root).resolve();output=pathlib.Path(output).absolute();require(not output.exists(),'Output directory already exists');require(root not in output.parents and root!=output,'Output must be outside source checkout')
    entries=registrations(root);old={} if previous is None else {p['id']:p for p in object_json(pathlib.Path(previous).read_bytes())['plugins']};output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage=pathlib.Path(temporary)/'site';stage.mkdir();catalog=[];changes=[]
        for entry in entries:
            data=download(entry['packageUrl']);manifest,files=verify_package(data,entry);digest=hashlib.sha256(data).hexdigest();prior=old.get(entry['id'])
            require(not prior or prior['version']!=entry['version'] or prior['sha256']==digest,'Published version changed: '+entry['id'])
            item={key:manifest.get(key,default) for key,default in [('id',''),('name',''),('version',''),('description',''),('author',''),('apiVersion',1),('details',''),('category','其他'),('tags',[]),('changelog','')]}
            publish=manifest.get('publish') or {}
            item.update(url=entry['packageUrl'],sha256=digest,icon=publish.get('icon'),screenshots=publish.get('screenshots',[]),runAs=metadata(manifest)['backend']['runAs'] if manifest.get('backend') else (manifest.get('lifecycle') or {}).get('runAs'),permissions=manifest.get('permissions',[]))
            catalog.append(item)
            current={key:item[key] for key in ('version','runAs','permissions')};before=None if prior is None else {key:prior.get(key) for key in current}
            if current!=before:changes.append({'id':item['id'],'before':before,'after':current})
        for identifier in sorted(set(old)-{p['id'] for p in catalog}):changes.append({'id':identifier,'before':{key:old[identifier].get(key) for key in ('version','runAs','permissions')},'after':None})
        encoded=json.dumps({'schemaVersion':1,'name':name,'plugins':catalog},ensure_ascii=False,indent=2)+'\n';require(len(encoded.encode())<=2*1024*1024,'Catalog exceeds Framely 2 MiB limit');(stage/'catalog.json').write_text(encoded)
        if report is not None:pathlib.Path(report).write_text('### 插件版本、运行身份与权限变化\n\n```json\n'+json.dumps(changes,ensure_ascii=False,indent=2)+'\n```\n')
        os.rename(stage,output)
    return len(entries)

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    validate=sub.add_parser('validate');validate.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]);validate.add_argument('--packages',action='store_true')
    fetch=sub.add_parser('fetch');fetch.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1])
    publish=sub.add_parser('build');publish.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]);publish.add_argument('--output',type=pathlib.Path,required=True);publish.add_argument('--name',default='Framely Plugins');publish.add_argument('--previous-catalog',type=pathlib.Path);publish.add_argument('--report',type=pathlib.Path)
    previous=sub.add_parser('previous');previous.add_argument('--url',required=True);previous.add_argument('--output',required=True,type=pathlib.Path)
    args=parser.parse_args()
    if args.command=='previous':
        previous_catalog(args.url,args.output)
    elif args.command=='fetch':
        print('Fetched',len(fetch_sources(args.root)),'pinned plugins')
    elif args.command=='validate':
        entries=registrations(args.root)
        if args.packages:
            for entry in entries:verify_package(download(entry['packageUrl']),entry)
        print('Validated',len(entries),'plugin registrations')
    else:print('Published',build(args.root,args.output,args.name,args.previous_catalog,args.report),'plugins to',args.output)
if __name__=='__main__':main()
