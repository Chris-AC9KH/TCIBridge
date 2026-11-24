#!/bin/zsh
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
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

SCRIPT_DIR="${0:A:h}"
APP_NAME="TCIBridge.app"
APP_DIR="$SCRIPT_DIR/$APP_NAME"
PY_SRC="$SCRIPT_DIR/TCI-bridge.py"
BUNDLE_ID="com.hamshack.TCIBridge"

# Set your Developer ID Application identity & notarytool keychain profile here (or override via env vars)
DEV_ID="${DEV_ID:-$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/{print $2; exit}')}"
NOTARY_PROFILE="${NOTARY_PROFILE:-notarytool-profile}"

RELEASE_BUILD=""
for arg in "$@"; do
    case "$arg" in
        --release|-r) RELEASE_BUILD=true ;;
        --dev|-d) RELEASE_BUILD=false ;;
        --help|-h)
            echo "Usage: ./Mac_build_TCIBridge.sh [--release|--dev]"
            echo "  Env vars: DEV_ID='Developer ID Application: ...' NOTARY_PROFILE='notarytool-profile'"
            exit 0
            ;;
    esac
done

if [[ -z "$RELEASE_BUILD" ]]; then
    read -r "REPLY?Release build (sign & send to Apple for notarization)? [y/N]: "
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        RELEASE_BUILD=true
    else
        RELEASE_BUILD=false
    fi
fi

if [[ ! -f "$PY_SRC" ]]; then
    echo "Error: $PY_SRC not found." >&2
    exit 1
fi

if [[ -z "$DEV_ID" ]]; then
    echo "Error: No 'Developer ID Application' certificate found in keychain." >&2
    exit 1
fi

echo "==> Building $APP_NAME (Release/Notarize: $RELEASE_BUILD)"
echo "==> Signing Identity: $DEV_ID"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

cp "$PY_SRC" "$APP_DIR/Contents/Resources/TCI-bridge.py"
[[ -f "$SCRIPT_DIR/LICENSE" ]] && cp "$SCRIPT_DIR/LICENSE" "$APP_DIR/Contents/Resources/LICENSE.txt"
[[ -f "$SCRIPT_DIR/THIRD-PARTY-LICENSES.txt" ]] && cp "$SCRIPT_DIR/THIRD-PARTY-LICENSES.txt" "$APP_DIR/Contents/Resources/THIRD-PARTY-LICENSES.txt"
mkdir -p "$APP_DIR/Contents/Resources/hamlib/lib"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

dest="$APP_DIR/Contents/Resources/python"
uv python install --no-bin "cpython-3.12-macos-x86_64" --install-dir "$TMP_DIR/uv-x86_64"
cp -RL "$(find "$TMP_DIR/uv-x86_64" -maxdepth 1 -name "cpython-*" | head -n 1)" "$dest"
uv pip install --system --break-system-packages --python "$dest/bin/python3" --python-platform "x86_64-apple-darwin" --only-binary=:all: --no-cache numpy pyserial sounddevice websockets
rm -rf "$dest/include" "$dest/share" "$dest/lib/python3.12/test"
find "$dest" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

