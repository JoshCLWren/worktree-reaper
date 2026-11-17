#!/usr/bin/python3
"""Pressure-gated worktree and stale Cargo target cleanup.

Preview unless --apply is supplied. Policy per filesystem:

* below PRESSURE_THRESHOLD (85% used): worktrees idle over 24h are reaped; Cargo
  targets are left alone so an unpressured disk does not pay for needless rebuilds;
* at or above the threshold: worktrees idle over 60 minutes AND Cargo targets
  idle over 60 minutes on that filesystem are reaped.

Git lock labels and GitHub ownership claims are ignored by default (opt in with
--respect-leases). Live processes, in-flight Git operations, and recent source
activity are always protected, and worktree removals are backed up first.

Machine-specific paths are supplied through environment variables (see README.md);
no usernames, home directories or project names are baked into this file.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
from zoneinfo import ZoneInfo

def env_paths(name, default):
    """Read a colon-separated list of paths from the environment."""
    value = os.environ.get(name)
    return tuple(item for item in value.split(':')) if value else default


REPO = Path(os.environ.get('CWC_REPO', str(Path.home() / 'code/my-project'))).expanduser()
STATE = Path(os.environ.get(
    'CWC_STATE', str(Path.home() / '.local/state/comic-worktree-cleanup'),
)).expanduser()
BACKUPS = Path(os.environ.get(
    'CWC_BACKUPS', str(Path.home() / 'worktree-cleanup-backups'),
)).expanduser()
ZONE = ZoneInfo(os.environ.get('CWC_TIMEZONE', 'UTC'))
REMOTE = os.environ.get('CWC_REMOTE', '')
MAX_IDLE_MINUTES_DEFAULT = 60
GENTLE_IDLE_MINUTES_DEFAULT = 24 * 60
PRESSURE_THRESHOLD_DEFAULT = 85.0
MOUNTS = env_paths('CWC_MOUNTS', ('/', '/tmp'))
PRUNE_DIRS = frozenset({
    'target', 'node_modules', '.git', '__pycache__', '.venv', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', 'dist', 'build', '.tox', '.nox',
})
MANAGED_CACHE = Path(os.environ.get(
    'CWC_MANAGED_CACHE', str(Path.home() / '.cache/rust-build-cache'),
)).expanduser()
TARGET_ROOTS = env_paths('CWC_TARGET_ROOTS', (str(Path.home() / 'code'), '/tmp'))


def run(*args, cwd=None):
    """Run a bounded command without shell interpolation."""
    return subprocess.check_output(args, cwd=cwd, timeout=120)


def git(path, *args):
    """Run Git against an explicit checkout."""
    return run('git', '--no-pager', '-C', str(path), *args)


def log(event, **values):
    """Write a journal-friendly JSON event."""
    print(json.dumps({'event': event, **values}), flush=True)


def iso(mtime):
    """Render an epoch timestamp as an ISO string in the local zone."""
    return dt.datetime.fromtimestamp(mtime, ZONE).isoformat()


def worktrees(repo):
    """Read NUL-delimited Git records so paths cannot inject commands."""
    records = []
    current = {}
    for field in git(repo, 'worktree', 'list', '--porcelain', '-z').decode().split('\0'):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = field.partition(' ')
        current[key] = value
    if current:
        records.append(current)
    return records


def eligible(record, repo=REPO):
    """Select every registered linked worktree except the main checkout.

    Lock labels and GitHub lease claims are intentionally ignored; recency is
    enforced separately in the main loop.
    """
    path = Path(record['worktree'])
    return (
        path != repo
        and path.is_dir()
        and (path / '.git').exists()
        and not path.is_symlink()
    )


def newest_source_mtime(path):
    """Return the newest source-file mtime, pruning build and cache directories."""
    newest = 0.0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if name not in PRUNE_DIRS]
        for name in files:
            try:
                newest = max(newest, (Path(root) / name).lstat().st_mtime)
            except OSError:
                continue
    return newest


def dir_newest_mtime(path):
    """Return the newest file mtime anywhere below a directory."""
    result = subprocess.run(
        ['find', str(path), '-type', 'f', '-printf', '%T@\n'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=300, check=False,
    )
    return max((float(value) for value in result.stdout.split()), default=0.0)


def filesystem_for(path):
    """Return the watched mount point containing a path."""
    resolved = Path(os.path.realpath(path))
    for mount in sorted(MOUNTS, key=len, reverse=True):
        if resolved == Path(mount) or Path(mount) in resolved.parents:
            return mount
    return '/'


def pressure_for(path, threshold, cache):
    """Return (mount, percent used, over threshold) for a path's filesystem."""
    mount = filesystem_for(path)
    if mount not in cache:
        usage = shutil.disk_usage(mount)
        # Match `df` Use%: reserved blocks are excluded from the denominator.
        available = usage.used + usage.free
        percent = usage.used / available * 100 if available else 0.0
        over = percent >= threshold
        cache[mount] = (percent, over)
        log('pressure', mount=mount, percent_used=round(percent, 1), over_threshold=over)
    percent, over = cache[mount]
    return mount, percent, over


