#!/usr/bin/with-contenv bash
# The image installs the newest mt5linux (1.1.1), which uses Python 3.12-only syntax, but the container's
# Linux Python is 3.11, so the bridge on port 8001 never starts. 0.1.9 runs on 3.11 and is what the image's
# own start script expects. The image skips its install when the package already exists, so pin it here.
# Runs as root at every container start, before the desktop and MT5 start.
mkdir -p /config
want="0.1.9"
have="$(s6-setuidgid abc env HOME=/config python3 -m pip show mt5linux 2>/dev/null | awk '/^Version:/{print $2}')"
if [ "$have" != "$want" ]; then
  echo "[pin-mt5linux] have '${have:-none}', installing mt5linux==$want"
  s6-setuidgid abc env HOME=/config python3 -m pip uninstall -y mt5linux >/dev/null 2>&1
  s6-setuidgid abc env HOME=/config python3 -m pip install --user --break-system-packages --no-cache-dir --no-deps "mt5linux==$want" \
    && echo "[pin-mt5linux] done" || echo "[pin-mt5linux] FAILED, bridge will not start"
else
  echo "[pin-mt5linux] mt5linux $want already installed"
fi
