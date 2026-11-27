"""Include user instructions and the bundled Python runtime license."""

from pathlib import Path
from shutil import copyfile, copytree, ignore_patterns
import sys
import sysconfig


def prepare_bundle(folder: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    candidates = (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    )
    license_path = next((path for path in candidates if path.is_file()), None)
    if license_path is None:
        raise FileNotFoundError("Could not locate the bundled Python runtime's license.")
    copyfile(project / "README.md", folder / "README.md")
    (folder / "docs").mkdir(exist_ok=True)
    copyfile(project / "docs" / "h5-action-collection.md", folder / "docs" / "h5-action-collection.md")
    copyfile(license_path, folder / "PYTHON-LICENSE.txt")
    copytree(project / "extensions" / "wpk-h5-capture", folder / "wpk-h5-capture",
             dirs_exist_ok=True, ignore=ignore_patterns("test"))


if __name__ == "__main__":
    prepare_bundle(Path(sys.argv[1]).resolve(strict=True))
