"""R004-15: the local endpoint ceiling is one number, and the kernel agrees with it.

The defect this pins was not that the guard measured the wrong string -- an earlier
pass fixed that -- but that the *usable* ceiling moved with the width of the process
identifier. A server binds a staging name and renames it into place, and that name
carried a bare `os.getpid()`, so it ran 11 bytes longer than the endpoint for a
one-digit pid and 17 longer for a seven-digit one. A 90-byte workspace bound on one
launch and refused on the next. R004-15 fixes the staging suffix at a constant width
and publishes what is left -- 86 encoded bytes -- as the hard maximum.

Every length here is *derived*: from the constants for the contract, and from a real
`bind()` for the kernel. Nothing restates 86 except the one test whose whole job is
to state the published number, and nothing restates the platform's `sun_path` size at
all -- it is measured, so these assertions are correct on Linux (108) as well as on
macOS and BSD (104).

This module sits in `tests/phase2` deliberately: that tree is already collected by
`phase2-platform.yml`'s `[ubuntu, macos, windows]` matrix, so the macOS real-bind
evidence R004-15 requires arrives with no workflow change.
"""

from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    MAX_ENDPOINT_PATH_BYTES,
    MAX_SOCKET_PATH_BYTES,
    EndpointScheme,
    TransportError,
    _staging_socket_path,
    assert_socket_path_fits,
)

#: A Unix-socket cap is meaningless where the local endpoint is a named pipe, whose
#: name is derived and bounded by construction. Skipped rather than adapted: there is
#: no pipe-side equivalent of this contract to assert.
pytestmark = pytest.mark.skipif(
    LOCAL_SCHEME is not EndpointScheme.UNIX,
    reason="the `sun_path` ceiling is a Unix-socket contract; this platform serves a "
    "named pipe",
)


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A directory short enough that an 86-byte path under it can be built.

    pytest's `tmp_path` is far past the ceiling on its own, which is the point of the
    ceiling; a short `mkdtemp` under the system temp root is not.
    """
    directory = Path(tempfile.mkdtemp(prefix="ovc-", dir=tempfile.gettempdir()))
    try:
        yield directory
    finally:
        for leftover in directory.iterdir():
            leftover.unlink()
        directory.rmdir()


def _path_of_length(directory: Path, length: int) -> Path:
    """A socket path under `directory` that encodes to exactly `length` bytes."""
    fill = length - len(str(directory).encode("utf-8")) - len("/.sock")
    assert fill >= 1, f"{directory} is too deep to build a {length}-byte path under"
    path = directory / f"{'s' * fill}.sock"
    assert len(str(path).encode("utf-8")) == length
    return path


def _kernel_binds(path: Path) -> bool:
    """Whether this kernel accepts `path` in `sockaddr_un.sun_path`.

    The oracle. Not a restatement of the constant under test: a real `bind(2)`.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(str(path))
    except OSError:
        return False
    else:
        path.unlink()
        return True
    finally:
        probe.close()


def _sun_path_field_bytes(directory: Path) -> int:
    """This kernel's `sun_path` size, measured by binding rather than assumed.

    The first length the kernel refuses *is* the field size: a path of `n` bytes needs
    `n + 1` with its terminator, so refusing `n` means `n >= size`, and having
    accepted `n - 1` means `n <= size`.
    """
    length = len(str(directory).encode("utf-8")) + len("/x.sock")
    while _kernel_binds(_path_of_length(directory, length)):
        length += 1
        assert length < 4096, "this kernel accepts an implausible `sun_path`"
    return length


def test_the_published_ceiling_is_eighty_six_encoded_bytes() -> None:
    """The one number R004-15 publishes, stated once so a change to it is visible.

    This and `test_a_real_bind_agrees_with_the_ceiling_at_both_boundaries` are easy to
    mistake for each other and prove different things. This one pins *the number*; the
    real bind pins *exactness at whatever number is published*. Narrowing
    `_STAGING_PID_DIGITS` from 7 to 5 is the case that separates them: the ceiling
    becomes 88, still perfectly deterministic, so the kernel oracle stays green and
    only this test and the CLI drift test fail. A ceiling that moved because someone
    retuned the staging width is exactly the class of change R004-15 exists to make
    visible, so it needs a test that objects to the number itself.
    """
    assert MAX_ENDPOINT_PATH_BYTES == 86


def test_the_ceiling_is_exactly_what_the_field_leaves_after_the_staging_name(
    socket_dir: Path,
) -> None:
    """The accepted maximum's staging name fills `sun_path` to the last byte.

    This is what makes 86 the *right* number rather than a chosen one, and it fails
    if the ceiling and the staging suffix are ever changed independently of each
    other.
    """
    staging = _staging_socket_path(_path_of_length(socket_dir, MAX_ENDPOINT_PATH_BYTES))
    assert len(str(staging).encode("utf-8")) + 1 == MAX_SOCKET_PATH_BYTES


