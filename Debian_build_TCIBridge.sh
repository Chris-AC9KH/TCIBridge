#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Chris Olson
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

set -euo pipefail

VERSION="1.0.2"
ARCH="$(dpkg --print-architecture)"
PKG_NAME="tcibridge"
BUILD_DIR="dist_deb/${PKG_NAME}_${VERSION}_${ARCH}"

rm -rf dist_deb
mkdir -p "${BUILD_DIR}/DEBIAN"
mkdir -p "${BUILD_DIR}/opt/tcibridge"
mkdir -p "${BUILD_DIR}/lib/systemd/system"

cp TCI-bridge.py "${BUILD_DIR}/opt/tcibridge/TCI-bridge.py"
chmod 0755 "${BUILD_DIR}/opt/tcibridge/TCI-bridge.py"

cat << CONTROL > "${BUILD_DIR}/DEBIAN/control"
Package: tcibridge
Version: 1.0.2
Section: hamradio
Priority: optional
Architecture: ${ARCH}
Maintainer: Chris Olson
Depends: python3, python3-venv, python3-numpy, python3-serial, python3-websockets, libhamlib-utils, libportaudio2, alsa-utils, avahi-utils
Description: Hamlib USB Audio to TCI 2.1 Network Bridge
 Exposes any Hamlib-supported transceiver and USB audio interface as a
 TCI 2.1 server over the local network with Bonjour/mDNS discovery
 and a built-in web configuration UI on port 58080.
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
Environment=PULSE_CLIENTCONFIG=/dev/null
Environment=PULSE_SERVER=none
Environment=JACK_NO_START_SERVER=1
ExecStartPre=-/usr/bin/pkill -u tcibridge -x rigctld
ExecStart=/opt/tcibridge/venv/bin/python /opt/tcibridge/TCI-bridge.py --host 0.0.0.0 --port 50001
Restart=always
RestartSec=3
TimeoutStopSec=5

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
cat << 'ASOUNDRC' > /var/lib/tcibridge/.asoundrc
pcm.!default {
    type plug
    slave.pcm "sysdefault"
}
ctl.!default {
    type hw
    card 0
}
ASOUNDRC
chown -R tcibridge:tcibridge /var/lib/tcibridge /opt/tcibridge

if [ ! -d /opt/tcibridge/venv ]; then
    python3 -m venv --system-site-packages /opt/tcibridge/venv
fi
/opt/tcibridge/venv/bin/pip install --no-cache-dir --upgrade sounddevice

CODEC_CARD=$(awk '/USB Audio CODEC/ {print $1; exit}' /proc/asound/cards 2>/dev/null || true)
if [ -n "$CODEC_CARD" ]; then
    amixer -c "$CODEC_CARD" sset PCM 100% unmute >/dev/null 2>&1 || true
    alsactl store "$CODEC_CARD" >/dev/null 2>&1 || true
fi
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
dpkg-deb --root-owner-group --build "${BUILD_DIR}"
echo "Built: ${BUILD_DIR}.deb"
