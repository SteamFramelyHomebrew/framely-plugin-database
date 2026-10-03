import hashlib, importlib.util, io, json, pathlib, subprocess, tempfile, unittest, zipfile, urllib.request
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('database',pathlib.Path(__file__).parents[1]/'scripts/database.py');db=importlib.util.module_from_spec(spec);spec.loader.exec_module(db)
URL='https://github.com/example/plugin/releases/download/v1.0.0/plugin.framely'
class Database(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name)/'repo';self.root.mkdir();subprocess.run(['git','init','-q',str(self.root)],check=True)
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
 def test_empty_database_outputs_only_catalog(self):
  output=pathlib.Path(self.temp.name)/'site';self.assertEqual(db.build(self.root,output),0);self.assertEqual([p.name for p in output.iterdir()],['catalog.json'])
 def test_pinned_manifest_not_working_tree_and_default_normalization(self):
  entry=self.register();(self.root/'plugins/test/manifest.json').write_text('{}')
  self.assertEqual(db.registrations(self.root)[0]['manifest'],entry['manifest'])
  manifest=self.manifest();manifest['backend'].update(runAs='framely',autostart=False,args=[]);manifest['ui']['windows']={}
  db.verify_package(self.package(manifest),entry)
 def test_catalog_uses_author_urls_and_never_copies_packages_or_images(self):
  entry=self.register();data=self.package();output=pathlib.Path(self.temp.name)/'site';report=pathlib.Path(self.temp.name)/'report.md'
  with patch.object(db,'download',return_value=data):self.assertEqual(db.build(self.root,output,report=report),1)
  self.assertEqual([p.name for p in output.iterdir()],['catalog.json'])
  item=json.loads((output/'catalog.json').read_text())['plugins'][0]
  self.assertEqual(item['url'],URL);self.assertEqual(item['sha256'],hashlib.sha256(data).hexdigest());self.assertEqual(item['icon'],'https://example.org/icon.png');self.assertEqual(item['runAs'],'framely');self.assertIn('notifications',report.read_text());self.assertNotIn('publicKey',item)
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
if __name__=='__main__':unittest.main()
