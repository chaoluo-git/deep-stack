"""Include user instructions and the bundled Python runtime license."""

from pathlib import Path
from shutil import copyfile
import sys
import sysconfig


def prepare_bundle(folder: Path) -> None:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    candidates = (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    )
    license_path = next((path for path in candidates if path.is_file()), None)
    if license_path is None:
        raise FileNotFoundError("Could not locate the bundled Python runtime's license.")
    copyfile(readme, folder / "README.md")
    copyfile(license_path, folder / "PYTHON-LICENSE.txt")


if __name__ == "__main__":
    prepare_bundle(Path(sys.argv[1]).resolve(strict=True))
