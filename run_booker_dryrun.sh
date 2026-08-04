#!/bin/bash
# Usage: ./run_booker_dryrun.sh [1|2|3] [book]
#   1 = Abu Dhabi (244)
#   2 = Cairo (45)
#   3 = Astana (49)
#   book (optional 2nd arg) = also submit + delete a real test booking
#        (default, if omitted, is preview-only - stops before final submit)
set -euo pipefail
OPTION="${1:-1}"
MODE="${2:-preview}"

case "$OPTION" in
  1)
    export EMBASSY_ID=244
    export EMBASSY_NAME="Abu Dhabi"
    ;;
  2)
    export EMBASSY_ID=45
    export EMBASSY_NAME="Cairo"
    ;;
  3)
    export EMBASSY_ID=49
    export EMBASSY_NAME="Astana"
    ;;
  *)
    echo "Usage: $0 [1|2|3] [book]"
    echo "  1 = Abu Dhabi (244)"
    echo "  2 = Cairo (45)"
    echo "  3 = Astana (49)"
    echo "  book (optional 2nd arg) = also submit + delete a real test booking"
    exit 1
    ;;
esac

case "$MODE" in
  preview)
    unset DRY_RUN_TEST_SUBMIT || true
    ;;
  book)
    export DRY_RUN_TEST_SUBMIT=1
    ;;
  *)
    echo "Usage: $0 [1|2|3] [book]"
    echo "  book (optional 2nd arg) = also submit + delete a real test booking"
    echo "  omit 2nd arg for preview-only (stops before final submit)"
    exit 1
    ;;
esac

# --- secrets ---
export TELEGRAM_BOT_TOKEN=
export TELEGRAM_CHAT_ID=
export TWOCAPTCHA_API_KEY=
export CAPSOLVER_API_KEY=
export REQUESTS_CA_BUNDLE="/home/abhijeetvyas13/estonia-d-visa-appointment/combined_ca_bundle.pem"
export CAPTCHA_PROVIDER=race

# --- dry-run flags ---
export DRY_RUN=1
export APPLICANT_DATA_DUMMY="/home/abhijeetvyas13/estonia-d-visa-appointment/applicant_data_dummy.json"
echo "=== DRY RUN ==="
echo "Embassy: $EMBASSY_NAME ($EMBASSY_ID)"
if [[ "${DRY_RUN_TEST_SUBMIT:-}" == "1" ]]; then
  echo "Mode: FULL (will submit + delete a real test booking)"
else
  echo "Mode: PREVIEW ONLY (stops before final submit)"
fi
echo "================"
cd /home/abhijeetvyas13/estonia-d-visa-appointment
/home/abhijeetvyas13/estonia-d-visa-appointment/venv/bin/python3 estonia_d_visa_booker.py
