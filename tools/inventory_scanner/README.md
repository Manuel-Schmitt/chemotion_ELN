# Raspberry Pi inventory station

A standalone Python 3.10+ application using existing chemotion APIs. No Rails,
database, or chemotion JavaScript changes are included. It supports standard
samples and sequence-based macromolecule samples (SBMM), a keyboard-mode USB 2D
reader, and a full-screen Tkinter display. A separate standalone Python script
generates printable **Data Matrix ECC200** SVG destination labels. New
location/person labels are Data Matrix, not QR symbols. The
scanner passes decoded text, so existing chemotion bottle codes can use either
supported symbology; enable Data Matrix decoding on the physical reader.

## Install and configure

On Raspberry Pi OS, from the chemotion checkout (the directory containing
`tools/`):

```sh
sudo apt install python3-venv python3-tk libdmtx0b
python3 -m venv .venv-inventory
.venv-inventory/bin/python -m pip install -r tools/inventory_scanner/requirements.txt
mkdir -p ~/.config/chemotion-inventory
cp tools/inventory_scanner/config.example.json ~/.config/chemotion-inventory/station.json
chmod 700 ~/.config/chemotion-inventory
chmod 600 ~/.config/chemotion-inventory/station.json
```

Edit the configuration:

- `base_url`: verified HTTPS URL of the chemotion installation.
- `station_name`: recognizable station identifier for the display/recovery notes.
- `timeout`: timeout per HTTP request, in seconds.
- `pair_timeout`: seconds before an incomplete ordinary scan pair resets.
- `fullscreen`: starts full screen by default. F11 toggles; Escape leaves full
  screen without exiting. `--windowed` is useful on a development workstation.
- `scanner_backend`: `keyboard` (default, no host-controlled sounds) or
  `zebra_ssi` (serial input plus scanner-controlled feedback).
- `scanner_port`: serial device for `zebra_ssi`, preferably a stable Linux
  `/dev/serial/by-id/...` path rather than an enumeration-dependent `/dev/ttyACM0`.
- `scanner_baudrate`: serial setting, default `9600`; match the device configuration.
- `scanner_timeout`: SSI acknowledgement/partial-packet timeout, default 2 seconds
  (allowed 0.2–30). An expired command is not automatically repeated.
- `scanners`: optional array of `{ "id": "...", "name": "...", "port": "..." }`
  definitions for multiple SSI scanners. IDs and device paths must be unique.
  With this array, select `zebra_ssi` and omit/leave empty the legacy `scanner_port`.
  The shared baudrate/timeout apply to each device. Existing single-scanner
  configurations remain supported.

Use one distinct normal account/token per station, not an administrator account.
Multiple scanners on one Pi share that station account/token.
The station is **not locked to a group**. Group and borrower names are static text
in each Data Matrix label. No group/user directory lookup or membership check is
performed. Different scanners or successive scans can use different printed names
without changing station configuration.
Give it intended access to the inventory collections, with Edit elements and full
detail level **10 for both sample types**. Configure actual collection visibility
so audits can read every relevant bottle. Run only one station process per device
and avoid overlapping cabinet audits or simultaneous edits of the same bottle.
Audit progress exists only in memory and is discarded when the application exits.

**Migration:** remove the old station-level `group_id` setting; it is rejected
with an actionable message, not silently used or ignored. Existing labels remain
valid only if they already use the static text schema below. Reprint old ID-based
location **and person** labels using `--group` and `--borrower` text, not
`--group-id`/`--person-id`. The standalone label generator is offline and needs no
station config or API token.

### Static destination information

No chemotion endpoint is added or modified. Authentication checks the station
account only; assigning a destination does not call `/groups`, user-name search
or an administrator-only lookup. The token needs actual access to the bottles
and audit collections, not membership in each printed group.

Printed group/borrower names are copied literally. They are not verified user
identities, current group memberships, or active accounts. Names can become
outdated or collide; maintain labels administratively and reprint after changes.

