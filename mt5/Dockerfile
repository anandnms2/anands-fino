# gmag11's MT5-under-Wine image plus one init script that works around its broken bridge install.
FROM gmag11/metatrader5_vnc:latest
COPY 10-pin-mt5linux.sh /custom-cont-init.d/10-pin-mt5linux.sh
RUN chmod 755 /custom-cont-init.d/10-pin-mt5linux.sh
