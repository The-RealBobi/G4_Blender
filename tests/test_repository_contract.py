from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_new_native_modules_do_not_import_prv(self):
        forbidden = ("g4_blender_prv", "G4_Blender_prv")
        for directory in (ROOT / "formats", ROOT / "shading", ROOT / "effects"):
            for path in directory.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                self.assertFalse(any(token in text for token in forbidden), path)

    def test_native_shader_binaries_are_not_packaged(self):
        owners = (ROOT / "formats", ROOT / "shading", ROOT / "effects")
        files = tuple(path for owner in owners for path in owner.rglob("*"))
        forbidden = {".spv", ".glsl", ".bnsh", ".fsb", ".vsb"}
        self.assertFalse(any(path.is_file() and path.suffix.casefold() in forbidden for path in files))


if __name__ == "__main__":
    unittest.main()
