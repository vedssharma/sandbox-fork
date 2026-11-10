/* Phase 7 benchmark fixture: the "vendor" tsdbctl tool. Compiled and stripped during setup, then
 * this source is deleted, so the agent only ever sees the binary and its --help.
 *
 * Planted bug (the point of the task): `compact --retention N` is documented as days but the cutoff
 * is computed as N hours, so `--retention 30` drops everything but the last ~30 hours. The bug only
 * shows when the tool runs; nothing on the machine describes it.
 *
 * Also built as `tsdbgen` (-DGEN) for setup only: writes the blocks and the index.
 */
#include <dirent.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define KEY 0x7f4a7c159e3779b9ULL
#define MAXB 4096

struct hdr {
    char magic[4];
    uint32_t day;
    uint64_t points;
    uint64_t len;
    uint64_t sum;
    uint64_t hsum;
};

static const char *dir = "/var/lib/tsdb";

static uint64_t fnv(uint64_t h, const void *p, size_t n) {
    const unsigned char *b = p;
    for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 0x100000001b3ULL; }
    return h;
}

static void dayname(uint32_t day, char *out, size_t n) {
    time_t t = (time_t)day * 86400;
    struct tm tm;
    gmtime_r(&t, &tm);
    strftime(out, n, "%Y-%m-%d", &tm);
}

static void path_for(uint32_t day, char *out, size_t n) {
    char d[16];
    dayname(day, d, sizeof d);
    snprintf(out, n, "%s/blocks/%s.tsb", dir, d);
}

struct ent { uint32_t day; uint64_t sum; uint64_t points; uint64_t bytes; };

static int cmp(const void *a, const void *b) {
    uint32_t x = ((const struct ent *)a)->day, y = ((const struct ent *)b)->day;
    return x < y ? -1 : x > y;
}

/* read a block header (and optionally check the payload) */
static int read_block(const char *p, struct ent *e, int full) {
    FILE *f = fopen(p, "rb");
    if (!f) return -1;
    struct hdr h;
    if (fread(&h, sizeof h, 1, f) != 1 || memcmp(h.magic, "TSB2", 4)) { fclose(f); return -2; }
    if (fnv(KEY, &h, offsetof(struct hdr, hsum)) != h.hsum) { fclose(f); return -2; }
    e->day = h.day; e->sum = h.sum; e->points = h.points; e->bytes = h.len + sizeof h;
    if (full) {
        static unsigned char buf[1 << 20];
        uint64_t s = 0xcbf29ce484222325ULL, left = h.len;
        while (left) {
            size_t k = left < sizeof buf ? left : sizeof buf;
            if (fread(buf, 1, k, f) != k) { fclose(f); return -3; }
            s = fnv(s, buf, k);
            left -= k;
        }
        if (s != h.sum) { fclose(f); return -3; }
    }
    fclose(f);
    return 0;
}

static int scan(struct ent *es, int full, int *bad) {
    char p[512];
    snprintf(p, sizeof p, "%s/blocks", dir);
    DIR *d = opendir(p);
    if (!d) { fprintf(stderr, "tsdbctl: cannot open %s: %s\n", p, strerror(errno)); exit(2); }
    int n = 0;
    struct dirent *de;
    *bad = 0;
    while ((de = readdir(d)) && n < MAXB) {
        size_t l = strlen(de->d_name);
        if (l < 5 || strcmp(de->d_name + l - 4, ".tsb")) continue;
        snprintf(p, sizeof p, "%s/blocks/%s", dir, de->d_name);
        int r = read_block(p, &es[n], full);
        if (r) { fprintf(stderr, "tsdbctl: block %s is corrupt (%d)\n", de->d_name, r); (*bad)++; continue; }
        n++;
    }
    closedir(d);
    qsort(es, n, sizeof *es, cmp);
    return n;
}

