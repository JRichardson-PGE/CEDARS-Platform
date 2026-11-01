"""The local worker learns its ReCET release from the engine's own command line (UAT 2 U01). ReCET's `recet worker` subcommand takes no
--version (only `recet --version` does), so asking `recet worker --version` always failed: every worker reported an unknown release, took
runs of every release and the engine refused the ones it was not, after their wait."""

import shutil
import sys
import textwrap

import pytest

from cedars.modules.calculations import dispatch

# A stand-in for ReCET's command line with its real argument rules: --version on the program, not on the worker subcommand.
FAKE_CLI = textwrap.dedent(
    """
    import argparse
    p = argparse.ArgumentParser(prog="recet")
    p.add_argument("--version", action="version", version="recet 0.1.89")
    sub = p.add_subparsers(dest="cmd")
    w = sub.add_parser("worker")
    w.add_argument("action", choices=["run", "validate", "schema"])
    p.parse_args()
    """
)


@pytest.fixture
def cli(tmp_path):
    script = tmp_path / "recet_cli.py"
    script.write_text(FAKE_CLI)
    return script


def test_the_release_is_read_from_the_program_not_the_worker_subcommand(settings, cli):
    settings.CALCULATIONS_WORKER_COMMAND = [sys.executable, str(cli), "worker"]
    assert dispatch.version_command() == [sys.executable, str(cli), "--version"]
    assert dispatch.worker_release() == "0.1.89"


def test_a_string_setting_works_the_same(settings, cli):
    settings.CALCULATIONS_WORKER_COMMAND = f"{sys.executable} {cli} worker"
    assert dispatch.worker_release() == "0.1.89"


def test_a_command_that_is_the_worker_itself_is_asked_as_it_is(settings, cli):
    settings.CALCULATIONS_WORKER_COMMAND = [sys.executable, str(cli)]
    assert dispatch.version_command() == [sys.executable, str(cli), "--version"]
    assert dispatch.worker_release() == "0.1.89"


@pytest.mark.skipif(shutil.which("recet") is None, reason="the pinned engine is not installed")
def test_the_default_setting_reads_the_installed_engines_release(settings):
    from importlib.metadata import version

    settings.CALCULATIONS_WORKER_COMMAND = "recet worker"
    assert dispatch.worker_release() == version("recet")
