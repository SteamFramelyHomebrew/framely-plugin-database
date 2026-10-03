import json
import pathlib
import shutil
import subprocess
import textwrap
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required to exercise the GitHub merge script')
class MergeOwnership(unittest.TestCase):
    def test_merge_rechecks_identity_author_permissions_and_all_branch_refs(self):
        workflow = (pathlib.Path(__file__).parents[1] / '.github/workflows/plugin-pr.yml').read_text()
        block = workflow.split('  merge:\n', 1)[1].split('  publish:\n', 1)[0]
        script = textwrap.dedent(block.split('          script: |\n', 1)[1])
        harness = r'''
const assert = require('node:assert/strict');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const execute = new AsyncFunction('github', 'context', 'core', SCRIPT);
async function scenario(options = {}) {
  let merged = 0;
  process.env.VALIDATED_HEAD = 'head'; process.env.VALIDATED_BASE = 'base';
  process.env.TARGET_BRANCH = 'testing'; process.env.PLUGIN_OWNERSHIP_TOKEN = '';
  process.env.VALIDATED_REFS = JSON.stringify({main: 'main', testing: 'base', publish: 'publish'});
  process.env.VALIDATED_REPOSITORIES = JSON.stringify([
    {repository: 'alice/one', repositoryId: 1, ownerId: 10},
    {repository: 'org/two', repositoryId: 2, ownerId: 20}
  ]);
  const github = {rest: {
    pulls: {get: async () => ({data: {number: 1, state: 'open', draft: false,
      base: {ref: 'testing', sha: 'base'}, head: {sha: 'head'}, user: {id: 10, login: 'alice'}}}),
      merge: async () => {merged++; return {data: {merged: true}};}},
    repos: {getBranch: async ({branch}) => ({data: {commit: {sha:
      options.stale && branch === 'main' ? 'changed' : {main:'main', testing:'base', publish:'publish'}[branch]}}})}
  }, request: async (route, {owner}) => {
    if (route.includes('collaborators')) {
      if (options.apiFailure) {const error = new Error('403'); error.status = 403; throw error;}
      return {data: {permission: options.noPermission ? 'read' : 'write', user: {id: 10}}};
    }
    return {data: {id: owner === 'alice' ? 1 : options.replaced ? 99 : 2,
      owner: {id: owner === 'alice' ? 10 : options.transfer ? 30 : 20},
      full_name: owner === 'alice' ? 'alice/one' : 'org/two', private: false}};
  }};
  const core = {setOutput(){}, summary: {addRaw(){return this}, async write(){}}};
  let failed = false;
  try {await execute(github, {repo: {owner:'db', repo:'database'}, issue: {number:1}}, core);}
  catch (error) {failed = true;}
  return {merged, failed};
}
(async () => {
  assert.deepEqual(await scenario(), {merged:1, failed:false});
  for (const key of ['noPermission','apiFailure','transfer','replaced','stale']) {
    assert.deepEqual(await scenario({[key]:true}), {merged:0, failed:true}, key);
  }
})().catch(error => {console.error(error); process.exit(1);});
'''
        result = subprocess.run(['node', '-e', harness.replace('SCRIPT', json.dumps(script), 1)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
