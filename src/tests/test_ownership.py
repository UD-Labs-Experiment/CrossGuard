from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from crossguard.simulation.ownership import CarlaLease


class OwnershipTests(unittest.TestCase):
    def test_second_process_cannot_own_world_and_release_allows_next_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"lease"
            code = "from crossguard.simulation.ownership import CarlaLease; import sys; lease=CarlaLease(sys.argv[1]); lease.close()"
            first = CarlaLease(path)
            try:
                blocked = subprocess.run([sys.executable,"-c",code,str(path)],capture_output=True,text=True)
                self.assertNotEqual(blocked.returncode,0)
                self.assertIn("owned by another",blocked.stderr)
            finally:
                first.close()
            second = subprocess.run([sys.executable,"-c",code,str(path)],capture_output=True,text=True)
            self.assertEqual(second.returncode,0,second.stderr)
