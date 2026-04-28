# Contributing

## Setup

```bash
git clone https://github.com/ChingEnLin/QueryMCPal.git
cd QueryMCPal
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

`pre-commit install` wires up ruff and mypy to run on every commit. To run them manually at any time:

```bash
pre-commit run --all-files
```

## Checks

| Tool | What it enforces |
|------|-----------------|
| `ruff check` | Linting (unused imports, style) |
| `ruff format --check` | Formatting |
| `mypy --strict` | Type correctness across all source files |

## Docker image

After code changes, rebuild and re-test the image:

```bash
docker build --platform linux/arm64 -t querymcpal:dev .

echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  | docker run --rm -i \
    --platform linux/arm64 \
    -v ~/.azure:/home/mcpuser/.azure \
    querymcpal:dev
```

Expected: JSON response with `serverInfo.name = "querymcpal"`.
