#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <linux/spi/spidev.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define LOG_W 480
#define LOG_H 800

#define EPD_W 800
#define EPD_H 480
#define EPD_STRIDE (EPD_W / 8)
#define FB_SIZE (EPD_W * EPD_H / 8)

#define GPIO_RST   17
#define GPIO_PWR   18
#define GPIO_BUSY  24
#define GPIO_DC    25

#define SPI_SPEED 4000000U
#define SPI_CHUNK 4096

static double monotonic_seconds(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return -1.0;

    return (double)ts.tv_sec +
           (double)ts.tv_nsec / 1000000000.0;
}

static void sleep_ms(unsigned int ms)
{
    struct timespec req;

    req.tv_sec = ms / 1000;
    req.tv_nsec = (long)(ms % 1000) * 1000000L;

    while (nanosleep(&req, &req) != 0 && errno == EINTR)
        ;
}

static void klog_msg(const char *fmt, ...)
{
    int fd;
    char body[384];
    char line[448];
    va_list ap;
    double t;

    va_start(ap, fmt);
    vsnprintf(body, sizeof(body), fmt, ap);
    va_end(ap);

    t = monotonic_seconds();

    snprintf(
        line,
        sizeof(line),
        "<6>PIBOOK_EARLY_SPLASH t=%.3f %s\n",
        t,
        body
    );

    fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);

    if (fd >= 0) {
        (void)write(fd, line, strlen(line));
        close(fd);
    }
}

static int gpio_request_line(
    int chip_fd,
    unsigned int offset,
    uint64_t flags,
    int initial_value
)
{
    struct gpio_v2_line_request req;

    memset(&req, 0, sizeof(req));

    req.offsets[0] = offset;
    req.num_lines = 1;

    snprintf(
        req.consumer,
        sizeof(req.consumer),
        "pibook-early-splash"
    );

    req.config.flags = flags;

    if (flags & GPIO_V2_LINE_FLAG_OUTPUT) {
        req.config.num_attrs = 1;

        req.config.attrs[0].attr.id =
            GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES;

        req.config.attrs[0].attr.values =
            initial_value ? 1ULL : 0ULL;

        req.config.attrs[0].mask = 1ULL;
    }

    if (ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, &req) < 0)
        return -1;

    return req.fd;
}

static int gpio_set(int line_fd, int value)
{
    struct gpio_v2_line_values values;

    memset(&values, 0, sizeof(values));

    values.mask = 1ULL;
    values.bits = value ? 1ULL : 0ULL;

    return ioctl(
        line_fd,
        GPIO_V2_LINE_SET_VALUES_IOCTL,
        &values
    );
}

static int gpio_get(int line_fd)
{
    struct gpio_v2_line_values values;

    memset(&values, 0, sizeof(values));

    values.mask = 1ULL;

    if (ioctl(
            line_fd,
            GPIO_V2_LINE_GET_VALUES_IOCTL,
            &values
        ) < 0)
        return -1;

    return (values.bits & 1ULL) ? 1 : 0;
}

static int spi_write_all(
    int spi_fd,
    const uint8_t *data,
    size_t length
)
{
    while (length > 0) {
        size_t chunk =
            length > SPI_CHUNK ? SPI_CHUNK : length;

        ssize_t written =
            write(spi_fd, data, chunk);

        if (written < 0)
            return -1;

        if (written == 0) {
            errno = EIO;
            return -1;
        }

        data += written;
        length -= (size_t)written;
    }

    return 0;
}

static int epd_command(
    int spi_fd,
    int dc_fd,
    uint8_t command
)
{
    if (gpio_set(dc_fd, 0) < 0)
        return -1;

    return spi_write_all(
        spi_fd,
        &command,
        1
    );
}

static int epd_data_byte(
    int spi_fd,
    int dc_fd,
    uint8_t data
)
{
    if (gpio_set(dc_fd, 1) < 0)
        return -1;

    return spi_write_all(
        spi_fd,
        &data,
        1
    );
}

static int epd_data_buffer(
    int spi_fd,
    int dc_fd,
    const uint8_t *data,
    size_t length
)
{
    if (gpio_set(dc_fd, 1) < 0)
        return -1;

    return spi_write_all(
        spi_fd,
        data,
        length
    );
}

