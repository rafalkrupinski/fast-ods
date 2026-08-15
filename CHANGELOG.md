# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- New parsing algorithm based on `xml.parsers.expat`.

## [0.3.0]

### Changed

- New parsing algorithm, with an approximate 3x increase in performance on tests*

### Fixed

- Fixed the skip/take N rows logic, aligning it to user expectations and to better reflect how rows are displayed in spreadsheet software
