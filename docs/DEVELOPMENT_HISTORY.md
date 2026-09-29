# PiBook Development History

This document reconstructs the development history of this PiBook fork
from the development records and validated device state.

It is not a synthetic Git commit history. Dates describe development
milestones and validation periods, while the original Git repository
remained largely uncommitted during this work.

## Starting point

PiBook started from:

    https://github.com/rolohaun/PiBook

Local starting commit:

    1c94596

The original repository was cloned with shallow history and remained
configured as `origin` during the initial development period.

## 2026-08-03 — Raspberry Pi Zero W hardware adaptation

The project was adapted and validated on:

- Raspberry Pi Zero W 1.1
- ARMv6
- Waveshare 7.5" e-Paper V2, 800x480
- Waveshare UPS HAT (C)
- approximately 4000 mAh LiPo battery

Work included:

- adapting the display stack to `epd7in5_V2`
- establishing Pi Zero W compatibility
- integrating Waveshare UPS HAT C / INA219 battery monitoring
- validating real battery voltage/percentage behaviour

This became the hardware baseline for the current PiBook.

## 2026-08-04 — Core device operation

Validated work included:

- clearing the e-paper display during shutdown
- adapting the Flask/web interface for Pi Zero W
- physical navigation using GPIO buttons
- EPUB reader fixes
- remote-control fixes

The project was already diverging significantly from the original
hardware assumptions.

## 2026-08-07 — Display/SPI stability and network recovery

The Waveshare `epdconfig.py` was modified so display controller
reinitialization reuses a single SPI descriptor instead of repeatedly
opening `/dev/spidev0.0`.

The current implementation is preserved as:

    patches/waveshare/epdconfig-spi-descriptor-reuse.patch

The validated Waveshare vendor revision is:

    e63d959eaf2babcdb25d03ed1c6d29dadbc1ce98

A PiBook network watchdog was also installed and validated.

The watchdog architecture later used:

- periodic systemd timer execution
- failure confirmation
- recovery cooldown
- hourly recovery limits

## 2026-08-08 to 2026-08-14 — Reader, network and web evolution

Development during this period expanded several subsystems:

- EPUB reader behaviour
- e-paper FULL/PARTIAL refresh handling
- Wi-Fi and hotspot handling
- network recovery
- Bluetooth policy
- web management interface
- remote controls
- application navigation

By this point the working tree had diverged sufficiently from upstream
that directly pulling the original repository was considered unsafe.

The original repository remained useful as upstream ancestry, but no
longer represented the active PiBook implementation.

### Terminal Web v2

The web terminal was extended and validated with features including:

- multiline input
- streamed output
- command stopping
- command history
- persistent output
- PiBook restart flow returning to the terminal interface

## 2026-08-16 — Boot and network timing work

Boot measurements identified network initialization as a significant
part of startup time.

Measured development observations included:

- NetworkManager startup around 24 s
- PiBook network startup around 28.8 s

Further boot optimization work followed from these measurements.

Not every experimental boot optimization from this period became part
of the final validated configuration.

## 2026-08-17 to 2026-08-18 — Battery safety and diagnostics

Battery monitoring evolved beyond simple voltage reporting.

Development included:

- Waveshare UPS HAT C / INA219 backend
- charge/discharge current handling
- SOC persistence
- battery-state persistence
- smoothing and filtering
- warning and critical thresholds
- battery diagnostics
- battery cycle logging
- safe shutdown logic

The critical-battery warning and safe shutdown were later validated
during a real discharge cycle.

Battery-cycle logs remain development/runtime data and are not stored
in Git by default.

## 2026-08-19 to 2026-08-20 — Early e-paper splash

A sequence of initramfs experiments moved the first visible e-paper
output much earlier in the boot process.

The final validated implementation is:

    early_splash_visual_v1_VALIDATED_20260820

The current active initramfs is byte-for-byte identical to that
validated image.

Active initramfs SHA-256:

    b5d46ebc1156651bce13b90531727ab1b084d15c4b32be7db6124e20891fc38c

Architecture:

- standard Raspbian kernel
- custom PiBook initramfs integration
- early SPI initialization
- GPIO character-device access
- `/dev/spidev0.0`
- C e-paper helper
- background refresh
- no wait for splash completion inside initramfs

Validated timing:

- hook start: 5.77 s
- hardware ready: 5.90 s
- helper start: 5.916 s
- refresh start: 6.123 s
- splash physically visible: 7.883 s

The early-splash source and assets now live under:

    system/initramfs/early-splash/

The complete binary initramfs is intentionally not part of normal
source control.

## Late August — Network tooling and IP scanner

The PiBook network stack developed into a system-level subsystem with:

- Wi-Fi scanning
- Wi-Fi connection control
- hotspot/captive-portal handling
- network startup
- network watchdog
- web-visible network state
- IP scanning
- hostname enrichment

The IP scanner supports multiple discovery mechanisms and is exposed
through the PiBook web application.

System-level helpers are preserved under:

    scripts/system/

Systemd units and drop-ins are preserved under:

    scripts/systemd/

## 2026-09 — Power management

Power management evolved into configurable profiles.

Power Profiles v2 includes:

- mains profile
- battery profile
- powersave profile
- sleep behaviour
- network behaviour while reading
- network behaviour during sleep
- reader prefetch settings
- automatic powersave threshold behaviour

The active development device currently uses `balanced` as its base
power mode.

## Current development state

PiBook remains under active development.

The current source tree includes substantial changes to:

- reader and EPUB processing
- display refresh behaviour
- navigation and e-paper UI
- battery monitoring and safety
- GPIO handling
- power management
- Wi-Fi and network services
- IP scanning
- web interface
- remote interface
- systemd integration
- boot optimization
- early initramfs splash

This state is a development baseline, not a final release.

## Current major pending work

Known development areas still include:

- e-paper UI v2 and visual consistency
- common header/content/footer screen architecture
- measured power-consumption optimization
- idle and active power reduction
- battery-curve calibration using accumulated cycle logs
- measurement of UPS HAT physical-button power cost
- PiBook enclosure / physical structure v2
- potential hardware improvements
- outdoor e-paper readability and usability testing
- general source-tree cleanup and architectural reorganization
- continued boot optimization
- reproducible installation from a clean Raspberry Pi OS image

## GitHub migration

The migration strategy is:

1. preserve the current validated device state
2. separate source from runtime/generated/private data
3. capture all required system-level integration
4. make third-party dependencies reproducible
5. retain the original PiBook project as upstream
6. move the user's fork to `origin`
7. compare current upstream development against the original base
8. selectively port useful upstream changes
9. continue active PiBook development from the new fork

Upstream changes must not be blindly merged into the active PiBook
working tree.
