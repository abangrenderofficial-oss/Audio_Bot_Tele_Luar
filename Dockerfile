FROM ghcr.io/imputnet/yt-session-generator:webserver

# Cobalt currently requests POST /get_pot while the official session generator
# exposes /token. The generator's WSGI server is method-agnostic, so add a
# route alias without changing token generation itself.
RUN python - <<'PY'
from pathlib import Path
p = Path('/app/potoken_generator/server.py')
s = p.read_text()
needle = "            '/token': self.get_potoken,\n"
replacement = "            '/token': self.get_potoken,\n            '/get_pot': self.get_potoken,\n"
if needle not in s:
    raise SystemExit('official server.py route layout changed')
p.write_text(s.replace(needle, replacement, 1))
PY
