#!/usr/bin/env bash
# ==============================================================================
# ClaimShield AI — Complete 1-Command Automated Pipeline
# 2026 8th FinTechathon (Topic D: Insurance Assistant)
# ==============================================================================

set -e

echo "======================================================================"
echo "  ClaimShield AI — 2026 8th FinTechathon Autonomous Verification"
echo "  Theme: AI as a Financial Participant"
echo "======================================================================"

# Determine Python command
if [ -d ".venv" ]; then
    if [ -f ".venv/bin/python" ]; then
        PYTHON=".venv/bin/python"
        PYTEST=".venv/bin/pytest"
    else
        PYTHON=".venv/Scripts/python.exe"
        PYTEST=".venv/Scripts/pytest.exe"
    fi
else
    PYTHON="python3"
    PYTEST="pytest"
fi

echo "[1/4] Generating synthetic insurance policies and hospital bill PDFs..."
$PYTHON data_generator/generate_synthetic_pdfs.py

echo "[2/4] Running automated test suite and generating Deliverable #06 evidence..."
$PYTEST -v

echo "[3/4] Generating Deliverable #02 12-slide Pitch Deck (.pptx)..."
$PYTHON deliverables/02_presentation_deck/generate_slides.py

echo "[4/4] Verifying all 6 competition deliverables..."
for f in \
    "deliverables/01_technical_documentation/Technical_Documentation.md" \
    "deliverables/02_presentation_deck/ClaimShield_AI_Pitch_Deck.pptx" \
    "deliverables/03_demo_video/Demo_Walkthrough_Script.md" \
    "README.md" \
    "deliverables/05_security_self_assessment/Security_Self_Assessment_Report.md" \
    "deliverables/06_execution_evidence/sandbox_operation_logs.jsonl" \
    "deliverables/06_execution_evidence/Execution_Evidence_Summary.md"; do
    if [ -f "$f" ]; then
        echo "  [OK] Found $f"
    else
        echo "  [ERROR] Missing $f"
        exit 1
    fi
done

echo "======================================================================"
echo "  ALL 6 DELIVERABLES VERIFIED & READY FOR SUBMISSION!"
echo "  To launch the interactive dashboard, run:"
echo "    streamlit run frontend/app.py"
echo "======================================================================"
