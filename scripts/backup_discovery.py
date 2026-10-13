"""Bounded inspection of historical backup names and archive metadata; never extract."""
import re
import tarfile
import zipfile
from pathlib import PurePosixPath

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 10000


def backup_name(name):
    return bool(re.search(r'(?:^|[._-])backups?(?:$|[._-])', name, re.I))


def partial_name(name):
    return bool(re.fullmatch(r'SKILL\.md\.(?:bak|backup|old)(?:[._-].+)?', name, re.I))


def inspect_archive(path):
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('压缩包超过检查上限，未完成检查')
    found = False
    total = 0

    def check(name, size, regular):
        nonlocal found, total
        total += size
        if total > MAX_EXPANDED_BYTES:
            raise ValueError('压缩包展开大小超过检查上限')
        parts = PurePosixPath(name.replace('\\', '/'))
        if parts.is_absolute() or '..' in parts.parts:
            raise ValueError('压缩包包含不安全路径')
        if regular and parts.name == 'SKILL.md':
            found = True

    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_MEMBERS:
                raise ValueError('压缩包条目数超过检查上限')
            for item in entries:
                if item.flag_bits & 1:
                    raise ValueError('加密压缩包无法检查')
                check(item.filename, item.file_size, not item.is_dir() and (item.external_attr >> 16) & 0o170000 != 0o120000)
    else:
        with tarfile.open(path, 'r|*') as archive:
            for index, item in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise ValueError('压缩包条目数超过检查上限')
                check(item.name, item.size, item.isfile())
    return found
