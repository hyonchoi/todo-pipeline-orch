#!/usr/bin/env bash
# Lane F.4: Idempotent 5-minute cron registration.
#
# Registers pipeline-watch auto to run every 5 minutes via crontab.
# Idempotent: re-running doesn't duplicate the cron entry.
#
# Usage:
#   bash scripts/install-cron.sh

set -euo pipefail

# Define the cron line
CRON_LINE="*/5 * * * * /usr/bin/env -i HOME=$HOME PATH=$PATH bash -lc 'pipeline-watch auto' >> $HOME/.hermes/cron.log 2>&1"

# Get current crontab (if any) and remove any existing pipeline-watch auto entries
# Then add the new entry
(crontab -l 2>/dev/null | grep -v 'pipeline-watch auto' ; echo "$CRON_LINE") | crontab -

echo "Cron entry installed successfully"
echo "  Cron line: $CRON_LINE"
echo "  Log file: $HOME/.hermes/cron.log"
