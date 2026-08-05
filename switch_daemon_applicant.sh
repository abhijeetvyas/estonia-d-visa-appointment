#!/bin/bash
# Switches which applicant the SINGLE booker daemon (estonia-booker-daemon
# systemd service) is hunting/booking for, then restarts it. Only one daemon
# process ever runs - this does not start a second one. See BOOKER_README.md
# ("Switching the daemon between applicants") for the full explanation.
#
# Usage (run from ~/estonia-d-visa-appointment on the VM):
#   ./switch_daemon_applicant.sh applicant1
#   ./switch_daemon_applicant.sh applicant2
#   ./switch_daemon_applicant.sh applicant1 --clear-state
#
# --clear-state additionally deletes that profile's stored "booked" record
# (.state/booker_state_40[_profile].json) before restarting - use this ONLY
# when the real appointment for that profile has actually been cancelled on
# the site, so the daemon is told, correctly, that nothing is booked and
# should hunt for the earliest slot again. Never run --clear-state for a
# profile whose appointment is still live - the daemon has no way to know
# you gave it up otherwise, and would stop looking for anything better.

set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${1:-}"
CLEAR_STATE=false
if [[ "${2:-}" == "--clear-state" ]]; then
  CLEAR_STATE=true
fi

if [[ "$PROFILE" != "applicant1" && "$PROFILE" != "applicant2" ]]; then
  echo "Usage: $0 applicant1|applicant2 [--clear-state]" >&2
  exit 1
fi

ENV_FILE="run_booker_daemon_env.sh"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: $ENV_FILE not found in $(pwd)." >&2
  echo "This script must be run from ~/estonia-d-visa-appointment on the VM." >&2
  exit 1
fi

EMBASSY_ID="${EMBASSY_ID:-40}"
MARK_START="# --- switch_daemon_applicant.sh managed block (do not edit by hand) ---"
MARK_END="# --- end switch_daemon_applicant.sh managed block ---"

# Remove any block this script previously appended, so re-running it (with a
# different profile) doesn't stack up old lines. Only ever touches lines
# between these two markers - every other line in this file (Telegram,
# CapSolver, Twilio secrets, etc.) is left completely alone.
if grep -qF "$MARK_START" "$ENV_FILE"; then
  sed -i.bak "/$MARK_START/,/$MARK_END/d" "$ENV_FILE"
fi

{
  echo "$MARK_START"
  if [[ "$PROFILE" == "applicant1" ]]; then
    echo "unset BOOKING_PROFILE"
    echo "export APPLICANT_DATA_REAL=\"$(pwd)/applicant_data_real.json\""
    STATE_FILE=".state/booker_state_${EMBASSY_ID}.json"
  else
    echo "export BOOKING_PROFILE=applicant2"
    echo "export APPLICANT_DATA_REAL=\"$(pwd)/applicant_data_real_2.json\""
    STATE_FILE=".state/booker_state_${EMBASSY_ID}_applicant2.json"
  fi
  echo "$MARK_END"
} >> "$ENV_FILE"

echo "Active profile set to: $PROFILE"

if [[ "$CLEAR_STATE" == true ]]; then
  if [[ -f "$STATE_FILE" ]]; then
    rm -f "$STATE_FILE"
    echo "Cleared stored booking record: $STATE_FILE"
  else
    echo "No stored booking record found at $STATE_FILE (already clear)."
  fi
fi

echo "Restarting estonia-booker-daemon..."
sudo systemctl restart estonia-booker-daemon
sleep 2
sudo systemctl status estonia-booker-daemon --no-pager | head -10

LOG_FILE="daemon.log"
if [[ "$PROFILE" == "applicant2" ]]; then
  LOG_FILE="daemon_applicant2.log"
fi
echo
echo "Tail of $LOG_FILE (confirm profile/applicant_data on the startup line):"
tail -5 "$LOG_FILE" 2>/dev/null || echo "(log not written yet - give it a second and check again)"
