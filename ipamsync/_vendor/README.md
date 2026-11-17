# Vendored dependencies

Pure-Python copies, used only when `requests` / `PyYAML` are not
installed, so `ipamsync` runs on an air-gapped host with just Python
3.9+. Each package's licence is in its `*.dist-info/` directory (PyYAML:
`yaml/LICENSE`).

| Package | Version | Source |
|---|---|---|
| requests | 2.32.3 | `requests-2.32.3-py3-none-any.whl` |
| urllib3 | 2.2.3 | `urllib3-2.2.3-py3-none-any.whl` (last 2.x supporting Python 3.8) |
| idna | 3.10 | `idna-3.10-py3-none-any.whl` |
| charset-normalizer | 3.4.0 | `charset_normalizer-3.4.0-py3-none-any.whl` (the pure wheel, not the compiled one) |
| certifi | 2024.8.30 | `certifi-2024.8.30-py3-none-any.whl` |
| PyYAML | 6.0.2 | `lib/yaml/` from the sdist: the pure-Python implementation, no libyaml |

To update: download the `py3-none-any` wheels with
`pip download --no-deps --only-binary=:all: --platform any --python-version 3.8`,
unzip them here, and copy `lib/yaml/` from the PyYAML sdist.
