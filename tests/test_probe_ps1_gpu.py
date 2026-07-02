"""Contract tests for the Windows probe's GPU fallback (probe.ps1 Read-Gpu).

Windows-only WMI / perf counters can't execute in Linux CI, so these tests assert
at the source level that probe.ps1's Read-Gpu keeps NVIDIA winning when present and
adds a WMI + perf-counter fallback that emits the SAME gpu JSON keys probe.py's
read_gpu() produces (so app.py renders an AMD/Intel Windows host with zero backend
changes). The Linux gpu contract (probe.read_gpu) is asserted alongside so the two
stay in lock-step."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(ROOT, "probe.ps1")

# The exact key contract app.py consumes for a gpu block (mirrors probe.py.read_gpu).
CONTRACT_KEYS = {"count", "name", "mem_used", "mem_total", "util", "temp"}


class ProbePs1GpuContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PS1, encoding="utf-8") as f:
            cls.src = f.read()
        m = re.search(r"function Read-Gpu \{.*?\n\}\n", cls.src, re.S)
        assert m, "Read-Gpu function not found in probe.ps1"
        cls.fn = m.group(0)

    def test_nvidia_path_preserved(self):
        """nvidia-smi is still tried first with the identical query + it must win."""
        self.assertIn("Get-Command nvidia-smi", self.fn)
        self.assertIn(
            "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name",
            self.fn,
        )
        self.assertIn("vendor    = 'nvidia'", self.fn)

    def test_wmi_fallback_present(self):
        """Falls back to Win32_VideoController when nvidia-smi is absent/empty."""
        self.assertIn("Win32_VideoController", self.fn)

    def test_registry_vram_preferred_over_adapterram(self):
        """qwMemorySize registry value preferred; AdapterRAM only as a fallback."""
        self.assertIn("HardwareInformation.qwMemorySize", self.fn)
        self.assertIn("4d36e968-e325-11ce-bfc1-08002be10318", self.fn)
        self.assertIn("AdapterRAM", self.fn)
        # Inside VramBytes, the registry qwMemorySize lookup must be tried before
        # the AdapterRAM fallback (AdapterRAM only used when bytes still <= 0).
        vram = self.fn[self.fn.index("function VramBytes"):]
        vram = vram[: vram.index("return")]
        self.assertLess(
            vram.index("qwMemorySize"),
            vram.index("AdapterRAM"),
            "registry qwMemorySize should be preferred before AdapterRAM",
        )
        self.assertIn("if ($bytes -le 0)", vram)

    def test_perf_counters_present(self):
        """Live util + VRAM-in-use come from the documented GPU perf counters."""
        self.assertIn(r"\GPU Engine(*)\Utilization Percentage", self.fn)
        self.assertIn(r"\GPU Adapter Memory(*)\Dedicated Usage", self.fn)

    def test_vendor_inference_all_classes(self):
        """vendor is inferred as nvidia|amd|intel|unknown."""
        for v in ("nvidia", "amd", "intel", "unknown"):
            self.assertIn(f"'{v}'", self.fn, f"vendor class {v} missing")

    def test_fallback_emits_full_key_contract(self):
        """The fallback gpu block emits every key app.py consumes, plus vendor."""
        # Isolate the fallback return (the block after the NVIDIA try).
        tail = self.fn[self.fn.index("Fallback"):]
        for key in CONTRACT_KEYS:
            self.assertRegex(
                tail, rf"\b{key}\s*=",
                f"fallback gpu block missing contract key '{key}'",
            )
        self.assertRegex(tail, r"\bvendor\s*=", "fallback missing vendor key")

    def test_reads_are_guarded(self):
        """Every Get-Counter / Get-CimInstance read is wrapped so missing counters
        degrade to {} rather than throwing."""
        self.assertGreaterEqual(self.fn.count("try {"), 4)
        self.assertIn("ErrorAction Stop", self.fn)
        # Empty adapter list must return an empty object (0-GPU host).
        self.assertIn("if ($cards.Count -eq 0) { return @{} }", self.fn)


class LinuxGpuContractUnchanged(unittest.TestCase):
    """Guard that the Linux probe still emits the same key set the ps1 fallback
    mirrors — keeps the two probes in lock-step."""

    def test_probe_read_gpu_keys(self):
        import sys
        sys.path.insert(0, ROOT)
        import probe
        src = open(os.path.join(ROOT, "probe.py"), encoding="utf-8").read()
        block = src[src.index("def read_gpu"):]
        for key in CONTRACT_KEYS:
            self.assertIn(f'"{key}"', block)


if __name__ == "__main__":
    unittest.main()
