"""Tests for duplo (main.py). Run: pytest -q test_main.py"""

import os
import pickle
import stat
import sys

import pytest

import main as duplo

POSIX_ONLY = pytest.mark.skipif(sys.platform == 'win32',
                                reason='symlink/FIFO need POSIX')


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _analyze(root):
    files, stats = duplo.scan_directory(str(root))
    duplicates = duplo.find_duplicates(files, None, stats.errors)
    return files, duplicates


def _names(group):
    return sorted(os.path.basename(e.path) for e in group)


def _left(root):
    return sorted(str(p.relative_to(root)).replace(os.sep, '/')
                  for p in root.rglob('*') if p.is_file())


def _run(root, *flags, answers=('y',), monkeypatch=None):
    feed = iter(answers)
    monkeypatch.setattr('builtins.input', lambda prompt='': next(feed))
    return duplo.main([str(root), '--no-cache', *flags])


# --- scan ------------------------------------------------------------------

@POSIX_ONLY
def test_scan_directory_symlink_skipped(tmp_path):
    _write(tmp_path / 'real.jpg', b'R' * 50)
    os.symlink(tmp_path / 'real.jpg', tmp_path / 'link.jpg')

    files, stats = duplo.scan_directory(str(tmp_path))

    assert [os.path.basename(f.path) for f in files] == ['real.jpg']
    assert stats.skipped['non_regular'] == 1


@POSIX_ONLY
def test_scan_directory_fifo_skipped(tmp_path):
    os.mkfifo(tmp_path / 'pipe')

    files, _ = duplo.scan_directory(str(tmp_path))

    assert files == []


@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0,
                    reason='root ignores permissions')
@POSIX_ONLY
def test_scan_directory_unreadable_dir_reported(tmp_path):
    locked = tmp_path / 'locked'
    _write(locked / 'a.jpg', b'x')
    locked.chmod(0)
    try:
        _, stats = duplo.scan_directory(str(tmp_path))
    finally:
        locked.chmod(stat.S_IRWXU)

    assert stats.errors


# --- duplicates -------------------------------------------------------------

def test_find_duplicates_same_content_grouped(tmp_path):
    _write(tmp_path / 'a' / 'one.jpg', b'Z' * 10)
    _write(tmp_path / 'b' / 'two.jpg', b'Z' * 10)
    _write(tmp_path / 'c' / 'other.jpg', b'Y' * 10)

    _, duplicates = _analyze(tmp_path)

    assert [_names(g) for g in duplicates.values()] == [['one.jpg', 'two.jpg']]


def test_find_duplicates_empty_files_not_reported(tmp_path):
    _write(tmp_path / 'pkg' / '__init__.py', b'')
    _write(tmp_path / 'data' / '.gitkeep', b'')

    _, duplicates = _analyze(tmp_path)

    assert duplicates == {}


def test_find_duplicates_hardlinks_not_reported(tmp_path):
    _write(tmp_path / 'a.jpg', b'H' * 64)
    os.link(tmp_path / 'a.jpg', tmp_path / 'b.jpg')

    _, duplicates = _analyze(tmp_path)

    assert duplicates == {}


def test_find_duplicates_groups_sorted_by_path(tmp_path):
    _write(tmp_path / 'z.jpg', b'S' * 8)
    _write(tmp_path / 'a.jpg', b'S' * 8)

    _, duplicates = _analyze(tmp_path)

    assert _names(next(iter(duplicates.values()))) == ['a.jpg', 'z.jpg']
    assert [os.path.basename(e.path)
            for e in next(iter(duplicates.values()))] == ['a.jpg', 'z.jpg']


def _failing_hash_for(name):
    """Returns a hash_file replacement that fails for one file name."""
    real = duplo.hash_file

    def fake(path):
        if os.path.basename(path) == name:
            raise PermissionError(13, 'Permission denied', path)
        return real(path)

    return fake


