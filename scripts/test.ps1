$ErrorActionPreference = 'Stop'

if (Test-Path '.local-dev/tests') {
    python -m unittest discover -s .local-dev/tests -v
} else {
    Write-Host 'Local regression tests are stored in .local-dev/tests and are not versioned.'
}

python -m compileall -q coding_agent main.py web_server.py