HDEST="$APP_DIR/Contents/Resources/hamlib"
for pkg in hamlib libusb libusb-compat libtool; do
    pdir="/usr/local/opt/$pkg"
    [[ -d "$pdir" ]] || continue
    for b in rigctld rigctl; do
        [[ -f "$pdir/bin/$b" ]] && cp "$pdir/bin/$b" "$HDEST/$b" && chmod +x "$HDEST/$b"
    done
    [[ -d "$pdir/lib" ]] && cp -P "$pdir"/lib/*.dylib "$HDEST/lib/" 2>/dev/null || true
done

for f in "$HDEST/rigctld" "$HDEST/rigctl" "$HDEST"/lib/*.dylib; do
    [[ -L "$f" ]] && continue; chmod u+w "$f"
    [[ "$f" == *.dylib ]] && install_name_tool -id "@loader_path/$(basename "$f")" "$f"
    otool -L "$f" | awk 'NR>1 {print $1}' | grep -E '^(/opt/homebrew|/usr/local|@@HOMEBREW)' | while read -r dep; do
        db="$(basename "$dep")"
        if [[ -e "$HDEST/lib/$db" ]]; then
            [[ "$f" == *.dylib ]] && pfx="@loader_path" || pfx="@executable_path/lib"
            install_name_tool -change "$dep" "$pfx/$db" "$f"
        fi
    done
done

cat << 'PLIST' > "$APP_DIR/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://apple.com">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>TCIBridge</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.hamshack.TCIBridge</string>
    <key>CFBundleName</key>
    <string>TCI Bridge</string>
    <key>CFBundleDisplayName</key>
    <string>TCI Bridge</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.1</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>TCI Bridge accepts local network connections from JS8Call-Mobile for rig control, audio streaming, and web configuration.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>TCI Bridge requires audio input access to stream receiver audio from your USB transceiver over TCI.</string>
</dict>
</plist>
PLIST


swift - "$TMP_DIR/icon.png" << 'SWIFT_ICON'
import AppKit
let outPath = CommandLine.arguments.last!
let size = NSSize(width: 1024, height: 1024)
let image = NSImage(size: size)
image.lockFocus()
let inset = NSRect(origin: .zero, size: size).insetBy(dx: 48, dy: 48)
let clip = NSBezierPath(roundedRect: inset, xRadius: 210, yRadius: 210)
clip.addClip()
NSGradient(colors: [
    NSColor(calibratedRed: 0.06, green: 0.12, blue: 0.22, alpha: 1.0),
    NSColor(calibratedRed: 0.03, green: 0.06, blue: 0.13, alpha: 1.0)
])!.draw(in: inset, angle: -90)
NSColor(calibratedRed: 0.18, green: 0.78, blue: 0.95, alpha: 0.35).setStroke()
clip.lineWidth = 10
clip.stroke()
let bars: [CGFloat] = (0..<12).map { i in CGFloat(110 + 170 * abs(sin(Double(i) * 0.65 + 0.4))) }
let bw: CGFloat = 34, sp: CGFloat = 18
let sx = (1024 - (CGFloat(bars.count) * bw + CGFloat(bars.count - 1) * sp)) / 2.0
for (i, h) in bars.enumerated() {
    let r = NSRect(x: sx + CGFloat(i) * (bw + sp), y: 535, width: bw, height: h)
    NSColor(calibratedRed: 0.15, green: 0.85, blue: 0.98, alpha: 0.55 + 0.45 * (h / 290.0)).setFill()
    NSBezierPath(roundedRect: r, xRadius: 12, yRadius: 12).fill()
}
let p = NSMutableParagraphStyle(); p.alignment = .center
("TCI" as NSString).draw(in: NSRect(x: 0, y: 270, width: 1024, height: 240), withAttributes: [
    .font: NSFont.systemFont(ofSize: 215, weight: .heavy), .foregroundColor: NSColor.white, .paragraphStyle: p
])
("SERVER" as NSString).draw(in: NSRect(x: 0, y: 155, width: 1024, height: 130), withAttributes: [
    .font: NSFont.systemFont(ofSize: 105, weight: .bold),
    .foregroundColor: NSColor(calibratedRed: 0.25, green: 0.88, blue: 1.0, alpha: 1.0),
    .paragraphStyle: p, .kern: 8.0
])
image.unlockFocus()
let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: outPath))
SWIFT_ICON

mkdir -p "$TMP_DIR/AppIcon.iconset"
for sz in 16 32 128 256 512; do
    sips -z $sz $sz "$TMP_DIR/icon.png" --out "$TMP_DIR/AppIcon.iconset/icon_${sz}x${sz}.png" >/dev/null
    dbl=$((sz * 2))
    sips -z $dbl $dbl "$TMP_DIR/icon.png" --out "$TMP_DIR/AppIcon.iconset/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$TMP_DIR/AppIcon.iconset" -o "$APP_DIR/Contents/Resources/AppIcon.icns"

cat << 'SWIFT_MAIN' > "$TMP_DIR/Launcher.swift"
import Cocoa
class AppDelegate: NSObject, NSApplicationDelegate {
    var pythonProcess: Process?
    func applicationDidFinishLaunching(_ notification: Notification) {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit TCI Bridge", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
        guard let scriptPath = Bundle.main.path(forResource: "TCI-bridge", ofType: "py"),
              let resPath = Bundle.main.resourcePath else {
            NSApp.terminate(nil); return
        }
        let bundledPy = URL(fileURLWithPath: resPath).appendingPathComponent("python/bin/python3")
        let logDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/TCIBridge")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let logURL = logDir.appendingPathComponent("tci-bridge.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let proc = Process()
        proc.executableURL = bundledPy
        proc.arguments = ["-s", scriptPath, "--host", "0.0.0.0", "--port", "50001"]
        if let h = try? FileHandle(forWritingTo: logURL) {
            h.seekToEndOfFile(); proc.standardOutput = h; proc.standardError = h
        }
        proc.terminationHandler = { _ in DispatchQueue.main.async { NSApp.terminate(nil) } }
        do { try proc.run(); self.pythonProcess = proc } catch { NSApp.terminate(nil) }
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let p = pythonProcess, p.isRunning { p.terminate() }
        return .terminateNow
    }
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
SWIFT_MAIN

swiftc -target x86_64-apple-macos12.0 "$TMP_DIR/Launcher.swift" -o "$APP_DIR/Contents/MacOS/TCIBridge"

cat << 'ENT' > "$TMP_DIR/entitlements.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://apple.com">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.audio-input</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
</dict>
</plist>
ENT

# Sign inside-out (never --deep): binary first with hardened runtime + audio-input entitlement, then bundle
find "$APP_DIR/Contents/Resources" -type f ! -lname '*' | while read -r bin; do
    if file -b "$bin" | grep -q "Mach-O"; then
        chmod u+w "$bin"
        codesign --force --options runtime --timestamp --entitlements "$TMP_DIR/entitlements.plist" --sign "$DEV_ID" "$bin"
    fi
done
codesign --force --options runtime --timestamp --entitlements "$TMP_DIR/entitlements.plist" --sign "$DEV_ID" "$APP_DIR/Contents/MacOS/TCIBridge"
codesign --force --options runtime --timestamp --entitlements "$TMP_DIR/entitlements.plist" --sign "$DEV_ID" "$APP_DIR"
codesign --verify --strict --verbose=2 "$APP_DIR"

if [[ "$RELEASE_BUILD" == true ]]; then
    if ! command -v create-dmg >/dev/null 2>&1; then
        echo "Error: create-dmg not found (brew install create-dmg)." >&2
        exit 1
    fi

    # 1. Zip, notarize, and staple the .app bundle first
    ZIP_PATH="$SCRIPT_DIR/TCIBridge-macOS.zip"
    rm -f "$ZIP_PATH"
    ditto -c -k --keepParent "$APP_DIR" "$ZIP_PATH"

    echo "==> Submitting $APP_NAME for Apple notarization (profile: $NOTARY_PROFILE)..."
    xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP_DIR"
    spctl --assess --type execute --verbose=2 "$APP_DIR"
    rm -f "$ZIP_PATH"
    
    printf "review results then press Enter"
    read dummy </dev/tty

    # 2. Create and sign the .dmg containing the notarized/stapled .app
    VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DIR/Contents/Info.plist")"
    RAW_DMG="$SCRIPT_DIR/TCI Bridge ${VERSION}.dmg"
    FINAL_DMG="$SCRIPT_DIR/TCIBridge-${VERSION}-macOS-intel.dmg"
    rm -f "$RAW_DMG" "$FINAL_DMG"

    if [[ -f "$SCRIPT_DIR/LICENSE" ]]; then
        cp "$SCRIPT_DIR/LICENSE" "$SCRIPT_DIR/license.txt"
    fi

    echo "==> Creating DMG installer..."
    create-dmg --overwrite --dmg-title="TCI Bridge ${VERSION}" "$APP_DIR" "$SCRIPT_DIR"
    rm -f "$SCRIPT_DIR/license.txt"

    if [[ -f "$RAW_DMG" ]]; then
        mv "$RAW_DMG" "$FINAL_DMG"
    fi

    # 3. Notarize and staple the .dmg itself
    echo "==> Submitting $FINAL_DMG for Apple notarization (profile: $NOTARY_PROFILE)..."
    xcrun notarytool submit "$FINAL_DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$FINAL_DMG"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$FINAL_DMG"

    echo "==> DONE! Notarized DMG ready at: $FINAL_DMG"
else
    echo "==> Dev/local Developer ID signed build ready at: $APP_DIR"
fi
