# Changelog

All notable changes to DicomPurge are documented in this file.

## [Unreleased]

### Changed
- Redacted files keep their compression: JPEG Baseline (re-encoded at quality 95, so one extra generation of loss), RLE Lossless and JPEG 2000 stay as they were; JPEG Lossless and JPEG-LS become JPEG 2000 Lossless. Formats with no encoder still fall back to uncompressed, with a warning. This stops compressed studies growing several-fold on export and in the XNAT zip.

### Fixed
- Uploading a study whose zip exceeds 2 GiB no longer breaks progress reporting (32-bit overflow in the progress signal and bar).
- An unhandled error on a worker thread now shows its dialog on the GUI thread instead of crashing the app on macOS; `threading.excepthook` is routed through the same path.

## [0.1.0] - 2026-09-07

### Added
- XNAT project and experiment label listing, session status checks, and upload support.
- License definitions and metadata added to the repository.

### Fixed
- Additional DICOM transfer syntaxes now handled correctly, with improved redaction logic.

## [0.0.5] - 2026-09-01

### Added
- XNAT login and a Settings dropdown for XNAT configuration, with automatic logout on program shutdown.
- Right-click "Skip" option on any series.
- Option to skip fully encapsulated PDFs that can't be displayed (and cases where a PDF and DICOM image share a file), plus optional skipping of reports.
- Settings → Export: new "Skip files with no image data" checkbox (default off); such series show the orange skipped status, are withheld from export, and no longer block it.
- About screen, and the About menu item renamed to "About dicompurge".
- `bump.sh` script for streamlined version management.

### Changed
- Project renamed to DicomPurge.
- PREFETCH settings moved into the new Settings dropdown.

### Fixed
- Performance issues with large datasets; a loading popup now displays while large series load.
- "Loading series" popup no longer outlives the load it's tracking.

## [0.0.4] - 2026-08-25

### Added
- Provenance hook script and startup script for de-identification.
- `bump-my-version` configuration and dev dependencies for version management.

### Changed
- Clearer project description and package inclusion in `pyproject.toml`.
- README updated to document structured report rendering, non-image DICOM handling, and refined tool description with a link to the Scuppernong River Wikipedia page.

## [0.0.3] - 2026-08-24

### Added
- Structured report rendering support, with updated view logic for non-image DICOM files.

### Changed
- Updated Scuppernong logo in assets.

## [0.0.2] - 2026-08-24

### Added
- Metadata tab for DICOM tag display, with corresponding tests.
- Cmd+S (Ctrl+S) shortcut for committing a series.
- ROI overlay type handling: ROI overlays are preserved while only graphics overlays are redacted, with corresponding tests and documentation.
- Credits section in README with citation and support acknowledgment.
- UI screenshot in README.

## [0.0.1] - 2026-08-22

### Added
- Initial release of the DICOM de-identification tool (Scuppernong).
- Mouse wheel scrolling to navigate image frames.
- Project logo and screenshots.
