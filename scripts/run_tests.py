"""Report concise unittest failure annotations alongside the normal CI log."""

import os
import unittest


def _annotation(test, traceback):
    conclusion = traceback.strip().splitlines()[-1]
    message = f"{test.id()}: {conclusion}"
    paths = (os.environ.get(name) for name in (
        "GITHUB_WORKSPACE", "RUNNER_TEMP", "TEMP", "TMP", "USERPROFILE", "HOME",
    ))
    for path in sorted({path for path in paths if path}, key=len, reverse=True):
        message = message.replace(path, "<runner-path>").replace(path.replace("\\", "/"), "<runner-path>")
    message = message[:600].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title=Unit test failure::{message}")


def main():
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for test, traceback in (result.errors + result.failures)[:6]:
            _annotation(test, traceback)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
