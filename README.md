# USOS Calendar Filter

[![CI](https://github.com/svb-gh/usos-calendar-filter/actions/workflows/ci.yml/badge.svg)](https://github.com/svb-gh/usos-calendar-filter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small filter for USOS iCalendar feeds. It downloads the source feed, cleans up
locations and descriptions, applies YAML rules, adds custom events, and atomically
publishes the resulting `.ics` file.

## Installation

Python 3.10 or newer is required.

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install -e .
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Copy `.env.example` to `.env` and set your private USOS feed URL:

```dotenv
USOS_ICAL_URL="https://apps.usos.pw.edu.pl/services/tt/upcoming_ical?..."
OUTPUT_ICS_PATH="/var/www/calendar/cleaned_schedule.ics"
HISTORY_ICS_PATH="/var/lib/uni-schedule/history.ics"
RULES_FILE_PATH="rules.yaml"
SUMMER_BREAK_SEARCH_START="01-20"
SUMMER_SEMESTER_HARD_START="03-15"
MIN_SEMESTER_BREAK_DAYS="14"
```

Copy `rules.example.yaml` to `rules.yaml`, then customize the new file. The `.env`,
`rules.yaml`, and all `.ics` files are ignored by Git. Never publish the access key
contained in the USOS URL or your private schedule rules. A missing rules file stops
the refresh so the application never publishes an accidentally unfiltered calendar.

### Keeping rules in a private repository

If you want to edit your rules from multiple machines, keep them in a separate private
Git repository. Clone that repository into the ignored `config.local` directory and
point the application to it:

```bash
git clone git@github.com:YOUR_ACCOUNT/usos-calendar-filter-config.git config.local
```

```dotenv
RULES_FILE_PATH="config.local/rules.yaml"
```

The private configuration repository stays independent from this public source-code
repository, so changes to either one are committed and pushed separately.

## Usage

```bash
usos-calendar-filter
```

Alternatively:

```bash
python clean_schedule.py
```

The program retries transient download failures and writes the output through a
temporary file, ensuring calendar clients never receive a partially written feed.

## Calendar history

USOS may stop returning events after they finish. On each refresh, the program reads
the private state file configured by `HISTORY_ICS_PATH` and preserves completed events
from the current semester. Older events are removed automatically. The state file and
the public calendar are both replaced atomically.

The winter semester always starts on October 1. For the summer semester, the program
looks for breaks of at least `MIN_SEMESTER_BREAK_DAYS` between USOS events, beginning
at `SUMMER_BREAK_SEARCH_START`. It selects the longest qualifying break only when
regular activity follows, ignoring custom events during detection. If multiple breaks
have the same length, the later one wins. The detected date is stored inside the
private history calendar and reused on subsequent runs.

If no reliable break is detected by `SUMMER_SEMESTER_HARD_START`, that date becomes the
semester boundary. The defaults search from January 20, require a 14-day break, and
force the transition on March 15. All recurring boundaries use `MM-DD` format.

On the first run after upgrading, if the history file does not exist, the program
imports available history from the previous public output file. Events that are absent
from both USOS and that output file cannot be recovered. Keep `HISTORY_ICS_PATH` on a
persistent disk or volume.

## Rules

```yaml
templates:
  office_hours:
    title: "Office hours"
    location: "room 101"
    start_time: "12:00"
    end_time: "13:00"

drop:
  - match: "Unwanted class"
    start_time: "17:15"

cancel:
  - match: "LAB - Course"
    date: "2026-11-04"
    start_time: "14:15" # optional narrowing condition

override:
  - match: "LAB - Course"
    date: "2026-11-18" # optional
    filter_start_time: "14:15"
    start_time: "14:30"
    end_time: "18:00"

custom:
  - template: office_hours
    description: "Exam preparation"
    date: "2026-11-20"
    # uid: "office-hours-2026-11-20@example.org" # optional stable UID
```

Rule dates and times are interpreted in the `Europe/Warsaw` time zone, even when the
source calendar stores events in UTC. A `cancel` rule requires a date to prevent an
entire recurring series from being removed accidentally. Custom events receive a
deterministic UID, so subsequent refreshes do not create duplicates. Custom events may
inherit reusable fields from a named template and override any inherited value locally.

## Automatic refresh

Example cron entry that runs every 15 minutes:

```cron
*/15 * * * * cd /opt/usos-calendar-filter && /opt/usos-calendar-filter/.venv/bin/usos-calendar-filter >> /var/log/usos-calendar-filter.log 2>&1
```

Example systemd service at `/etc/systemd/system/usos-calendar-filter.service`:

```ini
[Unit]
Description=Refresh cleaned USOS calendar
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/usos-calendar-filter
EnvironmentFile=/etc/usos-calendar-filter/usos-calendar-filter.env
StateDirectory=uni-schedule
ExecStart=/opt/usos-calendar-filter/.venv/bin/usos-calendar-filter
```

And its timer at `/etc/systemd/system/usos-calendar-filter.timer`:

```ini
[Unit]
Description=Refresh cleaned USOS calendar every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now usos-calendar-filter.timer
```

`StateDirectory=uni-schedule` creates the persistent `/var/lib/uni-schedule` directory
used by the default `HISTORY_ICS_PATH`. The service user must also have write access to
the directory configured by `OUTPUT_ICS_PATH`. Keep the environment file readable only
by the service administrator because it contains the private USOS URL.

## Tests and code quality

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

The test suite covers event filtering and cancellation, UTC-to-Warsaw time conversion,
daylight-saving offsets, time overrides, stable UIDs, `VTIMEZONE` preservation, and
atomic ICS writes.
