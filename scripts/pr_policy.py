"""Allow automatic merging only for regular .gitmodules and plugin gitlinks."""
import json
import os
import pathlib
import re
import subprocess


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def eligible_branches(base, head):
    return base in ('main', 'testing') and head == base


def eligible(root, base, head):
    records = git(root, 'diff', '--raw', '--no-abbrev', '--no-renames', '-z', base + '...' + head).split(b'\0')
    records = [record for record in records if record]
    if not records:
        return False
    for index in range(0, len(records), 2):
        old_mode, new_mode, _, _, _ = records[index].decode().lstrip(':').split()
        path = records[index + 1].decode()
        if path == '.gitmodules':
            if not {old_mode, new_mode} <= {'000000', '100644'}:
                return False
        elif re.fullmatch(r'plugins/[a-z0-9][a-z0-9._-]{0,79}', path) and '..' not in path:
            if not {old_mode, new_mode} <= {'000000', '160000'}:
                return False
        else:
            return False
    return True


def prepare(root, candidate, base, head):
    if not all(re.fullmatch(r'[0-9a-f]{40}', sha) for sha in (base, head)):
        raise ValueError('Expected full Git commit IDs')
    if not eligible(root, base, head):
        return False
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '--detach', str(candidate), base], check=True)
    subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Catalog validation', '-c', 'user.email=validation@localhost', 'merge', '--no-commit', '--no-ff', head], check=True)
    return True


def main():
    event = json.loads(pathlib.Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    pr = event['pull_request']
    root = pathlib.Path.cwd()
    candidate = pathlib.Path(os.environ['RUNNER_TEMP']) / 'candidate'
    base, head = pr['base']['sha'], pr['head']['sha']
    subprocess.run(['git', 'fetch', 'origin', 'refs/pull/' + str(int(pr['number'])) + '/head'], check=True)
    fetched = git(root, 'rev-parse', 'FETCH_HEAD').decode().strip()
    allowed = eligible_branches(pr['base']['ref'], pr['head']['ref']) and fetched == head and prepare(root, candidate, base, head)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('allowed=' + str(allowed).lower() + '\n')
        output.write('head=' + head + '\nbase=' + base + '\n')
        refs = {}
        for branch in ('main', 'testing', 'publish'):
            ref = 'refs/remotes/origin/' + branch
            refs[branch] = None
            if subprocess.run(['git', 'show-ref', '--verify', '--quiet', ref]).returncode == 0:
                refs[branch] = git(root, 'rev-parse', ref).decode().strip()
        refs[pr['base']['ref']] = base
        output.write('refs=' + json.dumps(refs, separators=(',', ':')) + '\n')
    if not allowed:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('仅允许 main → main、testing → testing 自动合并；分支不同名、提交已变化或包含插件登记以外文件的 PR 留给人工处理。\n')


if __name__ == '__main__':
    main()
