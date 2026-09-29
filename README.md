# PiBook

PiBook is a DIY e-reader built around a Raspberry Pi Zero W and a 7.5-inch Waveshare e-paper display.

This repository is an actively developed fork of the original `rolohaun/PiBook` project, adapted and expanded for a custom PiBook prototype with a strong focus on e-paper reading, battery operation, local connectivity and a lightweight interface suitable for the Raspberry Pi Zero.

The project is functional and usable, but development is still ongoing.

## Hardware

The current PiBook prototype uses:

- Raspberry Pi Zero W 1.1
- Waveshare 7.5-inch e-Paper V2
- Waveshare UPS HAT C
- LiPo battery around 4000 mAh
- microSD storage
- physical GPIO buttons

The current software baseline is Raspberry Pi OS / Raspbian 13 (Trixie) on ARMv6.

## Features

PiBook currently includes:

- EPUB reading
- persistent reading progress
- e-paper optimised navigation
- partial and full display refresh
- physical button controls
- book library management
- local web interface
- remote navigation and controls
- Wi-Fi management
- hotspot mode and captive portal
- network/IP scanner
- battery monitoring
- low-battery protection and safe shutdown
- battery cycle logging
- configurable power profiles
- boot diagnostics and recovery mechanisms
- early e-paper boot splash

## Installation

The current installation entry point is:

`sudo ./scripts/install/install.sh`

The installer prepares the required system packages, Python environment, Waveshare display driver and PiBook system integration.

The current installation baseline assumes the `pi` user and the project located at:

`/home/pi/PiBook`

Support for arbitrary users and installation paths is planned for a future revision.

### Local configuration

Personal configuration is not included in the repository.

After installation, local Wi-Fi settings must be configured on the device and books must be added by the user.

Example network configuration files are available under:

`system/network/`

## Waveshare e-paper support

PiBook uses the official Waveshare e-paper driver with a small project-specific patch.

The exact upstream driver revision and patch are stored under:

`patches/waveshare/`

The complete Waveshare repository is downloaded during installation rather than stored directly in this repository.

## Early boot splash

PiBook can display an e-paper splash screen during the early Linux boot process.

The splash is built from the source files under:

`system/initramfs/early-splash/`

Building the splash creates a separate initramfs candidate and does not automatically replace the active boot image.

## Documentation

More detailed technical information is available in:

- `docs/SYSTEM_BASELINE.md` — current validated hardware and system baseline
- `docs/DEVELOPMENT_HISTORY.md` — reconstructed development history before this fork began using regular Git commits
- `docs/POWER_OPTIMIZATION_LEGACY.md` — retained notes from earlier power-management experiments
- `system/network/README.md` — network configuration and architecture
- `patches/waveshare/README.md` — Waveshare driver patch information

## Project status

PiBook is still under active development.

Current areas of work include:

- redesigned and more consistent e-paper interface
- further battery and power optimisation
- battery percentage calibration using real charge/discharge cycle data
- improved boot performance
- enclosure / physical design v2
- hardware improvement experiments
- improved outdoor e-paper readability
- general code and architecture cleanup

## Development

The first commit in this fork represents the validated development baseline that existed before the project was prepared for publication.

From that point onward, development uses normal incremental Git commits.

Generated files, local settings and personal data are intentionally kept outside the repository.

## Original project and licence

PiBook is based on the original `rolohaun/PiBook` project.

The original project states that it is distributed under the MIT License. This fork preserves the original project attribution and licence statement.

Original project:

`https://github.com/rolohaun/PiBook`
