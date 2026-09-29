# PiBook System Baseline

This document describes the validated development baseline used while
preparing the PiBook fork for GitHub.

## Hardware

- Raspberry Pi Zero W 1.1
- Waveshare 7.5" e-Paper V2
- Waveshare UPS HAT (C)
- LiPo battery: approximately 4000 mAh

## Operating system

- Raspbian GNU/Linux 13 (trixie)
- ARMv6
- Python 3.13

## Display

Logical display:

- Width: 480
- Height: 800
- Rotation: 90 degrees

Driver:

- Waveshare `epd7in5_V2`

## Waveshare dependency

Validated Waveshare e-Paper repository revision:

    e63d959eaf2babcdb25d03ed1c6d29dadbc1ce98

PiBook applies:

    patches/waveshare/epdconfig-spi-descriptor-reuse.patch

The patch prevents repeated opening of `/dev/spidev0.0` when switching
between full and partial display modes.

## Battery

Default hardware backend:

    waveshare_ups_hat_c

INA219:

- I2C bus: 1
- Address: 0x43
- Shunt: 0.01 ohm
- Battery capacity: 4000 mAh

PiSugar2 and ADS1115 support remain optional compatibility backends.

## Python environment

The validated development device currently uses a virtual environment
created with `--system-site-packages`.

System Python packages are therefore intentionally visible inside the
PiBook virtual environment.

A future isolated Python environment must be validated separately on
Raspberry Pi Zero W / ARMv6 before replacing this setup.

## Early e-paper splash

The active PiBook initramfs includes:

    usr/bin/pibook-early-splash
    scripts/init-top/pibook-early-splash-run

Validated source material is stored under:

    system/initramfs/early-splash/

Validated active initramfs SHA-256:

    b5d46ebc1156651bce13b90531727ab1b084d15c4b32be7db6124e20891fc38c

Validated splash timing:

- hook start: 5.77 s
- hardware ready: 5.90 s
- helper start: 5.916 s
- refresh start: 6.123 s
- physically visible: 7.883 s

The splash helper runs in the background so initramfs boot can continue.

## System integration

Repository sources for system integration are stored under:

    scripts/system/
    scripts/systemd/
    system/modprobe.d/
    system/modules-load.d/
    system/tmpfiles.d/
    system/initramfs/

Installed runtime copies live under:

    /usr/local/sbin/
    /etc/systemd/system/
    /etc/modprobe.d/
    /etc/modules-load.d/
    /etc/tmpfiles.d/

## Development status

PiBook is still under active development.

This baseline is not a final release and should not be interpreted as
version 1.0.