static int epd_wait_busy(
    int spi_fd,
    int dc_fd,
    int busy_fd,
    unsigned int timeout_ms
)
{
    double start = monotonic_seconds();

    /*
     * Waveshare Python driver:
     *
     * send_command(0x71)
     * busy = digital_read(BUSY)
     * while busy == 0:
     *     send_command(0x71)
     *     busy = digital_read(BUSY)
     *
     * BUSY=1 significa pronto.
     */

    for (;;) {
        int busy;

        if (epd_command(spi_fd, dc_fd, 0x71) < 0)
            return -1;

        busy = gpio_get(busy_fd);

        if (busy < 0)
            return -1;

        if (busy == 1) {
            sleep_ms(20);
            return 0;
        }

        if (
            (monotonic_seconds() - start) * 1000.0
            >= timeout_ms
        ) {
            errno = ETIMEDOUT;
            return -1;
        }

        sleep_ms(2);
    }
}

static int epd_reset(int rst_fd)
{
    if (gpio_set(rst_fd, 1) < 0)
        return -1;

    sleep_ms(20);

    if (gpio_set(rst_fd, 0) < 0)
        return -1;

    sleep_ms(2);

    if (gpio_set(rst_fd, 1) < 0)
        return -1;

    sleep_ms(20);

    return 0;
}

static int epd_init_fast(
    int spi_fd,
    int rst_fd,
    int dc_fd,
    int busy_fd
)
{
    if (epd_reset(rst_fd) < 0)
        return -1;

    if (epd_command(spi_fd, dc_fd, 0x00) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x1F) < 0)
        return -1;

    if (epd_command(spi_fd, dc_fd, 0x50) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x10) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x07) < 0)
        return -1;

    if (epd_command(spi_fd, dc_fd, 0x04) < 0)
        return -1;

    sleep_ms(100);

    if (
        epd_wait_busy(
            spi_fd,
            dc_fd,
            busy_fd,
            8000
        ) < 0
    )
        return -1;

    if (epd_command(spi_fd, dc_fd, 0x06) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x27) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x27) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x18) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x17) < 0)
        return -1;

    if (epd_command(spi_fd, dc_fd, 0xE0) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x02) < 0)
        return -1;

    if (epd_command(spi_fd, dc_fd, 0xE5) < 0)
        return -1;

    if (epd_data_byte(spi_fd, dc_fd, 0x5A) < 0)
        return -1;

    return 0;
}

static int epd_display(
    int spi_fd,
    int dc_fd,
    int busy_fd,
    const uint8_t *framebuffer
)
{
    uint8_t inverse[SPI_CHUNK];
    size_t offset;

    /*
     * Reproduz exatamente o driver Python do PiBook:
     *
     * 0x10 -> ~image
     * 0x13 -> image
     */

    if (epd_command(spi_fd, dc_fd, 0x10) < 0)
        return -1;

    for (offset = 0; offset < FB_SIZE;) {
        size_t chunk =
            FB_SIZE - offset > SPI_CHUNK
            ? SPI_CHUNK
            : FB_SIZE - offset;

        size_t i;

        for (i = 0; i < chunk; i++)
            inverse[i] =
                (uint8_t)~framebuffer[offset + i];

        if (
            epd_data_buffer(
                spi_fd,
                dc_fd,
                inverse,
                chunk
            ) < 0
        )
            return -1;

        offset += chunk;
    }

    if (epd_command(spi_fd, dc_fd, 0x13) < 0)
        return -1;

    if (
        epd_data_buffer(
            spi_fd,
            dc_fd,
            framebuffer,
            FB_SIZE
        ) < 0
    )
        return -1;

    if (epd_command(spi_fd, dc_fd, 0x12) < 0)
        return -1;

    sleep_ms(100);

    if (
        epd_wait_busy(
            spi_fd,
            dc_fd,
            busy_fd,
            12000
        ) < 0
    )
        return -1;

    return 0;
}

/*
 * O buffer abaixo usa a mesma semantica de getbuffer():
 *
 * bit 0 = branco
 * bit 1 = preto
 *
 * Transformacao logica -> fisica:
 *
 * PiBook:
 *   480x800
 *   rotation=90
 *
 * DisplayDriver:
 *   PIL rotate(-90, expand=True)
 *
 * Portanto:
 *   physical_x = 799 - logical_y
 *   physical_y = logical_x
 */

static void set_logical_pixel(
    uint8_t *fb,
    int x,
    int y,
    int black
)
{
    int px;
    int py;
    size_t index;
    uint8_t mask;

    if (
        x < 0 ||
        x >= LOG_W ||
        y < 0 ||
        y >= LOG_H
    )
        return;

    px = (LOG_H - 1) - y;
    py = x;

    index =
        (size_t)py * EPD_STRIDE +
        (size_t)(px / 8);

    mask =
        (uint8_t)(0x80U >> (px & 7));

    if (black)
        fb[index] |= mask;
    else
        fb[index] &= (uint8_t)~mask;
}

