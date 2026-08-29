$ErrorActionPreference = 'Stop'
python -m unittest discover -s tests -v
python -m compileall -q coding_agent main.py web_server.py
