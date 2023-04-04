from __future__ import annotations

import argparse
import contextlib
import json
import math
import os.path
import random
import shlex
import subprocess
import sys
import tempfile
from typing import Sequence

import pytest

TESTIDS_INPUT_OPTION = '--dtp-testids-input-file'
TESTIDS_OUTPUT_OPTION = '--dtp-testids-output-file'
RESULTS_OUTPUT_OPTION = '--dtp-results-output-file'
PYTEST_OPTIONS = (
    '-p', __name__,
    # disable known test-randomization plugins
    '-p', 'no:randomly',
    # we don't read the output at all
    '--quiet', '--quiet',
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(TESTIDS_INPUT_OPTION)
    parser.addoption(TESTIDS_OUTPUT_OPTION)
    parser.addoption(RESULTS_OUTPUT_OPTION)


def pytest_collection_modifyitems(
        config: pytest.Config,
        items: list[pytest.Item],
) -> None:
    read_option = config.getoption(TESTIDS_INPUT_OPTION)
    write_option = config.getoption(TESTIDS_OUTPUT_OPTION)
    # if read_option is not None:
    #     by_id = {item.nodeid: item for item in items}
    #     testids = _parse_testids_file(read_option)
    #     print("testids", testids)
    #     items[:] = [by_id[testid] for testid in testids]
    if write_option is not None:
        with open(write_option, 'w', encoding='UTF-8') as f:
            for item in items:
                f.write(f'{item.nodeid}\n')


class CollectResults:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.results: dict[str, bool] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == 'call':
            self.results[report.nodeid] = report.outcome == 'passed' or report.outcome == 'skipped'

    def pytest_terminal_summary(self, config: pytest.Config) -> None:
        with open(self.filename, 'w') as f:
            f.write(json.dumps(self.results, indent=2))

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        config.pluginmanager.unregister(self)


def pytest_configure(config: pytest.Config) -> None:
    results_filename = config.getoption(RESULTS_OUTPUT_OPTION)
    if results_filename is not None:
        config.pluginmanager.register(CollectResults(results_filename))


def _run_pytest(*args: str) -> None:
    # XXX: this is potentially difficult to debug? maybe --verbose?
    # import code; code.interact(local=locals())
    # import code; code.interact(local=locals())
    # print(" ".join([sys.executable, '-mpytest', *PYTEST_OPTIONS, *args]))
    subprocess.check_call(
        (sys.executable, '-mpytest', *PYTEST_OPTIONS, *args),
    )


def _parse_filenames(filename: str) -> list[str]:
    with open(filename) as f:
        return list(set([line.split(":")[0] for line in f.read().splitlines() if line]))

def _parse_testids_file(filename: str) -> list[str]:
    with open(filename) as f:
        return [line for line in f.read().splitlines() if line]


def _discover_tests(path: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        testids_filename = os.path.join(tmpdir, 'testids.txt')
        _run_pytest(
            path,
            # use `=` to avoid pytest's basedir detection
            f'{TESTIDS_OUTPUT_OPTION}={testids_filename}',
            '--collect-only',
        )
        test_ids, file_names = (_parse_testids_file(testids_filename), _parse_filenames(testids_filename))
        return test_ids, file_names 
        # return 


def _common_testpath(testids: list[str]) -> str:
    paths = [testid.split('::')[0] for testid in testids]
    if not paths:
        return '.'
    else:
        return os.path.commonpath(paths) or '.'


def _passed_with_testlist(path: str, test: str, testids: list[str]) -> bool:
    with tempfile.TemporaryDirectory() as tmpdir:
        testids_filename = os.path.join(tmpdir, 'testids.txt')
        with open(testids_filename, 'w') as f:
            for testid in testids:
                f.write(f'{testid}\n')
            f.write(f'{test}\n')

        results_json = os.path.join(tmpdir, 'results.json')

        testids.append(test)
        with contextlib.suppress(subprocess.CalledProcessError):

            _run_pytest(
                # use `=` to avoid pytest's basedir detection
                *testids,
                f'{RESULTS_OUTPUT_OPTION}={results_json}',
            )

        with open(results_json) as f:
            contents = json.load(f)

        result = True
        for key, value in contents.items():
            if key.startswith(test):
                result = value and result
        return result

def _format_cmd(
        victim: str,
        cmd_tests: str | None,
        cmd_testids_filename: str | None,
) -> str:
    args = ['detect-test-pollution', '--failing-test', victim]
    if cmd_tests is not None:
        args.extend(('--tests', cmd_tests))
    elif cmd_testids_filename is not None:
        args.extend(('--testids-filename', cmd_testids_filename))
    else:
        raise AssertionError('unreachable?')
    return ' '.join(shlex.quote(part) for part in args)


def _fuzz(
        testpath: str,
        testids: list[str],
        cmd_tests: str | None,
        cmd_testids_filename: str | None,
) -> int:
    # make shuffling "deterministic"
    r = random.Random()
    r.seed(1542676187, version=2)

    with tempfile.TemporaryDirectory() as tmpdir:
        testids_filename = os.path.join(tmpdir, 'testids.txt')
        results_json = os.path.join(tmpdir, 'results.json')

        i = 0
        while True:
            i += 1
            print(f'run {i}...')

            r.shuffle(testids)
            with open(testids_filename, 'w') as f:
                for testid in testids:
                    f.write(f'{testid}\n')

            try:
                _run_pytest(
                    testpath,
                    '--maxfail=1',
                    # use `=` to avoid pytest's basedir detection
                    f'{TESTIDS_INPUT_OPTION}={testids_filename}',
                    f'{RESULTS_OUTPUT_OPTION}={results_json}',
                )
            except subprocess.CalledProcessError:
                print('-> found failing test!')
            else:
                print('-> OK!')
                continue

            with open(results_json) as f:
                contents = json.load(f)

            testids = list(contents)
            victim = testids[-1]

            cmd = _format_cmd(victim, cmd_tests, cmd_testids_filename)
            print(f'try `{cmd}`!')
            return 1


def _bisect(testpath: str, failing_test: str, testids: list[str], test_files) -> int:
    failing_test = failing_test.split(":")[0]
    if failing_test not in test_files:
        print('-> failing test was not part of discovered tests!')
        return 1

    # step 2: make sure the failing test passes on its own

    print('ensuring test passes by itself...')
    if _passed_with_testlist(testpath, failing_test, []):
        print('-> OK!')
    else:
        print('-> test failed! (output printed above)')
        return 1

    # we'll be bisecting testids
    test_files.remove(failing_test)

    # step 3: ensure test fails
    print('ensuring test fails with test group...')
    if _passed_with_testlist(testpath, failing_test, test_files):
        print('-> expected failure -- but it passed?')
        return 1
    else:
        print('-> OK!')

    # step 4: bisect time!
    n = 0
    while len(test_files) != 1:
        n += 1
        print(f'running step {n}:')
        n_left = len(test_files)
        steps_s = f'(about {math.ceil(math.log(n_left, 2))} steps)'
        print(f'- {n_left} tests remaining {steps_s}')

        pivot = len(test_files) // 2
        part1 = test_files[:pivot]
        part2 = test_files[pivot:]

        import code; code.interact(local=locals())
        if _passed_with_testlist(testpath, failing_test, part1):
            test_files = part2
        else:
            test_files = part1

    # step 5: make sure it still fails
    print('double checking we found it...')
    if _passed_with_testlist(testpath, failing_test, test_files):
        raise AssertionError('unreachable? unexpected pass? report a bug?')
    else:
        print(f'-> the polluting test is: {test_files[0]}')
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    print("HERE")
    parser = argparse.ArgumentParser()

    mutex1 = parser.add_mutually_exclusive_group(required=True)
    mutex1.add_argument(
        '--fuzz',
        action='store_true',
        help='repeatedly shuffle the test suite searching for failures',
    )
    mutex1.add_argument(
        '--failing-test',
        help=(
            'the identifier of the failing test, '
            'for example `tests/my_test.py::test_name_here`'
        ),
    )

    mutex2 = parser.add_mutually_exclusive_group(required=True)
    mutex2.add_argument(
        '--tests',
        help='where tests will be discovered from, often `--tests=tests/',
    )
    mutex2.add_argument(
        '--testids-file',
        help='optional pre-discovered test ids (one per line)',
    )
    args = parser.parse_args(argv)

    # step 1: discover all the tests
    print('discovering all tests...')
    if args.testids_file:
        testids = _parse_testids_file(args.testids_file)
        print(f'-> pre-discovered {len(testids)} tests!')
    else:
        testids, test_files = _discover_tests(args.tests)
        print(f'-> discovered {len(testids)} tests!')

    testpath = _common_testpath(testids)

    if args.fuzz:
        return _fuzz(testpath, testids, args.tests, args.testids_file)
    else:
        return _bisect(testpath, args.failing_test, testids, test_files)


if __name__ == '__main__':
    raise SystemExit(main())
