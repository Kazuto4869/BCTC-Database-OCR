import os
import fcntl
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ServerLauncherTests(unittest.TestCase):
    def test_duplicate_launcher_does_not_start_another_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "run_server.sh"
            shutil.copyfile(Path(__file__).with_name("run_server.sh"), script)
            model = root / "weights" / "DotsMOCR"
            model.mkdir(parents=True)
            for name in ("config.json", "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
                (model / name).touch()
            fake = root / "fake_vllm"
            record = root / "started.txt"
            fake.write_text('#!/bin/sh\necho started > "$RECORD"\n')
            fake.chmod(0o700)
            (root / "result").mkdir()
            with (root / "result" / ".server.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                run = subprocess.run(["bash", str(script)], env=dict(os.environ, VLLM_BIN=str(fake), RECORD=str(record)),
                                     capture_output=True, text=True)
            self.assertEqual(run.returncode, 0)
            self.assertFalse(record.exists())
            self.assertIn("đã chạy", run.stdout)

    def test_launcher_passes_local_model_and_preserves_server_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "run_server.sh"
            shutil.copyfile(Path(__file__).with_name("run_server.sh"), script)
            model = root / "weights" / "DotsMOCR"
            model.mkdir(parents=True)
            for name in ("config.json", "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
                (model / name).touch()
            fake = root / "fake_vllm"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\necho "$VLLM_USE_FLASHINFER_SAMPLER" > "$SAMPLER_RECORD"\necho TEST_STUB_ERROR\nexit 17\n')
            fake.chmod(0o700)
            record = root / "args.txt"
            sampler_record = root / "sampler.txt"
            curl = root / "curl"
            curl.write_text('#!/bin/sh\nexit 1\n')
            curl.chmod(0o700)
            env = dict(os.environ, VLLM_BIN=str(fake), RECORD=str(record),
                       SAMPLER_RECORD=str(sampler_record), VLLM_USE_FLASHINFER_SAMPLER="1",
                       PATH=str(root) + os.pathsep + os.environ["PATH"])
            run = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 17)
            self.assertEqual(sampler_record.read_text().strip(), "0")
            args = record.read_text().splitlines()
            self.assertEqual(args[:2], ["serve", str(model)])
            self.assertEqual(args[args.index("--served-model-name") + 1], "model")
            self.assertEqual(args[args.index("--max-model-len") + 1], "32768")
            logs = list((root / "result").glob("server_*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn("TEST_STUB_ERROR", logs[0].read_text())


if __name__ == "__main__":
    unittest.main()
