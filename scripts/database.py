#!/usr/bin/env python3
"""Validate pinned source registrations and generate a Framely catalog from author releases."""
import argparse, configparser, hashlib, json, os, pathlib, re, stat, struct, subprocess, tempfile, urllib.error, urllib.parse, urllib.request, zipfile
MAX_PACKAGE=64*1024*1024
MAX_EXPANDED=128*1024*1024
ID=re.compile(r'[a-z0-9][a-z0-9._-]{0,79}\Z')
CATALOG_FIELDS=frozenset(('id','name','version','description','author','authorUrl','documentationUrl','homepage','apiVersion','url','sha256','runAs','icon','details','category','tags','screenshots','changelog','dependencies','optionalDependencies','conflicts','exclusiveResources'))

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

MANIFEST_FIELDS=('schemaVersion','apiVersion','id','name','version','description','author','authorUrl','documentationUrl','homepage','icon','details','category','tags','screenshots','changelog','backend','lifecycle','dependencies','optionalDependencies','conflicts','exclusiveResources','ui','downloadUrl','publish','files')

def validate_lifecycle(manifest,hashed=None):
    backend=manifest.get('backend')
    if backend is not None:
        require(backend.get('restart','on-failure') in ('never','on-failure'),'Invalid restart policy')
        limit=backend.get('restartLimit',3);require(type(limit) is int and 1<=limit<=10,'Invalid restart limit')
    lifecycle=manifest.get('lifecycle')
    if lifecycle is None:return
    fields(lifecycle,('runAs','onInstall','onUpdate','onStart','onStop','onUninstall','onCrashCleanup','timeoutSeconds'),())
    user=lifecycle.get('runAs');require(user is None or user in ('steamos','root'),'Invalid hook identity')
    require(backend is None or user is None or user==backend.get('runAs','steamos'),'Hook/backend identity differs')
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

SEMVER=re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')
def semver(value):
    match=SEMVER.fullmatch(value);require(match is not None,'Relations require SemVer versions')
    if match.group(4):
        for part in match.group(4).split('.'):require(not part.isdigit() or len(part)==1 or not part.startswith('0'),'Invalid numeric prerelease')
def version_range(value):
    text(value,120,'version range',True)
    parts=re.split(r'[,\s]+',value.strip());require(all(parts),'Invalid version range')
    for part in parts:
        require(re.fullmatch(r'(?:\^|~|>=|<=|>|<|=)?(?:\*|[0-9]+(?:\.(?:[0-9]+|\*|x|X)){0,2}(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)',part) is not None,'Invalid version range')
def validate_relations(manifest):
    required=manifest.get('dependencies',{});optional=manifest.get('optionalDependencies',{});conflicts=manifest.get('conflicts',{});resources=manifest.get('exclusiveResources',[])
    require(isinstance(required,dict) and isinstance(optional,dict) and isinstance(conflicts,dict) and isinstance(resources,list),'Invalid relationships')
    require(len(required)+len(optional)<=64 and len(conflicts)<=64 and len(resources)<=32,'Too many relationships')
    if required or optional or conflicts or resources:semver(manifest['version'])
    require(not set(required)&set(optional),'Duplicate required/optional dependency')
    for target,dep in {**required,**optional}.items():
        identifier(target);require(target!=manifest['id'] and target not in conflicts,'Self dependency or dependency/conflict contradiction')
        if isinstance(dep,str):version_range(dep)
        else:fields(dep,('version','source'),('version','source'));version_range(dep['version']);https(dep['source'])
    for target,version in conflicts.items():identifier(target);require(target!=manifest['id'],'Self conflict');version_range(version)
    require(all(isinstance(resource,str) for resource in resources) and len(set(resources))==len(resources),'Invalid exclusive resources')
    for resource in resources:identifier(resource)

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