static void fill_rect(
    uint8_t *fb,
    int x,
    int y,
    int w,
    int h
)
{
    int yy;
    int xx;

    for (yy = y; yy < y + h; yy++)
        for (xx = x; xx < x + w; xx++)
            set_logical_pixel(
                fb,
                xx,
                yy,
                1
            );
}

static void draw_rect(
    uint8_t *fb,
    int x,
    int y,
    int w,
    int h,
    int thickness
)
{
    fill_rect(fb, x, y, w, thickness);

    fill_rect(
        fb,
        x,
        y + h - thickness,
        w,
        thickness
    );

    fill_rect(fb, x, y, thickness, h);

    fill_rect(
        fb,
        x + w - thickness,
        y,
        thickness,
        h
    );
}

static const uint8_t glyph_P[7] = {
    0x1E,
    0x11,
    0x11,
    0x1E,
    0x10,
    0x10,
    0x10
};

static const uint8_t glyph_I[7] = {
    0x1F,
    0x04,
    0x04,
    0x04,
    0x04,
    0x04,
    0x1F
};

static const uint8_t glyph_B[7] = {
    0x1E,
    0x11,
    0x11,
    0x1E,
    0x11,
    0x11,
    0x1E
};

static const uint8_t glyph_O[7] = {
    0x0E,
    0x11,
    0x11,
    0x11,
    0x11,
    0x11,
    0x0E
};

static const uint8_t glyph_K[7] = {
    0x11,
    0x12,
    0x14,
    0x18,
    0x14,
    0x12,
    0x11
};

static const uint8_t *glyph_for(char c)
{
    switch (c) {
    case 'P':
        return glyph_P;
    case 'I':
        return glyph_I;
    case 'B':
        return glyph_B;
    case 'O':
        return glyph_O;
    case 'K':
        return glyph_K;
    default:
        return NULL;
    }
}

static void draw_char(
    uint8_t *fb,
    int x,
    int y,
    char c,
    int scale
)
{
    const uint8_t *glyph =
        glyph_for(c);

    int row;
    int col;

    if (!glyph)
        return;

    for (row = 0; row < 7; row++) {
        for (col = 0; col < 5; col++) {
            if (
                glyph[row] &
                (uint8_t)(1U << (4 - col))
            ) {
                fill_rect(
                    fb,
                    x + col * scale,
                    y + row * scale,
                    scale,
                    scale
                );
            }
        }
    }
}

static void draw_text(
    uint8_t *fb,
    int x,
    int y,
    const char *text,
    int scale
)
{
    int cursor = x;

    while (*text) {
        draw_char(
            fb,
            cursor,
            y,
            *text,
            scale
        );

        cursor += 6 * scale;
        text++;
    }
}

static void render_splash(uint8_t *fb)
{
    const char *title = "PIBOOK";

    int scale = 10;

    int title_width =
        (int)strlen(title) *
        6 *
        scale -
        scale;

    int title_x =
        (LOG_W - title_width) / 2;

    int title_y = 335;

    int line_width = 300;
    int line_height = 4;

    int line_x =
        (LOG_W - line_width) / 2;

    int line_y = 445;

    /*
     * Fundo branco.
     *
     * Sem rebordo nem marcador:
     * esses elementos pertenciam apenas ao diagnóstico inicial.
     */
    memset(fb, 0x00, FB_SIZE);

    draw_text(
        fb,
        title_x,
        title_y,
        title,
        scale
    );

    fill_rect(
        fb,
        line_x,
        line_y,
        line_width,
        line_height
    );
}

static int write_framebuffer(
    const char *path,
    const uint8_t *fb
)
{
    int fd;
    size_t done = 0;

    fd = open(
        path,
        O_WRONLY |
        O_CREAT |
        O_TRUNC |
        O_CLOEXEC,
        0644
    );

    if (fd < 0)
        return -1;

    while (done < FB_SIZE) {
        ssize_t n =
            write(
                fd,
                fb + done,
                FB_SIZE - done
            );

        if (n < 0) {
            close(fd);
            return -1;
        }

        if (n == 0) {
            close(fd);
            errno = EIO;
            return -1;
        }

        done += (size_t)n;
    }

    close(fd);
    return 0;
}

