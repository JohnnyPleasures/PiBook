# Waveshare e-Paper dependency

PiBook uses the official Waveshare e-Paper repository as a third-party
dependency.

Validated vendor revision:

    e63d959eaf2babcdb25d03ed1c6d29dadbc1ce98

PiBook applies `epdconfig-spi-descriptor-reuse.patch` after installing
`waveshare_epd`.

The patch keeps a single SPI descriptor open across full/partial display
controller reinitializations, avoiding repeated `/dev/spidev0.0` opens.

The Waveshare source tree itself is not vendored in this repository.
