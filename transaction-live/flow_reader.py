"""Open a read-only flow view without interrupting an existing recorder."""
import argparse
from pathlib import Path
import sys

from server import ROOT, main, safe_error


def latest_archive(records):
    records = Path(records).resolve()
    if not records.is_dir():
        raise RuntimeError('还没有观察档案，请先启动实时记录器。')
    candidates = []
    for index, child in enumerate(records.iterdir()):
        if index >= 1000:
            raise RuntimeError('档案目录超过1000项，请用 --archive 明确选择。')
        if child.is_symlink() or not child.is_dir() or child.resolve().parent != records:
            continue
        database = child / 'archive.sqlite3'
        if database.is_file() and not database.is_symlink():
            updated = database.stat().st_mtime_ns
            # A running recorder writes commits to WAL before SQLite checkpoints
            # the main database. SHM reflects reader activity, so do not use it.
            wal = child / 'archive.sqlite3-wal'
            try:
                if wal.is_file() and not wal.is_symlink():
                    updated = max(updated, wal.stat().st_mtime_ns)
            except FileNotFoundError:
                pass  # A concurrent checkpoint may remove the WAL normally.
            candidates.append((updated, child))
    if not candidates:
        raise RuntimeError('没有找到有效观察档案，请先启动实时记录器。')
    return max(candidates, key=lambda pair: pair[0])[1]


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='默认读取 records 中最近更新的档案')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args(argv)
    archive = args.archive or latest_archive(ROOT / 'records')
    print('只读流程页正在查看：' + str(archive), flush=True)
    options = ['--read-only', '--archive', str(archive), '--port', str(args.port), '--recompute-lab']
    if args.open:
        options.append('--open')
    return main(options)


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, ValueError) as exc:
        print(safe_error(exc), file=sys.stderr)
        raise SystemExit(1)