def test_find_duplicates_read_error_reported_and_excluded(tmp_path,
                                                          monkeypatch):
    for name in ('a.jpg', 'b.jpg', 'locked.jpg'):
        _write(tmp_path / name, b'D' * 32)
    monkeypatch.setattr('main.hash_file', _failing_hash_for('locked.jpg'))
    files, stats = duplo.scan_directory(str(tmp_path))

    duplicates = duplo.find_duplicates(files, None, stats.errors)

    assert [_names(g) for g in duplicates.values()] == [['a.jpg', 'b.jpg']]
    assert len(stats.errors) == 1
    assert 'locked.jpg' in stats.errors[0]


def test_find_duplicates_read_error_not_cached(tmp_path, monkeypatch):
    for name in ('a.jpg', 'locked.jpg'):
        _write(tmp_path / name, b'D' * 32)
    monkeypatch.setattr('main.hash_file', _failing_hash_for('locked.jpg'))
    files, _ = duplo.scan_directory(str(tmp_path))
    cache = duplo.HashCache(str(tmp_path / 'cache.json'))

    duplo.find_duplicates(files, cache, [])

    locked = next(f for f in files if f.path.endswith('locked.jpg'))
    assert cache.get(locked) is None


def test_find_identical_directories_dir_with_unreadable_file_excluded(
        tmp_path, monkeypatch):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    # Same size as x.jpg, so hashing is attempted and fails.
    _write(tmp_path / 'B' / 'locked.jpg', b'X' * 100)
    monkeypatch.setattr('main.hash_file', _failing_hash_for('locked.jpg'))
    files, stats = duplo.scan_directory(str(tmp_path))
    duplicates = duplo.find_duplicates(files, None, stats.errors)

    assert duplo.find_identical_directories(duplicates) == []


# --- identical directories --------------------------------------------------

def test_find_identical_directories_dir_with_unique_file_excluded(tmp_path):
    _write(tmp_path / 'A' / 'shared.jpg', b'X' * 100)
    _write(tmp_path / 'A' / 'only_a.jpg', b'a' * 7)
    _write(tmp_path / 'B' / 'shared.jpg', b'X' * 100)
    _write(tmp_path / 'B' / 'only_b.jpg', b'b' * 9)
    _, duplicates = _analyze(tmp_path)

    assert duplo.find_identical_directories(duplicates) == []


def test_find_identical_directories_parent_with_subdir_excluded(tmp_path):
    _write(tmp_path / 'A' / 'x.jpg', b'X' * 100)
    _write(tmp_path / 'A' / 'sub' / 'x.jpg', b'X' * 100)
    _write(tmp_path / 'B' / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)

    groups = duplo.find_identical_directories(duplicates)

    assert all(str(tmp_path / 'A') not in g for g in groups)