def fetch_sources(root,ownership_only=False):
    root=pathlib.Path(root).resolve()
    for path,repository,commit in source_pins(root):
        require(not (root/path).is_symlink(),'Submodule cannot be a symlink')
        subprocess.run(['git','-C',str(root),'submodule','sync','--',path],check=True)
        subprocess.run(['git','-C',str(root),'-c','protocol.file.allow=never','-c','protocol.ssh.allow=never','-c','protocol.http.allow=never','-c','protocol.https.allow=always','submodule','update','--init','--checkout','--',path],check=True)
    # Read Git objects at the pin, not files that checkout filters could modify.
    return ownership_entries(root) if ownership_only else registrations(root)

def pinned_manifest(root,path,commit):
    source=pathlib.Path(root).resolve()/path
    require(source.is_dir() and not source.is_symlink(),'Initialize submodules using database.py fetch')
    top=subprocess.check_output(['git','-C',str(source),'rev-parse','--show-toplevel'],text=True).strip()
    require(pathlib.Path(top).resolve()==source,'Plugin must be a separate Git submodule')
    spec=commit+':manifest.json'
    size=int(subprocess.check_output(['git','-C',str(source),'cat-file','-s',spec]))
    require(size<=256*1024,'Manifest too large')
    return object_json(subprocess.check_output(['git','-C',str(source),'show',spec]))

def ownership_entries(root):
    entries=[];seen=set()
    for path,repository,commit in source_pins(root):
        manifest=pinned_manifest(root,path,commit)
        require(isinstance(manifest,dict),'Invalid manifest object')
        plugin_id=manifest.get('id');identifier(plugin_id)
        require(path=='plugins/'+plugin_id,'Submodule path must match manifest ID: '+path)
        require(plugin_id not in seen,'Duplicate plugin ID');seen.add(plugin_id)
        entries.append({'id':plugin_id,'repository':repository,'commit':commit})
    return entries

def registrations(root):
    root=pathlib.Path(root).resolve();entries=[];seen=set()
    for path,repository,commit in source_pins(root):
        manifest=pinned_manifest(root,path,commit)
        fields(manifest,MANIFEST_FIELDS+('downloadSha256',),('schemaVersion','apiVersion','id','name','version','author','files'))
        if manifest.get('backend') is not None:fields(manifest['backend'],('entry','args','runAs','autostart','restart','restartLimit'),('entry',))
        validate_lifecycle(manifest)
        validate_relations(manifest)
        ui=manifest.get('ui',{});fields(ui,('quickPage','windows'),())
        for window in ui.get('windows',{}).values():fields(window,('entry','title','dockIcon','localWeb'),('entry','title'))
        if manifest.get('publish') is not None:fields(manifest['publish'],('icon','screenshots'),())
        identifier(manifest['id']);require(manifest['id'] not in seen,'Duplicate plugin ID');seen.add(manifest['id'])
        require(manifest['schemaVersion']==1 and manifest['apiVersion']==1,'Unsupported Framely API')
        text(manifest['version'],64,'version',True);require(re.fullmatch(r'[A-Za-z0-9.+-]+',manifest['version']),'Invalid version')
        automatic='downloadUrl' not in manifest
        if automatic:
            match=re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?',repository)
            require(match is not None,'downloadUrl is required for repositories outside GitHub')
            manifest['downloadUrl']='https://github.com/'+match.group(1)+'/releases/download/v'+manifest['version']+'/'+manifest['id']+'-'+manifest['version']+'.framely'
        url=https(manifest['downloadUrl']);parsed=urllib.parse.urlsplit(url)
        if parsed.hostname=='github.com':
            parts=parsed.path.split('/')
            require(len(parts)==7 and parts[3:5]==['releases','download'] and parts[5]!='latest','Use a fixed GitHub Release download URL')
        expected=sha256_value(manifest.pop('downloadSha256')) if 'downloadSha256' in manifest else None
        require(expected is not None or parsed.hostname=='github.com','Custom downloadUrl requires downloadSha256')
        entries.append({'automaticDownload':automatic,'expectedSha256':expected,'id':manifest['id'],'version':manifest['version'],'repository':repository,'commit':commit,'packageUrl':url,'manifest':manifest})
    return entries

