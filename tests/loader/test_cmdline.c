/* Host test for orbis_cmdline_compose() extracted from the loader patch (main-aio.c hunk). */
#include <stdio.h>
#include <string.h>
#include "extracted_cmdline.inc"

int main(void)
{
	static char out[4096];
	int fails = 0;

	if (orbis_cmdline_compose(out, sizeof(out), "panic=0 console=tty0", 1024, 0, NULL) ||
	    strcmp(out, "panic=0 console=tty0 orbis.loader=ge2 orbis.vram_mb=1024")) {
		printf("FAIL no-gtt: '%s'\n", out); fails++;
	}
	if (orbis_cmdline_compose(out, sizeof(out), "a=b", 512, 4096, NULL) ||
	    strcmp(out, "a=b orbis.loader=ge2 orbis.vram_mb=512 orbis.gtt_mb=4096 ttm.pages_limit=1048576 amdgpu.gttsize=4096")) {
		printf("FAIL gtt: '%s'\n", out); fails++;
	}
	{
		char small[24];
		if (orbis_cmdline_compose(small, sizeof(small), "0123456789", 1024, 0, NULL) != -1) {
			printf("FAIL overflow not detected: '%s'\n", small); fails++;
		}
	}
	{
		static char big[5000];
		memset(big, 'x', sizeof(big) - 1);
		if (orbis_cmdline_compose(out, sizeof(out), big, 1024, 0, NULL) != -1) {
			printf("FAIL long source not rejected\n"); fails++;
		}
	}
	{
		struct orbis_boot_time t = { 1793000000LL, 5000L, 123456789012ULL };
		if (orbis_cmdline_compose(out, sizeof(out), "a=b", 1024, 0, &t) ||
		    strcmp(out, "a=b orbis.loader=ge2 orbis.vram_mb=1024 orbis.time=1793000000.000005000 "
			   "orbis.time_tsc=123456789012")) {
			printf("FAIL time: '%s'\n", out); fails++;
		}
		t.tsc = 0;
		t.nsec = 999999999L;
		if (orbis_cmdline_compose(out, sizeof(out), "", 512, 0, &t) ||
		    strcmp(out, " orbis.loader=ge2 orbis.vram_mb=512 orbis.time=1793000000.999999999") ||
		    0) {
			printf("FAIL time no tsc: '%s'\n", out); fails++;
		}
		t.sec = 0;   /* no clock: nothing appended */
		if (orbis_cmdline_compose(out, sizeof(out), "a=b", 512, 0, &t) ||
		    strcmp(out, "a=b orbis.loader=ge2 orbis.vram_mb=512")) {
			printf("FAIL time absent: '%s'\n", out); fails++;
		}
		t.sec = 1793000000LL; t.nsec = 1000000000L;   /* invalid fraction: skipped */
		if (orbis_cmdline_compose(out, sizeof(out), "a=b", 512, 0, &t) ||
		    strcmp(out, "a=b orbis.loader=ge2 orbis.vram_mb=512")) {
			printf("FAIL bad nsec: '%s'\n", out); fails++;
		}
	}
	{
		/* the kernel limit: 2048 bytes including the terminator */
		static char lim[ORBIS_CMDLINE_MAX];
		static char src[2000];
		memset(src, 'y', sizeof(src) - 1);
		struct orbis_boot_time t = { 1793000000LL, 1L, 1ULL };
		if (orbis_cmdline_compose(lim, ORBIS_CMDLINE_MAX, src, 1024, 4096, &t) != -1) {
			printf("FAIL 2048 limit not enforced (%zu)\n", strlen(lim)); fails++;
		}
	}
	printf("cmdline %s\n", fails ? "FAILED" : "OK");
	return fails != 0;
}
