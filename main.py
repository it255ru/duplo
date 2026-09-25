"""duplo: finds duplicate files and directories and removes extra copies.

Works as plan / apply: a filesystem snapshot is taken once, a deletion plan
is built from it and validated (every duplicate group keeps at least one
copy), and every step is re-verified against the filesystem right before
deletion. Anything unexpected makes the step fail instead of deleting.

Exit codes:
  0: success (including "nothing to do" and a cancelled deletion).
  1: at least one deletion step failed.
  2: invalid input: missing directory, invalid plan, closed stdin.
  130: interrupted by the user.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import filecmp
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import types
from collections.abc import Callable, Iterable
from typing import Optional

__version__ = '0.3.1'

_FILE_CATEGORIES_SPEC = {
    'images': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp',
               '.raw', '.heic', '.svg', '.ico', '.jpe', '.tif'},
    'videos': {'.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm',
               '.m4v', '.mpg', '.mpeg', '.3gp', '.3gpp', '.m2ts', '.mts',
               '.ts', '.vob'},
    'audio': {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a',
              '.amr', '.mka', '.opus'},
    'documents': {'.pdf', '.doc', '.docx', '.txt', '.rtf', '.xls', '.xlsx',
                  '.ppt', '.pptx', '.odt', '.ods', '.odp', '.md', '.tex'},
    'archives': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
                 '.tgz', '.tbz2'},
    'executables': {'.exe', '.msi', '.bat', '.cmd', '.bin', '.app', '.apk',
                    '.deb', '.rpm'},
    'scripts': {'.py', '.js', '.java', '.c', '.cpp', '.html', '.css', '.php',
                '.rb', '.pl', '.sh', '.bash', '.ps1', '.vbs'},
    'data': {'.db', '.csv', '.json', '.xml', '.sql', '.sqlite', '.sqlite3',
             '.mdb', '.accdb', '.ini', '.cfg'},
    'system': {'.dll', '.sys', '.inf', '.cat', '.drv', '.ocx', '.cpl'},
    'fonts': {'.ttf', '.otf', '.woff', '.woff2', '.eot', '.fon'},
    'design': {'.psd', '.ai', '.sketch', '.fig', '.xd', '.indd'},
}
FILE_CATEGORIES = types.MappingProxyType(
    {cat: frozenset(exts) for cat, exts in _FILE_CATEGORIES_SPEC.items()})
"""Read-only: category -> extensions. Order defines priority."""
_CATEGORY_BY_EXT = types.MappingProxyType(
    {ext: cat for cat, exts in FILE_CATEGORIES.items() for ext in exts})
_SIZE_UNITS = ('B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB')
_PREVIEW_LIMIT = 20        # Files listed in the deletion preview and errors.
_PROGRESS_INTERVAL = 100   # Hashed files between progress updates.
_TOP_EXTENSIONS = 15       # Rows in the per-extension summary.
_TOP_DIRECTORIES = 10      # Rows in the per-directory summary.
_SEPARATOR_WIDTH = 60      # Width of section separator lines.
_HASH_BLOCK = 1 << 20      # Read size for hashing, 1 MiB.


class PlanError(ValueError):
    """Raised when a deletion plan would violate a safety invariant."""


@dataclasses.dataclass(frozen=True)
class FileEntry:
    """Snapshot of one regular file taken during the scan."""
    path: str
    size: int
    mtime_ns: int
    dev: int
    ino: int


Bucket = dict[str, int]
"""Counters {'count': N, 'size': bytes} for one statistics key."""
DuplicateGroups = dict[str, list[FileEntry]]
"""Content digest -> copies sorted by path, 2+ entries each."""
DirGroups = list[list[str]]
"""Groups of identical directories, each sorted, 2+ entries each."""
Selection = tuple[list[str], list[str]]
"""(paths_to_delete, dirs_to_delete) before validation by build_plan."""
KeepChoice = tuple[Optional[set[int]], bool]
"""(kept indices or None to skip the group, apply_to_rest)."""


def _bucket() -> Bucket:
    return {'count': 0, 'size': 0}


@dataclasses.dataclass
class ScanStats:
    """Aggregated scan statistics and non-fatal errors."""
    total_files: int = 0
    total_size: int = 0
    by_extension: collections.defaultdict[str, Bucket] = dataclasses.field(
        default_factory=lambda: collections.defaultdict(_bucket))
    by_category: collections.defaultdict[str, Bucket] = dataclasses.field(
        default_factory=lambda: collections.defaultdict(_bucket))
    by_directory: collections.defaultdict[str, Bucket] = dataclasses.field(
        default_factory=lambda: collections.defaultdict(_bucket))
    skipped: collections.Counter[str] = dataclasses.field(
        default_factory=collections.Counter)
    errors: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Plan:
    """Validated deletion plan.

    Attributes:
      deletions: Pairs (victim, keeper). The keeper is never deleted.
      dirs: Directories to remove with os.rmdir after their files are gone.
      real_parents: Parent directory -> its realpath when the plan was
        built. A mismatch at apply time means a path component was
        replaced (e.g. by a symlink) and the step is refused.
    """
    deletions: list[tuple[FileEntry, FileEntry]]
    dirs: list[str]
    real_parents: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def total_size(self) -> int:
        return sum(victim.size for victim, _ in self.deletions)

    def is_empty(self) -> bool:
        return not self.deletions and not self.dirs


def get_file_category(extension: str) -> str:
    """Maps a file extension to a category.

    Args:
      extension: Extension with a leading dot in any case, or ''.

    Returns:
      Category name from FILE_CATEGORIES, or 'other'.
    """
    return _CATEGORY_BY_EXT.get(extension.lower(), 'other')


def format_size(size_bytes: float) -> str:
    """Formats a byte count, e.g. 1536 -> '1.50 KB'."""
    size = float(size_bytes)
    for unit in _SIZE_UNITS[:-1]:
        if size < 1024.0:
            return f'{size:.2f} {unit}'
        size /= 1024.0
    return f'{size:.2f} {_SIZE_UNITS[-1]}'


def safe_text(text: str) -> str:
    """Escapes non-printable characters so a file name cannot fake output."""
    return ''.join(ch if ch.isprintable() else f'\\u{ord(ch):04x}'
                   for ch in text)


def _key(path: str) -> str:
    """Normalized path used for all identity comparisons."""
    return os.path.normcase(os.path.abspath(path))


def scan_directory(directory: str) -> tuple[list[FileEntry], ScanStats]:
    """Takes a snapshot of all regular files under a directory.

    Symlinks, FIFOs, sockets and devices are skipped and counted. Walk and
    stat errors are recorded in stats.errors instead of being ignored.

    Args:
      directory: Root directory.

    Returns:
      Tuple (files, stats): list of FileEntry and ScanStats.
    """
    stats = ScanStats()
    files = []

    def on_error(err: OSError) -> None:
        stats.errors.append(f'{err.filename}: {err.strerror}')

    for root, _, names in os.walk(os.path.abspath(directory),
                                  onerror=on_error):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError as err:
                on_error(err)
                continue
            if not stat.S_ISREG(st.st_mode):
                stats.skipped['non_regular'] += 1
                continue
            entry = FileEntry(path, st.st_size, st.st_mtime_ns, st.st_dev,
                              st.st_ino)
            files.append(entry)
            ext = os.path.splitext(name)[1].lower()
            stats.total_files += 1
            stats.total_size += entry.size
            for bucket in (stats.by_extension[ext],
                           stats.by_category[get_file_category(ext)],
                           stats.by_directory[root]):
                bucket['count'] += 1
                bucket['size'] += entry.size
    return files, stats


def hash_file(path: str) -> str:
    """Returns a BLAKE2b hex digest of a file. Raises OSError on read errors."""
    digest = hashlib.blake2b(usedforsecurity=False)
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(_HASH_BLOCK), b''):
            digest.update(block)
    return digest.hexdigest()


def default_cache_path() -> str:
    """Returns the per-user cache location (never the current directory)."""
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    else:
        base = (os.environ.get('XDG_CACHE_HOME')
                or os.path.join(os.path.expanduser('~'), '.cache'))
    return os.path.join(base, 'duplo', 'hash_cache.json')


class HashCache:
    """JSON cache of file hashes keyed by path, size, mtime_ns and inode.

    The cache is an optimization only: any load error yields an empty cache
    with a warning. It never deserializes executable content.
    """

    def __init__(self, cache_file: str) -> None:
        self._path = cache_file
        self._data = {}
        try:
            with open(cache_file, encoding='utf-8') as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            print(f'[WARN] Кэш проигнорирован ({safe_text(cache_file)}): {err}',
                  file=sys.stderr)
            return
        if isinstance(loaded, dict):
            self._data = loaded

    def get(self, entry: FileEntry) -> Optional[str]:
        record = self._data.get(_key(entry.path))
        if (isinstance(record, dict)
            and record.get('key') == [entry.size, entry.mtime_ns, entry.ino]):
            digest = record.get('hash')
            return digest if isinstance(digest, str) else None
        return None

    def set(self, entry: FileEntry, digest: str) -> None:
        self._data[_key(entry.path)] = {
            'key': [entry.size, entry.mtime_ns, entry.ino], 'hash': digest}

    def save(self) -> None:
        """Writes the cache atomically with mode 0600. Raises OSError."""
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.hash_cache.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._data, f)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def find_duplicates(files: Iterable[FileEntry],
                    cache: Optional[HashCache] = None,
                    errors: Optional[list[str]] = None) -> DuplicateGroups:
    """Groups non-empty regular files with identical content hashes.

    Hardlinks to one inode are reported once: deleting one of them frees no
    space. When st_ino is 0 (no stable file ID) no collapsing happens.

    Args:
      files: Output of scan_directory.
      cache: Optional HashCache.
      errors: Optional list that receives read errors.

    Returns:
      Dict digest -> list of FileEntry sorted by path, 2+ entries each.
    """
    by_size = collections.defaultdict(list)
    seen_inodes = set()
    for entry in files:
        if entry.size == 0:
            continue
        if entry.ino:
            inode = (entry.dev, entry.ino)
            if inode in seen_inodes:
                continue
            seen_inodes.add(inode)
        by_size[entry.size].append(entry)

    candidates = [e for group in by_size.values() if len(group) > 1
                  for e in group]
    groups = collections.defaultdict(list)
    start = time.monotonic()
    for done, entry in enumerate(candidates, 1):
        digest = cache.get(entry) if cache else None
        if digest is None:
            try:
                digest = hash_file(entry.path)
            except OSError as err:
                if errors is not None:
                    errors.append(f'{err.filename}: {err.strerror}')
                continue
            if cache:
                cache.set(entry, digest)
        groups[digest].append(entry)
        if done % _PROGRESS_INTERVAL == 0:
            elapsed = max(time.monotonic() - start, 1e-9)  # Avoid div by 0.
            rate = done / elapsed
            print(f'Обработано: {done}/{len(candidates)} ({rate:.1f} файл/с)',
                  end='\r', file=sys.stderr)
    return {d: sorted(g, key=lambda e: e.path)
            for d, g in groups.items() if len(g) > 1}


def _leaf_signature(dir_path: str,
                    hash_by_key: dict[str, str]) -> Optional[tuple[str, ...]]:
    """Returns sorted hashes of a leaf directory, or None if not eligible.

    Eligible: no subdirectories, no non-regular entries, every file is in a
    duplicate group.
    """
    hashes = []
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    return None
                digest = hash_by_key.get(_key(entry.path))
                if digest is None:
                    return None
                hashes.append(digest)
    except OSError:
        return None
    return tuple(sorted(hashes)) or None


def find_identical_directories(duplicates: DuplicateGroups) -> DirGroups:
    """Finds leaf directories whose contents are identical.

    Args:
      duplicates: Output of find_duplicates.

    Returns:
      Sorted list of groups; each group is a sorted list of 2+ directories.
    """
    hash_by_key = {_key(e.path): d for d, group in duplicates.items()
                   for e in group}
    candidate_dirs = {os.path.dirname(e.path) for group in duplicates.values()
                      for e in group}
    by_signature = collections.defaultdict(list)
    for dir_path in candidate_dirs:
        signature = _leaf_signature(dir_path, hash_by_key)
        if signature:
            by_signature[signature].append(dir_path)
    return sorted(sorted(g) for g in by_signature.values() if len(g) > 1)


def parse_keep_indices(answer: str, count: int) -> set[int]:
    """Parses 1-based copy numbers to keep ('1 3' or '1,3').

    Raises:
      ValueError: Empty answer, non-numeric token or number out of range.
    """
    tokens = answer.replace(',', ' ').split()
    if not tokens:
        raise ValueError('не выбрано ни одной копии для сохранения')
    indices = set()
    for token in tokens:
        if not token.isdigit() or not 1 <= int(token) <= count:
            raise ValueError(f'неверный номер: {token!r}, допустимо 1..{count}')
        indices.add(int(token) - 1)
    return indices


def ask_keep(count: int,
             read: Optional[Callable[[str], str]] = None) -> KeepChoice:
    """Asks which items of a group to keep until the answer is valid.

    Args:
      count: Number of items in the group.
      read: Input function; defaults to builtins.input resolved at call
        time.

    Returns:
      Tuple (kept_indices or None to skip the group, apply_to_rest).
    """
    read = read or input
    while True:
        answer = read('Ваш выбор: ').strip()
        if answer == 's':
            return None, False
        if answer == 'a':
            return {0}, False
        if answer == 'A':
            return {0}, True
        if answer == 'b':
            return {count - 1}, False
        if answer == 'm':
            try:
                kept = parse_keep_indices(read('Номера сохраняемых: '), count)
                return kept, False
            except ValueError as err:
                print(f'Ошибка: {err}')
                continue
        print('Неверный выбор')


_MENU = ('  [s] пропустить  [a] оставить первую  [b] оставить последнюю\n'
         '  [m] выбрать вручную  [A] оставить первую во всех оставшихся')


def select_interactive(duplicates: DuplicateGroups, identical_dirs: DirGroups,
                       read: Optional[Callable[[str], str]] = None
                       ) -> Selection:
    """Interactively selects files and directories to delete.

    Returns:
      Tuple (paths_to_delete, dirs_to_delete). Not validated: pass the
      result to build_plan.

    Raises:
      EOFError: stdin was closed.
    """
    paths, dirs = [], []
    auto = False
    for i, group in enumerate(duplicates.values(), 1):
        print(f'\nГруппа {i}, размер {format_size(group[0].size)}')
        for j, entry in enumerate(group, 1):
            print(f'  [{j}] {safe_text(entry.path)}')
        if auto:
            keep = {0}
        else:
            print(_MENU)
            keep, auto = ask_keep(len(group), read)
        if keep is not None:
            paths.extend(e.path for k, e in enumerate(group) if k not in keep)

    auto = False
    for i, group in enumerate(identical_dirs, 1):
        print(f'\nГруппа идентичных каталогов {i}')
        for j, dir_path in enumerate(group, 1):
            print(f'  [{j}] {safe_text(dir_path)}')
        if auto:
            keep = {0}
        else:
            print(_MENU)
            keep, auto = ask_keep(len(group), read)
        if keep is not None:
            dirs.extend(d for k, d in enumerate(group) if k not in keep)
    return paths, dirs


def select_auto_first(duplicates: DuplicateGroups,
                      identical_dirs: DirGroups) -> Selection:
    """Keeps the first item (by sorted path) of every group."""
    paths = [e.path for group in duplicates.values() for e in group[1:]]
    dirs = [d for group in identical_dirs for d in group[1:]]
    return paths, dirs


def build_plan(duplicates: DuplicateGroups, paths_to_delete: Iterable[str],
               dirs_to_delete: Iterable[str]) -> Plan:
    """Builds a deletion plan and checks its safety invariants.

    Every file of a directory to delete is added to the deletions. Each
    deleted file gets a keeper: a surviving copy from the same group.

    Raises:
      PlanError: A group would lose all copies, or a path is not a known
        duplicate.
    """
    known = {_key(e.path) for g in duplicates.values() for e in g}
    doomed = {_key(p) for p in paths_to_delete}
    unknown = doomed - known
    if unknown:
        raise PlanError('в плане есть файлы, не являющиеся дубликатами: '
                        + ', '.join(sorted(safe_text(p) for p in unknown)))
    dirs = sorted(set(dirs_to_delete))
    dir_keys = {_key(d) for d in dirs}
    doomed |= {k for k in known if os.path.dirname(k) in dir_keys}

    deletions = []
    for group in duplicates.values():
        survivors = [e for e in group if _key(e.path) not in doomed]
        victims = [e for e in group if _key(e.path) in doomed]
        if victims and not survivors:
            raise PlanError('план удаляет все копии группы: '
                            + ', '.join(safe_text(e.path) for e in group))
        deletions.extend((victim, survivors[0]) for victim in victims)
    parents = {os.path.dirname(v.path) for v, _ in deletions}
    parents |= {os.path.dirname(d) for d in dirs}
    real_parents = {p: os.path.realpath(p) for p in parents}
    return Plan(deletions=deletions, dirs=dirs, real_parents=real_parents)


def _parent_moved(path: str, plan: Plan) -> bool:
    """True if the parent of path resolves elsewhere than at plan time.

    Catches a directory on the path being swapped for a symlink between
    confirmation and deletion. A window between this check and the
    removal itself remains.
    """
    parent = os.path.dirname(path)
    return os.path.realpath(parent) != plan.real_parents.get(parent)


def verify_before_delete(victim: FileEntry,
                         keeper: FileEntry) -> Optional[str]:
    """Checks that victim is still a byte-identical copy of keeper.

    Returns:
      None if deletion is safe, otherwise a reason. OSError propagates.
    """
    st = os.lstat(victim.path)
    if not stat.S_ISREG(st.st_mode):
        return 'не является обычным файлом'
    if (st.st_size, st.st_mtime_ns) != (victim.size, victim.mtime_ns):
        return 'изменён после сканирования'
    kst = os.lstat(keeper.path)
    if not stat.S_ISREG(kst.st_mode):
        return 'сохраняемая копия не является обычным файлом'
    if st.st_ino and (st.st_dev, st.st_ino) == (kst.st_dev, kst.st_ino):
        return 'это тот же файл, что и сохраняемая копия'
    # filecmp keeps a module-level result cache keyed by (size, mtime);
    # clear it so a stale result can never authorize a deletion.
    filecmp.clear_cache()
    if not filecmp.cmp(keeper.path, victim.path, shallow=False):
        return 'содержимое отличается от сохраняемой копии'
    return None


def _delete_file(victim: FileEntry, keeper: FileEntry, plan: Plan,
                 dry_run: bool) -> bool:
    """Verifies and deletes one planned file. Returns True on success."""
    shown = safe_text(victim.path)
    try:
        if _parent_moved(victim.path, plan):
            reason = 'каталог изменился после построения плана'
        else:
            reason = verify_before_delete(victim, keeper)
        if reason:
            print(f'[SKIP] {shown}: {reason}', file=sys.stderr)
            return False
        if dry_run:
            print(f'[DRY-RUN] удалить {shown}')
        else:
            os.remove(victim.path)
            print(f'Удалён {shown}')
        return True
    except OSError as err:
        print(f'[ERROR] {shown}: {err.strerror}', file=sys.stderr)
        return False


def _predict_rmdir(dir_path: str, plan: Plan) -> bool:
    """Dry-run check: would the directory be empty after planned deletions?

    Raises:
      OSError: The directory cannot be listed.
    """
    shown = safe_text(dir_path)
    planned = {os.path.normcase(os.path.basename(v.path))
               for v, _ in plan.deletions
               if _key(os.path.dirname(v.path)) == _key(dir_path)}
    extra = {os.path.normcase(n) for n in os.listdir(dir_path)} - planned
    if extra:
        print(f'[DRY-RUN] каталог {shown} не будет удалён: '
              f'{len(extra)} объектов вне плана', file=sys.stderr)
        return False
    print(f'[DRY-RUN] удалить пустой каталог {shown}')
    return True


def _remove_dir(dir_path: str, plan: Plan, dry_run: bool) -> bool:
    """Removes one planned directory if it is empty. Returns True on success.

    os.rmdir fails on a non-empty directory, so anything not covered by the
    plan keeps the directory in place.
    """
    shown = safe_text(dir_path)
    try:
        if dry_run:
            return _predict_rmdir(dir_path, plan)
        if _parent_moved(dir_path, plan) or os.path.islink(dir_path):
            print(f'[SKIP] каталог {shown}: путь изменился после '
                  f'построения плана', file=sys.stderr)
            return False
        os.rmdir(dir_path)
        print(f'Удалён каталог {shown}')
        return True
    except OSError as err:
        print(f'[ERROR] каталог {shown} не удалён: {err.strerror}',
              file=sys.stderr)
        return False


def apply_plan(plan: Plan, dry_run: bool = False) -> int:
    """Executes a plan: verified file deletions, then os.rmdir of dirs.

    Args:
      plan: Output of build_plan.
      dry_run: Verify and print, but delete nothing.

    Returns:
      Number of failed steps. Files are never deleted without verification;
      directories are never removed recursively.
    """
    failures = 0
    freed = 0
    deleted = 0
    for victim, keeper in plan.deletions:
        if _delete_file(victim, keeper, plan, dry_run):
            deleted += 1
            freed += victim.size
        else:
            failures += 1
    failures += sum(not _remove_dir(d, plan, dry_run) for d in plan.dirs)
    prefix = 'Будет удалено' if dry_run else 'Удалено'
    print(f'\n{prefix}: {deleted} файлов, {format_size(freed)}; '
          f'ошибок: {failures}')
    return failures


def print_section(title: str) -> None:
    separator = '=' * _SEPARATOR_WIDTH
    print(f'\n{separator}\n{title}\n{separator}')


def print_summary(stats: ScanStats) -> None:
    """Prints scan totals, categories, top extensions and directories."""
    print_section('СВОДНАЯ СТАТИСТИКА')
    print(f'Файлов: {stats.total_files}, '
          f'объём: {format_size(stats.total_size)}')
    if stats.skipped:
        print(f'Пропущено необычных объектов (symlink, FIFO и т.п.): '
              f'{stats.skipped["non_regular"]}')

    print_section('ПО КАТЕГОРИЯМ')
    for category, data in sorted(stats.by_category.items()):
        share = data['count'] / stats.total_files * 100
        print(f'{category.upper():<12} {data["count"]:>7} ({share:5.1f}%) '
              f'{format_size(data["size"]):>12}')

    print_section(f'ПО РАСШИРЕНИЯМ (ТОП-{_TOP_EXTENSIONS} ПО ОБЪЁМУ)')
    top_ext = sorted(stats.by_extension.items(),
                     key=lambda item: item[1]['size'],
                     reverse=True)[:_TOP_EXTENSIONS]
    for ext, data in top_ext:
        print(f'{safe_text(ext) or "(нет)":<10} {data["count"]:>7} '
              f'{format_size(data["size"]):>12}')

    print_section(f'ПО КАТАЛОГАМ (ТОП-{_TOP_DIRECTORIES} ПО ОБЪЁМУ, '
                  f'БЕЗ ПОДКАТАЛОГОВ)')
    top_dirs = sorted(stats.by_directory.items(),
                      key=lambda item: item[1]['size'],
                      reverse=True)[:_TOP_DIRECTORIES]
    for dir_path, data in top_dirs:
        print(f'{safe_text(dir_path)}: {data["count"]} файлов, '
              f'{format_size(data["size"])}')


def print_duplicates(duplicates: DuplicateGroups, by_category: bool) -> None:
    """Prints duplicate groups, optionally grouped by category."""
    wasted = sum(g[0].size * (len(g) - 1) for g in duplicates.values())
    print_section('ДУБЛИКАТЫ')
    print(f'Групп: {len(duplicates)}, лишний объём: {format_size(wasted)}')
    sections = collections.defaultdict(list)
    for digest, group in duplicates.items():
        ext = os.path.splitext(group[0].path)[1]
        key = get_file_category(ext) if by_category else 'все'
        sections[key].append((digest, group))
    for section, groups in sorted(sections.items()):
        if by_category:
            print_section(f'КАТЕГОРИЯ: {section.upper()}')
        for i, (digest, group) in enumerate(groups, 1):
            print(f'\nГруппа {i} ({digest[:8]}), {format_size(group[0].size)}')
            for entry in group:
                print(f'  -> {safe_text(entry.path)}')


def print_identical_dirs(identical_dirs: DirGroups) -> None:
    print_section('ИДЕНТИЧНЫЕ КАТАЛОГИ (ТОЛЬКО БЕЗ ПОДКАТАЛОГОВ)')
    for i, group in enumerate(identical_dirs, 1):
        print(f'\nГруппа {i}')
        for dir_path in group:
            print(f'  -> {safe_text(dir_path)}')


def print_preview(plan: Plan) -> None:
    """Prints exactly what apply_plan will attempt to delete."""
    print_section('ПЛАН УДАЛЕНИЯ')
    for victim, keeper in plan.deletions[:_PREVIEW_LIMIT]:
        print(f'  - {safe_text(victim.path)}\n'
              f'      копия остаётся: {safe_text(keeper.path)}')
    hidden = len(plan.deletions) - _PREVIEW_LIMIT
    if hidden > 0:
        print(f'  ... и ещё {hidden} файлов')
    for dir_path in plan.dirs:
        print(f'  каталог (после удаления файлов, только если пуст): '
              f'{safe_text(dir_path)}')
    print(f'\nФайлов: {len(plan.deletions)}, каталогов: {len(plan.dirs)}, '
          f'освободится: {format_size(plan.total_size)}')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='duplo', description='Поиск и удаление дубликатов файлов.')
    parser.add_argument('source_dir', help='каталог для анализа')
    parser.add_argument('--cache-file', default=default_cache_path(),
                        help='файл кэша хешей (JSON), по умолчанию %(default)s')
    parser.add_argument('--no-cache', action='store_true',
                        help='не читать и не записывать кэш')
    parser.add_argument('--group-by-category', action='store_true',
                        help='группировать дубликаты по категориям')
    parser.add_argument('--find-identical-dirs', action='store_true',
                        help='искать идентичные каталоги без подкаталогов')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--interactive', action='store_true',
                      help='интерактивный выбор копий для удаления')
    mode.add_argument('--auto-first', action='store_true',
                      help='оставить первую по пути копию в каждой группе')
    parser.add_argument('--dry-run', action='store_true',
                        help='показать и проверить план без удаления')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')
    return parser


def _analyze(args: argparse.Namespace
             ) -> tuple[DuplicateGroups, DirGroups, ScanStats]:
    """Scans, hashes and groups. Prints the summary; never deletes."""
    files, stats = scan_directory(args.source_dir)
    print_summary(stats)
    cache = None if args.no_cache else HashCache(args.cache_file)
    duplicates = find_duplicates(files, cache, stats.errors)
    if cache:
        try:
            cache.save()
        except OSError as err:
            print(f'[WARN] Кэш не сохранён: {err}', file=sys.stderr)
    identical_dirs = (find_identical_directories(duplicates)
                      if args.find_identical_dirs else [])
    return duplicates, identical_dirs, stats


def _report(duplicates: DuplicateGroups, identical_dirs: DirGroups,
            stats: ScanStats, by_category: bool) -> None:
    """Prints found groups and non-fatal read errors."""
    if duplicates:
        print_duplicates(duplicates, by_category)
    else:
        print('\nДубликаты не найдены.')
    if identical_dirs:
        print_identical_dirs(identical_dirs)
    if stats.errors:
        print(f'\n[WARN] Ошибок чтения: {len(stats.errors)}. Эти файлы и '
              f'каталоги не участвуют в анализе:', file=sys.stderr)
        for error in stats.errors[:_PREVIEW_LIMIT]:
            print(f'  {safe_text(error)}', file=sys.stderr)


def _confirmed() -> bool:
    """Asks for a final 'y'. A closed stdin counts as 'no'."""
    try:
        answer = input('\nПодтвердите удаление (y/n): ')
    except EOFError:
        return False
    return answer.strip().lower() == 'y'


def _delete(args: argparse.Namespace, duplicates: DuplicateGroups,
            identical_dirs: DirGroups) -> int:
    """Selects, validates, previews and applies a plan. Returns exit code."""
    try:
        if args.auto_first:
            paths, dirs = select_auto_first(duplicates, identical_dirs)
        else:
            paths, dirs = select_interactive(duplicates, identical_dirs)
        plan = build_plan(duplicates, paths, dirs)
    except EOFError:
        print('Ошибка: stdin закрыт, выбор невозможен.', file=sys.stderr)
        return 2
    except PlanError as err:
        print(f'Ошибка плана, ничего не удалено: {err}', file=sys.stderr)
        return 2
    if plan.is_empty():
        print('\nНечего удалять.')
        return 0
    print_preview(plan)
    if not args.dry_run and not _confirmed():
        print('Удаление отменено.')
        return 0
    return 1 if apply_plan(plan, dry_run=args.dry_run) else 0


def _tolerate_unencodable_output() -> None:
    """Prevents crashes on names the output encoding cannot represent.

    A redirected stdout on Windows uses the ANSI code page (e.g. cp1251),
    which has no emoji and, on non-Russian systems, no Cyrillic. Such
    characters are written as backslash escapes instead of raising
    UnicodeEncodeError in the middle of a report or a deletion.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(errors='backslashreplace')


def main(argv: Optional[list[str]] = None) -> int:
    """Command-line entry point.

    Args:
      argv: Arguments without the program name; None means sys.argv[1:].

    Returns:
      Process exit code, see the module docstring.
    """
    _tolerate_unencodable_output()
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.source_dir):
        print(f'Ошибка: каталог не найден: {safe_text(args.source_dir)}',
              file=sys.stderr)
        return 2
    duplicates, identical_dirs, stats = _analyze(args)
    _report(duplicates, identical_dirs, stats, args.group_by_category)
    if not (args.interactive or args.auto_first) or not duplicates:
        return 0
    return _delete(args, duplicates, identical_dirs)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nПрервано.', file=sys.stderr)
        sys.exit(130)
