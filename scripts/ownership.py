"""Verify plugin namespaces, immutable repository ownership and PR author permissions."""
import argparse
import functools
import json
import os
import pathlib
import re
import urllib.error
import urllib.parse

import database as db

MAX_PREFIXES_PER_OWNER = 5
PLUGIN_ID = re.compile(r'[a-z0-9]+\.[a-z0-9]+(?:[.-][a-z0-9]+)*\Z')


def repository_name(url):
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', url)
    db.require(match is not None, 'Ownership checks require a GitHub source repository')
    return match.group(1).lower()


@functools.lru_cache(maxsize=2048)
def api(path):
    return db.object_json(db.download('https://api.github.com/' + path, limit=2 * 1024 * 1024, token=os.environ.get('PLUGIN_OWNERSHIP_TOKEN')))


def repository(url):
    name = repository_name(url)
    value = api('repos/' + name)
    db.require(value.get('full_name', '').lower() == name, 'Repository rename or transfer is not allowed: ' + name)
    db.require(value.get('private') is False, 'Plugin repository must be public')
    db.require(type(value.get('id')) is int and type(value.get('owner', {}).get('id')) is int, 'Missing GitHub repository identity')
    return {'repository': name, 'repositoryId': value['id'], 'ownerId': value['owner']['id']}


def require_maintainer(identity, actor):
    # Account owners can be verified using public repository metadata.
    db.require(type(actor.get('id')) is int and isinstance(actor.get('login'), str), 'Missing PR author identity')
    if identity['ownerId'] == actor['id']:
        return
    path = 'repos/' + identity['repository'] + '/collaborators/' + urllib.parse.quote(actor['login'], safe='') + '/permission'
    try:
        permission = api(path)
    except urllib.error.HTTPError as error:
        raise ValueError('Cannot verify PR author write access to ' + identity['repository'] + ' (GitHub HTTP ' + str(error.code) + '); automatic merge denied') from error
    db.require(permission.get('user', {}).get('id') == actor['id'] and permission.get('permission') in ('write', 'admin', 'maintain'), 'PR author must have write access to every affected plugin repository')


def empty_registry():
    return {'schemaVersion': 1, 'namespaces': {}, 'plugins': {}}


def read_registry(path):
    if path is None:
        return empty_registry()
    value = db.object_json(pathlib.Path(path).read_bytes())
    db.fields(value, ('schemaVersion', 'namespaces', 'plugins'), ('schemaVersion', 'namespaces', 'plugins'))
    db.require(value['schemaVersion'] == 1 and isinstance(value['namespaces'], dict) and isinstance(value['plugins'], dict), 'Invalid ownership registry')
    return value


def reserve(registry, entry):
    identifier = entry['id']
    db.require(isinstance(identifier, str) and PLUGIN_ID.fullmatch(identifier), 'Plugin ID must be namespace.name: ' + str(identifier))
    prefix = identifier.split('.', 1)[0]
    identity = repository(entry['repository'])
    previous = registry['plugins'].get(identifier)
    db.require(previous is None or previous == identity, 'Plugin ID cannot change source repository or owner: ' + identifier)
    owner = registry['namespaces'].get(prefix)
    db.require(owner is None or owner == identity['ownerId'], 'Namespace belongs to another GitHub owner: ' + prefix)
    if owner is None:
        occupied = sum(value == identity['ownerId'] for value in registry['namespaces'].values())
        db.require(occupied < MAX_PREFIXES_PER_OWNER, 'GitHub owner may reserve at most 5 namespace prefixes')
    registry['namespaces'][prefix] = identity['ownerId']
    registry['plugins'][identifier] = identity
    return identity


def entries(root):
    result = {}
    sources = db.source_pins(root)
    for source, entry in zip(sources, db.registrations(root)):
        path = source[0]
        db.require(path == 'plugins/' + entry['id'], 'Submodule path must match manifest ID: ' + path)
        result[path] = entry
    return result


def seed(registry, roots):
    for root in roots:
        for entry in entries(root).values():
            reserve(registry, entry)
    return registry


def check_changes(before, after, registry, actor):
    """Validate each affected plugin, including both sides of ID/path changes."""
    before_repositories = {repository_name(entry['repository']): entry for entry in before.values()}
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if old is not None:
            identity = reserve(registry, old)
            require_maintainer(identity, actor)
        if new is None:
            continue
        if old is not None:
            db.require(repository_name(old['repository']) == repository_name(new['repository']), 'Replacing an existing plugin repository is forbidden')
        original = before_repositories.get(repository_name(new['repository']))
        if original is not None and original['id'] != new['id']:
            db.require(new['id'] not in registry['plugins'], 'Renamed plugin ID has already been used: ' + new['id'])
        identity = reserve(registry, new)
        require_maintainer(identity, actor)
    return registry


def affected_repositories(before, after, registry):
    names = {repository_name(entry['repository']) for path in set(before) | set(after) if before.get(path) != after.get(path) for entry in (before.get(path), after.get(path)) if entry is not None}
    return sorted((identity for identity in {value['repository']: value for value in registry['plugins'].values()}.values() if identity['repository'] in names), key=lambda identity: identity['repository'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--roots', nargs='+', type=pathlib.Path, required=True)
    parser.add_argument('--previous', type=pathlib.Path)
    parser.add_argument('--candidate', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path)
    args = parser.parse_args()
    persisted = read_registry(args.previous)
    reserved_ids = set(persisted['plugins'])
    registry = seed(persisted, args.roots)
    if args.command == 'check':
        db.require(args.candidate is not None, 'Candidate checkout is required')
        event = db.object_json(pathlib.Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
        actor = event['pull_request']['user']  # Never trust workflow sender or manifest.author.
        baseline_ids = {entry['id'] for root in args.roots for entry in entries(root).values()}
        db.require(baseline_ids <= reserved_ids, 'Publish the ownership registry before changing existing registrations')
        before, after = entries(args.roots[0]), entries(args.candidate)
        check_changes(before, after, registry, actor)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('repositories=' + json.dumps(affected_repositories(before, after, registry), separators=(',', ':')) + '\n')
    if args.output is not None:
        args.output.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n')
    print('Plugin namespace and repository ownership checks passed')


if __name__ == '__main__':
    main()
