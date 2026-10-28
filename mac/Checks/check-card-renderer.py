#!/usr/bin/env python3
"""Compile the real core + SwiftUI card sources, without app/WebRTC dependencies.
This is a card-renderer build check, NOT a complete SpeakeasyClient/app build.
"""
from pathlib import Path
import os
import subprocess
import tempfile

mac = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='speakeasy-card-check-', dir=os.environ.get('TMPDIR')) as directory:
    package = Path(directory)
    (package / 'Sources').mkdir()
    (package / 'Sources/SpeakeasyCore').symlink_to(mac / 'Sources/SpeakeasyCore', target_is_directory=True)
    (package / 'Sources/SpeakeasyClient').symlink_to(mac / 'Sources/SpeakeasyClient/Cards', target_is_directory=True)
    (package / 'Package.swift').write_text('''// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "CardRendererCheck", platforms: [.macOS(.v14)], targets: [
    .target(name: "SpeakeasyCore"),
    .target(name: "SpeakeasyClient", dependencies: ["SpeakeasyCore"])
], swiftLanguageModes: [.v5])
''')
    result = subprocess.run(['swift', 'build', '--package-path', str(package), '--target', 'SpeakeasyClient'])
    raise SystemExit(result.returncode)