### Authentication

Sign in as the station account, enable 2FA under **Settings → Account**, then create
an **Authentication Token** using the current OTP. The token is shown once.
The API defaults to 30 days and supports 1–600 days; schedule rotation/revocation.
There are no inventory-only token scopes.

Supply the token only through `CHEMOTION_API_TOKEN`. For example, a protected
file outside the repository can contain:

```sh
export CHEMOTION_API_TOKEN='REPLACE_WITH_STATION_TOKEN'
```

Set its permissions to `600`, then source it in the launching shell. Never put
the real token in the repository, a QR code, command arguments, screenshots, or
support logs. Do not use a shared user's token.

```sh
. ~/.config/chemotion-inventory/token.env
.venv-inventory/bin/python -m tools.inventory_scanner \
  --config ~/.config/chemotion-inventory/station.json
```

The Pi needs a graphical desktop session. This is not a web server; camera
scanning, automatic OS startup, and system-service installation are not included.
The layout is tested at 800 × 480 pixels and its 720 × 440 minimum window size.
The header and status area have bounded heights so long labels or errors cannot
push the scan entry and action buttons off-screen. Full destination values,
collection scope, scanner status and diagnostic messages appear in the
scrollable details area. A shortened status message points there when necessary.
Instructions use a neutral background; working/warning messages use yellow,
verified success green, and errors red. Scanner sounds remain tied to recognition,
verified success or failure, independently of those visual levels.
Configure the USB 2D scanner to send the complete decoded text followed by Enter, using
the same keyboard layout as the Pi. Test punctuation and non-ASCII location
names on the physical device. Click the code entry if focus has moved away.

## Zebra DS2278-SR: scanner-generated feedback

The optional SSI backend uses `pyserial`, not the Zebra SDK. It keeps all serial
I/O on its own thread and sends native SSI BEEP commands for:

| Application event | Scanner pattern | SSI beep code |
| --- | --- | --- |
| Bottle recognized / lookup complete / audit location ready | One high short beep | `0x00` |
| Assignment or audit bottle saved and verified / audit completed | Two high short beeps | `0x01` |
| Rejected scan, failed operation, conflict, incomplete audit, or unverified write | Three low short beeps | `0x07` |

Buffered destination receipt alone produces no save-success sound. Looking up a
bottle produces recognition feedback only, never a save-success sound. A scanner
ACK confirms the command was received; it is not proof of a database save.

### Hardware setup

Use the DS2278-SR with a compatible USB **communication** cradle and cable.
In the DS2278 Product Reference Guide, scan the configuration barcodes for:

1. **SSI over USB CDC**, printed page **8-6**. Do not select ordinary USB CDC
   Host, USB HID keyboard, or Bluetooth HID for this backend.
2. **Disable Beep After Good Decode**, printed page **5-6**, so decoding does
   not sound like application success.
3. Enable **packetized decode data** and **ACK/NAK handshaking** as documented
   in the SSI configuration section (chapter 9). Barcode bytes must be sent in
   SSI DECODE_DATA packets, not as an unframed serial string.
4. Transmit the original decoded UTF-8 label bytes with no prefix/suffix,
   AIM/code-ID text, case conversion, or formatting. The SSI symbology byte is
   parsed separately; it is not part of the application label.
5. Enable Data Matrix decoding and disable offline/batch barcode storage for this
   live station workflow. Do not replay stored scans into the application.

The guide's SSI-over-USB-CDC option and protocol are documented, but the exact
scanner firmware/cradle/Pi combination still requires physical validation.
Direct Bluetooth SPP, arbitrary USB Bluetooth dongles, and automatic pairing are
not implemented. Ordinary USB CDC with BEL is a different protocol and is not
used here.

On Raspberry Pi OS, identify the serial device and grant the station account
access through the normal serial-device group, typically `dialout`:

