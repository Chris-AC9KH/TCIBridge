#!/usr/bin/env python3
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

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Set

import numpy as np
import serial.tools.list_ports
import sys
try:
    import sounddevice as sd
except Exception:
    sys.modules.pop("sounddevice", None)
    os.environ["PULSE_CLIENTCONFIG"] = "/dev/null"
    os.environ["PULSE_SERVER"] = "none"
    os.environ["JACK_NO_START_SERVER"] = "1"
    if os.path.exists("/usr/share/alsa/alsa.conf"):
        os.environ["ALSA_CONFIG_PATH"] = "/usr/share/alsa/alsa.conf"
    try:
        import sounddevice as sd
    except Exception as _sd_err:
        sys.modules.pop("sounddevice", None)
        sd = None
        print(f"Warning: sounddevice unavailable at import ({_sd_err})")
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tci-bridge")

_sd_checked = False
def _ensure_sd():
    global sd, _sd_checked
    if _sd_checked:
        return sd
    _sd_checked = True
    try:
        if sd is None:
            import sounddevice as _sd
            sd = _sd
        sd.query_devices()
    except Exception:
        sys.modules.pop("sounddevice", None)
        sd = None
    return sd

def _find_alsa_pcm(name_hint: Optional[str]) -> Optional[str]:
    if platform.system() != "Linux" or not shutil.which("arecord"):
        return None
    target = (name_hint or "usb audio codec").lower()
    try:
        out = subprocess.check_output(["arecord", "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    for line in out.splitlines():
        m = re.match(r"^card\s+(\d+):\s*(\S+)\s+\[([^\]]+)\],\s*device\s+(\d+):", line)
        if m:
            card_id, card_name, dev_num = m.group(2), m.group(3), m.group(4)
            if target in card_name.lower() or target in card_id.lower() or target in f"plughw:{card_id},{dev_num}":
                return f"plughw:{card_id},{dev_num}"
    return None

TCI_HEADER_FMT = "<16I"
TCI_HEADER_SIZE = 64
FORMAT_INT16 = 0
FORMAT_FLOAT32 = 3
STREAM_RX_AUDIO = 1
STREAM_TX_AUDIO = 2
STREAM_TX_CHRONO = 3

TCI_TO_HAMLIB_MODE = {
    "lsb": "LSB", "usb": "USB", "am": "AM", "cw": "CW", "rtty": "RTTY",
    "fm": "FM", "cwr": "CWR", "rttyr": "RTTYR", "digl": "PKTLSB", "digu": "PKTUSB",
}
HAMLIB_TO_TCI_MODE = {
    "LSB": "lsb", "USB": "usb", "AM": "am", "CW": "cw", "RTTY": "rtty",
    "FM": "fm", "CWR": "cwr", "RTTYR": "rttyr", "PKTLSB": "digl", "PKTUSB": "digu",
    "DATA-L": "digl", "DATA-U": "digu", "DATA-R": "digl",
}


def get_config_path() -> Path:
    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "TCIBridge"
    else:
        base = Path.home() / ".config" / "tcibridge"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


@dataclass
class BridgeConfig:
    rig_model: int = 1
    rig_name: str = "Hamlib Dummy"
    serial_port: str = ""
    baud_rate: int = 38400
    ptt_type: str = "RIG"
    audio_device: str = ""
    rigctld_port: int = 4532

    @classmethod
    def load(cls) -> "BridgeConfig":
        path = get_config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception as e:
                log.warning(f"Failed to load config ({e}), using defaults")
        return cls()

    def save(self):
        get_config_path().write_text(json.dumps(asdict(self), indent=2))


@dataclass
class RadioState:
    vfo_a: int = 14074000
    vfo_b: int = 14074000
    mode: str = "digu"
    tx: bool = False
    tune: bool = False
    drive: int = 50
    sample_rate: int = 48000
    audio_enabled: bool = False
    audio_channels: int = 2
    audio_sample_type: str = "float32"
    audio_samples: int = 2048
    split: bool = False


def find_hamlib_bin(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(prefix, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def list_hamlib_rigs() -> list[dict]:
    rigctl = find_hamlib_bin("rigctl")
    if not rigctl:
        return [{"id": 1, "mfg": "Hamlib", "model": "Dummy (Hamlib not installed in PATH)"}]
    try:
        out = subprocess.check_output([rigctl, "-l"], text=True, stderr=subprocess.DEVNULL)
        rigs = []
        for line in out.splitlines():
            m = re.match(r"^\s*(\d+)\s+(\S+)\s+(.+?)\s+\d{4}\d{2}\d{2}", line)
            if m:
                rigs.append({"id": int(m.group(1)), "mfg": m.group(2), "model": m.group(3).strip()})
        return sorted(rigs, key=lambda r: (r["mfg"].lower(), r["model"].lower()))
    except Exception as e:
        log.error(f"Error listing Hamlib rigs: {e}")
        return [{"id": 1, "mfg": "Hamlib", "model": "Dummy"}]


class HamlibRigController:
    def __init__(self, cfg: BridgeConfig, state: RadioState):
        self.cfg = cfg
        self.state = state
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.connected = False
        self.status_text = "Starting..."

    async def start(self):
        async with self._start_lock:
            if self.connected:
                return
            await self.stop()
            rigctld = find_hamlib_bin("rigctld")
            if not rigctld:
                self.status_text = "Error: rigctld not found (install hamlib)"
                log.error(self.status_text)
                return

            cmd = [rigctld, "-m", str(self.cfg.rig_model), "-t", str(self.cfg.rigctld_port)]
            if self.cfg.rig_model != 1 and self.cfg.serial_port:
                cmd += ["-r", self.cfg.serial_port, "-s", str(self.cfg.baud_rate)]
            if self.cfg.ptt_type and self.cfg.ptt_type != "RIG":
                cmd += ["-P", self.cfg.ptt_type]

            log.info(f"Launching Hamlib: {' '.join(cmd)}")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
                for _ in range(16):
                    await asyncio.sleep(0.25)
                    if self.proc.returncode is not None:
                        err = (await self.proc.stderr.read()).decode(errors='ignore').strip()
                        self.status_text = f'rigctld exited: {err or "check serial port"}'
                        log.error(self.status_text); return
                    try:
                        self.reader, self.writer = await asyncio.open_connection('0.0.0.0', self.cfg.rigctld_port)
                        break
                    except OSError: continue
                else:
                    self.status_text = 'Timed out waiting for rigctld'
                    log.error(self.status_text); return
                self.connected = True
                self.status_text = f"Connected ({self.cfg.rig_name})"
                log.info(self.status_text)
            except Exception as e:
                self.connected = False
                self.status_text = f"Connection failed: {e}"
                log.error(self.status_text)

    async def stop(self):
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = self.reader = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except Exception:
                self.proc.kill()
        self.proc = None

    async def send_cmd(self, cmd: str) -> list[str]:
        if not self.connected or not self.writer or not self.reader:
            return []
        async with self._lock:
            try:
                self.writer.write((cmd.strip() + "\n").encode())
                await self.writer.drain()
                lines = []
                while True:
                    line = await asyncio.wait_for(self.reader.readline(), timeout=0.5)
                    if not line:
                        break
                    txt = line.decode(errors="ignore").strip()
                    lines.append(txt)
                    if txt.startswith("RPRT "):
                        break
                    if cmd in ("f", "t") and len(lines) == 1 and not txt.startswith("RPRT"):
                        break
                    if cmd == "m" and len(lines) == 2:
                        break
                return lines
            except Exception as e:
                log.warning(f"Hamlib command '{cmd}' failed: {e}")
                self.connected = False
                self.status_text = "Disconnected from rig"
                return []

    async def set_freq(self, freq_hz: int):
        self.state.vfo_a = freq_hz
        await self.send_cmd(f"F {freq_hz}")

    async def set_mode(self, tci_mode: str):
        m = tci_mode.lower()
        if m in TCI_TO_HAMLIB_MODE:
            self.state.mode = m
            await self.send_cmd(f"M {TCI_TO_HAMLIB_MODE[m]} -1")

    async def set_ptt(self, tx_on: bool):
        self.state.tx = tx_on
        await self.send_cmd(f"T {1 if tx_on else 0}")

    async def set_tune(self, tune_on: bool):
        self.state.tune = tune_on
        await self.send_cmd(f"T {3 if tune_on else 0}")

    async def set_drive(self, pct: int):
        self.state.drive = max(0, min(100, pct))
        await self.send_cmd(f"L RFPOWER {self.state.drive / 100.0:.2f}")

    async def poll(self) -> list[str]:
        if not self.connected:
            return []
        updates = []
        f_res = await self.send_cmd("f")
        if f_res and f_res[0].isdigit():
            freq = int(f_res[0])
            if freq != self.state.vfo_a:
                self.state.vfo_a = freq
                updates.append(f"vfo:0,0,{freq};")

        m_res = await self.send_cmd("m")
        if m_res and not m_res[0].startswith("RPRT"):
            tci_m = HAMLIB_TO_TCI_MODE.get(m_res[0].upper())
            if tci_m and tci_m != self.state.mode:
                self.state.mode = tci_m
                updates.append(f"modulation:0,{tci_m};")
        return updates


class TCIBridgeServer:
    def __init__(self, cfg: BridgeConfig, rig: HamlibRigController, state: RadioState):
        self.cfg = cfg
        self.rig = rig
        self.state = state
        self.clients: Set[WebSocketServerProtocol] = set()
        self.audio_clients: Set[WebSocketServerProtocol] = set()
        self.tx_owner: Optional[WebSocketServerProtocol] = None
        self.rx_cnt = self.tx_cnt = 0
        self.tx_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=32)
        self.audio_dev_idx = self._resolve_audio_device(cfg.audio_device)

    @staticmethod
    def _resolve_audio_device(name_hint: Optional[str]):
        target = (name_hint or "usb audio codec").lower()
        if _ensure_sd() is None:
            return None
        try:
            sd._terminate()
        except Exception:
            pass
        try:
            sd._initialize()
        except Exception:
            pass
        in_idx = out_idx = None
        try:
            devs = sd.query_devices()
        except Exception:
            return None
        for idx, dev in enumerate(devs):
            name = dev["name"].lower()
            if "blackhole" not in name and target in name:
                if dev["max_input_channels"] > 0 and in_idx is None:
                    in_idx = idx
                if dev["max_output_channels"] > 0 and out_idx is None:
                    out_idx = idx
        if in_idx is not None and out_idx is not None:
            log.info(f"Selected audio devices: input=[{in_idx}], output=[{out_idx}]")
            return (in_idx, out_idx) if in_idx != out_idx else in_idx
        return None

    async def reload_config(self, new_cfg: BridgeConfig):
        self.cfg = new_cfg
        self.cfg.save()
        self.audio_dev_idx = self._resolve_audio_device(new_cfg.audio_device)
        await self.rig.stop()
        self.rig.cfg = new_cfg
        await self.rig.start()
        await self.broadcast_text(f"device:{self.cfg.rig_name} (TCI Bridge);")

    async def broadcast_text(self, msg: str):
        if self.clients:
            await asyncio.gather(*(ws.send(msg) for ws in self.clients), return_exceptions=True)

    async def broadcast_binary(self, payload: bytes):
        if self.clients:
            await asyncio.gather(*(ws.send(payload) for ws in self.clients), return_exceptions=True)

    def build_initial_sync_messages(self) -> list[str]:
        s = self.state
        return [
            "protocol:esdr,2.1;", f"device:{self.cfg.rig_name} (TCI Bridge);", "receive_only:false;",
            "trx_count:1;", "channel_count:2;", "channels_count:2;", "vfo_limits:30000,500000000;", "if_limits:-1500,1500;",
            "modulations_list:lsb,usb,am,cw,rtty,fm,cwr,rttyr,digl,digu;",
            f"audio_samplerate:{s.sample_rate};", "iq_samplerate:48000;",
            f"vfo:0,0,{s.vfo_a};", f"vfo:0,1,{s.vfo_b};", f"modulation:0,{s.mode};",
            "rx_enable:0,true;", "tx_enable:0,true;", f"drive:0,{s.drive};",
            "volume:0;", "mute:false;", "mon_volume:0;", "mon_enable:true;",
            f"trx:0,{str(s.tx).lower()};", f"tune:0,{str(s.tune).lower()};", "start;", "ready;",
        ]

    async def handle_text_command(self, raw_msg: str, ws: Optional[WebSocketServerProtocol] = None):
        log.info(f"TCI RX <- {raw_msg.strip()}")
        for item in raw_msg.strip().split(";"):
            if not (item := item.strip()):
                continue
            cmd, arg_str = item.split(":", 1) if ":" in item else (item, "")
            args = [a.strip() for a in arg_str.split(",")] if arg_str else []
            cmd = cmd.lower()
            if cmd == "vfo" and len(args) >= 3:
                rx, vfo, freq = int(args[0]), int(args[1]), int(float(args[2]))
                if rx == 0 and vfo == 0:
                    await self.rig.set_freq(freq)
                else:
                    self.state.vfo_b = freq
                await self.broadcast_text(f"vfo:{rx},{vfo},{freq};")
            elif cmd == "modulation" and len(args) >= 2:
                await self.rig.set_mode(args[1].lower())
                await self.broadcast_text(f"modulation:{int(args[0])},{self.state.mode};")
            elif cmd == "trx" and len(args) >= 2:
                on = args[1].lower() == "true"
                self.tx_owner = ws if on else None
                await self.rig.set_ptt(on)
                await self.broadcast_text(f"trx:{int(args[0])},{str(on).lower()};")
            elif cmd == "tune" and len(args) >= 2:
                on = args[1].lower() == "true"
                await self.rig.set_tune(on)
                await self.broadcast_text(f"tune:{int(args[0])},{str(on).lower()};")
            elif cmd == "drive" and len(args) >= 2:
                await self.rig.set_drive(int(float(args[1])))
                await self.broadcast_text(f"drive:{int(args[0])},{self.state.drive};")
            elif cmd == "audio_samplerate" and len(args) >= 1 and int(args[0]) in (8000, 12000, 24000, 48000):
                self.state.sample_rate = int(args[0])
                await self.broadcast_text(f"audio_samplerate:{self.state.sample_rate};")
            elif cmd == "audio_stream_sample_type":
                if args and args[0].lower() in ("int16", "float32"):
                    self.state.audio_sample_type = args[0].lower()
                await self.broadcast_text(f"audio_stream_sample_type:{self.state.audio_sample_type};")
            elif cmd == "audio_stream_samples":
                if args and args[0].isdigit():
                    self.state.audio_samples = max(100, min(2048, int(args[0])))
                await self.broadcast_text(f"audio_stream_samples:{self.state.audio_samples};")
            elif cmd == "split_enable" and len(args) >= 1:
                if args[0].lower() in ("true", "false"):
                    self.state.split = args[0].lower() == "true"
                    await self.broadcast_text(f"split_enable:0,{str(self.state.split).lower()};")
                else:
                    if len(args) >= 2:
                        self.state.split = args[1].lower() == "true"
                    await self.broadcast_text(f"split_enable:{int(args[0])},{str(self.state.split).lower()};")
            elif cmd == "dds" and len(args) >= 1:
                await self.broadcast_text(f"dds:{int(args[0])},{self.state.vfo_a};")
            elif cmd == "if" and len(args) >= 2:
                await self.broadcast_text(f"if:{int(args[0])},{int(args[1])},0;")
            elif cmd == "drive" and len(args) == 1:
                await self.broadcast_text(f"drive:{int(args[0])},{self.state.drive};")
            elif cmd == "tune" and len(args) == 1:
                await self.broadcast_text(f"tune:{int(args[0])},{str(self.state.tune).lower()};")
            elif cmd == "audio_start":
                if ws: self.audio_clients.add(ws)
                if ws: await ws.send("audio_start:0;")
            elif cmd == "audio_stop":
                if ws: self.audio_clients.discard(ws)
                if ws: await ws.send("audio_stop:0;")
            elif cmd in ("start", "stop"):
                await self.broadcast_text(f"{cmd};")

    def handle_binary_frame(self, payload: bytes, ws: Optional[WebSocketServerProtocol] = None):
        if self.tx_owner is not None and ws is not None and ws != self.tx_owner:
            return
        if len(payload) < TCI_HEADER_SIZE:
            return
        hdr = struct.unpack(TCI_HEADER_FMT, payload[:TCI_HEADER_SIZE])
        if hdr[6] != STREAM_TX_AUDIO:
            return
        bps = 4 if hdr[2] == FORMAT_FLOAT32 else (2 if hdr[2] == FORMAT_INT16 else 0)
        if not bps or hdr[5] == 0:
            return
        pcm = payload[TCI_HEADER_SIZE : TCI_HEADER_SIZE + hdr[5] * bps]
        samples = np.frombuffer(pcm, dtype="<f4") if hdr[2] == FORMAT_FLOAT32 else np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        is_stereo = hdr[7] == 2
        if len(samples) > 0:
            stereo = samples[:len(samples) - (len(samples) % 2)].reshape(-1, 2) if (is_stereo and len(samples) >= 2) else np.column_stack((samples, samples))
            try:
                self.tx_queue.put_nowait(stereo)
            except asyncio.QueueFull:
                pass

    async def ws_handler(self, ws: WebSocketServerProtocol):
        self.clients.add(ws)
        try:
            for line in self.build_initial_sync_messages():
                await ws.send(line)
            async for msg in ws:
                if isinstance(msg, str):
                    await self.handle_text_command(msg, ws)
                elif isinstance(msg, bytes):
                    self.handle_binary_frame(msg, ws)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            self.audio_clients.discard(ws)
            if self.tx_owner == ws:
                self.tx_owner = None
            if not self.clients and self.state.tx:
                await self.rig.set_ptt(False)

    async def rig_poll_loop(self):
        while True:
            if not self.rig.connected:
                await self.rig.start()
                await asyncio.sleep(3.0)
                continue
            for update in await self.rig.poll():
                await self.broadcast_text(update)
            await asyncio.sleep(0.35)

    async def _emit_rx_and_tx_chrono(self, blk: np.ndarray, sr: int):
        if not self.audio_clients:
            return
        nsamp = blk.shape[0] * 2
        is_i16 = self.state.audio_sample_type == "int16"
        fmt = FORMAT_INT16 if is_i16 else FORMAT_FLOAT32
        pcm = (np.clip(blk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes() if is_i16 else blk.astype("<f4").tobytes()
        hdr = struct.pack(TCI_HEADER_FMT, 0, sr, fmt, 0, 0, nsamp, STREAM_RX_AUDIO, 2, self.rx_cnt, 0, 0, 0, 0, 0, 0, 0)
        self.rx_cnt = (self.rx_cnt + 1) & 0xFFFFFFFF
        await asyncio.gather(*(c.send(hdr + pcm) for c in list(self.audio_clients)), return_exceptions=True)
        if self.state.tx and self.tx_owner:
            chdr = struct.pack(TCI_HEADER_FMT, 0, sr, fmt, 0, 0, nsamp, STREAM_TX_CHRONO, 2, self.tx_cnt, 0, 0, 0, 0, 0, 0, 0)
            self.tx_cnt = (self.tx_cnt + 1) & 0xFFFFFFFF
            await asyncio.gather(*(c.send(chdr + pcm) for c in list(self.audio_clients)), return_exceptions=True)

    async def _alsa_audio_step(self):
        sr = self.state.sample_rate
        blocksize = int(sr * 0.05)
        pcm_dev = _find_alsa_pcm(self.cfg.audio_device)
        if not pcm_dev or self.cfg.rig_model == 1:
            await asyncio.sleep(2.0)
            return
        rx_proc = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-D", pcm_dev, "-f", "S16_LE", "-r", str(sr), "-c", "2", "-t", "raw",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        tx_proc = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-D", pcm_dev, "--buffer-time=60000", "-f", "S16_LE", "-r", str(sr), "-c", "2", "-t", "raw",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        log.info(f"ALSA audio bridge running @ {sr} Hz on {pcm_dev}")
        nbytes = blocksize * 4
        silence = bytes(nbytes)
        tx_res = np.zeros((0, 2), dtype=np.float32)
        active_pref = self.cfg.audio_device
        try:
            while sr == self.state.sample_rate and active_pref == self.cfg.audio_device and rx_proc.stdout:
                raw = await asyncio.wait_for(rx_proc.stdout.readexactly(nbytes), timeout=2.0)
                blk = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768.0
                await self._emit_rx_and_tx_chrono(blk, sr)
                while len(tx_res) < blocksize:
                    try:
                        tx_res = np.vstack((tx_res, self.tx_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break
                if len(tx_res) >= blocksize:
                    out_blk, tx_res = tx_res[:blocksize], tx_res[blocksize:]
                    out_bytes = (np.clip(out_blk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    if not getattr(self, "_alsa_tx_logged", False):
                        self._alsa_tx_logged = True
                        log.info(f"ALSA TX audio streaming to {pcm_dev}")
                else:
                    out_bytes = silence
                if tx_proc.stdin:
                    tx_proc.stdin.write(out_bytes)
                    await tx_proc.stdin.drain()
        except Exception as e:
            log.warning(f"ALSA audio stream restarted: {e}")
        finally:
            for proc in (rx_proc, tx_proc):
                if proc.returncode is None:
                    proc.terminate()

    async def audio_stream_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            if _ensure_sd() is None:
                if platform.system() == "Linux":
                    await self._alsa_audio_step()
                else:
                    await asyncio.sleep(5.0)
                continue
            sr = self.state.sample_rate
            blocksize = int(sr * 0.05)
            if self.audio_dev_idx is None and self.cfg.rig_model != 1:
                self.audio_dev_idx = self._resolve_audio_device(self.cfg.audio_device)
                await asyncio.sleep(1.5)
                continue
            active_dev = self.audio_dev_idx
            rx_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=16)
            tx_res = np.zeros((0, 2), dtype=np.float32)

            def cb(indata, outdata, frames, _t, _st):
                nonlocal tx_res
                while len(tx_res) < frames:
                    try:
                        tx_res = np.vstack((tx_res, self.tx_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break
                if len(tx_res) >= frames:
                    outdata[:], tx_res = tx_res[:frames], tx_res[frames:]
                else:
                    outdata.fill(0.0)
                try:
                    loop.call_soon_threadsafe(rx_q.put_nowait, indata.copy())
                except asyncio.QueueFull:
                    pass

            kw = dict(samplerate=sr, blocksize=blocksize, channels=2, dtype="float32", callback=cb)
            if active_dev is not None:
                kw["device"] = active_dev
            try:
                with sd.Stream(**kw):
                    log.info(f"Audio bridge running @ {sr} Hz on device {active_dev}")
                    stale_ticks = 0
                    while self.audio_dev_idx == active_dev and sr == self.state.sample_rate:
                        try:
                            blk = await asyncio.wait_for(rx_q.get(), timeout=1.0)
                            stale_ticks = 0
                        except asyncio.TimeoutError:
                            stale_ticks += 1
                            if stale_ticks >= 3:
                                log.warning("CoreAudio stream stalled; reopening USB audio device...")
                                self.audio_dev_idx = None
                                break
                            continue
                        if not self.audio_clients:
                            continue
                        nsamp = blk.shape[0] * 2
                        is_i16 = self.state.audio_sample_type == "int16"
                        fmt = FORMAT_INT16 if is_i16 else FORMAT_FLOAT32
                        pcm = (np.clip(blk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes() if is_i16 else blk.astype("<f4").tobytes()
                        hdr = struct.pack(TCI_HEADER_FMT, 0, sr, fmt, 0, 0, nsamp, STREAM_RX_AUDIO, 2, self.rx_cnt, 0, 0, 0, 0, 0, 0, 0)
                        self.rx_cnt = (self.rx_cnt + 1) & 0xFFFFFFFF
                        await asyncio.gather(*(c.send(hdr + pcm) for c in list(self.audio_clients)), return_exceptions=True)
                        if self.state.tx and self.tx_owner:
                            chdr = struct.pack(TCI_HEADER_FMT, 0, sr, fmt, 0, 0, nsamp, STREAM_TX_CHRONO, 2, self.tx_cnt, 0, 0, 0, 0, 0, 0, 0)
                            self.tx_cnt = (self.tx_cnt + 1) & 0xFFFFFFFF
                            await asyncio.gather(self.tx_owner.send(chdr), return_exceptions=True)
            except Exception as e:
                log.warning(f"Audio paused ({e}); retrying...")
                self.audio_dev_idx = None
                await asyncio.sleep(1.5)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>TCI Bridge Setup</title>
<style>
  :root { color-scheme: dark; --bg: #0d1117; --card: #161b22; --accent: #2ea043; --blue: #38bdf8; --text: #f0f6fc; --muted: #8b949e; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 16px; }
  .wrap { max-width: 520px; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 4px; color: var(--blue); }
  .status { font-size: 0.85rem; color: var(--muted); margin-bottom: 16px; padding: 10px 12px; background: var(--card); border-radius: 10px; border: 1px solid #30363d; }
  .card { background: var(--card); border: 1px solid #30363d; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  label { display: block; font-size: 0.85rem; font-weight: 600; margin: 12px 0 6px; color: #c9d1d9; }
  label:first-child { margin-top: 0; }
  input, select { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #30363d; background: #0d1117; color: var(--text); font-size: 1rem; }
  button { width: 100%; padding: 13px; margin-top: 18px; border: none; border-radius: 10px; background: var(--accent); color: #fff; font-size: 1rem; font-weight: 700; cursor: pointer; }
  button:active { opacity: 0.85; }
  .toast { display: none; text-align: center; padding: 10px; margin-top: 12px; border-radius: 8px; background: rgba(46, 160, 67, 0.2); color: #7ee787; font-weight: 600; }
</style>
</head>
<body>
<div class="wrap">
  <h1>TCI Bridge Configuration</h1>
  <div class="status" id="status">Rig Status: __STATUS__</div>
  <form class="card" method="POST" action="/save">
    <label for="rig_search">Filter Radio Models</label>
    <input type="text" id="rig_search" placeholder="Type brand or model (e.g. IC-7300, FT-991A)..." oninput="filterRigs()">

    <label for="rig_model">Transceiver Model (Hamlib)</label>
    <select id="rig_model" name="rig_model" onchange="updateRigName()">
      __RIG_OPTIONS__
    </select>
    <input type="hidden" id="rig_name" name="rig_name" value="__RIG_NAME__">

    <label for="serial_port">Serial / CAT Port</label>
    <select id="serial_port" name="serial_port">
      __SERIAL_OPTIONS__
    </select>

    <label for="baud_rate">CAT Baud Rate</label>
    <select id="baud_rate" name="baud_rate">
      __BAUD_OPTIONS__
    </select>

    <label for="ptt_type">PTT Method</label>
    <select id="ptt_type" name="ptt_type">
      __PTT_OPTIONS__
    </select>

    <label for="audio_device">USB Audio Interface</label>
    <select id="audio_device" name="audio_device">
      __AUDIO_OPTIONS__
    </select>

    <button type="submit">Save &amp; Apply Settings</button>
    <div class="toast" id="toast">Settings saved and applied!</div>
  </form>
</div>
<script>
const allOptions = Array.from(document.getElementById('rig_model').options).map(o => ({value: o.value, text: o.text}));
function filterRigs() {
  const q = document.getElementById('rig_search').value.toLowerCase();
  const sel = document.getElementById('rig_model');
  const cur = sel.value;
  sel.innerHTML = '';
  for (const opt of allOptions) {
    if (!q || opt.text.toLowerCase().includes(q) || opt.value === cur) {
      const el = document.createElement('option');
      el.value = opt.value;
      el.text = opt.text;
      if (opt.value === cur) el.selected = true;
      sel.appendChild(el);
    }
  }
  updateRigName();
}
function updateRigName() {
  const sel = document.getElementById('rig_model');
  if (sel.selectedIndex >= 0) {
    document.getElementById('rig_name').value = sel.options[sel.selectedIndex].text.replace(/^\\[\\d+\\]\\s*/, '');
  }
}
if (window.location.search.includes('saved=1')) {
  document.getElementById('toast').style.display = 'block';
}
</script>
</body>
</html>"""


class WebConfigServer:
    def __init__(self, bridge: TCIBridgeServer, rigs: list[dict]):
        self.bridge = bridge
        self.rigs = rigs

    def render_page(self) -> str:
        cfg = self.bridge.cfg
        rig_opts = "\n".join(
            f'<option value="{r["id"]}" {"selected" if r["id"] == cfg.rig_model else ""}>[{r["id"]}] {r["mfg"]} {r["model"]}</option>'
            for r in self.rigs
        )
        ports = [p.device for p in serial.tools.list_ports.comports() if "Bluetooth" not in p.device]
        if cfg.serial_port and cfg.serial_port not in ports:
            ports.insert(0, cfg.serial_port)
        serial_opts = '<option value="">-- None / Dummy --</option>\n' + "\n".join(
            f'<option value="{p}" {"selected" if p == cfg.serial_port else ""}>{p}</option>' for p in ports
        )
        bauds = (4800, 9600, 19200, 38400, 57600, 115200)
        baud_opts = "\n".join(
            f'<option value="{b}" {"selected" if b == cfg.baud_rate else ""}>{b} bps</option>' for b in bauds
        )
        ptts = [("RIG", "CAT Command (RIG)"), ("RTS", "RTS Line"), ("DTR", "DTR Line"), ("NONE", "VOX / None")]
        ptt_opts = "\n".join(
            f'<option value="{k}" {"selected" if k == cfg.ptt_type else ""}>{label}</option>' for k, label in ptts
        )
        if _ensure_sd() is not None:
            sd._terminate()
            sd._initialize()
        audio_names = []
        if _ensure_sd() is not None:
            try:
                audio_names = sorted({d["name"] for d in sd.query_devices() if "BlackHole" not in d["name"]})
            except Exception:
                audio_names = []
        elif platform.system() == "Linux" and shutil.which("arecord"):
            try:
                out = subprocess.check_output(["arecord", "-l"], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    m = re.match(r"^card\s+(\d+):\s*(\S+)\s+\[([^\]]+)\],\s*device\s+(\d+):", line)
                    if m:
                        audio_names.append(f"{m.group(3)} (plughw:{m.group(2)},{m.group(4)})")
            except Exception:
                pass
        audio_opts = '<option value="">Auto-detect ("USB Audio CODEC")</option>\n' + "\n".join(
            f'<option value="{n}" {"selected" if n == cfg.audio_device else ""}>{n}</option>' for n in audio_names
        )

        page = HTML_TEMPLATE
        page = page.replace("__STATUS__", self.bridge.rig.status_text)
        page = page.replace("__RIG_OPTIONS__", rig_opts)
        page = page.replace("__RIG_NAME__", cfg.rig_name)
        page = page.replace("__SERIAL_OPTIONS__", serial_opts)
        page = page.replace("__BAUD_OPTIONS__", baud_opts)
        page = page.replace("__PTT_OPTIONS__", ptt_opts)
        page = page.replace("__AUDIO_OPTIONS__", audio_opts)
        return page

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not req_line:
                return
            parts = req_line.decode(errors="ignore").split()
            method = parts[0] if len(parts) >= 1 else "GET"
            path = parts[1] if len(parts) >= 2 else "/"
            headers = {}
            while True:
                hline = await reader.readline()
                if not hline or hline == b"\r\n":
                    break
                if b":" in hline:
                    k, v = hline.decode(errors="ignore").split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if method == "POST" and path.startswith("/save"):
                length = int(headers.get("content-length", "0"))
                body = (await reader.readexactly(length)).decode(errors="ignore")
                form = urllib.parse.parse_qs(body)
                new_cfg = BridgeConfig(
                    rig_model=int(form.get("rig_model", ["1"])[0]),
                    rig_name=form.get("rig_name", ["Hamlib Dummy"])[0],
                    serial_port=form.get("serial_port", [""])[0],
                    baud_rate=int(form.get("baud_rate", ["38400"])[0]),
                    ptt_type=form.get("ptt_type", ["RIG"])[0],
                    audio_device=form.get("audio_device", [""])[0],
                    rigctld_port=self.bridge.cfg.rigctld_port,
                )
                await self.bridge.reload_config(new_cfg)
                resp = "HTTP/1.1 303 See Other\r\nLocation: /?saved=1\r\nContent-Length: 0\r\n\r\n"
                writer.write(resp.encode())
            elif path.startswith("/api/status"):
                payload = json.dumps({"status": self.bridge.rig.status_text, "config": asdict(self.bridge.cfg)}).encode()
                hdr = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
                writer.write(hdr.encode() + payload)
            else:
                html = self.render_page().encode()
                hdr = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(html)}\r\n\r\n"
                writer.write(hdr.encode() + html)
            await writer.drain()
        except Exception:
            log.exception("Web UI error")
        finally:
            writer.close()


def start_mdns_advertiser(rig_name: str, tci_port: int, web_port: int) -> Optional[subprocess.Popen]:
    host = platform.node().split(".")[0] or "Mac"
    svc = f"TCI Bridge ({host})"
    if platform.system() == "Darwin" and shutil.which("dns-sd"):
        cmd = ["dns-sd", "-R", svc, "_tci._tcp", "local", str(tci_port), f"rig={rig_name}", f"webport={web_port}"]
    elif shutil.which("avahi-publish-service"):
        cmd = ["avahi-publish-service", svc, "_tci._tcp", str(tci_port), f"rig={rig_name}", f"webport={web_port}"]
    else:
        return None
    log.info(f"Advertising mDNS: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main():
    p = argparse.ArgumentParser(description="Hamlib USB to TCI 2.1 Bridge")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=50001)
    p.add_argument("--web-port", type=int, default=58080)
    args = p.parse_args()

    cfg = BridgeConfig.load()
    rigs = list_hamlib_rigs()
    state = RadioState()
    rig = HamlibRigController(cfg, state)
    bridge = TCIBridgeServer(cfg, rig, state)
    web = WebConfigServer(bridge, rigs)

    http_server = await asyncio.start_server(web.handle_client, args.host, args.web_port)
    log.info(f"Web Config UI listening on http://{args.host}:{args.web_port}")

    mdns = start_mdns_advertiser(cfg.rig_name, args.port, args.web_port)
    try:
        async with websockets.serve(bridge.ws_handler, args.host, args.port), http_server:
            log.info(f"TCI 2.1 Server listening on ws://{args.host}:{args.port}")
            await asyncio.gather(bridge.rig_poll_loop(), bridge.audio_stream_loop())
    finally:
        if mdns and mdns.poll() is None:
            mdns.terminate()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    asyncio.run(main())