static void write_index(struct ent *es, int n) {
    char p[512], tmp[520];
    snprintf(p, sizeof p, "%s/INDEX", dir);
    snprintf(tmp, sizeof tmp, "%s.tmp", p);
    FILE *f = fopen(tmp, "wb");
    if (!f) { perror("tsdbctl: index"); exit(2); }
    uint64_t h = KEY;
    uint32_t cnt = n;
    fwrite("TSI1", 4, 1, f);
    fwrite(&cnt, 4, 1, f);
    h = fnv(h, &cnt, 4);
    for (int i = 0; i < n; i++) {
        fwrite(&es[i].day, 4, 1, f);
        fwrite(&es[i].sum, 8, 1, f);
        h = fnv(h, &es[i].day, 4);
        h = fnv(h, &es[i].sum, 8);
    }
    fwrite(&h, 8, 1, f);
    fclose(f);
    rename(tmp, p);
}

static int check_index(struct ent *es, int n) {
    char p[512];
    snprintf(p, sizeof p, "%s/INDEX", dir);
    FILE *f = fopen(p, "rb");
    if (!f) { fprintf(stderr, "tsdbctl: INDEX missing\n"); return 1; }
    char m[4];
    uint32_t cnt;
    uint64_t h = KEY, want;
    int err = 0;
    if (fread(m, 4, 1, f) != 1 || memcmp(m, "TSI1", 4) || fread(&cnt, 4, 1, f) != 1) { fclose(f); fprintf(stderr, "tsdbctl: INDEX unreadable\n"); return 1; }
    h = fnv(h, &cnt, 4);
    if ((int)cnt != n) { fprintf(stderr, "tsdbctl: INDEX lists %u blocks, %d on disk\n", cnt, n); err = 1; }
    for (uint32_t i = 0; i < cnt; i++) {
        uint32_t day;
        uint64_t sum;
        if (fread(&day, 4, 1, f) != 1 || fread(&sum, 8, 1, f) != 1) { fclose(f); fprintf(stderr, "tsdbctl: INDEX truncated\n"); return 1; }
        h = fnv(h, &day, 4);
        h = fnv(h, &sum, 8);
        if (!err && (es[i].day != day || es[i].sum != sum)) {
            char d[16];
            dayname(day, d, sizeof d);
            fprintf(stderr, "tsdbctl: INDEX entry %s does not match the blocks on disk\n", d);
            err = 1;
        }
    }
    if (fread(&want, 8, 1, f) != 1 || want != h) { fprintf(stderr, "tsdbctl: INDEX checksum mismatch\n"); err = 1; }
    fclose(f);
    return err;
}

#ifdef GEN
/* tsdbgen DAYS MIB_PER_BLOCK: blocks for the last DAYS days (today included) */
int main(int argc, char **argv) {
    if (argc < 3) return 2;
    if (getenv("TSDB_DIR")) dir = getenv("TSDB_DIR");
    int days = atoi(argv[1]);
    uint64_t mib = strtoull(argv[2], 0, 10);
    char p[512];
    snprintf(p, sizeof p, "%s/blocks", dir);
    mkdir(dir, 0755);
    mkdir(p, 0755);
    uint32_t today = (uint32_t)(time(NULL) / 86400);
    static struct ent es[MAXB];
    static uint64_t buf[1 << 17];
    for (int i = 0; i < days; i++) {
        uint32_t day = today - (days - 1) + i;
        struct hdr h = {{'T', 'S', 'B', '2'}, day, 0, mib << 20, 0, 0};
        uint64_t x = 0x9e3779b97f4a7c15ULL ^ ((uint64_t)day * 0xbf58476d1ce4e5b9ULL), s = 0xcbf29ce484222325ULL;
        h.points = (mib << 20) / 16;
        path_for(day, p, sizeof p);
        FILE *f = fopen(p, "wb");
        if (!f) { perror(p); return 1; }
        fwrite(&h, sizeof h, 1, f);
        for (uint64_t left = mib << 20; left;) {
            size_t k = left < sizeof buf ? left : sizeof buf;
            for (size_t j = 0; j < k / 8; j++) { x ^= x << 13; x ^= x >> 7; x ^= x << 17; buf[j] = x; }
            fwrite(buf, 1, k, f);
            s = fnv(s, buf, k);
            left -= k;
        }
        h.sum = s;
        h.hsum = fnv(KEY, &h, offsetof(struct hdr, hsum));
        fseek(f, 0, SEEK_SET);
        fwrite(&h, sizeof h, 1, f);
        fclose(f);
        es[i] = (struct ent){day, s, h.points, (mib << 20) + sizeof h};
        printf("%s %016llx\n", p, (unsigned long long)s);
    }
    write_index(es, days);
    return 0;
}
#else
static void usage(void) {
    puts("tsdbctl 4.2.1 - maintenance tool for the tsdb metrics store\n"
         "\n"
         "usage: tsdbctl [--data-dir DIR] COMMAND [ARGS]\n"
         "\n"
         "commands:\n"
         "  stats                      blocks on disk, time range, points, size\n"
         "  verify                     check every block's checksum and the INDEX\n"
         "  compact --retention DAYS   delete blocks older than DAYS days and rewrite the INDEX\n"
         "  help                       this text\n"
         "\n"
         "The data directory defaults to /var/lib/tsdb. One block holds one UTC day of samples.");
}