```sh
ls -l /dev/serial/by-id/
sudo usermod -aG dialout YOUR_STATION_OS_USER
```

Log out and back in after changing group membership. Do not run the station as
root or make the device world-writable. Close serial monitors or other scanner
applications before launching.

Set these configuration fields:

```json
{
  "scanner_backend": "zebra_ssi",
  "scanner_port": "/dev/serial/by-id/REPLACE_WITH_YOUR_DEVICE",
  "scanner_baudrate": 9600,
  "scanner_timeout": 2
}
```

These fields belong in the existing station configuration alongside the URL,
timeouts, and audit settings. Launch normally. The application sends
SCAN_ENABLE and waits for an SSI acknowledgement before accepting scanner
input. A **Connect scanner** button allows explicit reconnecting. Keyboard
entry remains available for testing while the SSI device is connected.

### Failure handling and verification

Checksum errors, unsupported messages, malformed UTF-8, oversized/unfinished
packets, command rejection, disconnects, and missing acknowledgements stop the
backend with a visible diagnostic. Valid SSI EVENT notifications are acknowledged;
a scanner boot or parameter-default event requires configuration review and
reconnection. Barcode fragments are bounded and reassembled; recognized
retransmitted fragments are acknowledged without submitting another application
scan. Unsolicited acknowledgements require resynchronization.

SSI acknowledgement of a scan means **received**, not accepted by chemotion.
Busy or otherwise rejected application scans receive error feedback. Commands
are serialized; none of these transport actions retries an inventory PUT.
For conservative handling of ambiguous command outcomes, beeper commands are
not automatically retried either. A failed beep cannot reliably announce its own
failure, so the display explicitly reports unreliable sound and disables new
input/Finish until reconnecting. A verified database save remains shown as
verified even when its subsequent sound fails; inspect it rather than rescanning
blindly.

If a scanner failure occurs during an audit, that audit is blocked from clean
completion even after reconnection: cancel it and perform a fresh physical
scan. Already-saved changes remain applied. This prevents lost input from being
mistaken for an absent bottle.

Before deployment, test all three patterns, two fast scans, Unicode/long labels,
SSI retransmissions, the scanner going out of range, USB unplugging, and a sound
acknowledgement timeout after a verified save. Ensure error sounds are distinct
from the scanner's own connection/battery/error tones. A failed optical read that
produces no message cannot be detected by the application.

Protocol references:

