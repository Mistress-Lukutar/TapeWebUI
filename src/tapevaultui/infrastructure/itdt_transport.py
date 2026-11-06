"""
File:   itdt_transport.py
Brief:  Async subprocess wrapper around the IBM ITDT-SE CLI.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import structlog

from tapevaultui.infrastructure.itdt_parse import is_open_failure

logger = structlog.get_logger(__name__)


class ItDtTransportError(RuntimeError):
    """Raised when an ITDT command cannot be executed at all."""


class ItDtTimeoutError(ItDtTransportError):
    """Raised when an ITDT command exceeds its time budget."""


@dataclass(frozen=True)
class ItDtResult:
    """Result of one completed ITDT command."""

    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


class ItDtTransport:
    """The only module that knows how to invoke ITDT-SE.

    Every command is built as
    ``itdt -force-generic-dd -f <device> <args...>``; ``-force-generic-dd``
    is mandatory on this system (see .agents/AGENTS.md hardware facts).
    """

    def __init__(
        self,
        itdt_exe: Path,
        device_name: str,
        *,
        force_generic_dd: bool = True,
        default_timeout_s: int = 600,
        open_retry_s: float = 2.0,
        open_retries: int = 3,
    ) -> None:
        """Initialize the transport.

        Args:
            itdt_exe: Path to itdt.exe.
            device_name: Device special file, e.g. ``\\\\.\\tape0``.
            force_generic_dd: Prepend the ``-force-generic-dd`` flag.
            default_timeout_s: Default per-command timeout in seconds.
            open_retry_s: Delay before retrying after an open failure.
            open_retries: Number of retries on transient open failures.
        """
        self._itdt_exe = itdt_exe
        self._device_name = device_name
        self._force_generic_dd = force_generic_dd
        self._default_timeout_s = default_timeout_s
        self._open_retry_s = open_retry_s
        self._open_retries = open_retries

    def build_args(self, args: list[str] | tuple[str, ...]) -> list[str]:
        """Build the full ITDT command line for a subcommand.

        Args:
            args: ITDT subcommand and its arguments.

        Returns:
            Complete argv list for subprocess execution.
        """
        command = [str(self._itdt_exe)]
        if self._force_generic_dd:
            command.append("-force-generic-dd")
        command.extend(["-f", self._device_name])
        command.extend(args)
        return command

    async def run(self, args: list[str], timeout_s: int | None = None) -> ItDtResult:
        """Run one ITDT command to completion.

        Transient open failures ("Open failed:") are retried up to
        ``open_retries`` times — they were observed on this machine even
        though the device is healthy.

        Args:
            args: ITDT subcommand and its arguments.
            timeout_s: Timeout override; defaults to ``default_timeout_s``.

        Returns:
            ItDtResult with exit code, output and duration.

        Raises:
            ItDtTimeoutError: If the command exceeded the timeout.
            ItDtTransportError: If the executable is missing or the device
                could not be opened after all retries.
        """
        effective_timeout = (
            timeout_s if timeout_s is not None else self._default_timeout_s
        )
        argv = self.build_args(args)
        attempts = self._open_retries + 1

        for attempt in range(1, attempts + 1):
            result = await self._run_once(argv, effective_timeout)
            if result.exit_code != 0 and is_open_failure(result.stdout):
                if attempt < attempts:
                    logger.warning(
                        "itdt_open_failed_retry",
                        args=args,
                        attempt=attempt,
                        retry_in_s=self._open_retry_s,
                    )
                    await asyncio.sleep(self._open_retry_s)
                    continue
                raise ItDtTransportError(
                    f"ITDT could not open {self._device_name} "
                    f"after {attempts} attempts: {result.stdout.strip()}"
                )
            return result

        raise ItDtTransportError(  # pragma: no cover - loop always returns/raises
            "unreachable"
        )

    async def _run_once(self, argv: list[str], timeout_s: float) -> ItDtResult:
        """Execute one subprocess invocation.

        Args:
            argv: Full command line.
            timeout_s: Time budget in seconds.

        Returns:
            ItDtResult for the invocation.

        Raises:
            ItDtTimeoutError: On timeout (the process is killed).
            ItDtTransportError: If the executable cannot be started.
        """
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ItDtTransportError(
                f"ITDT executable not found: {self._itdt_exe}"
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_s
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ItDtTimeoutError(
                f"ITDT command timed out after {timeout_s}s: {argv[3:]}",
            ) from None

        duration = time.monotonic() - started
        result = ItDtResult(
            args=tuple(argv),
            exit_code=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_s=duration,
        )
        logger.debug(
            "itdt_command_done",
            args=tuple(argv[3:]),
            exit_code=result.exit_code,
            duration_s=round(duration, 3),
        )
        return result

    async def stream(self, args: list[str]) -> AsyncIterator[str]:
        """Stream stdout lines of a long ITDT command as they arrive.

        Used later for write/read/erase progress; each yielded line is the
        raw stdout line without the trailing newline.

        Args:
            args: ITDT subcommand and its arguments.

        Yields:
            Non-empty stdout lines.

        Raises:
            ItDtTransportError: If the executable cannot be started.
        """
        argv = self.build_args(args)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise ItDtTransportError(
                f"ITDT executable not found: {self._itdt_exe}"
            ) from exc

        try:
            stdout = process.stdout
            if stdout is None:  # pragma: no cover - PIPE is always requested
                raise ItDtTransportError("subprocess stdout unavailable")
            while True:
                raw = await stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.strip():
                    yield line
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class FakeItDtTransport:
    """Hardware-free stand-in for ItDtTransport.

    Scenario table maps the first subcommand word to a (stdout, exit_code)
    response; every call is recorded in ``commands`` for assertions.
    Unknown commands fall back to ``default_response``. For ``read -d``
    commands the scenario stdout is also written to the destination file,
    mimicking ITDT's behavior for downstream code that reads the file.
    """

    def __init__(
        self,
        scenarios: dict[str, tuple[str, int]] | None = None,
        *,
        command_scenarios: dict[tuple[str, ...], tuple[str, int]] | None = None,
        default_response: tuple[str, int] = ("", 0),
    ) -> None:
        """Initialize the fake.

        Args:
            scenarios: Mapping from subcommand word to (stdout, exit_code).
            command_scenarios: Exact full-command matches (checked first),
                e.g. ``("write", "-raw", "-s", "D:/a.txt")``.
            default_response: Response for subcommands not in scenarios.
        """
        self.scenarios = scenarios if scenarios is not None else {}
        self.command_scenarios = (
            command_scenarios if command_scenarios is not None else {}
        )
        self.default_response = default_response
        self.commands: list[tuple[str, ...]] = []
        self.device_name = "\\\\.\\tape0"

    def build_args(self, args: list[str] | tuple[str, ...]) -> list[str]:
        """Build the argv the real transport would use.

        Args:
            args: ITDT subcommand and its arguments.

        Returns:
            Complete argv list (synthetic executable path).
        """
        return ["itdt", "-force-generic-dd", "-f", self.device_name, *args]

    async def run(self, args: list[str], timeout_s: int | None = None) -> ItDtResult:
        """Record the call and return the scenario response.

        For ``read -d <path>`` the scenario stdout is written to <path>.

        Args:
            args: ITDT subcommand and its arguments.
            timeout_s: Ignored (accepted for interface compatibility).

        Returns:
            ItDtResult built from the scenario table.
        """
        self.commands.append(tuple(args))
        key = tuple(args)
        if key in self.command_scenarios:
            stdout, exit_code = self.command_scenarios[key]
        else:
            stdout, exit_code = self.scenarios.get(args[0], self.default_response)
        if args[0] == "read" and "-d" in args:
            destination = Path(args[args.index("-d") + 1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(stdout.encode("utf-8"))
        return ItDtResult(
            args=tuple(self.build_args(args)),
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            duration_s=0.0,
        )

    async def stream(self, args: list[str]) -> AsyncIterator[str]:
        """Yield the scenario response as one line (interface compatibility).

        Args:
            args: ITDT subcommand and its arguments.

        Yields:
            The scenario stdout split into non-empty lines.
        """
        self.commands.append(tuple(args))
        stdout, _ = self.scenarios.get(args[0], self.default_response)
        for line in stdout.splitlines():
            if line.strip():
                yield line