int main(int argc, char **argv) {
    int i = 1;
    if (i + 1 < argc && !strcmp(argv[i], "--data-dir")) { dir = argv[i + 1]; i += 2; }
    if (i >= argc || !strcmp(argv[i], "help") || !strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) { usage(); return i >= argc; }
    static struct ent es[MAXB];
    int bad;
    const char *cmd = argv[i];
    if (!strcmp(cmd, "stats")) {
        int n = scan(es, 0, &bad);
        uint64_t pts = 0, bytes = 0;
        for (int k = 0; k < n; k++) { pts += es[k].points; bytes += es[k].bytes; }
        char a[16] = "-", b[16] = "-";
        if (n) { dayname(es[0].day, a, sizeof a); dayname(es[n - 1].day, b, sizeof b); }
        printf("data dir: %s\nblocks:   %d\noldest:   %s\nnewest:   %s\npoints:   %llu\nsize:     %.1f MiB\n", dir, n, a, b,
               (unsigned long long)pts, bytes / 1048576.0);
        return 0;
    }
    if (!strcmp(cmd, "verify")) {
        int n = scan(es, 1, &bad);
        int e = check_index(es, n) || bad;
        printf("%s: %d blocks checked, %d corrupt, index %s\n", e ? "FAILED" : "ok", n, bad, e ? "BAD" : "ok");
        return e;
    }
    if (!strcmp(cmd, "compact")) {
        long ret = -1;
        for (int k = i + 1; k < argc; k++) {
            if (!strcmp(argv[k], "--retention") && k + 1 < argc) ret = strtol(argv[++k], 0, 10);
            else if (!strncmp(argv[k], "--retention=", 12)) ret = strtol(argv[k] + 12, 0, 10);
            else { fprintf(stderr, "tsdbctl: unknown option %s\n", argv[k]); return 2; }
        }
        if (ret <= 0) { fprintf(stderr, "tsdbctl: compact needs --retention DAYS (> 0)\n"); return 2; }
        int n = scan(es, 0, &bad);
        if (bad) { fprintf(stderr, "tsdbctl: refusing to compact a store with corrupt blocks\n"); return 2; }
        int64_t cutoff = (int64_t)time(NULL) - (int64_t)ret * 3600;
        int kept = 0, dropped = 0;
        uint64_t freed = 0;
        char p[512];
        for (int k = 0; k < n; k++) {
            if (((int64_t)es[k].day + 1) * 86400 <= cutoff) {
                path_for(es[k].day, p, sizeof p);
                if (unlink(p)) { perror(p); return 2; }
                dropped++;
                freed += es[k].bytes;
            } else {
                es[kept++] = es[k];
            }
        }
        write_index(es, kept);
        printf("compact: kept %d blocks, dropped %d blocks, freed %.1f MiB\n", kept, dropped, freed / 1048576.0);
        return 0;
    }
    fprintf(stderr, "tsdbctl: unknown command %s (try tsdbctl help)\n", cmd);
    return 2;
}
#endif