def test_find_identical_directories_leaf_dirs_grouped(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
        _write(tmp_path / name / 'y.jpg', b'Y' * 50)
    _, duplicates = _analyze(tmp_path)

    groups = duplo.find_identical_directories(duplicates)

    assert groups == [[str(tmp_path / 'A'), str(tmp_path / 'B')]]


# --- plan -------------------------------------------------------------------

def test_parse_keep_indices_empty_answer_raises():
    with pytest.raises(ValueError):
        duplo.parse_keep_indices('', 3)


def test_parse_keep_indices_out_of_range_raises():
    with pytest.raises(ValueError):
        duplo.parse_keep_indices('0 4', 3)


def test_parse_keep_indices_comma_separated():
    assert duplo.parse_keep_indices('1,3', 3) == {0, 2}


def test_ask_keep_uppercase_a_applies_to_rest():
    answers = iter(['A'])
    assert duplo.ask_keep(3, lambda _: next(answers)) == ({0}, True)


def test_ask_keep_invalid_manual_answer_reasked():
    answers = iter(['m', '', 'm', '2'])
    assert duplo.ask_keep(3, lambda _: next(answers)) == ({1}, False)


def test_select_interactive_uppercase_a_applies_to_rest(tmp_path):
    for i in range(3):
        _write(tmp_path / f'g{i}_1.jpg', bytes([i]) * (10 + i))
        _write(tmp_path / f'g{i}_2.jpg', bytes([i]) * (10 + i))
    _, duplicates = _analyze(tmp_path)
    answers = iter(['A'])

    paths, dirs = duplo.select_interactive(duplicates, [],
                                           lambda _: next(answers))

    assert sorted(os.path.basename(p) for p in paths) == [
        'g0_2.jpg', 'g1_2.jpg', 'g2_2.jpg']
    assert dirs == []


def test_select_interactive_file_auto_does_not_leak_to_dirs(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)
    identical = duplo.find_identical_directories(duplicates)
    answers = iter(['A', 's'])

    paths, dirs = duplo.select_interactive(duplicates, identical,
                                           lambda _: next(answers))

    assert len(paths) == 1
    assert dirs == []


def test_file_categories_read_only():
    with pytest.raises(TypeError):
        duplo.FILE_CATEGORIES['new'] = {'.x'}
    with pytest.raises(AttributeError):
        duplo.FILE_CATEGORIES['images'].add('.x')


def test_build_plan_rejects_deleting_all_copies(tmp_path):
    _write(tmp_path / 'A' / 'x.jpg', b'X' * 100)
    _write(tmp_path / 'B' / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)

    with pytest.raises(duplo.PlanError):
        duplo.build_plan(duplicates, [str(tmp_path / 'A' / 'x.jpg')],
                         [str(tmp_path / 'B')])


def test_build_plan_rejects_non_duplicate_path(tmp_path):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    _write(tmp_path / 'b.jpg', b'X' * 10)
    _write(tmp_path / 'unique.jpg', b'U' * 10)
    _, duplicates = _analyze(tmp_path)

    with pytest.raises(duplo.PlanError):
        duplo.build_plan(duplicates, [str(tmp_path / 'unique.jpg')], [])


# --- apply ------------------------------------------------------------------

def test_apply_plan_modified_file_not_deleted(tmp_path):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    _write(tmp_path / 'b.jpg', b'X' * 10)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [str(tmp_path / 'b.jpg')], [])
    (tmp_path / 'b.jpg').write_bytes(b'Y' * 10)

    failures = duplo.apply_plan(plan)

    assert failures == 1
    assert (tmp_path / 'b.jpg').read_bytes() == b'Y' * 10


def test_apply_plan_missing_keeper_victim_kept(tmp_path):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    _write(tmp_path / 'b.jpg', b'X' * 10)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [str(tmp_path / 'b.jpg')], [])
    (tmp_path / 'a.jpg').unlink()

    assert duplo.apply_plan(plan) == 1
    assert (tmp_path / 'b.jpg').exists()


def test_apply_plan_dir_with_new_file_not_removed(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [], [str(tmp_path / 'B')])
    _write(tmp_path / 'B' / 'new.jpg', b'new')

    assert duplo.apply_plan(plan) == 1
    assert (tmp_path / 'B' / 'new.jpg').exists()


def test_apply_plan_dir_with_new_subdir_not_removed(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [], [str(tmp_path / 'B')])
    _write(tmp_path / 'B' / 'sub' / 'late.jpg', b'late')

    assert duplo.apply_plan(plan) == 1
    assert (tmp_path / 'B' / 'sub' / 'late.jpg').exists()


def test_apply_plan_dir_file_replaced_after_plan_kept(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [], [str(tmp_path / 'B')])
    (tmp_path / 'B' / 'x.jpg').write_bytes(b'Y' * 100)

    assert duplo.apply_plan(plan) == 2
    assert (tmp_path / 'B' / 'x.jpg').read_bytes() == b'Y' * 100


def test_apply_plan_dry_run_predicts_dir_not_empty(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [], [str(tmp_path / 'B')])
    _write(tmp_path / 'B' / 'new.jpg', b'new')

    assert duplo.apply_plan(plan, dry_run=True) == 1
    assert _left(tmp_path) == ['A/x.jpg', 'B/new.jpg', 'B/x.jpg']


def test_apply_plan_dry_run_empty_dir_predicted_ok(tmp_path):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [], [str(tmp_path / 'B')])

    assert duplo.apply_plan(plan, dry_run=True) == 0


