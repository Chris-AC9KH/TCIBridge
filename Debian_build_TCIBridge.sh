#!/usr/bin/env bash
set -euo pipefail

VERSION="1.0.0"
ARCH="arm64"
PKG_NAME="tcibridge"
BUILD_DIR="dist_deb/${PKG_NAME}_${VERSION}_${ARCH}"

rm -rf dist_deb
mkdir -p "${BUILD_DIR}/DEBIAN"
mkdir -p "${BUILD_DIR}/opt/tcibridge"
mkdir -p "${BUILD_DIR}/lib/systemd/system"

cp TCI-bridge.py "${BUILD_DIR}/opt/tcibridge/TCI-bridge.py"
chmod 0755 "${BUILD_DIR}/opt/tcibridge/TCI-bridge.py"

cat << 'CONTROL' > "${BUILD_DIR}/DEBIAN/control"
Package: tcibridge
Version: 1.0.0
Section: hamradio
Priority: optional
Architecture: arm64
Maintainer: Chris Olson
Depends: python3, python3-venv, python3-numpy, python3-serial, python3-websockets, libhamlib-utils, libportaudio2, avahi-utils
Description: Hamlib USB Audio to TCI 2.1 Network Bridge
 Exposes any Hamlib-supported transceiver and USB audio interface as a
 TCI 2.1 server over the local network with Bonjour/mDNS discovery
 and a built-in web configuration UI on port 50002.
CONTROL

cat << 'SERVICE' > "${BUILD_DIR}/lib/systemd/system/tcibridge.service"
[Unit]
Description=TCI 2.1 Hamlib & USB Audio Network Bridge
After=network-online.target sound.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service

[Service]
Type=simple
User=tcibridge
Group=tcibridge
WorkingDirectory=/opt/tcibridge
Environment=HOME=/var/lib/tcibridge
ExecStart=/opt/tcibridge/venv/bin/python /opt/tcibridge/TCI-bridge.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

cat << 'POSTINST' > "${BUILD_DIR}/DEBIAN/postinst"
#!/usr/bin/env bash
set -e

if ! id -u tcibridge >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/tcibridge --create-home --shell /usr/sbin/nologin tcibridge
fi
usermod -aG dialout,audio tcibridge
chown -R tcibridge:tcibridge /var/lib/tcibridge /opt/tcibridge

if [ ! -d /opt/tcibridge/venv ]; then
    python3 -m venv --system-site-packages /opt/tcibridge/venv
fi
/opt/tcibridge/venv/bin/pip install --no-cache-dir --upgrade sounddevice

systemctl daemon-reload
systemctl enable tcibridge.service
systemctl restart tcibridge.service
POSTINST

cat << 'PRERM' > "${BUILD_DIR}/DEBIAN/prerm"
#!/usr/bin/env bash
set -e
if [ "$1" = "remove" ]; then
    systemctl stop tcibridge.service || true
    systemctl disable tcibridge.service || true
fi
PRERM

chmod 0755 "${BUILD_DIR}/DEBIAN/postinst" "${BUILD_DIR}/DEBIAN/prerm"
dpkg-deb --build "${BUILD_DIR}"
echo "Built: ${BUILD_DIR}.deb"
