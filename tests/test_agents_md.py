"""wl-479 — L1 AGENTS.md is CORE + pointers, not the lane handbook.

Pattern: osp-1217. Generated hands interior is doctor-owned (pc-1347);
this file locks CORE shape. Internal-lanes assertions skip on the public
export (scripts/export_worklane.sh strips that marker pair).
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
TEXT = AGENTS.read_text(encoding="utf-8")
HANDS_START = "<!-- bp:generated:hands -->"
HANDS_END = "<!-- /bp:generated:hands -->"
LANES_START = "<!-- internal-lanes:start -->"
LANES_END = "<!-- internal-lanes:end -->"


def _core() -> str:
    assert HANDS_START in TEXT
    return TEXT.split(HANDS_START, 1)[0]


class AgentsMdCoreTest(unittest.TestCase):
    def test_title_is_l1_core(self) -> None:
        first = TEXT.splitlines()[0]
        self.assertEqual(first, "# WorkLane — Project instructions (L1 CORE)")

    def test_core_is_scannable(self) -> None:
        core = _core()
        core_lines = [ln for ln in core.splitlines() if ln.strip()]
        self.assertLessEqual(len(core.splitlines()), 140)
        self.assertLessEqual(len(core_lines), 120)

    def test_points_at_handbook(self) -> None:
        core = _core()
        self.assertTrue(
            "PROTOCOL.md" in core or "PROTOCOL.md" in core,
            "CORE must point at PROTOCOL.md (PROTOCOL.md after public export)",
        )
        for needle in (
            "ARCHITECTURE.md",
            "INSTALL.md",
            "HOST_PROFILE_TEMPLATE.md",
        ):
            self.assertIn(needle, core)

    def test_handbook_not_inline(self) -> None:
        """Runway watermarks, chew procedure, and publish recipe live in CONTRACT."""
        core = _core()
        self.assertNotIn("worker:lili` **< 3**", core)
        self.assertNotIn("hired `worker:tess` **< 2**", core)
        self.assertNotIn("gate_type=human` + `worker:you`", core)
        self.assertNotIn("head -n1 .sync-head", core)
        self.assertNotIn("git commit -m \"sync: worklane internal HEAD", core)

    def test_export_strip_markers_present(self) -> None:
        """Internal tree keeps the pair; public dest strips it."""
        if LANES_START not in TEXT:
            self.skipTest("public export strips internal-lanes")
        self.assertIn(LANES_END, TEXT)
        self.assertLess(TEXT.index(LANES_START), TEXT.index(LANES_END))
        self.assertIn("workers/lili/CONTRACT.md", TEXT)

    def test_generated_hands_markers_present(self) -> None:
        start = TEXT.index(HANDS_START)
        end = TEXT.index(HANDS_END) + len(HANDS_END)
        self.assertLess(start, end)
        self.assertEqual(TEXT[end:].strip(), "")


if __name__ == "__main__":
    unittest.main()
