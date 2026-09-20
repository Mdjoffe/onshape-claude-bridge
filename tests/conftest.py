"""Say which copy of the package is under test.

A non-editable `pip install .` puts a snapshot in site-packages that shadows
`src/`, so `pytest` then reports on code that is not the code you are editing.
It passes, it looks fine, and it is answering about the wrong file -- the same
stale-install trap that once had the devcontainer running a week-old bridge.

CI installs non-editable on purpose, because what the content repo consumes is
the installed artifact. That is correct there and misleading here, so the path
is printed either way and whoever is reading can tell which they got.

`pip install -e .` is what makes local edits live.
"""

import onshape_bridge


def pytest_report_header(config):
    return f"onshape_bridge: {onshape_bridge.__file__}"
