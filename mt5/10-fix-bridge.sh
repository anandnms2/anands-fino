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

# ---- Windows side: the MetaTrader5 wheel was built for NumPy 1.x, but pip pulls NumPy 2 and the import fails
# ("_ARRAY_API not found"). Pin numpy<2 inside the Wine Python.
wine_py() { s6-setuidgid abc env HOME=/config WINEPREFIX=/config/.wine WINEDEBUG=-all DISPLAY= wine python.exe "$@"; }
fix_numpy() {
  local v
  v="$(wine_py -c 'import numpy; print(numpy.__version__)' 2>/dev/null | tr -d '\r' | tail -1)"
  case "$v" in
    1.*) echo "[fix-numpy] numpy $v is fine"; return 0;;
  esac
  echo "[fix-numpy] numpy '${v:-none}' in Wine, installing numpy<2"
  wine_py -m pip install --no-cache-dir "numpy<2" >/dev/null 2>&1 && echo "[fix-numpy] done" || echo "[fix-numpy] FAILED"
}
PYEXE="/config/.wine/drive_c/Program Files (x86)/Python39-32/python.exe"
if [ -f "$PYEXE" ]; then
  fix_numpy
else
  # Fresh volume: Python is installed later by the image's own script. Wait for the MetaTrader5 package, then fix.
  (
    for _ in $(seq 1 240); do
      sleep 10
      if [ -d "/config/.wine/drive_c/Program Files (x86)/Python39-32/Lib/site-packages/MetaTrader5" ]; then
        fix_numpy; exit 0
      fi
    done
  ) >/proc/1/fd/1 2>&1 &
fi

# ---- Watcher: if the MT5 terminal is not running 90s after start, launch it and log what Wine says.
(
  sleep 90
  {
    echo "[watch-mt5] processes: $(pgrep -a -f 'terminal64|wineserver|server.py' | cut -c1-120 | tr '\n' ';')"
    if pgrep -f terminal64.exe >/dev/null 2>&1; then
      echo "[watch-mt5] terminal64.exe is running"
    else
      echo "[watch-mt5] terminal64.exe NOT running, launching it"
      s6-setuidgid abc env HOME=/config WINEPREFIX=/config/.wine DISPLAY=:1 WINEDEBUG=err+all \
        wine "C:\\Program Files\\MetaTrader 5\\terminal64.exe" 2>&1 | head -60 &
      sleep 30
      echo "[watch-mt5] after launch: $(pgrep -a -f terminal64 | cut -c1-100 | tr '\n' ';')"
    fi
  } >/proc/1/fd/1 2>&1
) &
