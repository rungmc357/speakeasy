#!/usr/bin/env python3
"""CLT-only assertion smoke check; not a substitute for the XCTest suite."""
from pathlib import Path
import subprocess

mac = Path(__file__).resolve().parents[1]
sources = [mac / 'Sources/SpeakeasyCore/ViewCards.swift',
           mac / 'Sources/SpeakeasyCore/CardExpansion.swift',
           mac / 'Checks/CardDrilldownChecks.swift']
result = subprocess.run(['swift', '-'], input='\n'.join(p.read_text() for p in sources), text=True)
raise SystemExit(result.returncode)
