# LibUpdate

A dependency update checker that scans projects for outdated libraries and generates reports.

## Usage

```
python LibUpdate.py [scan_path] [options]
```

### Options

| Option        | Short | Description                                       |
|---------------|-------|---------------------------------------------------|
| `--file PATH` | `-f`  | Output file path (default: `libupdate-report.md`) |
| `--text`      | `-t`  | Output plain text instead of Markdown             |
| `--dry-run`   | `-n`  | Preview report without creating a file            |
| `--overwrite` | `-y`  | Overwrite existing file without prompting         |
| `--verbose`   | `-v`  | Show progress while checking dependencies         |

### Examples

```bash
# Scan current directory, output to libupdate-report.md
python LibUpdate.py .

# Scan with custom output filename
python LibUpdate.py . --file report.md

# Output plain text format
python LibUpdate.py . --text

# Preview report without creating file
python LibUpdate.py . --dry-run

# Overwrite existing file without prompting
python LibUpdate.py . --overwrite
```

## Supported Ecosystems

### Java (Maven Central)

- **Maven**: `pom.xml`
- **Gradle**: `build.gradle`, `build.gradle.kts`, version catalogs
- **SBT**: `build.sbt`
- **Mill**: `build.sc`
- **Ivy**: `ivy.xml`
- **Grape**: `@Grab` annotations in Groovy scripts
- **Leiningen**: `project.clj`
- **Buildr**: `buildfile`

### Python (PyPI)

- **pip**: `requirements.txt`

## Features

- Scans directories recursively for dependency files
- Checks current versions against latest available versions
- Groups results by project and ecosystem
- Generates structured markdown reports with:
    - Summary statistics (updatable, up-to-date, unknown/error)
    - Discovered files by project
    - Detailed update information with source URLs

## Output

### Console Summary

After scanning, a summary is displayed in the console:

```
==================================================
SCAN SUMMARY
==================================================
  Files scanned:        2
  Dependencies found:   4
--------------------------------------------------
  Updatable:            4
  Up-to-date:           0
  Unknown/Error:        0
==================================================

4 dependencies have updates available.
```

### Report File

The generated report (Markdown or plain text) includes:

- **Summary**: Total dependencies checked, updatable count, up-to-date count, errors
- **Discovered files**: Lists all found dependency files organized by project
- **Update results**: Per-project, per-ecosystem breakdown of available updates with links to package sources