class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections=5
    max_repeats=5
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        https(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def download(url,limit=MAX_PACKAGE,token=None):
    https(url);request=urllib.request.Request(url,headers={'User-Agent':'Framely-Plugin-Database/1'})
    token=token or os.environ.get('GITHUB_TOKEN')
    if urllib.parse.urlsplit(url).hostname=='api.github.com' and token:
        request.add_unredirected_header('Authorization','Bearer '+token)
    with urllib.request.build_opener(HTTPSRedirect).open(request,timeout=30) as response:
        https(response.geturl())
        length=response.headers.get('Content-Length');require(length is None or 0<int(length)<=limit,'Package exceeds size limit')
        data=response.read(limit+1);require(0<len(data)<=limit,'Package exceeds size limit')
        require(length is None or len(data)==int(length),'Truncated package download');return data

def sha256_value(value):
    require(isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{64}',value),'Expected a SHA256 hex digest')
    return value.lower()

def release_sha256(entry):
    parsed=urllib.parse.urlsplit(entry['packageUrl']);parts=parsed.path.split('/')
    require(parsed.hostname=='github.com' and not parsed.query and len(parts)==7 and parts[3:5]==['releases','download'],'Provide downloadSha256 for a custom download URL')
    owner,repository,tag,filename=(urllib.parse.unquote(parts[i]) for i in (1,2,5,6))
    endpoint='https://api.github.com/repos/'+urllib.parse.quote(owner,safe='')+'/'+urllib.parse.quote(repository,safe='')+'/releases/tags/'+urllib.parse.quote(tag,safe='')
    release=object_json(download(endpoint,limit=2*1024*1024))
    require(release.get('tag_name')==tag and release.get('draft') is False,'Release must be published at the registered tag')
    assets=[asset for asset in release.get('assets',[]) if asset.get('name')==filename and asset.get('browser_download_url')==entry['packageUrl'] and asset.get('state')=='uploaded']
    require(len(assets)==1,'Release asset is missing or ambiguous')
    digest=assets[0].get('digest')
    require(isinstance(digest,str) and digest.startswith('sha256:'),'Release asset has no SHA256 digest')
    return sha256_value(digest[7:])

def verified_download(entry):
    expected=entry.get('expectedSha256')
    if entry.get('automaticDownload') or expected is None:
        release_digest=release_sha256(entry)
        require(expected is None or expected==release_digest,'Configured SHA256 differs from Release digest')
        expected=release_digest
    data=download(entry['packageUrl']);actual=hashlib.sha256(data).hexdigest()
    require(actual==expected,'Downloaded package SHA256 differs from declared or Release digest: '+entry['id'])
    manifest,files=verify_package(data,entry)
    return manifest,files,expected

def metadata(manifest):
    # Normalize defaults emitted by the Rust packer before comparing declarations.
    value={key:manifest.get(key,default) for key,default in [('schemaVersion',1),('apiVersion',1),('id',''),('name',''),('version',''),('description',''),('author',''),('authorUrl',None),('documentationUrl',None),('homepage',None),('icon',None),('details',''),('tags',[]),('screenshots',[]),('changelog',''),('downloadUrl',None)]}
    backend=manifest.get('backend')
    value['backend']=None if backend is None else {key:backend.get(key,default) for key,default in [('entry',''),('args',[]),('runAs','steamos'),('autostart',False),('restart','on-failure'),('restartLimit',3)]}
    lifecycle=manifest.get('lifecycle')
    value['lifecycle']=None if lifecycle is None else {key:lifecycle.get(key,default) for key,default in [('runAs',None),('onStart',False),('onStop',False),('timeoutSeconds',10)]}
    if lifecycle is not None:
        for phase in ('onInstall','onUpdate','onUninstall','onCrashCleanup'):
            hook=lifecycle.get(phase);value['lifecycle'][phase]=None if hook is None else {'entry':hook['entry'],'args':hook.get('args',[])}
    ui=manifest.get('ui',{});value['ui']={'quickPage':ui.get('quickPage'),'windows':{key:{field:window.get(field,default) for field,default in [('entry',''),('title',''),('dockIcon',False),('localWeb',False)]} for key,window in ui.get('windows',{}).items()}}
    publish=manifest.get('publish') or {};value['publish']={'icon':publish.get('icon'),'screenshots':publish.get('screenshots',[])}
    for field in ('dependencies','optionalDependencies','conflicts'):value[field]=manifest.get(field,{})
    value['exclusiveResources']=manifest.get('exclusiveResources',[])
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
    for key,limit in [('name',120),('author',120),('description',4096),('details',32768),('changelog',16384)]:text(manifest.get(key,''),limit,key,key in ('name','author'))
    for key in ('authorUrl','documentationUrl','homepage'):
        value=manifest.get(key)
        if value is not None:
            text(value,2048,key,True);url=urllib.parse.urlsplit(value)
            require(url.scheme in ('http','https') and url.hostname and not url.username and not url.password and not any(c.isspace() for c in value),'Invalid web link')
    tags=manifest.get('tags',[]);require(isinstance(tags,list) and len(tags)<=12,'Too many tags')
    for tag in tags:text(tag,80,'tag',True)
    hashes=manifest['files'];require(isinstance(hashes,dict) and 0<len(hashes)<=2048 and set(hashes)==set(files),'Missing or unlisted payload')
    for path,digest in hashes.items():safe_path(path);require(hashlib.sha256(files[path]).hexdigest()==digest,'Payload hash mismatch: '+path)
    def hashed(path):safe_path(path);require(path in files,'Entry missing from payload')
    backend=manifest.get('backend')
    if backend is not None:
        fields(backend,('entry','args','runAs','autostart','restart','restartLimit'),('entry',));hashed(backend['entry']);require(backend.get('runAs','steamos') in ('steamos','root'),'Invalid backend identity');require(isinstance(backend.get('autostart',False),bool),'Invalid autostart');args=backend.get('args',[]);require(isinstance(args,list) and len(args)<=64,'Invalid backend arguments')
        for arg in args:text(arg,4096,'backend argument');require('\0' not in arg,'NUL backend argument')
    validate_lifecycle(manifest,hashed)
    validate_relations(manifest)
    ui=manifest.get('ui',{});fields(ui,('quickPage','windows'),());
    if ui.get('quickPage'):hashed(ui['quickPage'])
    windows=ui.get('windows',{});require(isinstance(windows,dict) and len(windows)<=8,'Invalid windows')
    for key,window in windows.items():identifier(key);fields(window,('entry','title','dockIcon','localWeb'),('entry','title'));hashed(window['entry']);text(window['title'],120,'window title',True);require(isinstance(window.get('dockIcon',False),bool),'Invalid dockIcon');require(isinstance(window.get('localWeb',False),bool),'Invalid localWeb')
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

def write_tags(catalogs,output):
    tags=set()
    for catalog in catalogs:
        for plugin in catalog.get('plugins',[]):
            for tag in plugin.get('tags',[]):
                text(tag,80,'tag',True);tags.add(tag)
    pathlib.Path(output).write_text(json.dumps({'schemaVersion':1,'tags':sorted(tags)},ensure_ascii=False,indent=2)+'\n')

def store_icon(entry,files):
    icon=entry['manifest'].get('icon')
    if not icon:
        return (entry['manifest'].get('publish') or {}).get('icon')
    safe_path(icon)
    match=re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?',entry['repository'])
    require(match is not None,'Store icon requires a GitHub repository')
    require(re.fullmatch(r'[0-9a-f]{40}',entry['commit']) is not None,'Invalid icon source commit')
    url='https://raw.githubusercontent.com/'+match.group(1)+'/'+entry['commit']+'/'+urllib.parse.quote(icon,safe='/')
    require(download(url,limit=1024*1024)==files[icon],'Repository icon differs from verified package icon')
    return url

def build(root,output,name='Framely Plugins',previous=None,report=None):
    root=pathlib.Path(root).resolve();output=pathlib.Path(output).absolute();require(not output.exists(),'Output directory already exists');require(root not in output.parents and root!=output,'Output must be outside source checkout')
    entries=registrations(root);history=[] if previous is None else object_json(pathlib.Path(previous).read_bytes())['plugins'];old={};seen=set()
    for item in history:
        pair=(item['id'],item['version']);require(pair not in seen,'Duplicate catalog version');seen.add(pair);old.setdefault(item['id'],item)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage=pathlib.Path(temporary)/'site';stage.mkdir();catalog=[];current_plugins=[];changes=[]
        for entry in entries:
            manifest,files,digest=verified_download(entry);prior=old.get(entry['id'])
            require(all(item['id']!=entry['id'] or item['version']!=entry['version'] or item['sha256']==digest for item in history),'Published version changed: '+entry['id'])
            published=next((p for p in history if p['id']==entry['id'] and p['version']==entry['version']),None)
            item={key:manifest.get(key,default) for key,default in [('id',''),('name',''),('version',''),('description',''),('author',''),('authorUrl',None),('documentationUrl',None),('homepage',None),('apiVersion',1),('details',''),('tags',[]),('changelog','')]}
            for field in ('dependencies','optionalDependencies','conflicts','exclusiveResources'):item[field]=metadata(manifest)[field]
            publish=manifest.get('publish') or {}
            item.update(url=entry['packageUrl'],sha256=digest,icon=published.get('icon') if published is not None else store_icon(entry,files),screenshots=publish.get('screenshots',[]),runAs=metadata(manifest)['backend']['runAs'] if manifest.get('backend') else ((manifest.get('lifecycle') or {}).get('runAs') or 'steamos'))
            catalog.append(item)
            current_plugins.append(item)
            archived=[p for p in history if p['id']==item['id'] and p['version']!=item['version'] and set(p)<=CATALOG_FIELDS and p.get('runAs') in ('steamos','root')]
            catalog.extend(archived[:19])
            current={key:item[key] for key in ('version','runAs')};before=None if prior is None else {key:prior.get(key) for key in current}
            if current!=before:changes.append({'id':item['id'],'before':before,'after':current})
        for identifier in sorted(set(old)-{p['id'] for p in catalog}):changes.append({'id':identifier,'before':{key:old[identifier].get(key) for key in ('version','runAs')},'after':None})
        require(len(catalog)<=1000,'Catalog exceeds 1000 version entries')
        encoded=json.dumps({'schemaVersion':1,'name':name,'plugins':catalog},ensure_ascii=False,indent=2)+'\n';require(len(encoded.encode())<=2*1024*1024,'Catalog exceeds Framely 2 MiB limit');(stage/'catalog.json').write_text(encoded)
        write_tags([{'plugins':current_plugins}],stage/'tags.json')
        if report is not None:pathlib.Path(report).write_text('### 插件版本与运行用户变化\n\n```json\n'+json.dumps(changes,ensure_ascii=False,indent=2)+'\n```\n')
        os.rename(stage,output)
    return len(entries)

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    validate=sub.add_parser('validate');validate.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]);validate.add_argument('--packages',action='store_true')
    fetch=sub.add_parser('fetch');fetch.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]);fetch.add_argument('--ownership-only',action='store_true')
    publish=sub.add_parser('build');publish.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]);publish.add_argument('--output',type=pathlib.Path,required=True);publish.add_argument('--name',default='Framely Plugins');publish.add_argument('--previous-catalog',type=pathlib.Path);publish.add_argument('--report',type=pathlib.Path)
    previous=sub.add_parser('previous');previous.add_argument('--url',required=True);previous.add_argument('--output',required=True,type=pathlib.Path)
    args=parser.parse_args()
    if args.command=='previous':
        previous_catalog(args.url,args.output)
    elif args.command=='fetch':
        print('Fetched',len(fetch_sources(args.root,ownership_only=args.ownership_only)),'pinned plugins')
    elif args.command=='validate':
        entries=registrations(args.root)
        if args.packages:
            for entry in entries:verified_download(entry)
        print('Validated',len(entries),'plugin registrations')
    else:print('Published',build(args.root,args.output,args.name,args.previous_catalog,args.report),'plugins to',args.output)
if __name__=='__main__':main()
