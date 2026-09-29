# PiBook

PiBook is an experimental Raspberry Pi Zero W e-reader built around a
Waveshare 7.5-inch V2 e-paper display.

This repository is an actively developed fork of the original
`rolohaun/PiBook` project. It contains substantial hardware, software,
networking, power-management and boot-time changes developed for the current
PiBook prototype.

## Current hardware baseline

- Raspberry Pi Zero W 1.1
- Waveshare 7.5-inch e-Paper V2
- Waveshare UPS HAT C
- LiPo battery around 4000 mAh
- Raspberry Pi OS / Raspbian 13 (Trixie)
- ARMv6

The validated system state is documented in `docs/SYSTEM_BASELINE.md`.

## Current features

- EPUB reading and persistent reading progress
- physical-button navigation
- partial and full e-paper refresh management
- local web interface
- Wi-Fi and hotspot management
- captive portal
- IP/network scanner
- remote controls and terminal tools
- battery monitoring and safety shutdown
- battery-cycle logging
- configurable power profiles
- boot guard and boot diagnostics
- early e-paper boot splash

PiBook is still under active development and should not yet be considered a
final consumer-ready release.

## Installation

The supported installation entry point is:

`sudo ./scripts/install/install.sh`

The installation is split into:

- `scripts/install/install-packages.sh`
- `scripts/install/install-waveshare.sh`
- `scripts/install/install-system.sh`
- `scripts/install/install-early-splash.sh`
- `scripts/install/install.sh`

The current reproducible baseline deliberately assumes:

- user: `pi`
- project: `/home/pi/PiBook`

Support for arbitrary users and installation paths is future work.

## Python environment

The Raspberry Pi Zero W baseline uses Debian / Raspberry Pi OS Python packages
inside a virtual environment created with `--system-site-packages`.

`requirements.txt` is therefore a Python dependency reference. It is not the
supported installation method for the Pi Zero W baseline.

Optional or legacy backends are listed separately in
`requirements-optional.txt`.

## Waveshare driver

The large Waveshare upstream repository is not included directly in this
repository.

The installer retrieves the exact upstream revision recorded in
`patches/waveshare/VENDOR_COMMIT` and applies the PiBook patch
`patches/waveshare/epdconfig-spi-descriptor-reuse.patch`.

The patch reuses the SPI descriptor across display reinitialisation to avoid
repeated `/dev/spidev0.0` opens.

## Network configuration

Real Wi-Fi credentials and NetworkManager identifiers are never stored in Git.

Public configuration sources are under `system/network/`.

A new installation must create local
`/etc/pibook-network/config.json` and
`/etc/pibook-network/startup.json` from the supplied examples before network
startup/watchdog behaviour can be fully enabled.

## Early e-paper splash

The early splash is built from source.

The validated design inserts a small `newc` CPIO overlay immediately before
the normal initramfs ZSTD stream, leaving the original compressed stream
unchanged.

The early-splash helper is reproducible byte-for-byte from the versioned C
source for the validated baseline.

Running `./scripts/install/install-early-splash.sh build` creates
`build/early-splash/initramfs-pibook-candidate`.

It does not replace the active boot initramfs. Promotion and boot testing are
intentionally separate manual operations.

## Runtime data

Personal and generated runtime data is excluded from Git, including books,
logs, backups, diagnostics, build output, reading progress, battery state,
local settings, Wi-Fi credentials and NetworkManager profile identifiers.

Required empty directories are retained using `.gitkeep` files.

## Development history

This fork was developed for a significant period before its Git history was
prepared for publication.

Rather than creating artificial retroactive commits for experiments, reverts
and intermediate states, the reconstructed development history is documented
in `docs/DEVELOPMENT_HISTORY.md`.

The first fork commit represents the current validated development baseline.
Future development should use normal incremental commits.

## Current development priorities

Planned work includes:

- e-paper interface v2
- measured battery and power optimisation
- battery-curve calibration from real cycle logs
- UPS HAT physical-button consumption measurement
- enclosure / physical structure v2
- hardware improvement analysis
- outdoor e-paper readability testing
- further boot optimisation
- code and architecture cleanup

## Original project and licence

This project is based on the original `rolohaun/PiBook` project.

The original repository README states that the project is distributed under
the MIT License. The original revision used as the base of this fork did not
contain a standalone `LICENSE` file, so this repository preserves the original
attribution and licence statement rather than inventing copyright metadata.
