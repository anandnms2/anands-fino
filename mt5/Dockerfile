# gmag11's MT5-under-Wine image plus one init script that works around its broken bridge install.
FROM gmag11/metatrader5_vnc:latest
COPY 10-fix-bridge.sh /custom-cont-init.d/10-fix-bridge.sh
RUN chmod 755 /custom-cont-init.d/10-fix-bridge.sh
