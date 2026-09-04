# DicomPurge


<img alt="DicomPurge Logo" height="100" width="100" src="./images/DicomPurge_logo_512x512.png" />

A DICOM De-identification tool for de-identifying burned-in annotations in DICOM images. You draw
boxes on one image of a series, the boxes apply to every image in that series, and then 
  export those files to an output directory with the same structure.

## Running

The project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run python main.py
```

All dependencies publish wheels for CPython 3.11–3.14. If a wheel is ever missing for
your interpreter, pin the venv to an older Python and re-sync:

```bash
uv venv --python 3.12 && uv sync
```
## UI Screenshot 

![Screenshot](./images/Screenshot.png)

## Layout

| File | Purpose |
| --- | --- |
| `main.py` | Entry point; installs the logging bridge and a global exception hook |
| `deid_app/resources.py` | Bundled images — the toolbar logo and window icon |
| `images/` | `DicomPurge_logo_512x512.png`, shown top-left and used as the window icon |
| `deid_app/logging_setup.py` | Root logging config: `~/dicompurge.log` (rotating) + Qt signal bridge |
| `deid_app/log_pane.py` | Log tab: level filter, colouring, auto-scroll, clear button |
| `deid_app/metadata_pane.py` | Metadata tab: read-only DICOM tag tree for the displayed instance |
| `deid_app/model.py` | Recursive `*.dcm` scan (worker thread) and the `Series` model |
| `deid_app/render.py` | DICOM instance → `QImage` (modality LUT, VOI LUT, MONOCHROME1 inversion); `has_pixel_data()` detects pixel-free instances |
| `deid_app/sr_render.py` | Detects Structured Report–family instances and renders their `ContentSequence` as read-only HTML |
| `deid_app/image_view.py` | Image canvas and rubber-band box selection |
| `deid_app/frame_worker.py` | Frame rendering off the GUI thread: the render worker the display waits on, and the prefetch worker that renders ahead of the scroll |
| `deid_app/decode_cache.py` | The two thread-safe LRU caches those workers share — decoded datasets, and rendered frames bounded by count *and* bytes |
| `deid_app/redaction.py` | The de-identification itself — pixel data and overlays |
| `deid_app/export.py` | Export worker: mirrors the input tree into the output tree |
| `deid_app/xnat_settings.py`, `xnat_client.py`, `xnat_worker.py` | XNAT server/credentials, the login session on its own thread, and the project / session listings |
| `deid_app/xnat_upload.py` | One upload job, no Qt: de-identify into a temp directory, zip or post, clean up |
| `deid_app/xnat_upload_worker.py` | Upload jobs on their own threads and the queue that limits how many run at once |
| `deid_app/xnat_pane.py` | XNAT server tab: project, study, session label, Upload, per-upload progress |
| `deid_app/main_window.py` | Window assembly, tree, tabs, wiring |
| `tests/` | Fixture generator, six headless GUI scripts, and two pytest-style unit modules |

## Workflow

1. **Pick an input directory** from the drop-down at the top (it remembers recent
   directories) or via **Browse…**. Nothing happens until you do.
2. The tree fills with `Patient → Study → Series`. Only **series** nodes are selectable;
   patient and study nodes are display-only.
3. Selecting a series renders its first image in the **Image review** tab. A modal
   **Loading series** popup covers the wait — it counts through the instance headers, then
   shows an indeterminate bar while the first image is decoded, and closes as soon as that
   image (or its placeholder) is on screen. A series that loads in under 300 ms never
   shows it. The slider at the bottom cycles through every image in the series
   (multi-frame instances contribute one slider position per frame). **Scrolling the mouse
   wheel** does the same thing, over the image *and* over the slider — wheel up moves
   toward the start of the series, wheel down toward the end, and it stops at either end
   rather than wrapping. Trackpad deltas accumulate, so one notch-equivalent of scrolling
   advances exactly one image. The wheel is ignored while you are mid-drag on a redaction
   box.
4. The **Metadata** tab shows the DICOM tags of whichever instance is on screen —
   *Field Name / Tag / VR / Size / Content*, with the `(0002,xxxx)` file meta group
   in grey (toggle it off with the **File meta** checkbox). Sequences expand into
   `Item n` sub-trees, multi-valued elements expand into one `value` row each, and
   binary elements (pixel data, overlays) show their size instead of their bytes.
   The **Filter** box narrows the tree to rows whose name, tag or content match.
   Building that tree is expensive, so it is rebuilt once the scrolling settles rather
   than for every image you pass — switching to the tab builds it immediately.
   It is read-only: nothing here is edited or exported. Note this app redacts pixels
   and overlays only — the tags shown are **not** de-identified on export.
5. **Drag on the image** to place a redaction box. While dragging, releasing inside the
   image keeps the box; dragging outside the image turns the selection **red** and
   releasing there discards it.
6. Selecting a series **automatically marks it "reviewed"** — the assumption is that you
   looked at the images. Placing a box moves it to **pending** (red), and **Commit series**
   moves it to **committed** (green). **⌘S** (Ctrl+S off macOS) commits the series being
   reviewed without reaching for the button — it works from any tab, is a no-op with a
   status-bar note if nothing is selected or the series is already committed, and the
   status bar confirms each commit.
7. **Reset boxes** drops every box on the series; it falls back to *reviewed*, since you
   have still seen the images. Adding a new box to a committed series re-opens it as
   pending.
8. **Right-click a series** for a popup menu: *Set status to Clean* (clears reviewed and
   committed, and discards any boxes after a confirmation), *Commit series*, *Reset boxes*,
   *Set status to Skipped* and *Un-skip series*. Setting the currently displayed series to
   Clean also deselects it, so clicking it again re-marks it reviewed. **Set status to
   Skipped** withholds that one series from the export whatever it contains — useful for an
   ordinary image series you simply do not want to send — and discards any boxes on it,
   after a confirmation naming how many.
9. **Export** requires every series to be **reviewed or committed**. A series carrying
   uncommitted boxes (*pending*) blocks the export until you commit it; a *clean* series
   blocks it until you look at it. A *skipped* (orange) series never blocks it — it is
   withheld deliberately, so there is nothing to review. Export then prompts for an output
   directory and writes each file to the same relative path underneath it. The summary
   reports how many files were written, redacted, had an embedded document removed, and
   were skipped — naming the skipped ones, since the exported set is then smaller than the
   input set.

### Non-pixel-data DICOM files

Not every DICOM instance carries pixel data, and the **Image review** tab handles both
cases it might encounter instead of crashing:

- **Structured Reports** (Basic Text/Enhanced/Comprehensive/Comprehensive 3D/Extensible
  SR, Key Object Selection, CAD SR variants, radiation-dose SR variants, and related
  report-only SOP Classes — the full list lives in `deid_app/sr_render.py`) have no
  pixel data at all; their content is a nested `ContentSequence` of text/code/numeric
  items. These render as a **read-only HTML report** in the image pane in place of the
  canvas — there is nothing to drag a redaction box onto, and the report cannot be
  edited.
- **Any other pixel-data-free instance** — Encapsulated PDF/CDA/STL, Presentation States
  (GSPS/CSPS), Waveform Storage, RT Structure Set/Plan, Registration objects, Real World
  Value Mapping, etc. — shows a placeholder instead of the missing image:
  `Uneditable File : No Pixel Data for image {filename} of image type {SOP Class name}`.
  This is expected and logged at info level, not treated as an error. These are the
  instances the **Skip files with no image data** export setting covers.

Series containing these instances still go through the normal review/commit workflow
(status colors, export) even though there is nothing to redact on that particular
instance — unless the export settings withhold them, which is what the next section
covers.

### Files boxes cannot de-identify

Some DICOM objects carry identifying content that a redaction box can never reach,
because it is not in the pixels. Three **Export** settings (Settings → Export, `Ctrl+,` /
`Cmd+,`) decide what happens to them.

**Embedded documents — removed by default.** A report object can carry a PDF (or HTML)
copy of the report in `(0042,0011)` *EncapsulatedDocument* **alongside** its image. The
image is a rasterised picture of the page; the document is a second, independent copy of
the same text. Redaction only rewrites pixels, so boxing out a name on screen leaves the
document untouched — and nothing in the UI displays it, so there is no way to notice
before the file leaves the machine.

> ☑ **Remove embedded documents (PDFs) from exported files** — *default on*

With this on, `(0042,0011)` and `(0042,0012)` *MIMETypeOfEncapsulatedDocument* are
deleted on export and the redacted image is kept. Such a file is **rewritten even when it
has no boxes**, because the byte-for-byte copy that ordinarily applies is exactly what
would leak the document. Objects that are *only* a document (Encapsulated PDF/CDA/STL/…
SOP Classes, no pixel data at all) cannot be de-identified in any way, so they are
withheld from the export entirely and shown as `skipped`.

**Structured Reports — kept by default.** An SR holds its content as text in a
`ContentSequence`, not as pixels, so boxes cannot alter it either.

> ☐ **Skip Structured Reports (they cannot be de-identified with boxes)** — *default off*

This one defaults to **off** because, unlike an embedded PDF that duplicates the image
beside it, an SR is often the only copy of the report — dropping it silently would lose
data. Turn it on and every SR series is withheld and shown as `skipped`.

**Everything else with no pixel data — kept by default.** Presentation states, waveforms,
RT objects, registrations and the like carry no image at all; these are the instances the
image pane shows as `Uneditable File : No Pixel Data …`.

> ☐ **Skip files with no image data (shown as "Uneditable File")** — *default off*

This covers exactly the placeholder case and **not** Structured Reports, which have no
pixels either but render as an HTML report and have their own checkbox above — so the two
options stay independent and neither silently overrides the other. A series is only
withheld when *every* instance in it lacks an image, so a series that merely contains one
such file does not lose its real images.

The three options are independent and can be combined; a series withheld for any reason
shows the same orange `skipped` status.

Every strip and every skip is logged by filename, and the export summary reports the
counts and names the skipped files. The exported set can be smaller than the input set,
so this is stated rather than left to be discovered.

**What this does not cover:** `(0042,0010)` *DocumentTitle* is not removed, and neither
are DICOM header identifiers — see the scope note under
[How the redaction works](#how-the-redaction-works).

### Scrolling performance

Decoding a DICOM frame is far too slow to do while someone scrolls, so nothing that
touches pixel data happens on the GUI thread:

- **Two worker threads.** One renders the frame the display is waiting on; the other
  renders frames *ahead of* the scroll (6 positions in the direction of travel, 2 behind).
  Both run the same render path, so a prefetched frame is exactly what the display would
  otherwise have waited for.
- **Two caches**, shared by both workers: decoded datasets, and finished frames keyed by
  `(file, frame)`. A frame that is already rendered — revisited, or fetched ahead — is
  painted immediately with no worker round-trip at all. The frame cache is bounded by
  entry count and by total bytes (counting the dataset each entry keeps alive), so a
  series of large radiographs cannot grow it without limit.
- **Stale work is dropped, not decoded.** Prefetch requests from an abandoned series, or
  for positions a fast scrub has already passed, are discarded when the worker reaches
  them; and a slow render that finishes after you have moved on never paints over the
  image you are actually looking at.
- The **Metadata** tab and the scaled on-screen pixmap are both rebuilt only when they
  actually change, keeping per-image work on the GUI thread down to the paint itself.

Per-frame timings (dataset read, render, metadata build, display) are logged at **debug**
level — set the Log tab's level filter to `DEBUG` to see where the time goes.

### Series status

| Indicator | Status | Meaning | Export? |
| --- | --- | --- | --- |
| Grey | `clean` | never selected, or reset via the right-click menu | blocked |
| Blue | `reviewed` | you selected the series and saw the images | allowed |
| Red | `pending` | boxes placed but not committed | blocked |
| Green | `committed` | boxes locked in | allowed |
| Orange | `skipped` | withheld from the export — automatically, or because you said so | withheld |

`skipped` outranks every other status: once a series is withheld, its review state no
longer matters, and it does **not** block the export the way an unreviewed image does. Its
files are simply never written, and the review actions are disabled for it.

A series becomes skipped either **automatically** — by one of the three Export settings,
see [Files boxes cannot de-identify](#files-boxes-cannot-de-identify) — or **by hand**,
via *Set status to Skipped* in the right-click menu. The two are tracked separately, so
changing an Export checkbox never overwrites a decision you made yourself. Only your own
skips can be undone from the menu (*Un-skip series*); to un-skip an automatic one, change
the setting that caused it — the menu entry is disabled there and says so.

Manual skips are session state, like redaction boxes: rescanning or loading another
directory discards them, and warns first.

Everything — selections, commits, per-file results, and exceptions — is logged to the
**Log** tab and to `~/dicompurge.log`.

## Uploading to XNAT

Set the server, user ID and password under the **XNAT Settings** menu, then press
**XNAT Login**. On success the button reads `Logged in as <user>` and the **XNAT server**
tab (after *Log*) fills its project list with the projects you may create subjects in.

The tab lists every loaded **study** — one XNAT session each — with how many of its series
still block it. A study can be uploaded once every series in it is `reviewed`, `committed`
or `skipped`, exactly the rule Export applies. Pick a project (type part of its name or ID
to filter the list), pick a study, accept or edit the **session label**, and press
**Upload to XNAT**.

- **Subject label** is the DICOM `PatientID`, reduced to letters, digits, `_` and `-`.
- **Session label** defaults to `<PatientID>_<modality>_<n>` — the modality of the study's
  first series, `n` counting from 1 per subject and skipping any label the project already
  has. Edit it freely; *Default* puts the generated one back. A label already in the
  project is refused before anything is sent.
- The upload **de-identifies on the fly**: the study's files go through the same
  redaction, document-stripping and skip rules as Export, into the temporary directory,
  and it is that copy that is sent. Nothing under the input directory is uploaded as-is.
- **Headers.** In zip mode the DICOM headers travel as they are; the subject and session
  parameters decide where the files are filed. In individual-file mode XNAT's DICOM
  receiver ignores those parameters and files by the headers — subject from `PatientName`,
  session from `PatientID` — so the staged copy gets `PatientName := subject label` and
  `PatientID := session label` before it is sent. Dates and every other tag are left
  alone, and the input files and Export output are never modified.

Two ways to send, chosen under **Settings → XNAT upload**:

| Mode | Request | Lands in |
| --- | --- | --- |
| One zip per study (default) | one `POST /data/services/import` with `import-handler=DICOM-zip`, `Direct-Archive=true`, `Ignore-Unparsable=true` | the project **archive** (XNAT 1.8.3+; older servers leave it in the prearchive). Direct-Archive is asynchronous — the server builds the session in the background, a few minutes on a busy server — so the job stays in its *Archiving* phase until the session appears in the project, and reports `accepted (archiving in background)` if that takes longer than ten minutes |
| Individual files | one POST per file with `import-handler=gradual-DICOM` and `dest=/prearchive/projects/<id>`; subject and session come from the rewritten headers | the **prearchive**, to review and archive in XNAT (unless the project auto-archives) |

The same settings group sets the **temporary directory** (empty = the system one; it needs
about twice the study's size free while an upload runs) and how many studies upload
**at the same time** (1–8). Further uploads wait in the queue shown on the tab and start
as slots free up; each running upload logs in with its own XNAT session. *Cancel* removes a
waiting upload or stops a running one between files — a zip that is already being sent
completes first. Quitting with uploads still running asks first.

While you stay logged in, the uploads table keeps checking the server once a minute for
every finished row that is not yet `archived`: a prearchive session shows its XNAT state
(`in prearchive (receiving)`, `(ready)`, ...) and flips to `archived` once it lands in the
project, whether the project auto-archived it or you archived it in XNAT. Logging out
stops the checks.

## How the redaction works

For every file in a series with boxes:

- **`(7FE0,0010)` Pixel Data** — decoded to an array, and each box region set to the
  value that renders black (0, or the max stored value for `MONOCHROME1`). Applied to
  every frame of multi-frame instances.
- **`(60xx,3000)` Overlay Data** — every overlay group in `6000`–`601E` is unpacked
  bit-by-bit, the box regions are zeroed, and the plane is repacked. Box coordinates are
  mapped onto the overlay grid using `(60xx,0050)` Overlay Origin, so overlays smaller
  than the image or offset from it are handled correctly. Multi-frame overlays
  (`(60xx,0015)`) are handled too. Overlays are redacted even though only `(7FE0,0010)` is
  shown in the UI.
- **Compressed input (JPEG etc.)** — the frame cannot be edited in place, so the file is
  decoded and rewritten as Explicit VR Little Endian. `YBR_*` data is normalised to `RGB`
  (so that 0,0,0 really is black) and `PhotometricInterpretation` / `PlanarConfiguration`
  are updated to match. This is logged per file.
- `(0028,0301) BurnedInAnnotation` is set to `NO` on redacted files.

Series with no boxes are copied through byte-for-byte — except files carrying an
embedded document, which are rewritten so the document can be removed (see
[Files boxes cannot de-identify](#files-boxes-cannot-de-identify)).

Boxes are stored as fractions of the image (0–1), not screen pixels, so they are immune
to window resizing and to instances within a series having different dimensions.

**Scope note:** this tool redacts *pixels and overlays*, and removes embedded documents
that pixels cannot cover. It does **not** scrub identifying DICOM metadata (patient name,
IDs, dates, UIDs, private tags), which remains present in every exported file — pair it
with a tag-level de-identification step if you need PS3.15 Annex E conformance.

## Tests

```bash
uv run python tests/make_fixtures.py /tmp/fixtures   # synthetic mono / RGB multiframe / JPEG + overlays
uv run python tests/test_redaction.py                # verifies pixels + overlay bits are zeroed
QT_QPA_PLATFORM=offscreen uv run python tests/test_gui_smoke.py
QT_QPA_PLATFORM=offscreen uv run python tests/test_metadata_pane.py
QT_QPA_PLATFORM=offscreen uv run python tests/test_commit_shortcut.py
QT_QPA_PLATFORM=offscreen uv run python tests/test_load_dialog.py   # popup never outlives a load
uv run python tests/test_overlay_display.py
uv run python tests/test_embedded_document.py       # embedded PDFs never survive an export
uv run python tests/test_skipped_series.py          # SR/document series are withheld and shown
uv run python tests/test_xnat_settings.py           # login error wording, no password in the log
uv run python tests/test_xnat_worker.py             # login/logout/listing signals against a fake session
uv run python tests/test_xnat_upload_labels.py      # subject/session label rules
uv run python tests/test_xnat_upload_job.py         # one upload job: staging, zip/individual calls, cleanup
QT_QPA_PLATFORM=offscreen uv run python tests/test_xnat_upload_manager.py  # the concurrency limit and queue
QT_QPA_PLATFORM=offscreen uv run python tests/test_xnat_pane.py            # the tab, end to end with fakes
QT_QPA_PLATFORM=offscreen uv run python tests/test_xnat_login_button.py
QT_QPA_PLATFORM=offscreen uv run python tests/test_xnat_shutdown.py        # logout on quit
QT_QPA_PLATFORM=offscreen uv run python tests/test_upload_settings.py
```

The last two build their own fixtures (`build_documents()` in `tests/make_fixtures.py`)
in a separate root, deliberately: the shared `/tmp/fixtures` set is treated by the other
tests as all-images, and a report object is neither an image nor redactable.

Each of those is a standalone script that prints `PASS`/`FAIL` per check and exits
non-zero on failure. `tests/test_render.py` and `tests/test_sr_render.py` are plain
pytest-style modules of unit tests instead.

## Credits
- Developed by the **CTSI of SE WI**
- If you utilize CTSI resources, please cite the **NIH CTSA; 2UL1TR001436, 2TL1TR001437, 2KL2TR001438** and acknowledge support.
