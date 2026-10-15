"""Verify frozen inputs and run the source-release offline regression suite."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'src' / 'tools')]


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    catalogue_path = ROOT / 'study' / 'frozen_catalogue.json'
    expected = json.loads((ROOT / 'study' / 'frozen_catalogue.sha256.json').read_text())['sha256']
    actual = hashlib.sha256(catalogue_path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError('Frozen catalogue SHA-256 mismatch')
    catalogue = json.loads(catalogue_path.read_text())
    if (catalogue['robustness_runs_requested'], catalogue['validation_runs_requested']) != (240, 28):
        raise RuntimeError('Unexpected frozen study denominators')
    print(f'Frozen catalogue verified: {actual}', flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'src' / 'tests'))
    excluded = 'test_town10_progress_dashboard.TestTown10ProgressDashboard.test_actual_catalogue_counts'
    selected = unittest.TestSuite()
    for test in cases(suite):
        if test.id() == excluded:
            print(f'Excluded evidence-dependent check: {excluded}', flush=True)
        else:
            selected.addTest(test)
    return 0 if unittest.TextTestRunner(verbosity=1).run(selected).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