- [DS2278 Product Reference Guide](https://www.zebra.com/content/dam/support-dam/en/documentation/unrestricted/guide/product/ds2278-prg-en.pdf):
  SSI command support, chapter 9; Beeper/LED actions, appendix L.
- [Cordless SSI Programmer's Guide](https://www.zebra.com/content/dam/support-dam/en/documentation/unrestricted/guide/software/ssi-cordless-pg-en.pdf):
  packet framing/checksum, chapter 3; BEEP, pages 4-10–4-12; CMD_ACK/NAK,
  pages 4-20–4-26; DECODE_DATA, pages 4-28 onward.

## Multiple scanners, one visible panel

Use one DS2278 and its separately identifiable USB communication cradle per
scanner, connected to the same Pi (with a suitably powered USB hub if needed).
Sharing one cradle between scanners is not implemented or assumed to work.
Each cradle must provide an independent SSI serial device. If the OS does not
provide unique `/dev/serial/by-id/` names, use distinct stable
`/dev/serial/by-path/` names tied to the USB ports.

Start from `config.multiscanner.example.json`, or replace the single port with:

```json
{
  "scanner_backend": "zebra_ssi",
  "scanner_baudrate": 9600,
  "scanner_timeout": 2,
  "scanners": [
    {"id": "blue", "name": "Blue scanner", "port": "/dev/serial/by-id/BLUE_CRADLE"},
    {"id": "orange", "name": "Orange scanner", "port": "/dev/serial/by-id/ORANGE_CRADLE"}
  ]
}
```

Merge these settings with the normal station URL, timeout and audit configuration.
The IDs are your configured channel identities, not a claim that the application
queried the scanner's hardware serial number. Put matching name/colour stickers
on the physical scanners.

- **Exactly one panel is shown.** Its top-right selector displays the current
  scanner name/ID; it can also be used to select a scanner manually.
- A new physical scan selects that scanner's panel. Concurrent queued events
  are ordered by their receive timestamps, not by USB-device polling order.
- Background lookups/saves, connection notices, timeouts and failures update
  their originating scanner's state **without selecting its panel**.
- Every scanner retains its own pending bottle, buffered destination, mode,
  timeout, instructions and outcome when hidden. The terminal's single audit is
  shared but has one owning scanner.
- Sounds go only to the scanner that originated the operation.
- Mode/Reset/Finish/Reconnect/keyboard input affect the **currently displayed**
  scanner. Check its name before using controls. Switching clears unsent typed
  entry text, never another scanner's stored pending bottle.
- **Confirm Found** appears for the relevant scanner when recovery needs
  confirmation. Background recovery never opens a modal dialog or steals focus;
  select that scanner and press the button. While a confirmation dialog is open,
  incoming scanner input is rejected with error feedback and must be rescanned.
- Scanner failures are isolated. A disconnected scanner cannot accept work, but
  others remain usable. The selector marks disconnected/blocked/error or
  confirmation-needed sessions with `!`.
- Exit checks all scanners for in-flight work and closes all sessions together.
  The Hub owns the shared worker; closing one controller cannot shut it down for
  other scanners. Cleanup attempts every device and worker shutdown even if a
  device fails to close; cleanup failures are reported in the terminal log.

One shared worker serializes complete API operations, including fresh read,
mutation and verification, across all scanners. This avoids concurrent writes
inside this application; it does not eliminate races with browsers or other
stations. Two users scanning the same bottle can still receive an explicit
changed-assignment conflict rather than overwrite one another.

Only one audit can be active on a terminal at a time, regardless of scanner.
The scanner that starts it owns the audit; other scanners cannot scan into,
finish or cancel that audit. Select the owning scanner to continue. There are no
automatic cabinet reservations: users are responsible for avoiding conflicting
audits and assignments across terminals or users.
Shared audit changes refresh the displayed panel even when its owner is hidden;
they never switch the selected scanner or replace its bottle workflow.

Recovery notes include the configured scanner name/ID. Audit data is not written
to disk or exported.
Chemotion's authenticated history actor remains the **shared station account**,
not the physical scanner or an identified operator.

Test the actual multi-cradle/Pi hardware before deployment, including USB power,
stable device identity, simultaneous scans, hidden-screen completions, sound
routing, disconnected devices, single-audit ownership and user-managed cabinet
coordination.

## Assign and Lookup

1. In **Assign**, scan a bottle's full existing chemotion numeric code.
2. Check its readable identity, stable ID, status, and current location/borrower.
3. Scan a location or person label. The station saves and reads back the result.
4. Wait for **Saved and verified** before scanning another bottle.

The scanner's decode beep means only "code decoded", not "saved".
One destination can be buffered while the first bottle is loading. Extra scans
are visibly rejected rather than queued for later writes.

**Location and person codes have identical assignment semantics.** Each supplies
any subset of these five text fields:

| Label field | Inventory field |
|---|---|
| `building` | `current_building` |
| `room` | `current_room` |
| `cabinet` | `current_cabinet` |
| `group` | `current_group` |
| `borrower` | `borrowed_by` |

Every scan replaces **all five current-location fields**: supplied text is copied
and omitted fields become `""`. Thus an ordinary cabinet label without `borrower`
clears the borrower, while a borrower-only label clears building/room/cabinet/group.
A person label may also supply physical location, group and borrower together;
the generator's `location`/`person` choice is for operator convenience only and
does not add a type field to the payload.

Only these current-location fields are replaced. Home location, ownership,
quantity, unrelated inventory fields and other array entries remain unchanged.
Existing Missing-recovery status/note handling still applies. Printed names are
not resolved through chemotion; identical names remain ambiguous.

Missing bottles require a **Found** confirmation during ordinary Assign. This
is requested through the **Confirm Found** button on that scanner's panel.
Confirmation restores Available, applies the destination, and appends a recovery note without
replacing existing notes. Available is not certification of contents or fitness.
**Lookup** never changes anything and never restores Missing.

Incomplete pairs reset with a countdown. Reset cannot cancel an in-flight write.
Changed assignments/statuses are shown as conflicts: review the refreshed bottle
and scan the target again. Authentication failures disable writes until Reconnect.
An unverified write displays an error warning that it **may have reached
chemotion**. Inspect the current inventory. A fresh bottle scan is accepted
without pressing Reset or restarting the station; the old bottle/destination
pair, buffered destination and Found confirmation are discarded. The new pair
loads fresh inventory before another destination can be applied. The previous
mutation is never automatically replayed. Successful updates remain applied
even if a later operation is cancelled.

Unexpected worker or GUI callback errors are displayed and logged with a
traceback to the launching terminal. Polling continues and the next scan can
start a fresh attempt. An actual in-flight write is never cancelled by an
unrelated UI error: wait for its result before rescanning. Complete diagnostics
and the last error remain available in the scrollable details area. Do not share
unredacted logs publicly. Authentication failures still require Reconnect, and a
disconnected scanner must be reconnected before it can accept input.

If a PUT is explicitly refused with **HTTP 403**, the station displays
**"Write denied: the API token lacks permission to update this inventory"**,
sounds the scanner's error pattern, and never reports it as saved. Grant the
station account Edit elements access to the relevant inventory collection before
rescanning. **HTTP 401** can mean invalid/expired authentication or a server-side
authorization refusal: the error asks you to check the token and collection
permissions, and Reconnect is required. Neither response automatically retries a
write. A denied read-back after a PUT is still an **unverified write**, not proof
that the preceding write was refused.

Audits keep explicit permission denials and per-bottle outcomes in memory for the
current run. Failed scanned-bottle writes block Finish; denied Missing writes
produce an incomplete audit with a visible permission-error summary. No failed
Missing write is replayed by pressing Finish again. Cancel the current audit and
start a new physical scan if you need to restart.
An unexpected error or an unverified scanned-bottle write does not lock out new
bottle scans, but its unresolved audit record remains. Explicitly rescan that
bottle to reconcile it, or cancel; scanning some other bottle cannot silently
turn it into a verified result. Missing updates interrupted by unexpected errors
also remain unresolved and are not automatically retried.

## Cabinet audits

For small locations with **at most 30 bottles**, scanned in one sitting:

1. Choose **Audit**, scan either destination label type, and wait for the baseline
   to finish. Audit requires `collection_id`, selecting exactly one inventory collection.
   Every page for its standard and SBMM samples must be readable with full detail
   access. The audit starts only after a complete snapshot; expected bottles match
   all five **current** assignment values supplied by the label (including borrower
   and empty/omitted fields), not the home location. Matching is exact, not recursive by building. Check the displayed
   collection ID before proceeding.
2. Scan every physical bottle. The location stays selected only in Audit mode.
   Readable bottles outside the label's collection are also accepted, flagged
   in the current GUI summary, and corrected without changing collection memberships.
   Duplicates are ignored. Misplaced bottles are relocated, and Missing bottles are restored
   to Available automatically, without another Found dialog. Other statuses,
   including Out of stock, are preserved.
3. Press **Finish location** only when all bottles have been scanned.
   **This immediately marks unseen expected bottles Missing.** There is no
   chemotion review step or individual confirmation.
4. Read the completion summary. Conflicts, failed scanned-bottle updates, and
   unverified mutations prevent a clean completion. Do not interpret an
   incomplete summary as a successful inventory.

**One group's inventory collection per cabinet:** each cabinet's intended stock
must belong to the one collection printed on its location label. Mixed-collection
cabinet baselines and implicit unions of collections are not supported. The
collection ID is audit scope only: neither Assign nor Audit adds, removes or
moves collection memberships, and it is not a permission grant.

Only bottles in that collection **and at that location when the audit starts**
enter the fixed expected set. Physically scanned outsiders prove their presence
and can be relocated/recovered, but never enlarge that expected set. Unseen
outsiders cannot become Missing during this audit. A wrong collection ID can
therefore produce an incomplete expected list; verify labels and collection
contents before deployment.

Enumeration covers the existing paginated collection lists, not every raw
database association: chemotion's standard list excludes reaction-only
reactants/solvents. Use dedicated stock collections with ordinary bottle records,
not project collections containing derived reaction material, as audit sources.

A failed bottle lookup is remembered in the current process as an unresolved scan fingerprint.
Finish remains blocked until that code is successfully rescanned, or the audit
is cancelled. Scanning some other bottle does not clear the failure.

A missing bottle retains its last recorded current location, home, borrower,
quantity and notes; nothing is deleted or archived. If found in another cabinet's
audit later, its current location and status are corrected then. Bottles already
recorded as checked out/elsewhere are not expected at their home cabinet.

Before Missing writes the station rechecks identity, status and assignment.
Intervening changes are conflicts, not overwritten. A failed or unknown bottle
scan is not accepted as successful inventory work: resolve the failure before
choosing Finish. Cancel before Finish never marks unseen bottles Missing, but
already-saved relocations/recoveries remain applied.

### Audit state and restart behavior

Audit progress, expected/seen bottles, unresolved scans and write outcomes are
kept only in process memory. There is no local audit database, audit export, or
list/close command. The GUI shows the current audit's progress and completion or
failure summary.
Only expected cabinet bottles and additional bottles actually scanned retain
baseline snapshots. A set of original collection identifiers distinguishes
same-collection misplaced bottles from outsiders without keeping snapshots for
every unscanned bottle in a large collection. The complete collection must still
be read successfully before the audit starts.

If the station exits or crashes during an audit, that run's progress is lost.
Any chemotion writes already completed remain saved; the station does not undo
or automatically replay them. Start a new audit and physically rescan the
location to establish a fresh baseline. This is intended for small locations
that can be rescanned in one sitting.

## Generate labels

The standalone script below is independent of the station GUI, configuration
and API token. It creates an SVG file; open and print it with a local SVG viewer:

```sh
.venv-inventory/bin/python tools/generate_inventory_label.py location \
  --group 'Working Group A' --collection-id 100 \
  --building B1 --room 204 --cabinet C3 --output cabinet-c3.svg
```

For a person destination:

```sh
.venv-inventory/bin/python tools/generate_inventory_label.py person \
  --group 'Working Group A' --borrower 'Alice Example' --output person-alice.svg
```

The SVG includes a one-module quiet zone, readable supplied field values, an
optional collection ID and a compact note that blank fields clear. The label
script never invokes a printer. Existing output files are not overwritten.
Generation is offline; a well-formed collection ID does not prove the collection
exists or is accessible.

Compact payload format (all six positions after `ci1` are always present):

```text
ci1|B1|204|C3|Working Group A||100
ci1||||Working Group A|Alice Example|
```

Positions are building, room, cabinet, group, borrower and collection ID. Empty
destination positions clear their corresponding current inventory field. The
collection position is empty when absent; otherwise it must be a positive
canonical decimal ID. It is optional for assignment and required for Audit. It
selects audit scope only, never collection membership. At least one destination
value must be nonblank.

Text fields allow trimmed Unicode up to 100 characters each, without C0 control
characters, DEL or surrogates. Literal `|` and `%` are escaped as `%7C` and
`%25`; all other values remain UTF-8 text. Unknown escapes, wrong slot counts,
unsupported versions and invalid collection IDs are rejected. Input is limited
to 2048 UTF-8 bytes. The standalone encoder uses automatic Data Matrix encoding;
actual symbol size depends on content.

**Migration:** remove `inventory_collection_ids` from station configuration;
remove `audit_directory`; both legacy settings are rejected with migration
instructions rather than used as fallbacks. Reprint **all previous location and
person labels** using the standalone generator and the `ci1` format. The station
does not accept the old JSON payload format or infer any legacy values.

The text payload is independent of the symbol format: printed labels remain Data
Matrix ECC200. Generation uses local libdmtx via pylibdmtx; no online encoder or
image service receives label data.

Labels are descriptive, not signed credentials. Anyone able to print syntactically
valid labels can create destinations. Restrict label printing administratively.
Reprint/retire old labels after renaming locations; there is no central revocation.
Person labels identify borrowers, not authenticated operators.

## Production Missing option

The user-managed production overlay must already add:

```js
{ label: 'Missing', value: 'Missing' },
```

to `chemicalStatusOptions` in a **complete, deployed-version-matched**
`app/javascript/src/components/staticDropdownOptions/options.js`, placed at
`shared/pullin/app/javascript/src/components/staticDropdownOptions/options.js`
when Compose maps `./shared/pullin:/shared`.

Verify your actual volume mapping and image startup behavior. In the previously
examined upstream startup sequence, `/shared` is copied over the application
before asset precompilation. Recreate/restart according to your deployment
procedure and confirm compilation, dropdown/search visibility, persistence and
history on a designated test bottle. This repository's options file is not
modified by this tool. Rebase the full overlaid module on every chemotion upgrade.
The API does not expose the compiled dropdown, so the station cannot verify this
deployment prerequisite automatically.

## Existing API limitations

- The Chemical API does not itself enforce parent ElementPolicy authorization.
  The station's label/collection checks are client safeguards, **not** a repaired
  server authorization boundary. Normal Assign/Lookup do not use audit collection
  scoping as a replacement for server authorization.
- API tokens have no inventory-only scope. Protect the device/token and revoke
  lost tokens. Membership responses do not expose an active-account flag.
- Inventory PUT replaces the whole JSON array. Fresh reads preserve observed
  changes, but there is no conditional update or cabinet-wide transaction.
  Readback verifies the complete intended inventory at that instant, not absence
  of all races or proof that no concurrent update was lost. Avoid concurrent edits.
- Missing inventory entries must first be created in chemotion. The station
  never creates bottle records or inventory entries.
- Standard-sample Chemical changes use existing history; the actor is the station
  account. A scanned borrower is not an authenticated actor. No SBMM History tab
  is added.
- Audits operate on explicit current location and inventory scope, not nested
  collections, institute aggregation, stock consumption, or purchasing.

## Validation and pilot

```sh
python3 -m unittest discover -s tools/inventory_scanner/tests
python3 -m compileall -q tools/inventory_scanner
```

To include real Tkinter smoke tests without a desktop, use
`xvfb-run -a python3 -m unittest discover -s tools/inventory_scanner/tests`
after installing the optional OS package `xvfb`.

Use a dedicated test inventory for integration checks, never production bottles
without approval. Before deployment, test both sample types, real QR decoding,
static name changes/reprinting, token expiry, two rapid scans, wrong/duplicate scans, timeout,
Found cancellation, concurrent changes, network loss during PUT/readback, and
audit cancellation/interruption/late discovery in another cabinet. Verify the
existing standard History tab and the production Missing dropdown separately.
Synthetic tests do not prove physical scanner/layout/display compatibility.
Optional SVG round-trip tests use `rsvg-convert` and `dmtxread` (OS packages
`librsvg2-bin` and `dmtx-utils`); these are not runtime dependencies.
