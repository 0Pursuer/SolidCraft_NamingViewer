"""Discover current-user SolidCraft channels and retain one launch's identity.

Discovery is a filesystem operation only. Callers must authenticate a candidate
before offering it as a live instance. Credentials never belong in labels or URLs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import secrets
import stat


MAX_TOKEN_BYTES = 256
MAX_DIRECTORY_ENTRIES = 256
MAX_PID_BYTES = 128
TOKEN_PATTERN = re.compile(r'[0-9a-fA-F]{32,128}')
PORT_FILE_PATTERN = re.compile(r'([1-9][0-9]{0,4})\.token')


class InvalidBinding(RuntimeError):
    """A discovery record cannot safely identify a SolidCraft launch."""


class SelectedInstanceChanged(RuntimeError):
    """The selected launch's discovery records no longer match its identity."""


class DiscoveryLimitError(RuntimeError):
    """Bound discovery work instead of silently omitting possible choices."""


@dataclass(frozen=True)
class InstanceBinding:
    control_dir: Path
    port: int
    token: str = field(repr=False)
    pid: int | None = None
    fingerprint: tuple[int, int, int, int] | None = field(default=None, repr=False)


def default_control_dir() -> Path:
    override = os.environ.get('SOLIDCRAFT_CONTROL_DIR')
    if override:
        return Path(override)
    local_value = os.environ.get('LOCALAPPDATA')
    local = Path(local_value) if local_value else Path.home() / 'AppData' / 'Local'
    return local / 'SolidCraft' / 'control'


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _read_token(path: Path):
    """Read at most 257 bytes and reject a replaced or concurrently changed file."""
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_TOKEN_BYTES:
        raise InvalidBinding('SolidCraft 的控制凭据记录无效。')
    identity = _fingerprint(before)
    with path.open('rb') as source:
        opened = _fingerprint(os.fstat(source.fileno()))
        # Windows Python can report creation time differently through stat and
        # fstat. Compare file identity and modification time across APIs, then
        # all four fields within each API before/after the bounded read.
        if opened[:3] != identity[:3]:
            raise InvalidBinding('SolidCraft 的控制凭据正在变化，请重新选择实例。')
        raw = source.read(MAX_TOKEN_BYTES + 1)
        if _fingerprint(os.fstat(source.fileno())) != opened:
            raise InvalidBinding('SolidCraft 的控制凭据正在变化，请重新选择实例。')
    if _fingerprint(path.stat(follow_symlinks=False)) != identity:
        raise InvalidBinding('SolidCraft 的控制凭据正在变化，请重新选择实例。')
    try:
        token = raw.decode('ascii').strip()
    except UnicodeDecodeError:
        raise InvalidBinding('SolidCraft 的控制凭据记录无效。') from None
    if len(raw) > MAX_TOKEN_BYTES or not TOKEN_PATTERN.fullmatch(token):
        raise InvalidBinding('SolidCraft 的控制凭据记录无效。')
    return token, identity


def _read_pid(path: Path) -> int | None:
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_PID_BYTES + 1)
        text = raw.decode('ascii').strip()
        if len(raw) <= MAX_PID_BYTES and re.fullmatch(r'[1-9][0-9]{0,9}', text):
            pid = int(text)
            if pid <= 0xFFFFFFFF:
                return pid
    except (OSError, UnicodeDecodeError):
        pass
    return None


def read_binding(control_dir: Path, port: int) -> InstanceBinding:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('SolidCraft 控制端口必须是 1 到 65535 的整数。')
    directory = Path(control_dir).resolve()
    token, fingerprint = _read_token(directory / f'{port}.token')
    pid = _read_pid(directory / f'{port}.pid')
    # PID and token are separate files: a launch in between the reads must not
    # leave a hybrid identity which looks stable to the caller.
    if _fingerprint((directory / f'{port}.token').stat(follow_symlinks=False)) != fingerprint:
        raise InvalidBinding('SolidCraft 的控制凭据正在变化，请重新选择实例。')
    return InstanceBinding(directory, port, token, pid, fingerprint)


def verify_binding(binding: InstanceBinding) -> None:
    try:
        observed = read_binding(binding.control_dir, binding.port)
        unchanged = secrets.compare_digest(observed.token, binding.token)
        unchanged = unchanged and (binding.pid is None or observed.pid == binding.pid)
        unchanged = unchanged and (binding.fingerprint is None or observed.fingerprint == binding.fingerprint)
    except (OSError, ValueError, InvalidBinding):
        unchanged = False
    if not unchanged:
        raise SelectedInstanceChanged(
            '所选 SolidCraft 实例已退出或身份记录已变化；保持原绑定，'
            '请重新启动查看器并选择实例。')


def discover_bindings(control_dir: Path | None = None) -> list[InstanceBinding]:
    directory = Path(control_dir) if control_dir is not None else default_control_dir()
    found = []
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DIRECTORY_ENTRIES:
                    raise DiscoveryLimitError(
                        'SolidCraft 控制目录记录过多，请指定 --control-port 选择实例。')
                match = PORT_FILE_PATTERN.fullmatch(entry.name)
                if not match or not 1 <= int(match[1]) <= 65535:
                    continue
                try:
                    found.append(read_binding(directory, int(match[1])))
                except (OSError, ValueError, InvalidBinding):
                    continue
    except FileNotFoundError:
        return []
    return sorted(found, key=lambda binding: binding.port)