@pytest.mark.parametrize("pid", [1, 42, 999, 99_999, 4_194_304, 10**7, 10**9 + 7])
def test_the_staging_name_width_does_not_follow_the_pid(
    monkeypatch: pytest.MonkeyPatch, socket_dir: Path, pid: int
) -> None:
    """The defect itself: a staging name whose width moved with the process id.

    The last two values are past any `pid_max` a kernel will issue and are here to
    prove the width is *folded*, not merely usually short.
    """
    monkeypatch.setattr(os, "getpid", lambda: pid)
    endpoint = _path_of_length(socket_dir, MAX_ENDPOINT_PATH_BYTES)
    staging = _staging_socket_path(endpoint)
    assert len(str(staging).encode("utf-8")) + 1 == MAX_SOCKET_PATH_BYTES


def test_the_maximum_accepted_path_is_accepted(socket_dir: Path) -> None:
    assert_socket_path_fits(_path_of_length(socket_dir, MAX_ENDPOINT_PATH_BYTES))


def test_the_first_rejected_path_is_one_byte_over_the_maximum(socket_dir: Path) -> None:
    with pytest.raises(TransportError, match="endpoint path is too long"):
        assert_socket_path_fits(
            _path_of_length(socket_dir, MAX_ENDPOINT_PATH_BYTES + 1)
        )


def test_the_ceiling_is_measured_in_encoded_bytes_not_characters(
    socket_dir: Path,
) -> None:
    """A name inside the ceiling in characters and past it in bytes is refused.

    `sun_path` holds bytes. A guard counting characters would accept this path and
    then watch the bind fail, which is the same class of defect as the pid width:
    correct on an ASCII home directory, wrong on the user's real one.
    """
    room = MAX_ENDPOINT_PATH_BYTES - len(str(socket_dir).encode("utf-8")) - len("/.sock")
    # Every "é" is one character and two bytes, so this is `room` characters of name
    # -- inside the ceiling by character count -- and `room + 1` bytes over it.
    path = socket_dir / f"{'é' * ((room + 1) // 2)}{'s' * ((room + 1) % 2)}.sock"
    assert len(str(path)) <= MAX_ENDPOINT_PATH_BYTES
    assert len(str(path).encode("utf-8")) > MAX_ENDPOINT_PATH_BYTES
    with pytest.raises(TransportError, match="endpoint path is too long"):
        assert_socket_path_fits(path)


def test_a_real_bind_agrees_with_the_ceiling_at_both_boundaries(
    socket_dir: Path,
) -> None:
    """The operating system, not a length function, on the two paths that matter.

    The strings bound here are the *staging* names, because those are what a server
    hands the kernel. On macOS and BSD the accepted maximum's staging name is the
    longest one `sun_path` can hold and the first rejected one's is one byte too
    long, so the ceiling is proved tight from both sides. On Linux the field is 108
    and the ceiling is deliberately the macOS floor, so the far side has headroom
    instead -- asserted rather than skipped, because "it binds on Linux too" is the
    portability claim the floor exists to make.
    """
    field = _sun_path_field_bytes(socket_dir)
    accepted = _staging_socket_path(
        _path_of_length(socket_dir, MAX_ENDPOINT_PATH_BYTES)
    )
    over = _staging_socket_path(
        _path_of_length(socket_dir, MAX_ENDPOINT_PATH_BYTES + 1)
    )

    assert _kernel_binds(accepted), "the accepted maximum does not bind"
    if field == MAX_SOCKET_PATH_BYTES:
        assert not _kernel_binds(over), "the ceiling is below what the kernel refuses"
    else:
        assert field > MAX_SOCKET_PATH_BYTES, field
        assert _kernel_binds(over), "the roomier platform lost its headroom"


def test_the_cli_restates_the_runtime_ceiling_exactly() -> None:
    """The CLI's copy of the number, checked from the side allowed to see both.

    `omnivia-core-cli` may not import `omnivia_core_runtime` -- ADR-036, enforced by
    `scripts/check-package-boundaries.py` -- so it restates the ceiling to refuse an
    over-long `--home` with the reason instead of an opaque failure from the process
    it spawned. A restated constant is only safe while something fails when the two
    drift, and this is that something. It lives here because the boundary applies to
    package *source*, not to tests, so the runtime's suite can import the CLI while
    the CLI's cannot import the runtime.

    Drift is not hypothetical: the CLI's copy read 104 and was checked against the
    endpoint, which made it 11 to 17 bytes looser than the runtime's and admitted
    exactly the paths that then failed to bind.
    """
    from omnivia_core_cli.lifecycle import MAX_ENDPOINT_PATH_BYTES as cli_ceiling

    assert cli_ceiling == MAX_ENDPOINT_PATH_BYTES