@POSIX_ONLY
def test_apply_plan_parent_swapped_for_symlink_outside_untouched(tmp_path):
    root = tmp_path / 'root'
    outside = tmp_path / 'outside'
    _write(root / 'A' / 'x.jpg', b'X' * 100)
    _write(root / 'B' / 'x.jpg', b'X' * 100)
    _write(outside / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(root)
    plan = duplo.build_plan(duplicates, [str(root / 'B' / 'x.jpg')], [])
    victim = plan.deletions[0][0]
    # Same size and mtime as the victim: only the parent check can stop it.
    os.utime(outside / 'x.jpg', ns=(victim.mtime_ns, victim.mtime_ns))
    (root / 'B' / 'x.jpg').unlink()
    (root / 'B').rmdir()
    os.symlink(outside, root / 'B')

    assert duplo.apply_plan(plan) == 1
    assert (outside / 'x.jpg').exists()


@POSIX_ONLY
def test_apply_plan_dir_swapped_for_symlink_not_removed(tmp_path):
    root = tmp_path / 'root'
    for name in ('A', 'B'):
        _write(root / name / 'x.jpg', b'X' * 100)
    _, duplicates = _analyze(root)
    plan = duplo.build_plan(duplicates, [], [str(root / 'B')])
    (root / 'B' / 'x.jpg').unlink()
    (root / 'B').rmdir()
    (tmp_path / 'empty').mkdir()
    os.symlink(tmp_path / 'empty', root / 'B')

    assert duplo.apply_plan(plan) >= 1
    assert (tmp_path / 'empty').is_dir()
    assert os.path.islink(root / 'B')


def test_main_source_dir_is_symlink_works(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        return
    _write(tmp_path / 'real' / 'a.jpg', b'X' * 10)
    _write(tmp_path / 'real' / 'b.jpg', b'X' * 10)
    os.symlink(tmp_path / 'real', tmp_path / 'link')

    code = _run(tmp_path / 'link', '--auto-first', monkeypatch=monkeypatch)

    assert code == 0
    assert _left(tmp_path / 'real') == ['a.jpg']


def test_apply_plan_dry_run_deletes_nothing(tmp_path):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    _write(tmp_path / 'b.jpg', b'X' * 10)
    _, duplicates = _analyze(tmp_path)
    plan = duplo.build_plan(duplicates, [str(tmp_path / 'b.jpg')], [])

    assert duplo.apply_plan(plan, dry_run=True) == 0
    assert _left(tmp_path) == ['a.jpg', 'b.jpg']


# --- cache ------------------------------------------------------------------

def test_hash_cache_pickle_payload_not_executed(tmp_path):
    marker = tmp_path / 'pwned'

    class Payload:
        def __reduce__(self):
            return (open, (str(marker), 'w'))

    cache_path = tmp_path / 'hash_cache.pkl'
    cache_path.write_bytes(pickle.dumps(Payload()))

    duplo.HashCache(str(cache_path))

    assert not marker.exists()


def test_hash_cache_roundtrip_stale_entry_ignored(tmp_path):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    files, _ = duplo.scan_directory(str(tmp_path))
    cache_file = str(tmp_path / 'c' / 'cache.json')
    cache = duplo.HashCache(cache_file)
    cache.set(files[0], 'deadbeef')
    cache.save()

    reloaded = duplo.HashCache(cache_file)
    stale = files[0].__class__(files[0].path, 11, files[0].mtime_ns,
                               files[0].dev, files[0].ino)

    assert reloaded.get(files[0]) == 'deadbeef'
    assert reloaded.get(stale) is None


def test_hash_cache_poisoned_hash_cannot_authorize_deletion(tmp_path):
    _write(tmp_path / 'data' / 'keep.jpg', b'K' * 64)
    _write(tmp_path / 'data' / 'other.jpg', b'O' * 64)
    files, _ = duplo.scan_directory(str(tmp_path / 'data'))
    cache_file = str(tmp_path / 'cache.json')
    poisoned = duplo.HashCache(cache_file)
    for entry in files:
        poisoned.set(entry, 'same-fake-digest')
    poisoned.save()

    duplicates = duplo.find_duplicates(files, duplo.HashCache(cache_file), [])
    group = next(iter(duplicates.values()))
    plan = duplo.build_plan(duplicates, [group[1].path], [])

    assert duplo.apply_plan(plan) == 1
    assert _left(tmp_path / 'data') == ['keep.jpg', 'other.jpg']


def test_hash_cache_malformed_json_structures_ignored(tmp_path):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    files, _ = duplo.scan_directory(str(tmp_path))
    key = duplo._key(files[0].path)
    bad_payloads = [
        '[1, 2, 3]',
        '{"%s": "not-a-dict"}' % key,
        '{"%s": {"key": "x", "hash": 1}}' % key,
        '{"%s": {"key": [10, %d, %d], "hash": 42}}'
        % (key, files[0].mtime_ns, files[0].ino),
    ]
    for payload in bad_payloads:
        cache_file = tmp_path / 'c.json'
        cache_file.write_text(payload, encoding='utf-8')

        assert duplo.HashCache(str(cache_file)).get(files[0]) is None


def test_hash_cache_old_pickle_file_ignored(tmp_path):
    cache_file = tmp_path / 'hash_cache.pkl'
    cache_file.write_bytes(pickle.dumps({'a': {'hash': 'h'}}))
    _write(tmp_path / 'd' / 'a.jpg', b'X' * 10)
    files, _ = duplo.scan_directory(str(tmp_path / 'd'))

    assert duplo.HashCache(str(cache_file)).get(files[0]) is None


# --- misc -------------------------------------------------------------------

def test_format_size_beyond_terabytes_returns_string():
    assert duplo.format_size(2 ** 60) == '1.00 EB'


def test_safe_text_escapes_newline_and_ansi():
    assert duplo.safe_text('a\nb\x1b[31m') == 'a\\u000ab\\u001b[31m'


def test_get_file_category_uppercase_extension():
    assert duplo.get_file_category('.JPG') == 'images'


# --- end-to-end -------------------------------------------------------------

def test_main_missing_directory_exit_2(tmp_path):
    assert duplo.main([str(tmp_path / 'absent')]) == 2


def test_main_auto_first_identical_dirs_keeps_unique_files(tmp_path,
                                                          monkeypatch):
    _write(tmp_path / 'A' / 'shared.jpg', b'X' * 100)
    _write(tmp_path / 'A' / 'uniqA.jpg', b'a' * 7)
    _write(tmp_path / 'B' / 'shared.jpg', b'X' * 100)
    _write(tmp_path / 'B' / 'uniqB.jpg', b'b' * 9)
    _write(tmp_path / 'B' / 'sub' / 'deep.jpg', b'deep')

    code = _run(tmp_path, '--auto-first', '--find-identical-dirs',
                monkeypatch=monkeypatch)

    assert code == 0
    assert _left(tmp_path) == ['A/shared.jpg', 'A/uniqA.jpg',
                               'B/sub/deep.jpg', 'B/uniqB.jpg']


def test_main_auto_first_identical_leaf_dirs_removed(tmp_path, monkeypatch):
    for name in ('A', 'B'):
        _write(tmp_path / name / 'x.jpg', b'X' * 100)

    code = _run(tmp_path, '--auto-first', '--find-identical-dirs',
                monkeypatch=monkeypatch)

    assert code == 0
    assert _left(tmp_path) == ['A/x.jpg']
    assert not (tmp_path / 'B').exists()


def test_main_conflicting_choice_deletes_nothing(tmp_path, monkeypatch):
    _write(tmp_path / 'A' / 'x.jpg', b'X' * 100)
    _write(tmp_path / 'B' / 'x.jpg', b'X' * 100)

    code = _run(tmp_path, '--interactive', '--find-identical-dirs',
                answers=('a', 'b'), monkeypatch=monkeypatch)

    assert code == 2
    assert _left(tmp_path) == ['A/x.jpg', 'B/x.jpg']


def test_main_closed_stdin_exit_2(tmp_path, monkeypatch):
    _write(tmp_path / 'a.jpg', b'X' * 10)
    _write(tmp_path / 'b.jpg', b'X' * 10)

    def closed(prompt=''):
        raise EOFError

    monkeypatch.setattr('builtins.input', closed)

    assert duplo.main([str(tmp_path), '--no-cache', '--interactive']) == 2
    assert _left(tmp_path) == ['a.jpg', 'b.jpg']


# --- anti-example (do not copy) --------------------------------------------
#
# def test_dedup():
#     os.system('python main.py /home/anton/photos --auto-first')
#     assert True
#
# Real data, no assertion on the outcome, exit code ignored.
