import hashlib, importlib.util, io, json, pathlib, subprocess, tempfile, unittest, zipfile, urllib.request
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('database',pathlib.Path(__file__).parents[1]/'scripts/database.py');db=importlib.util.module_from_spec(spec);spec.loader.exec_module(db)
URL='https://github.com/example/plugin/releases/download/v1.0.0/plugin.framely'
class Database(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name)/'repo';self.root.mkdir();subprocess.run(['git','init','-q',str(self.root)],check=True)
  self.release_digest=patch.object(db,'release_sha256',side_effect=lambda entry:hashlib.sha256(self.package(entry['manifest'])).hexdigest());self.release_digest.start();self.addCleanup(self.release_digest.stop)
 def tearDown(self):self.temp.cleanup()
 def manifest(self):
  return {'schemaVersion':1,'apiVersion':1,'id':'test.plugin','name':'测试插件','author':'Tester','version':'1.0.0','details':'Description','category':'工具','backend':{'entry':'backend.py'},'permissions':['notifications'],'ui':{'quickPage':'page.js'},'files':{},'downloadUrl':URL,'publish':{'icon':'https://example.org/icon.png','screenshots':['https://example.org/screen.png']}}
 def register(self,manifest=None):
  manifest=manifest or self.manifest();source=self.root/'plugins/test';source.mkdir(parents=True,exist_ok=True)
  if not (source/'.git').exists():subprocess.run(['git','init','-q',str(source)],check=True)
  (source/'manifest.json').write_text(json.dumps(manifest));subprocess.run(['git','-C',str(source),'add','manifest.json'],check=True);subprocess.run(['git','-C',str(source),'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','Fixture'],check=True)
  commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
  (self.root/'.gitmodules').write_text('[submodule "plugins/test"]\n path = plugins/test\n url = https://github.com/example/plugin.git\n');subprocess.run(['git','-C',str(self.root),'update-index','--add','--cacheinfo','160000,'+commit+',plugins/test'],check=True)
  return db.registrations(self.root)[0]
 def package(self,manifest=None,payload=None):
  manifest=manifest or self.manifest();payload=payload or {'page.js':b'page','backend.py':b'#!/usr/bin/python3\n'}
  manifest={**manifest,'files':{name:hashlib.sha256(data).hexdigest() for name,data in payload.items()}}
  buffer=io.BytesIO()
  with zipfile.ZipFile(buffer,'w') as archive:
   archive.writestr('manifest.json',json.dumps(manifest))
   for name,data in payload.items():archive.writestr(name,data)
  return buffer.getvalue()
 def test_source_without_url_resolves_versioned_asset_and_checks_package(self):
  for version in ('1.0.0','1.0.1-preview.1'):
   manifest=self.manifest();manifest.pop('downloadUrl');manifest['version']=version
   entry=self.register(manifest)
   expected=f'https://github.com/example/plugin/releases/download/v{version}/test.plugin-{version}.framely'
   self.assertEqual(entry['packageUrl'],expected)
   packed={**manifest,'downloadUrl':expected}
   db.verify_package(self.package(packed),entry)
   with self.assertRaises(ValueError):db.verify_package(self.package({**packed,'downloadUrl':URL}),entry)
   output=pathlib.Path(self.temp.name)/version
   with patch.object(db,'download',return_value=self.package(packed)) as download:db.build(self.root,output)
   download.assert_called_once_with(expected)
   self.assertEqual(json.loads((output/'catalog.json').read_text())['plugins'][0]['url'],expected)
 def test_non_github_source_requires_explicit_url(self):
  manifest=self.manifest();manifest.pop('downloadUrl');self.register(manifest)
  modules=self.root/'.gitmodules';modules.write_text(modules.read_text().replace('https://github.com/example/plugin.git','https://example.org/plugin.git'))
  with self.assertRaises(ValueError):db.registrations(self.root)
 def test_custom_source_hash_is_registration_only_and_validated_before_publish(self):
  manifest=self.manifest();manifest['downloadUrl']='https://example.org/plugin.framely'
  data=self.package(manifest);digest=hashlib.sha256(data).hexdigest()
  entry=self.register({**manifest,'downloadSha256':digest.upper()})
  self.assertEqual(entry['expectedSha256'],digest);self.assertNotIn('downloadSha256',entry['manifest'])
  output=pathlib.Path(self.temp.name)/'custom'
  with patch.object(db,'download',return_value=data):db.build(self.root,output)
  self.assertEqual(json.loads((output/'catalog.json').read_text())['plugins'][0]['sha256'],digest)
  for value in ('bad',None,123,'0'*63):
   with self.subTest(value=value),self.assertRaises(ValueError):self.register({**manifest,'downloadSha256':value})
 def test_empty_database_outputs_catalog_and_empty_tags(self):
  output=pathlib.Path(self.temp.name)/'site';self.assertEqual(db.build(self.root,output),0);self.assertEqual(sorted(p.name for p in output.iterdir()),['catalog.json','tags.json'])
 def test_pinned_manifest_not_working_tree_and_default_normalization(self):
  entry=self.register();(self.root/'plugins/test/manifest.json').write_text('{}')
  self.assertEqual(db.registrations(self.root)[0]['manifest'],entry['manifest'])
  manifest=self.manifest();manifest['backend'].update(runAs='framely',autostart=False,args=[]);manifest['ui']['windows']={}
  db.verify_package(self.package(manifest),entry)
 def test_catalog_uses_author_urls_and_never_copies_packages_or_images(self):
  entry=self.register();data=self.package();output=pathlib.Path(self.temp.name)/'site';report=pathlib.Path(self.temp.name)/'report.md'
  with patch.object(db,'download',return_value=data):self.assertEqual(db.build(self.root,output,report=report),1)
  self.assertEqual(sorted(p.name for p in output.iterdir()),['catalog.json','tags.json'])
  item=json.loads((output/'catalog.json').read_text())['plugins'][0]
  self.assertEqual(item['url'],URL);self.assertEqual(item['sha256'],hashlib.sha256(data).hexdigest());self.assertEqual(item['icon'],'https://example.org/icon.png');self.assertEqual(item['runAs'],'framely');self.assertIn('notifications',report.read_text());self.assertNotIn('publicKey',item)
 def test_single_icon_config_emits_verified_immutable_repository_url(self):
  manifest=self.manifest();manifest.pop('publish');manifest['icon']='art/icon.png'
  entry=self.register(manifest)
  import struct
  image=b'\x89PNG\r\n\x1a\n'+b'\0\0\0\rIHDR'+struct.pack('>II',256,256)
  data=self.package(manifest,{'page.js':b'page','backend.py':b'backend','art/icon.png':image})
  expected='https://raw.githubusercontent.com/example/plugin/'+entry['commit']+'/art/icon.png'
  output=pathlib.Path(self.temp.name)/'icon'
  with patch.object(db,'release_sha256',return_value=hashlib.sha256(data).hexdigest()),patch.object(db,'download',side_effect=lambda url,**kwargs:data if url==URL else image) as download:
   db.build(self.root,output)
  self.assertEqual(json.loads((output/'catalog.json').read_text())['plugins'][0]['icon'],expected)
  download.assert_any_call(expected,limit=1024*1024)
  with patch.object(db,'download',return_value=b'wrong'),self.assertRaises(ValueError):db.store_icon(entry,{'art/icon.png':image})
  with patch.object(db,'download',side_effect=urllib.error.HTTPError(expected,404,'Missing',None,None)),self.assertRaises(urllib.error.HTTPError):db.store_icon(entry,{'art/icon.png':image})
 def test_package_identity_permissions_and_all_metadata_must_match_source(self):
  entry=self.register()
  for change in ({'id':'other.plugin'},{'version':'2.0.0'},{'permissions':['network']},{'backend':{'entry':'backend.py','runAs':'root'}},{'downloadUrl':'https://example.org/other.framely'},{'name':'Other'}):
   with self.subTest(change=change),self.assertRaises(ValueError):db.verify_package(self.package({**self.manifest(),**change}),entry)
 def test_republishing_same_version_with_new_hash_is_rejected(self):
  self.register();output=pathlib.Path(self.temp.name)/'old'
  with patch.object(db,'download',return_value=self.package()):db.build(self.root,output)
  new=pathlib.Path(self.temp.name)/'new'
  altered=self.package(payload={'page.js':b'changed','backend.py':b'backend'})
  with patch.object(db,'download',return_value=altered),self.assertRaises(ValueError):db.build(self.root,new,previous=output/'catalog.json')
  self.assertFalse(new.exists())
  with patch.object(db,'download',return_value=self.package()):db.build(self.root,new,previous=output/'catalog.json')
 def test_corruption_unlisted_files_and_paths_rejected(self):
  entry=self.register();data=self.package()
  for extra in (False,True):
   buffer=io.BytesIO()
   with zipfile.ZipFile(io.BytesIO(data)) as source,zipfile.ZipFile(buffer,'w') as target:
    for item in source.infolist():target.writestr(item,source.read(item) if extra or item.filename!='page.js' else b'tampered')
    if extra:target.writestr('manifest.sig',b'ignored')
   with self.assertRaises(ValueError):db.verify_package(buffer.getvalue(),entry)
  for path in ['../escape','/absolute','a//b','a/../b','a\\b']:
   with self.assertRaises(ValueError):db.safe_path(path)
 def test_https_redirect_policy(self):
  handler=db.HTTPSRedirect();request=urllib.request.Request(URL)
  self.assertEqual(handler.redirect_request(request,None,302,'Found',{},'https://release-assets.githubusercontent.com/file').full_url,'https://release-assets.githubusercontent.com/file')
  for target in ['http://example.org/file','file:///etc/passwd','https://user:secret@example.org/file']:
   with self.assertRaises(ValueError):handler.redirect_request(request,None,302,'Found',{},target)
  with self.assertRaises(ValueError):db.download('http://example.org/plugin.framely')
 def test_mutable_github_release_urls_rejected(self):
  self.register()
  for url in ['https://github.com/example/plugin/releases/latest/download/file.framely','https://github.com/example/plugin/releases/download/latest/file.framely']:
   manifest=self.manifest();manifest['downloadUrl']=url
   # Read validation is intentionally at the Git pin, not from local edits.
   with self.assertRaises(ValueError):self.register(manifest)
 def test_lifecycle_defaults_and_hashed_hooks_match_pinned_source(self):
  manifest=self.manifest();manifest['lifecycle']={'onInstall':{'entry':'backend.py'},'onStart':True};entry=self.register(manifest)
  packed=json.loads(json.dumps(manifest));packed['backend'].update(restart='on-failure',restartLimit=3)
  packed['lifecycle'].update(onStop=False,timeoutSeconds=10,runAs=None,onUpdate=None,onUninstall=None,onCrashCleanup=None);packed['lifecycle']['onInstall']['args']=[]
  db.verify_package(self.package(packed),entry)
  for change in [{'onInstall':{'entry':'missing.py'}},{'runAs':'root'},{'timeoutSeconds':30},{'onStop':'yes'}]:
   invalid={**manifest,'lifecycle':{**manifest['lifecycle'],**change}}
   with self.assertRaises(ValueError):db.verify_package(self.package(invalid),{'id':invalid['id'],'version':invalid['version'],'manifest':invalid})
 def test_unannounced_lifecycle_changes_are_rejected(self):
  entry=self.register();manifest=self.manifest();manifest['lifecycle']={'onUninstall':{'entry':'backend.py'}}
  with self.assertRaises(ValueError):db.verify_package(self.package(manifest),entry)
 def test_relationships_are_validated_and_exported(self):
  manifest=self.manifest();manifest['dependencies']={'other.plugin':{'version':'>=1.2.0 <2.0.0','source':'https://example.org/catalog.json'}};manifest['exclusiveResources']=['test.resource'];self.register(manifest)
  output=pathlib.Path(self.temp.name)/'relations'
  with patch.object(db,'download',return_value=self.package(manifest)):db.build(self.root,output)
  item=json.loads((output/'catalog.json').read_text())['plugins'][0];self.assertEqual(item['dependencies'],manifest['dependencies']);self.assertEqual(item['exclusiveResources'],['test.resource'])
  for changed in [{'version':'1'},{'dependencies':{'test.plugin':'*'}},{'dependencies':{'other.plugin':{'version':'*','source':'http://example.org/catalog.json'}}},{'optionalDependencies':manifest['dependencies']},{'conflicts':{'other.plugin':'*'}}]:
   with self.assertRaises(ValueError):db.validate_relations({**manifest,**changed})
 def test_unannounced_dependency_changes_are_rejected(self):
  entry=self.register();manifest=self.manifest();manifest['dependencies']={'other.plugin':'^1.0.0'}
  with self.assertRaises(ValueError):db.verify_package(self.package(manifest),entry)

 def test_tags_aggregate_deduplicate_sort_and_remove_stale_values(self):
  output=pathlib.Path(self.temp.name)/'tags.json'
  db.write_tags([{'plugins':[{'tags':['温控','工具','工具']},{'tags':['工具','风扇']},{}]},{'plugins':[{'tags':['风扇','工具']}]}],output)
  self.assertEqual(json.loads(output.read_text()),{'schemaVersion':1,'tags':['工具','温控','风扇']})
  db.write_tags([{'plugins':[]}],output)
  self.assertEqual(json.loads(output.read_text())['tags'],[])
 def test_build_emits_tags_and_ignores_legacy_category(self):
  manifest=self.manifest();manifest['tags']=['工具','温控','工具'];self.register(manifest);data=self.package(manifest);output=pathlib.Path(self.temp.name)/'site'
  with patch.object(db,'download',return_value=data):db.build(self.root,output)
  self.assertEqual(json.loads((output/'tags.json').read_text())['tags'],['工具','温控'])
  self.assertNotIn('category',json.loads((output/'catalog.json').read_text())['plugins'][0])

 def test_catalog_keeps_version_choices_and_drops_removed_plugins(self):
  self.register();old=pathlib.Path(self.temp.name)/'v1'
  with patch.object(db,'download',return_value=self.package()):db.build(self.root,old)
  manifest=self.manifest();manifest['version']='2.0.0';manifest['downloadUrl']=URL.replace('v1.0.0','v2.0.0');self.register(manifest)
  new=pathlib.Path(self.temp.name)/'v2'
  with patch.object(db,'download',return_value=self.package(manifest)):db.build(self.root,new,previous=old/'catalog.json')
  entries=json.loads((new/'catalog.json').read_text())['plugins'];self.assertEqual([p['version'] for p in entries],['2.0.0','1.0.0']);self.assertEqual(entries[1]['url'],URL)
  again=pathlib.Path(self.temp.name)/'again'
  with patch.object(db,'download',return_value=self.package(manifest)):db.build(self.root,again,previous=new/'catalog.json')
  self.assertEqual(len(json.loads((again/'catalog.json').read_text())['plugins']),2)
  # An archived version remains immutable even after it stops being the default.
  subprocess.run(['git','-C',str(self.root),'update-index','--force-remove','plugins/test'],check=True);(self.root/'.gitmodules').unlink()
  empty=pathlib.Path(self.temp.name)/'empty';db.build(self.root,empty,previous=new/'catalog.json');self.assertEqual(json.loads((empty/'catalog.json').read_text())['plugins'],[])

if __name__=='__main__':unittest.main()