def stale_target_dirs(cutoff):
    """Yield idle Cargo target directories outside the managed cache.

    Args:
        cutoff: Epoch timestamp; only older directories are yielded.

    Yields:
        Tuples of (target path, newest file mtime).
    """
    result = subprocess.run(
        ['find', *TARGET_ROOTS, '-name', '.rustc_info.json', '-type', 'f', '-print0'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=300, check=False,
    )
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        target = Path(raw.decode(errors='replace')).parent
        if target == MANAGED_CACHE or MANAGED_CACHE in target.parents:
            continue
        if not target.is_dir():
            continue
        newest = dir_newest_mtime(target)
        if newest < cutoff:
            yield target, newest


def process_paths():
    """Find this user's live cwd, executable, command arguments and open files."""
    paths = set()
    infrastructure = {
        ('/usr/lib/systemd/systemd', '--user'),
        ('(sd-pam)',),
        ('/usr/bin/ssh-agent', '/usr/bin/im-launch', 'x-session-manager'),
        ('/usr/bin/ssh-agent', '-D', '-a', f'/run/user/{os.getuid()}/keyring/.ssh'),
    }
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            if re.search(r'^State:\s+Z', (proc / 'status').read_text(), re.MULTILINE):
                continue
            argv = tuple((proc / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0'))
            if argv in infrastructure:
                continue
            for link in [proc / 'cwd', proc / 'exe', *(proc / 'fd').iterdir()]:
                try:
                    target = os.readlink(link)
                    if target.startswith('/'):
                        paths.add(target.removesuffix(' (deleted)'))
                except FileNotFoundError:
                    continue
            for arg in (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0'):
                if arg.startswith('/'):
                    paths.add(arg)
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Permission errors intentionally abort cleanup rather than imply inactivity.
    return paths


def busy(path, paths):
    """Check component boundaries rather than matching similar worktree names."""
    return any(p == str(path) or p.startswith(str(path) + '/') for p in paths)


def git_busy(path):
    """Protect in-flight Git operations and explicit Git lock files."""
    admin = Path(git(path, 'rev-parse', '--absolute-git-dir').decode().strip())
    common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
    return (any(admin.rglob('*.lock')) or any(common.glob('*.lock'))
            or any((admin / name).exists() for name in
                   ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG')))


def gh(*args):
    """Fetch live ownership; unavailable authentication/network aborts removal."""
    return json.loads(run('gh', *args))


def owned(labels):
    """Protect explicit owners and implementation claims."""
    names = {label['name'] for label in labels}
    return ('ralph-status:in-progress' in names or 'factory:building' in names
            or any(re.fullmatch(r'factory:(local|[0-9]+)', name) for name in names))


def ownership():
    """Read all open issues and PRs without relying on capped search results."""
    if not REMOTE:
        log('warning', message='CWC_REMOTE is unset; lease checks disabled')
        return {}, []
    issues = [item for page in gh('api', '--paginate', '--slurp', f'repos/{REMOTE}/issues?state=open&per_page=100') for item in page]
    pulls = [item for page in gh('api', '--paginate', '--slurp', f'repos/{REMOTE}/pulls?state=open&per_page=100') for item in page]
    return {item['number']: item for item in issues}, pulls


def leased(record, issues, pulls):
    """Match branch/path issue references, PR ownership and unreleased claim markers."""
    branch = record.get('branch', '').removeprefix('refs/heads/')
    numbers = {int(n) for n in re.findall(r'(?<![0-9])[0-9]{4,}(?![0-9])', branch + ' ' + record['worktree'])}
    for pr in pulls:
        if pr['head']['ref'] == branch:
            numbers.add(pr['number'])
            if owned(pr['labels']):
                return True
    for number in numbers:
        issue = issues.get(number)
        if not issue:
            continue
        if owned(issue['labels']):
            return True
        pages = gh('api', '--paginate', '--slurp', f'repos/{REMOTE}/issues/{number}/comments?per_page=100')
        markers = []
        for page in pages:
            for comment in page:
                matches = re.findall(r'factory-(?:controller-claim-v\w+|implement-claim-v\w+|claim-released-v\w+|resume:v1)', comment['body'])
                markers.extend(matches)
        if markers and not markers[-1].startswith('factory-claim-released-'):
            return True
    return False


def backup(path, directory):
    """Preserve HEAD, binary staged/unstaged patches and untracked source files."""
    directory.mkdir(parents=True, mode=0o700)
    head = git(path, 'rev-parse', 'HEAD').decode().strip()
    (directory / 'HEAD').write_text(head + '\n')
    (directory / 'status.txt').write_bytes(git(path, 'status', '--porcelain=v1'))
    for name, args in [('unstaged.patch', ('diff', '--no-ext-diff', '--no-textconv', '--binary')), ('staged.patch', ('diff', '--no-ext-diff', '--no-textconv', '--cached', '--binary'))]:
        (directory / name).write_bytes(git(path, *args))
    files = [s for s in git(path, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0') if s]
    with tarfile.open(directory / 'untracked.tar.gz', 'w:gz', dereference=False) as archive:
        for filename in files:
            archive.add(path / filename, arcname=filename)
    # Verify the archive can be fully read before allowing deletion.
    with tarfile.open(directory / 'untracked.tar.gz', 'r:gz') as archive:
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as stream:
                    while stream.read(1024 * 1024):
                        pass
    git(REPO, 'update-ref', f'refs/cleanup-backups/{directory.parent.name}/{path.name}', head)
    (directory / 'complete').touch()


def snapshot(path):
    """Capture changes to source files so a concurrent edit cancels removal."""
    files = git(path, 'ls-files', '-c', '-o', '--exclude-standard', '-z').decode().split('\0')
    result = []
    for name in files:
        if not name:
            continue
        try:
            stat = (path / name).lstat()
            result.append((name, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            result.append((name, None))
    return git(path, 'rev-parse', 'HEAD'), git(path, 'diff', '--no-ext-diff', '--no-textconv', '--cached', '--binary'), result


def parse_args():
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument(
        '--max-idle-minutes', type=int, default=MAX_IDLE_MINUTES_DEFAULT,
        help='idle minutes that count as stale on a filesystem under pressure (default: 60)',
    )
    parser.add_argument(
        '--gentle-idle-minutes', type=int, default=GENTLE_IDLE_MINUTES_DEFAULT,
        help='idle minutes that count as stale below pressure (default: 1440)',
    )
    parser.add_argument(
        '--pressure-threshold', type=float, default=PRESSURE_THRESHOLD_DEFAULT,
        help='percent filesystem usage that enables aggressive reaping (default: 85)',
    )
    parser.add_argument(
        '--respect-leases', action='store_true',
        help='also skip worktrees claimed by open GitHub issues/PRs (off by default)',
    )
    parser.add_argument(
        '--skip-targets', action='store_true',
        help='do not reap stale Cargo target directories',
    )
    return parser.parse_args()


def reap_worktree(record, args, pressure_cache, issues, pulls, stamp):
    """Decide on one worktree and remove it in apply mode. Return True if removed."""
    path = Path(record['worktree'])
    mount, percent, hot = pressure_for(path, args.pressure_threshold, pressure_cache)
    idle_limit = args.max_idle_minutes if hot else args.gentle_idle_minutes
    newest = newest_source_mtime(path)
    if newest >= time.time() - idle_limit * 60:
        log('skipped', path=str(path), reason='recent activity', last_touched=iso(newest),
            mount=mount, percent_used=round(percent, 1), idle_limit_minutes=idle_limit)
        return False
    if busy(path, process_paths()) or git_busy(path):
        log('skipped', path=str(path), reason='active process or Git operation', mount=mount)
        return False
    if args.respect_leases and leased(record, issues, pulls):
        log('skipped', path=str(path), reason='active ownership or unreleased lease', mount=mount)
        return False
    if not args.apply:
        log('would_remove', path=str(path), last_touched=iso(newest), mount=mount,
            percent_used=round(percent, 1))
        return False

    initial = snapshot(path)
    destination = BACKUPS / stamp / path.name
    backup(path, destination)
    fresh_issues, fresh_pulls = ownership() if args.respect_leases else ({}, [])
    if (busy(path, process_paths()) or git_busy(path) or snapshot(path) != initial
            or (args.respect_leases and leased(record, fresh_issues, fresh_pulls))):
        log('skipped', path=str(path), reason='activity changed during backup', mount=mount)
        return False
    subprocess.run(
        ['git', '-C', str(REPO), 'worktree', 'unlock', str(path)],
        check=False, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ['git', '-C', str(REPO), 'worktree', 'remove', '--force', str(path)],
        check=True, timeout=300,
    )
    log('removed', path=str(path), backup=str(destination), mount=mount)
    return True


def reap_targets(args, pressure_cache, cutoff):
    """Reap stale Cargo targets on filesystems under pressure. Return count removed."""
    removed = 0
    paths = process_paths()
    for target, newest in stale_target_dirs(cutoff):
        mount, percent, hot = pressure_for(target, args.pressure_threshold, pressure_cache)
        if not hot:
            log('skip_target', path=str(target), reason='filesystem below pressure threshold',
                mount=mount, percent_used=round(percent, 1))
            continue
        if busy(target, paths):
            log('skip_target', path=str(target), reason='active process', mount=mount)
            continue
        if not args.apply:
            log('would_remove_target', path=str(target), last_touched=iso(newest), mount=mount,
                percent_used=round(percent, 1))
            continue
        try:
            shutil.rmtree(target)
        except OSError as error:
            log('target_failed', path=str(target), error=str(error), mount=mount)
            continue
        removed += 1
        log('removed_target', path=str(target), mount=mount)
    return removed


def main():
    """Preview or apply the pressure-gated policy under a single-process lock."""
    args = parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'cleanup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pressure_cache = {}
        before = shutil.disk_usage(REPO).free
        issues, pulls = ownership() if args.respect_leases else ({}, [])
        stamp = dt.datetime.now(ZONE).strftime('%Y%m%d-%H%M%S-%f')
        removed = sum(
            reap_worktree(record, args, pressure_cache, issues, pulls, stamp)
            for record in worktrees(REPO) if eligible(record)
        )
        targets_removed = 0
        if not args.skip_targets:
            cutoff = time.time() - args.max_idle_minutes * 60
            targets_removed = reap_targets(args, pressure_cache, cutoff)
        log('finished', removed=removed, targets_removed=targets_removed,
            reclaimed_bytes=shutil.disk_usage(REPO).free - before)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        log('failed', error=str(error))
        raise
