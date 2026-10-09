# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with the change of using daily dated entries instead of releases.

## 2026-10-09

### Added

- Added a uv-managed Python project skeleton (`agora` package, Python 3.14) with a version-only CLI entry point and pytest/ruff/ty quality gates.
- Added the architecture documentation index defining revision precedence, and implementation notes recording the M0 implementation-location, Python-version, and workflow decisions.
- Laid the roadmap milestones and near-term implementation packages into Beads as dependency-chained epics and tasks.

### Changed

- Aligned ruff, pytest, and ty configuration with the shared data-processing toolchain conventions: expanded lint rule set (`E,F,I,UP,B,SIM,C4,TID252,RUF`), line length 100, quiet test output, bounded dev-tool versions, explicit ty Python pin.

### Fixed

### Removed
