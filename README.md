# TCI Bridge

A cross-platform **Hamlib + USB Audio to TCI 2.1** network bridge for macOS and Linux.

TCI Bridge exposes any Hamlib-supported transceiver and USB audio interface as a **Transceiver Control Interface (TCI 2.1)** server over your local network, allowing TCI clients—such as **JS8Call-Mobile** (iPadOS/iOS/macOS), desktop **JS8Call**, **WSJT-X**, and **JTDX**—to control frequency, mode, and PTT while streaming low-latency RX/TX audio without serial cables or virtual audio routing on the client device.

## Features

- **Rig-Agnostic CAT & PTT via Hamlib (`rigctld`)**: Supports hundreds of transceivers over USB serial, CI-V, or network CAT.
  - *Note on Network-Connected Radios (e.g., Icom IC-7300Mk2/IC-705/IC-7610 over LAN or FlexRadio)*: Hamlib (`rigctld`) handles CAT control only (frequency, mode, PTT) and does not carry audio. To use network-connected radios with TCI Bridge, route the radio's audio through a local virtual audio device (such as **BlackHole** on macOS, **PulseAudio/PipeWire** virtual sinks on Linux, or **SmartSDR DAX** audio devices) and select that device in the Web Configuration UI.
- **TCI 2.1 Protocol Server**: Implements the core digital-mode TCI 2.1 specification (backward-compatible with TCI 1.8–2.1 clients), supporting `float32` and `int16` PCM audio (`8`–`48` kHz), per-client audio streams, isolated TX ownership, and automatic USB audio recovery after sleep or power cycles.
- **Built-in Web Configuration UI (`http://localhost:58080`)**: Mobile-friendly web UI for selecting transceiver model, serial port, baud rate, PTT mode, and USB audio device.
- **Zero-Config Bonjour / mDNS Auto-Discovery**: Advertises `_tci._tcp` automatically via `dns-sd` (macOS) or `avahi-publish-service` (Linux).
- **Native macOS `.app` Wrapper**: Includes `Launcher.swift` and `Mac_build_TCIBridge.sh` for signed/notarized `.app` and `.dmg` builds.

## TCI 2.1 Protocol Scope & Client Compatibility

- **Implemented**: Core digital-mode control (`VFO`, `MODULATION`, `TRX`, `TUNE`, `DRIVE`, `SPLIT_ENABLE`, `DDS`, `IF`) and bidirectional PCM audio streaming (`RX_AUDIO_STREAM`, `TX_AUDIO_STREAM`, `TX_CHRONO`) in `float32` and `int16` formats.
- **Omitted**: Expert Electronics SDR-specific hardware features (IQ wideband spectrum streams, CW macro keyers, DSP noise reduction/notch filters) and unused `int24` / `int32` audio formats.
- **Client Notes**:
  - **JS8Call-Mobile & Desktop JS8Call**: Full CAT, RX audio, and TX audio support, including simultaneous multi-client monitoring.
  - **WSJT-X**: Connects, tracks CAT frequency/mode, and decodes RX audio. *(Upstream WSJT-X's built-in TCI 1.4 client has known TX buffer bugs that prevent TCI TX until fixed upstream.)*

## Requirements

Builds for Mac and Debian-based arm64 Raspberry Pi are in the Releases

### macOS (Apple Silicon, macOS 13.0 Ventura or newer)
**No external dependencies required.** The prebuilt `TCIBridge.app` / `.dmg` in Releases bundles a self-contained Python 3.12 runtime (`numpy`, `pyserial`, `sounddevice`, `websockets`) and Hamlib (`rigctld`/`rigctl`). Simply download `TCIBridge.app` and run it like any other Mac application.

*(If running `TCI-bridge.py` directly from source instead of the `.app` bundle, install `brew install hamlib` and `pip3 install numpy pyserial sounddevice websockets`.)*

### Linux / Raspberry Pi
```bash
sudo apt update && sudo apt install -y libhamlib-utils libportaudio2 avahi-utils python3-pip
pip3 install --user numpy pyserial sounddevice websockets
sudo usermod -aG dialout,audio $USER
```

The .deb will install necessary dependencies and install a backgound daemon to run the server, configurable with the webpage-based applet.

## Quick Start

### Run via Python
```bash
python3 TCI-bridge.py
```

### Build macOS `TCIBridge.app`
Requires [uv](https://astral.sh) and Homebrew (`brew install uv hamlib`, plus `create-dmg` for `--release`):
```zsh
chmod +x Mac_build_TCIBridge.sh
./Mac_build_TCIBridge.sh --dev      # Local Developer ID build
./Mac_build_TCIBridge.sh --release  # Signed + notarized .app and .dmg
```

### Build & Install Headless Debian 13 (`arm64`) `.deb` Daemon (Raspberry Pi 4B / 5)
Run on an `arm64` Debian 13 (Trixie) / Raspberry Pi OS system to build and install `tcibridge` as a background `systemd` service:
```bash
chmod +x Debian_build_TCIBridge.sh
./Debian_build_TCIBridge.sh
sudo apt install ./dist_deb/tcibridge_1.0.0_arm64.deb
```
Manage the headless daemon:
```bash
sudo systemctl status tcibridge
journalctl -u tcibridge -f
```

## Configuration & Logs

- **Web Setup UI**: Open `http://localhost:58080` (or tap **Configure Bridge Rig & Audio** in **JS8Call-Mobile**).
- **Config File**: `~/Library/Application Support/TCIBridge/config.json` (macOS) or `~/.config/TCIBridge/config.json` (Linux).
- **Log File**: `~/Library/Logs/TCIBridge/tci-bridge.log` (macOS) or `~/.local/state/TCIBridge/tci-bridge.log` (Linux).

## License

Copyright © 2026 Chris Olson. Licensed under the **BSD 3-Clause License**.

### Third-Party Components
Prebuilt macOS `.app` / `.dmg` releases bundle unmodified binaries from **Hamlib** (`GPLv2` / `LGPLv2.1`), **libusb** (`LGPLv2.1`), **libltdl** (`LGPLv2+`), **CPython** (`PSF-2.0`), and **PortAudio / Python packages** (`MIT` / `BSD-3-Clause`). See [`THIRD-PARTY-LICENSES.txt`](THIRD-PARTY-LICENSES.txt) for license details and corresponding source code links.