static int configure_spi(int spi_fd)
{
    uint8_t mode = SPI_MODE_0;
    uint8_t bits = 8;
    uint32_t speed = SPI_SPEED;

    if (
        ioctl(
            spi_fd,
            SPI_IOC_WR_MODE,
            &mode
        ) < 0
    )
        return -1;

    if (
        ioctl(
            spi_fd,
            SPI_IOC_WR_BITS_PER_WORD,
            &bits
        ) < 0
    )
        return -1;

    if (
        ioctl(
            spi_fd,
            SPI_IOC_WR_MAX_SPEED_HZ,
            &speed
        ) < 0
    )
        return -1;

    return 0;
}

static int run_hardware(const uint8_t *fb)
{
    int chip_fd = -1;
    int spi_fd = -1;

    int rst_fd = -1;
    int pwr_fd = -1;
    int busy_fd = -1;
    int dc_fd = -1;

    int rc = 1;

    klog_msg("START");

    chip_fd =
        open(
            "/dev/gpiochip0",
            O_RDONLY | O_CLOEXEC
        );

    if (chip_fd < 0) {
        klog_msg(
            "ERROR open gpiochip0 errno=%d",
            errno
        );
        goto out;
    }

    rst_fd =
        gpio_request_line(
            chip_fd,
            GPIO_RST,
            GPIO_V2_LINE_FLAG_OUTPUT,
            1
        );

    pwr_fd =
        gpio_request_line(
            chip_fd,
            GPIO_PWR,
            GPIO_V2_LINE_FLAG_OUTPUT,
            1
        );

    dc_fd =
        gpio_request_line(
            chip_fd,
            GPIO_DC,
            GPIO_V2_LINE_FLAG_OUTPUT,
            0
        );

    busy_fd =
        gpio_request_line(
            chip_fd,
            GPIO_BUSY,
            GPIO_V2_LINE_FLAG_INPUT,
            0
        );

    if (
        rst_fd < 0 ||
        pwr_fd < 0 ||
        dc_fd < 0 ||
        busy_fd < 0
    ) {
        klog_msg(
            "ERROR request GPIO errno=%d",
            errno
        );
        goto out;
    }

    spi_fd =
        open(
            "/dev/spidev0.0",
            O_RDWR | O_CLOEXEC
        );

    if (spi_fd < 0) {
        klog_msg(
            "ERROR open spidev errno=%d",
            errno
        );
        goto out;
    }

    if (configure_spi(spi_fd) < 0) {
        klog_msg(
            "ERROR configure SPI errno=%d",
            errno
        );
        goto out;
    }

    if (gpio_set(pwr_fd, 1) < 0) {
        klog_msg(
            "ERROR PWR high errno=%d",
            errno
        );
        goto out;
    }

    klog_msg("INIT_FAST_START");

    if (
        epd_init_fast(
            spi_fd,
            rst_fd,
            dc_fd,
            busy_fd
        ) < 0
    ) {
        klog_msg(
            "ERROR init_fast errno=%d",
            errno
        );
        goto out;
    }

    klog_msg("INIT_FAST_DONE");
    klog_msg("REFRESH_START");

    if (
        epd_display(
            spi_fd,
            dc_fd,
            busy_fd,
            fb
        ) < 0
    ) {
        klog_msg(
            "ERROR refresh errno=%d",
            errno
        );
        goto out;
    }

    klog_msg("VISIBLE");
    rc = 0;

out:
    if (spi_fd >= 0)
        close(spi_fd);

    if (busy_fd >= 0)
        close(busy_fd);

    if (dc_fd >= 0)
        close(dc_fd);

    if (rst_fd >= 0)
        close(rst_fd);

    if (pwr_fd >= 0)
        close(pwr_fd);

    if (chip_fd >= 0)
        close(chip_fd);

    return rc;
}

static void usage(const char *prog)
{
    fprintf(
        stderr,
        "Uso:\n"
        "  %s --render <ficheiro>\n"
        "  %s --run\n"
        "\n"
        "Sem argumentos NAO toca no hardware.\n",
        prog,
        prog
    );
}

int main(int argc, char **argv)
{
    static uint8_t framebuffer[FB_SIZE];

    render_splash(framebuffer);

    if (
        argc == 3 &&
        strcmp(argv[1], "--render") == 0
    ) {
        if (
            write_framebuffer(
                argv[2],
                framebuffer
            ) < 0
        ) {
            perror("write framebuffer");
            return 1;
        }

        printf(
            "rendered=%s bytes=%d\n",
            argv[2],
            FB_SIZE
        );

        return 0;
    }

    if (
        argc == 2 &&
        strcmp(argv[1], "--run") == 0
    ) {
        return run_hardware(framebuffer);
    }

    usage(argv[0]);
    return 2;
}
