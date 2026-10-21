# gmag11's MT5-under-Wine image plus fixes for its broken bridge install and a Wine 11 startup crash.
FROM gmag11/metatrader5_vnc:latest
USER root
# Wine 11 makes the current MT5 build die at startup with a false "a debugger has been found" error.
# Wine 10.0 is the version people report working. Everything is logged so the Coolify build log shows what happened;
# the build never fails because of this step.
RUN set -x; \
    apt-get update || true; \
    cur="$(dpkg-query -W -f='${Version}' wine-stable 2>/dev/null || echo none)"; \
    echo "BUILD-INFO installed wine-stable: $cur"; \
    /opt/wine-stable/bin/wine --version || true; \
    apt-cache madison wine-stable | head -8 || true; \
    case "$cur" in \
      11.*|12.*) \
        tgt="$(apt-cache madison wine-stable | awk '{print $3}' | grep '^10\.0' | head -1)"; \
        echo "BUILD-INFO downgrade target: ${tgt:-none}"; \
        if [ -n "$tgt" ]; then \
          apt-get install -y --allow-downgrades --no-install-recommends \
            "winehq-stable=$tgt" "wine-stable=$tgt" "wine-stable-amd64=$tgt" "wine-stable-i386:i386=$tgt" \
          && echo "BUILD-INFO downgrade OK" || echo "BUILD-INFO downgrade FAILED"; \
        fi;; \
    esac; \
    /opt/wine-stable/bin/wine --version || true; \
    rm -rf /var/lib/apt/lists/*
COPY 10-fix-bridge.sh /custom-cont-init.d/10-fix-bridge.sh
RUN chmod 755 /custom-cont-init.d/10-fix-bridge.sh
