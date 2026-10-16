# ==============================================================================
# ClaimShield AI -- Complete 1-Command Automated Pipeline (PowerShell)
# 2026 8th FinTechathon (Topic D: Insurance Assistant)
# ==============================================================================

$ErrorActionPreference = "Stop"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  ClaimShield AI -- 2026 8th FinTechathon Autonomous Verification" -ForegroundColor Cyan
Write-Host "  Theme: AI as a Financial Participant" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# Determine Python paths
if (Test-Path ".venv\Scripts\python.exe") {
    $PYTHON = Resolve-Path ".venv\Scripts\python.exe"
    $PYTEST = Resolve-Path ".venv\Scripts\pytest.exe"
} else {
    $PYTHON = "python"
    $PYTEST = "pytest"
}

Write-Host "[1/4] Generating synthetic insurance policies and hospital bill PDFs..." -ForegroundColor Yellow
& $PYTHON data_generator\generate_synthetic_pdfs.py

Write-Host "[2/4] Running automated test suite and generating Deliverable #06 evidence..." -ForegroundColor Yellow
& $PYTEST -v

Write-Host "[3/4] Generating Deliverable #02 12-slide Pitch Deck (.pptx)..." -ForegroundColor Yellow
& $PYTHON deliverables\02_presentation_deck\generate_slides.py

Write-Host "[4/4] Verifying all 6 competition deliverables..." -ForegroundColor Yellow
$deliverables = @(
    "deliverables\01_technical_documentation\Technical_Documentation.md",
    "deliverables\02_presentation_deck\ClaimShield_AI_Pitch_Deck.pptx",
    "deliverables\03_demo_video\Demo_Walkthrough_Script.md",
    "README.md",
    "deliverables\05_security_self_assessment\Security_Self_Assessment_Report.md",
    "deliverables\06_execution_evidence\sandbox_operation_logs.jsonl",
    "deliverables\06_execution_evidence\Execution_Evidence_Summary.md"
)

foreach ($f in $deliverables) {
    if (Test-Path $f) {
        $len = (Get-Item $f).Length
        Write-Host "  [OK] Found $f ($len bytes)" -ForegroundColor Green
    } else {
        Write-Host "  [ERROR] Missing $f" -ForegroundColor Red
        exit 1
    }
}

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  ALL 6 DELIVERABLES VERIFIED & READY FOR SUBMISSION!" -ForegroundColor Green
Write-Host "  To launch the interactive dashboard, run:" -ForegroundColor White
Write-Host "    .\.venv\Scripts\streamlit.exe run frontend\app.py" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
