"""A delivery cannot certify changed sealed evidence or an unpassed gate."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tools.town10_continuation_delivery import deliver


class DeliveryTests(unittest.TestCase):
    def fixture(self,root):
        reports=root/'Reports';reports.mkdir()
        def save(path,value):path.write_text(json.dumps(value))
        save(root/'frozen_catalogue.json',{'tests':[]})
        digest=hashlib.sha256((root/'frozen_catalogue.json').read_bytes()).hexdigest()
        gate={'passed':True,'catalogue_sha256':digest,'completed_runs':28,'accepted_mode_pairs':14}
        save(reports/'validation_gate.json',gate)
        save(reports/'validation_package_gate.json',{'passed':True,'catalogue_sha256':digest,'evidence_sha256':{}})
        save(reports/'configuration_comparisons.json',{'completed_runs':28,'accepted_pairs':14})
        return save

    def test_changed_sealed_evidence_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);save=self.fixture(root)
            evidence=root/'recorded.json';evidence.write_text('original')
            seal=json.loads((root/'Reports/validation_package_gate.json').read_text())
            seal['evidence_sha256']={'recorded.json':hashlib.sha256(evidence.read_bytes()).hexdigest()}
            save(root/'Reports/validation_package_gate.json',seal);evidence.write_text('changed')
            with self.assertRaisesRegex(RuntimeError,'Sealed evidence changed'):deliver(root)
            self.assertFalse((root/'Reports/pre_robustness_readiness.json').exists())

    def test_unpassed_gate_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);save=self.fixture(root)
            gate=json.loads((root/'Reports/validation_gate.json').read_text());gate['passed']=False
            save(root/'Reports/validation_gate.json',gate)
            with self.assertRaisesRegex(RuntimeError,'Both current independent gates'):deliver(root)
            self.assertFalse((root/'Reports/pre_robustness_readiness.json').exists())
